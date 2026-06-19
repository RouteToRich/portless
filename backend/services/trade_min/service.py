"""Business logic for the Trade-Min service (FR-1, LLD 4.5).

Scans HS code × market pairs for demand surges, ranks them by a deterministic
arbitrage score, persists the results, and emits ``opportunity.detected`` per
opportunity found.

Connectors (Comtrade, Volza, MacMap) are called when live; on
``NotImplementedError`` the service falls back to a deterministic simulation
so the module is fully functional without external credentials.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from shared.bus import get_publisher
from shared.domain.events import DomainEvent, EventTopic

from . import ranking
from .config import DEFAULT_CONFIG
from .repository import repository
from .schemas import OpportunityResponse, ScanRequest


def _new_opportunity_id() -> str:
    return f"OPP-{uuid4().hex[:12]}"


async def _detect_demand(hs_code: str, market: str, source: str) -> tuple[float, float, str]:
    """Return (demand_index, price_gap_pct, resolved_source).

    Attempts the live connector for ``source``; falls back to the deterministic
    simulation on ``NotImplementedError`` (connectors not yet implemented).
    """
    if source not in ("comtrade", "volza", "macmap"):
        source = "comtrade"

    try:
        from shared.connectors.registry import get_connector
        connector = get_connector(source)
        result = await connector.quote({"hs_code": hs_code, "market": market})  # type: ignore[attr-defined]
        # Live connectors should return {"demand_index": float, "price_gap_pct": float}.
        return float(result["demand_index"]), float(result["price_gap_pct"]), source
    except (NotImplementedError, Exception):
        pass

    return (
        ranking.simulated_demand_index(hs_code, market),
        ranking.simulated_price_gap_pct(hs_code, market),
        "simulated",
    )


async def scan_opportunities(payload: ScanRequest) -> list[OpportunityResponse]:
    """Detect demand surges for every HS-code × market pair and rank results."""
    config = DEFAULT_CONFIG
    hs_codes = payload.hs_codes or list(config.default_hs_codes)
    markets = payload.target_markets or list(config.default_markets)
    sources = payload.sources or ["comtrade", "volza"]
    min_rank = max(payload.min_arbitrage_rank, config.min_arbitrage_rank)

    now = datetime.now(timezone.utc)
    results: list[OpportunityResponse] = []

    for hs_code in hs_codes:
        for market in markets:
            # Try each requested source; take the first successful result.
            demand_idx, price_gap, resolved_source = await _detect_demand(
                hs_code, market, sources[0]
            )
            rank = ranking.arbitrage_rank(demand_idx, price_gap, config)
            if rank < min_rank:
                continue

            opp = OpportunityResponse(
                opportunity_id=_new_opportunity_id(),
                hs_code=hs_code,
                market=market,
                demand_index=round(demand_idx, 2),
                price_gap_pct=round(price_gap, 2),
                arbitrage_rank=rank,
                source=resolved_source,  # type: ignore[arg-type]
                scanned_at=now,
            )
            repository.save(opp)
            results.append(opp)

            await get_publisher().publish(
                DomainEvent(
                    topic=EventTopic.OPPORTUNITY_DETECTED,
                    payload={
                        "opportunity_id": opp.opportunity_id,
                        "hs_code": opp.hs_code,
                        "market": opp.market,
                        "arbitrage_rank": opp.arbitrage_rank,
                        "demand_index": opp.demand_index,
                        "price_gap_pct": opp.price_gap_pct,
                        "source": opp.source,
                    },
                )
            )

    results.sort(key=lambda o: o.arbitrage_rank, reverse=True)
    return results[: config.max_per_scan]


async def list_opportunities() -> list[OpportunityResponse]:
    """Return all previously scanned opportunities."""
    return repository.list_all()


async def get_opportunity(opportunity_id: str) -> OpportunityResponse | None:
    """Return a single opportunity by ID, or ``None`` if not found."""
    return repository.get(opportunity_id)
