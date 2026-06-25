"""REST client for the Trade-Min service.

Used by the Deals service, DealMaker agent, and any other service that needs
to trigger a scan, list opportunities, or fetch the Match Engine seed values
derived from an opportunity.

Typical call flow
-----------------
1. Trade-Min emits ``opportunity.detected`` (via event bus / REST polling)
2. DealMaker calls ``get_match_seed(opportunity_id)`` to retrieve pre-seeded
   ScoreRequest inputs without redundant connector calls
3. DealMaker creates a deal, then calls ``mark_converted(opportunity_id)`` so
   the opportunity is not surfaced to parallel deal flows
"""
from __future__ import annotations

from typing import Any

from shared.clients import BaseServiceClient
from shared.config import settings


class TradeMinClient(BaseServiceClient):
    def __init__(self) -> None:
        super().__init__(settings.trade_min_url)

    # ── meta ──────────────────────────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        return await self.get("/trade-min/health")

    # ── scanning ──────────────────────────────────────────────────────────

    async def scan(
        self,
        hs_codes: list[str] | None = None,
        target_markets: list[str] | None = None,
        min_arbitrage_rank: float = 0.0,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """Trigger a 5-phase opportunity discovery scan."""
        return await self.post(
            "/trade-min/scan",
            json={
                "hs_codes": hs_codes,
                "target_markets": target_markets,
                "min_arbitrage_rank": min_arbitrage_rank,
                "force_refresh": force_refresh,
            },
        )

    # ── opportunities ─────────────────────────────────────────────────────

    async def list_opportunities(
        self,
        limit: int = 100,
        min_rank: float = 0.0,
        urgency_tier: int | None = None,
    ) -> list[dict[str, Any]]:
        """List active enriched opportunities, sorted by urgency then rank."""
        params: dict[str, Any] = {"limit": limit, "min_rank": min_rank}
        if urgency_tier is not None:
            params["urgency_tier"] = urgency_tier
        return await self.get("/trade-min/opportunities", params=params)

    async def get_opportunity(self, opportunity_id: str) -> dict[str, Any]:
        """Fetch a single enriched opportunity by ID."""
        return await self.get(f"/trade-min/opportunities/{opportunity_id}")

    # ── next-phase bridge ─────────────────────────────────────────────────

    async def get_match_seed(self, opportunity_id: str) -> dict[str, Any]:
        """Return pre-seeded Match Engine inputs derived from an opportunity.

        The returned dict maps directly onto ``ScoreRequest`` fields:
          ``buyer_target_price_usd``  → ``buyer_target_price``
          ``top_sellers[i].fob_price_usd`` → ``supplier_quote_price``
          ``top_sellers[i].supplier_to_port_km`` → ``supplier_to_port_km``

        Missing fields (``deal_id``, ``verification``, ``business_age_years``,
        ``udyam_registered``, ``annual_shipment_count``) must be resolved by
        the Sourcing phase before submitting a ``ScoreRequest``.
        """
        return await self.get(
            f"/trade-min/opportunities/{opportunity_id}/match-seed"
        )

    async def mark_converted(self, opportunity_id: str) -> dict[str, Any]:
        """Mark an opportunity as CONVERTED after a deal has been created.

        Call this immediately after ``deal.created`` is emitted to prevent the
        same opportunity from being picked up by parallel deal flows.
        """
        return await self.post(
            f"/trade-min/opportunities/{opportunity_id}/convert"
        )
