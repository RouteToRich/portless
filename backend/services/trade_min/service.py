"""Trade-Min service — 5-phase Global Opportunity Discovery pipeline (§10).

Phase 1: Historical demand  — Comtrade, Volza, ImportGenius, Trademo (concurrent)
Phase 2: Live demand signals — AlibabaRFQ, GDELT, EuTED (concurrent)
Phase 3: Tariff gate        — MacMap (sequential; hard-discard net gap < 1 %)
Phase 4: Seller discovery   — IndiaMART, TradeIndia (concurrent)
Phase 5: Confidence + assemble + MongoDB persist + opportunity.detected event

Each phase's output is the explicit typed input to the next phase.
No shared mutable state between phases.

Connector resilience
--------------------
Every connector call goes through ``_safe_quote()``, which:
  - catches KeyError when env credentials are missing (graceful skip)
  - catches all network / API errors (logs warning, returns None)
  - never propagates exceptions into asyncio.gather
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from shared.bus import get_publisher
from shared.domain.events import DomainEvent, EventTopic

from . import ranking
from .config import DEFAULT_CONFIG, TradeMinConfig
from .repository import get_repository
from .schemas import (
    BuyerSignal,
    DemandSignal,
    EnrichedOpportunity,
    MatchEngineSeed,
    ScanRequest,
    SellerCandidate,
    TariffData,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Static lookup tables
# ---------------------------------------------------------------------------

_ISO2_TO_M49: dict[str, str] = {
    "ES": "724", "DE": "276", "US": "842", "AE": "784", "GB": "826",
    "FR": "250", "IT": "380", "NL": "528", "PL": "616", "AU": "036",
    "CA": "124", "JP": "392", "SG": "702", "CN": "156", "BE": "056",
    "CH": "756", "SA": "682", "SE": "752", "DK": "208", "NO": "578",
    "AT": "040", "PT": "620", "IE": "372", "FI": "246", "TR": "792",
    "ZA": "710", "BR": "076", "MX": "484", "KR": "410", "TH": "764",
    "MY": "458", "EG": "818", "NG": "566", "AR": "032",
}

_COUNTRY_VAT_PCT: dict[str, float] = {
    "DE": 7.0,  "FR": 5.5,  "ES": 10.0, "IT": 10.0, "GB": 0.0,
    "NL": 9.0,  "BE": 6.0,  "PL": 8.0,  "SE": 12.0, "DK": 25.0,
    "NO": 15.0, "FI": 14.0, "CH": 2.5,  "AT": 10.0, "PT": 6.0,
    "IE": 13.5, "US": 0.0,  "CA": 5.0,  "AU": 10.0, "JP": 8.0,
    "SG": 9.0,  "AE": 5.0,  "SA": 15.0, "CN": 9.0,  "KR": 10.0,
    "BR": 12.0, "MX": 0.0,  "ZA": 15.0, "EG": 14.0, "NG": 7.5,
    "TR": 8.0,  "TH": 7.0,  "MY": 6.0,  "AR": 21.0, "IN": 0.0,
}

_STATE_TO_PORT_KM: dict[str, float] = {
    "Tamil Nadu": 50.0,       # Chennai / Ennore
    "Maharashtra": 80.0,      # JNPT / Mumbai
    "Gujarat": 120.0,         # Mundra / Kandla
    "Andhra Pradesh": 100.0,  # Visakhapatnam
    "Karnataka": 150.0,       # Mangalore / New Mangalore
    "Kerala": 80.0,           # Kochi / Cochin
    "West Bengal": 60.0,      # Kolkata / Haldia
    "Odisha": 100.0,          # Paradip
    "Goa": 50.0,              # Mormugao
    "Telangana": 320.0,
    "Rajasthan": 500.0,
    "Punjab": 600.0,
    "Haryana": 650.0,
    "Uttar Pradesh": 700.0,
    "Madhya Pradesh": 800.0,
    "Delhi": 650.0,
    "Bihar": 900.0,
}

_HS_COMPETITOR_ORIGINS: dict[str, list[str]] = {
    "0904": ["Vietnam", "Brazil", "Indonesia"],
    "0902": ["China", "Sri Lanka", "Kenya"],
    "0910": ["Guatemala", "Vietnam", "Tanzania"],
    "1001": ["Russia", "Ukraine", "Canada"],
    "2905": ["China", "Germany"],
}

_EU_MARKETS = frozenset({
    "DE", "FR", "ES", "IT", "NL", "PL", "BE", "AT",
    "SE", "DK", "FI", "IE", "PT", "HU", "CZ", "RO",
})

_P1_WEIGHTS: dict[str, float] = {
    "comtrade":     0.40,
    "volza":        0.25,
    "importgenius": 0.15,
    "trademo":      0.20,
}


# ---------------------------------------------------------------------------
# Inter-phase containers
# ---------------------------------------------------------------------------

@dataclass
class _Phase1Result:
    demand_signals: list[DemandSignal] = field(default_factory=list)
    buyer_signals: list[BuyerSignal] = field(default_factory=list)
    demand_index: float = 0.0
    gross_price_gap_pct: float = 0.0
    active_connectors: int = 0
    source: str = "simulated"


@dataclass
class _Phase2Result:
    demand_signals: list[DemandSignal] = field(default_factory=list)
    buyer_signals: list[BuyerSignal] = field(default_factory=list)
    active_rfq_count: int = 0
    active_tender_count: int = 0
    disruption_detected: bool = False


# ---------------------------------------------------------------------------
# Connector access — cached, never raises
# ---------------------------------------------------------------------------

_connector_cache: dict[str, object | None] = {}


def _build_connector(name: str) -> object | None:
    """Build and cache a connector.  Returns None (and logs) on missing creds."""
    if name in _connector_cache:
        return _connector_cache[name]
    try:
        from shared.connectors.registry import get_connector
        conn = get_connector(name)
        _connector_cache[name] = conn
        return conn
    except KeyError as exc:
        logger.warning(
            "Connector %r skipped — missing env var %s. "
            "Add the credential to backend/.env to enable this data source.",
            name, exc,
        )
        # Do NOT cache None here so the connector is retried if env var is later set.
        return None
    except Exception as exc:
        logger.error("Connector %r failed to initialise: %s", name, exc)
        return None


async def _safe_quote(name: str, req: dict) -> dict | None:
    """Call connector.quote(req) and return the result, or None on any failure.

    Handles:
      - Missing credentials (KeyError in connector __init__)
      - Network timeouts, API errors, unexpected exceptions
    """
    conn = _build_connector(name)
    if conn is None:
        return None
    try:
        result = await conn.quote(req)  # type: ignore[union-attr]
        return result
    except Exception as exc:
        logger.warning("Connector %r quote() failed: %s", name, exc)
        return None


def _new_opportunity_id() -> str:
    return f"OPP-{uuid4().hex[:12].upper()}"


# ---------------------------------------------------------------------------
# Phase 1 — Historical demand
# ---------------------------------------------------------------------------

async def _phase1_historical_demand(
    hs_code: str,
    market: str,
    config: TradeMinConfig,
) -> _Phase1Result:
    now = datetime.now(timezone.utc)
    prev_year = str(now.year - 1)

    comtrade_r, volza_r, ig_r, trademo_r = await asyncio.gather(
        _safe_quote("comtrade", {
            "hs_code": hs_code,
            "period": prev_year,
            "reporter_code": _ISO2_TO_M49.get(market, ""),
            "flow_code": "M",
            "partner_code": "356",
        }),
        _safe_quote("volza", {
            "hs_code": hs_code,
            "importer_country": market,
            "start_date": f"{now.year - 1}-01-01",
            "end_date": f"{now.year - 1}-12-31",
            "page_size": 100,
        }),
        _safe_quote("importgenius", {
            "hs_code": hs_code,
            "country": market,
            "limit": 100,
        }),
        _safe_quote("trademo", {
            "hs_code": hs_code,
            "buyer_country": market,
            "limit": 50,
        }),
    )

    result = _Phase1Result()
    active_weights: dict[str, float] = {}
    connector_data: dict[str, dict] = {}

    for cname, r in [
        ("comtrade", comtrade_r),
        ("volza", volza_r),
        ("importgenius", ig_r),
        ("trademo", trademo_r),
    ]:
        if r is not None:
            active_weights[cname] = _P1_WEIGHTS[cname]
            connector_data[cname] = r

    result.active_connectors = len(active_weights)

    if result.active_connectors < config.phase1_min_connectors:
        logger.info(
            "Phase 1 below minimum connectors (%d/%d) for %s×%s — using simulation",
            result.active_connectors, config.phase1_min_connectors, hs_code, market,
        )
        result.demand_index = ranking.simulated_demand_index(hs_code, market)
        result.gross_price_gap_pct = ranking.simulated_price_gap_pct(hs_code, market)
        result.source = "simulated"
        return result

    # Normalise weights among responding connectors
    total_w = sum(active_weights.values())
    norm_weights = {k: v / total_w for k, v in active_weights.items()}

    scores: list[tuple[str, float]] = []  # (connector, weighted_score)

    if "comtrade" in connector_data:
        score, signals, buyers, gap = _extract_comtrade(
            connector_data["comtrade"], hs_code, market, now
        )
        scores.append(("comtrade", score * norm_weights["comtrade"]))
        result.demand_signals.extend(signals)
        result.buyer_signals.extend(buyers)
        if gap:
            result.gross_price_gap_pct = gap

    if "volza" in connector_data:
        score, signals, buyers, gap = _extract_volza(
            connector_data["volza"], hs_code, market, now, config
        )
        scores.append(("volza", score * norm_weights["volza"]))
        result.demand_signals.extend(signals)
        result.buyer_signals.extend(buyers)
        if gap and result.gross_price_gap_pct == 0.0:
            result.gross_price_gap_pct = gap

    if "importgenius" in connector_data:
        score, signals, buyers = _extract_importgenius(
            connector_data["importgenius"], hs_code, market, now
        )
        scores.append(("importgenius", score * norm_weights["importgenius"]))
        result.demand_signals.extend(signals)
        result.buyer_signals.extend(buyers)

    if "trademo" in connector_data:
        score, signals, buyers = _extract_trademo(
            connector_data["trademo"], hs_code, market, now
        )
        scores.append(("trademo", score * norm_weights["trademo"]))
        result.demand_signals.extend(signals)
        result.buyer_signals.extend(buyers)

    result.demand_index = round(sum(s for _, s in scores), 2)

    if result.gross_price_gap_pct == 0.0:
        result.gross_price_gap_pct = ranking.simulated_price_gap_pct(hs_code, market)

    result.source = scores[0][0] if scores else "simulated"
    return result


def _extract_comtrade(
    data: dict, hs_code: str, market: str, now: datetime
) -> tuple[float, list[DemandSignal], list[BuyerSignal], float]:
    records = data.get("records", [])
    total = data.get("total_records", len(records))
    score = min(total / 5.0, 100.0)

    fob_vals = [r["fob_value_usd"] for r in records if r.get("fob_value_usd")]
    cif_vals = [r["cif_value_usd"] for r in records if r.get("cif_value_usd")]
    price_gap = 0.0
    if fob_vals and cif_vals:
        avg_fob = sum(fob_vals) / len(fob_vals)
        avg_cif = sum(cif_vals) / len(cif_vals)
        if avg_fob > 0:
            price_gap = round((avg_cif - avg_fob) / avg_fob * 100.0, 2)

    return (
        score,
        [DemandSignal(
            source="comtrade",
            signal_type="historical",
            urgency_tier=1,
            normalized_value=round(score, 2),
            weight=_P1_WEIGHTS["comtrade"],
            raw_summary=f"{total} import records for HS {hs_code} in {market} ({data.get('period', '')})",
            fetched_at=now,
        )],
        [],
        price_gap,
    )


def _extract_volza(
    data: dict, hs_code: str, market: str, now: datetime, config: TradeMinConfig
) -> tuple[float, list[DemandSignal], list[BuyerSignal], float]:
    shipments: list[dict] = data.get("shipments", [])
    total = data.get("pagination", {}).get("total_records", len(shipments))

    unique_buyers = {s["buyer_name"] for s in shipments if s.get("buyer_name")}
    score = min(len(unique_buyers) / 20.0 * 100.0, 100.0)

    # Price gap from unit prices (more accurate than value/weight ratio)
    unit_prices = [s["unit_price_usd"] for s in shipments if s.get("unit_price_usd")]
    price_gap = 0.0
    if unit_prices:
        avg_fob = sum(unit_prices) / len(unit_prices)
        est_cif = avg_fob * 1.15  # 15% logistics mark-up estimate
        price_gap = round((est_cif - avg_fob) / avg_fob * 100.0, 2)

    signals = [DemandSignal(
        source="volza",
        signal_type="manifest",
        urgency_tier=1,
        normalized_value=round(score, 2),
        weight=_P1_WEIGHTS["volza"],
        raw_summary=(
            f"{len(unique_buyers)} unique buyers in {market} for HS {hs_code} "
            f"(total {total} manifests)"
        ),
        fetched_at=now,
    )]

    buyers: list[BuyerSignal] = []
    seen: set[str] = set()
    for s in shipments[:25]:
        name = s.get("buyer_name", "")
        if not name or name in seen:
            continue
        seen.add(name)

        last_date_str: str | None = s.get("shipment_date") or None
        urgency = 1
        if last_date_str:
            try:
                last_dt = datetime.fromisoformat(last_date_str.replace("Z", "+00:00"))
                prob = ranking.reorder_probability(
                    days_since_last_import=(now - last_dt).days,
                    avg_days_between_imports=90.0,
                )
                if prob >= config.reorder_probability_threshold:
                    urgency = 2
            except (ValueError, TypeError):
                pass

        buyers.append(BuyerSignal(
            buyer_name=name,
            buyer_country=s.get("buyer_country", market),
            last_import_date=last_date_str,
            signal_type="manifest",
            source="volza",
            urgency_tier=urgency,
            estimated_order_value_usd=s.get("value_usd"),
            unit_price_usd=s.get("unit_price_usd"),
        ))

    return score, signals, buyers, price_gap


def _extract_importgenius(
    data: dict, hs_code: str, market: str, now: datetime
) -> tuple[float, list[DemandSignal], list[BuyerSignal]]:
    importers: list[dict] = data.get("importers", data.get("data", []))
    score = min(len(importers) / 50.0 * 100.0, 100.0)

    signals = [DemandSignal(
        source="importgenius",
        signal_type="manifest",
        urgency_tier=1,
        normalized_value=round(score, 2),
        weight=_P1_WEIGHTS["importgenius"],
        raw_summary=f"{len(importers)} repeat importers of HS {hs_code} in {market}",
        fetched_at=now,
    )]

    buyers: list[BuyerSignal] = []
    for imp in importers[:15]:
        name = imp.get("importer_name") or imp.get("company_name", "")
        if not name:
            continue
        volume_kg = _safe_float(imp.get("net_weight_kg"))
        buyers.append(BuyerSignal(
            buyer_name=name,
            buyer_country=imp.get("country", market),
            signal_type="manifest",
            source="importgenius",
            urgency_tier=1,
            annual_volume_mt=round(volume_kg / 1000.0, 2) if volume_kg else None,
            estimated_order_value_usd=_safe_float(imp.get("value_usd")),
        ))

    return score, signals, buyers


def _extract_trademo(
    data: dict, hs_code: str, market: str, now: datetime
) -> tuple[float, list[DemandSignal], list[BuyerSignal]]:
    companies: list[dict] = data.get("companies", data.get("buyers", data.get("data", [])))
    score = min(len(companies) / 30.0 * 100.0, 100.0)

    signals = [DemandSignal(
        source="trademo",
        signal_type="manifest",
        urgency_tier=1,
        normalized_value=round(score, 2),
        weight=_P1_WEIGHTS["trademo"],
        raw_summary=f"{len(companies)} high-volume buyers (Trademo) for HS {hs_code} in {market}",
        fetched_at=now,
    )]

    buyers: list[BuyerSignal] = []
    for c in companies[:10]:
        name = c.get("company_name") or c.get("name", "")
        if name:
            buyers.append(BuyerSignal(
                buyer_name=name,
                buyer_country=c.get("country", market),
                risk_score=_safe_float(c.get("risk_score")),
                signal_type="manifest",
                source="trademo",
                urgency_tier=1,
            ))

    return score, signals, buyers


# ---------------------------------------------------------------------------
# Phase 2 — Live demand signals
# ---------------------------------------------------------------------------

async def _phase2_live_signals(
    hs_code: str,
    market: str,
    phase1: _Phase1Result,  # noqa: ARG001 — future: use buyer names for targeted RFQ search
    config: TradeMinConfig,  # noqa: ARG001
) -> _Phase2Result:
    now = datetime.now(timezone.utc)

    from shared.connectors.trade_intel import _HS_TO_KEYWORD as _HS_KW
    keyword = _HS_KW.get(hs_code[:4], hs_code)
    competitors = _HS_COMPETITOR_ORIGINS.get(hs_code[:4], ["China", "Vietnam"])

    ted_req: dict = {"query": keyword, "limit": 25}
    if market in _EU_MARKETS:
        ted_req["country"] = market

    rfq_r, gdelt_r, ted_r = await asyncio.gather(
        _safe_quote("alibaba_rfq", {"query": keyword, "country": market, "page_size": 20}),
        _safe_quote("gdelt_disruption", {
            "origin_country": competitors[0],
            "product_keyword": keyword,
            "hours_lookback": 72,
        }),
        _safe_quote("eu_ted", ted_req),
    )

    result = _Phase2Result()

    if rfq_r is not None:
        rfqs: list[dict] = rfq_r.get("rfqs", [])
        result.active_rfq_count = rfq_r.get("total", len(rfqs))
        if result.active_rfq_count > 0:
            result.demand_signals.append(DemandSignal(
                source="alibaba_rfq",
                signal_type="rfq",
                urgency_tier=3,
                normalized_value=min(result.active_rfq_count / 20.0 * 100.0, 100.0),
                weight=0.0,
                raw_summary=f"{result.active_rfq_count} active RFQs on Alibaba for '{keyword}' in {market}",
                fetched_at=now,
            ))
            for r in rfqs[:10]:
                result.buyer_signals.append(BuyerSignal(
                    buyer_name=f"Alibaba Buyer ({r.get('buyer_country', market)})",
                    buyer_country=r.get("buyer_country", market),
                    signal_type="rfq",
                    source="alibaba_rfq",
                    urgency_tier=3,
                ))

    if gdelt_r is not None and gdelt_r.get("disruption_detected"):
        strength: str = gdelt_r.get("signal_strength", "LOW")
        urgency = 4 if strength == "HIGH" else 3
        result.disruption_detected = True
        result.demand_signals.append(DemandSignal(
            source="gdelt_disruption",
            signal_type="disruption",
            urgency_tier=urgency,
            normalized_value={"LOW": 40.0, "MEDIUM": 70.0, "HIGH": 100.0}.get(strength, 50.0),
            weight=0.0,
            raw_summary=(
                f"Supply disruption in {competitors[0]}: "
                f"{gdelt_r.get('article_count', 0)} articles, strength={strength}"
            ),
            fetched_at=now,
        ))

    if ted_r is not None:
        tenders: list[dict] = ted_r.get("tenders", [])
        result.active_tender_count = ted_r.get("total", len(tenders))
        if result.active_tender_count > 0:
            result.demand_signals.append(DemandSignal(
                source="eu_ted",
                signal_type="tender",
                urgency_tier=3,
                normalized_value=min(result.active_tender_count / 10.0 * 100.0, 100.0),
                weight=0.0,
                raw_summary=f"{result.active_tender_count} EU tenders for '{keyword}' in {market}",
                fetched_at=now,
            ))
            for t in tenders[:5]:
                result.buyer_signals.append(BuyerSignal(
                    buyer_name=t.get("buyer_name", "EU Institution"),
                    buyer_country=t.get("buyer_country", market),
                    signal_type="tender",
                    source="eu_ted",
                    urgency_tier=3,
                    estimated_order_value_usd=_safe_float(t.get("contract_value")),
                    contact_url=t.get("notice_url") or None,
                ))

    return result


# ---------------------------------------------------------------------------
# Phase 3 — Tariff gate (MacMap)
# ---------------------------------------------------------------------------

async def _phase3_tariff_gate(
    hs_code: str,
    market: str,
    gross_price_gap_pct: float,  # noqa: ARG001 — kept for future gate logic
    config: TradeMinConfig,  # noqa: ARG001
) -> TariffData:
    now = datetime.now(timezone.utc)
    vat = _COUNTRY_VAT_PCT.get(market, 0.0)

    raw = await _safe_quote("macmap", {
        "hs_code": hs_code,
        "reporter_iso2": market,
        "partner_iso2": "IN",
    })

    if raw is None:
        # MacMap unavailable — return zero-duty placeholder so pipeline continues.
        # Confidence score deducts points when tariff_data.source == "macmap" and
        # applied_duty_pct == 0 with no ntb_flags (ambiguous; not confirmed viable).
        logger.info("MacMap unavailable for %s×%s — tariff unverified", hs_code, market)
        return TariffData(
            hs_code=hs_code,
            destination_country=market,
            applied_duty_pct=0.0,
            gst_vat_pct=vat,
            total_landed_cost_add_pct=vat,
            fetched_at=now,
        )

    return _parse_macmap(raw, hs_code, market, vat, now)


def _parse_macmap(
    raw: dict, hs_code: str, market: str, vat: float, now: datetime
) -> TariffData:
    tariff_block = (
        raw.get("tariffResult")
        or raw.get("tariff")
        or raw.get("data")
        or raw
    )

    applied_duty = 0.0
    details = (
        tariff_block.get("appliedTariffDetails")
        or tariff_block.get("applied_tariff_details")
        or tariff_block.get("appliedTariff")
        or []
    )
    if isinstance(details, list) and details:
        rate_raw = str(
            details[0].get("tariffRate") or details[0].get("rate") or "0"
        ).replace("%", "").strip()
        try:
            applied_duty = float(rate_raw)
        except ValueError:
            pass
    elif isinstance(tariff_block.get("applied"), dict):
        rate_raw = str(tariff_block["applied"].get("rate", "0")).replace("%", "").strip()
        try:
            applied_duty = float(rate_raw)
        except ValueError:
            pass

    ntm_items = raw.get("ntmResult") or raw.get("ntm") or []
    ntb_flags: list[str] = []
    if isinstance(ntm_items, list):
        for ntm in ntm_items[:5]:
            code = ntm.get("ntmCode") or ntm.get("category") or ntm.get("code", "")
            desc = ntm.get("ntmDesc") or ntm.get("description", "")
            if code:
                ntb_flags.append(f"{code}: {desc}" if desc else code)

    roo = None
    roo_raw = raw.get("rooResult") or raw.get("roo")
    if isinstance(roo_raw, dict):
        roo = roo_raw.get("generalRule") or roo_raw.get("rule")
    elif isinstance(roo_raw, list) and roo_raw:
        roo = roo_raw[0].get("rule") or roo_raw[0].get("description")

    return TariffData(
        hs_code=hs_code,
        destination_country=market,
        applied_duty_pct=applied_duty,
        gst_vat_pct=vat,
        ntb_flags=ntb_flags,
        rules_of_origin=roo,
        total_landed_cost_add_pct=round(applied_duty + vat, 2),
        fetched_at=now,
    )


# ---------------------------------------------------------------------------
# Phase 4 — Seller discovery
# ---------------------------------------------------------------------------

async def _phase4_seller_discovery(
    hs_code: str,
    config: TradeMinConfig,  # noqa: ARG001
) -> list[SellerCandidate]:
    im_r, ti_r = await asyncio.gather(
        _safe_quote("indiamart", {"hs_code": hs_code, "page_size": 20}),
        _safe_quote("tradeindia", {"hs_code": hs_code, "limit": 20}),
    )

    candidates: list[SellerCandidate] = []

    if im_r is not None:
        for s in im_r.get("sellers", [])[:15]:
            candidates.append(_seller_from_raw(s, "indiamart", hs_code))

    if ti_r is not None:
        for s in ti_r.get("sellers", [])[:10]:
            candidates.append(_seller_from_raw(s, "tradeindia", hs_code))

    return candidates


def _seller_from_raw(s: dict, source: str, hs_code: str) -> SellerCandidate:
    state = s.get("state", "")
    lo = _safe_float(s.get("min_price"))
    hi = _safe_float(s.get("max_price"))
    if lo is not None and hi is not None:
        mid_price: float | None = round((lo + hi) / 2.0, 4)
    else:
        mid_price = lo

    return SellerCandidate(
        candidate_id=s.get("company_id", f"{source}-{uuid4().hex[:8]}"),
        company_name=s.get("company_name", ""),
        location_city=s.get("city", ""),
        location_state=state,
        hs_code=hs_code,
        estimated_fob_price_usd=mid_price,
        annual_capacity_mt=_safe_float(s.get("annual_capacity_mt")),
        source=source,  # type: ignore[arg-type]
        listing_url=s.get("listing_url") or None,
        contact_id=s.get("contact_id") or None,
        supplier_to_port_km=_STATE_TO_PORT_KM.get(state),
    )


# ---------------------------------------------------------------------------
# Phase 5 — Confidence + assemble + persist + emit
# ---------------------------------------------------------------------------

def _compute_confidence(
    phase1: _Phase1Result,
    phase2: _Phase2Result,
    tariff: TariffData,
    sellers: list[SellerCandidate],
    net_price_gap: float,
    config: TradeMinConfig,
) -> float:
    p1_map = {4: 50.0, 3: 40.0, 2: 25.0, 1: 10.0, 0: 0.0}
    score = p1_map.get(phase1.active_connectors, 0.0)

    live = 0.0
    if phase2.active_rfq_count > 0:
        live += 15.0
    if phase2.active_tender_count > 0:
        live += 10.0
    if phase2.disruption_detected:
        live = min(live + 5.0, 25.0)
    score += min(live, float(config.confidence_live_max))

    if tariff.applied_duty_pct > 0 or tariff.ntb_flags:
        # MacMap actually returned data (not the zero placeholder)
        if net_price_gap >= config.tariff_marginal_floor:
            score += float(config.confidence_tariff_max)
        elif net_price_gap >= config.tariff_hard_discard_floor:
            score += 5.0
    # else: MacMap was unavailable — 0 tariff pts (unverified)

    if len(sellers) >= 3:
        score += float(config.confidence_sellers_max)
    elif len(sellers) >= 1:
        score += 5.0

    return round(min(score, 100.0), 1)


def _determine_urgency(phase1: _Phase1Result, phase2: _Phase2Result) -> int:
    if phase2.disruption_detected:
        return 4
    if phase2.active_rfq_count > 0 or phase2.active_tender_count > 0:
        return 3
    if any(b.urgency_tier >= 2 for b in phase1.buyer_signals):
        return 2
    return 1


async def _phase5_assemble_and_persist(
    hs_code: str,
    market: str,
    phase1: _Phase1Result,
    phase2: _Phase2Result,
    tariff: TariffData,
    sellers: list[SellerCandidate],
    config: TradeMinConfig,
) -> EnrichedOpportunity | None:
    now = datetime.now(timezone.utc)

    net_price_gap = round(
        phase1.gross_price_gap_pct - tariff.total_landed_cost_add_pct, 2
    )

    if net_price_gap < config.tariff_hard_discard_floor:
        logger.info(
            "Hard discard %s×%s: net_price_gap=%.2f%% < floor=%.2f%%",
            hs_code, market, net_price_gap, config.tariff_hard_discard_floor,
        )
        return None

    tariff_viable = net_price_gap >= config.tariff_marginal_floor
    status: str = "ACTIVE" if tariff_viable else "MARGINAL"

    arb_rank = ranking.arbitrage_rank(phase1.demand_index, net_price_gap, config)
    if arb_rank < config.min_arbitrage_rank and status == "ACTIVE":
        return None

    urgency = _determine_urgency(phase1, phase2)
    confidence = _compute_confidence(
        phase1, phase2, tariff, sellers, net_price_gap, config
    )
    ttl_hours = config.urgency_ttl_hours[urgency]
    expires_at = now + timedelta(hours=ttl_hours)

    opp = EnrichedOpportunity(
        opportunity_id=_new_opportunity_id(),
        hs_code=hs_code,
        market=market,
        status=status,  # type: ignore[arg-type]
        scanned_at=now,
        demand_signals=phase1.demand_signals + phase2.demand_signals,
        demand_index=phase1.demand_index,
        price_gap_pct=phase1.gross_price_gap_pct,
        source=phase1.source,  # type: ignore[arg-type]
        buyer_signals=phase1.buyer_signals + phase2.buyer_signals,
        active_rfq_count=phase2.active_rfq_count,
        active_tender_count=phase2.active_tender_count,
        urgency_tier=urgency,
        tariff_data=tariff,
        net_price_gap_pct=net_price_gap,
        tariff_viable=tariff_viable,
        seller_candidates=sellers,
        arbitrage_rank=arb_rank,
        confidence_score=confidence,
        expires_at=expires_at,
    )

    try:
        repo = await get_repository()
        await repo.save(opp)
    except Exception as exc:
        logger.error("Failed to persist opportunity %s: %s", opp.opportunity_id, exc)

    try:
        await get_publisher().publish(DomainEvent(
            topic=EventTopic.OPPORTUNITY_DETECTED,
            payload={
                "opportunity_id":    opp.opportunity_id,
                "hs_code":           opp.hs_code,
                "market":            opp.market,
                "arbitrage_rank":    opp.arbitrage_rank,
                "confidence_score":  opp.confidence_score,
                "demand_index":      opp.demand_index,
                "net_price_gap_pct": opp.net_price_gap_pct,
                "urgency_tier":      opp.urgency_tier,
                "buyer_count":       len(opp.buyer_signals),
                "seller_count":      len(opp.seller_candidates),
                "tariff_viable":     opp.tariff_viable,
                "expires_at":        opp.expires_at.isoformat() if opp.expires_at else None,
            },
        ))
    except Exception as exc:
        logger.warning("Event publish failed for %s: %s", opp.opportunity_id, exc)

    return opp


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def scan_opportunities(payload: ScanRequest) -> list[EnrichedOpportunity]:
    """Run the 5-phase pipeline for every HS-code × market combination."""
    config = DEFAULT_CONFIG
    hs_codes = payload.hs_codes or list(config.default_hs_codes)
    markets = payload.target_markets or list(config.default_markets)
    min_rank = max(payload.min_arbitrage_rank, config.min_arbitrage_rank)

    tasks = [
        _scan_one(hs, mkt, config, min_rank)
        for hs in hs_codes
        for mkt in markets
    ]
    raw = await asyncio.gather(*tasks, return_exceptions=True)

    results: list[EnrichedOpportunity] = []
    for r in raw:
        if isinstance(r, BaseException):
            logger.error("Scan task raised an unhandled exception: %s", r)
        elif r is not None:
            results.append(r)

    results.sort(key=lambda o: (o.urgency_tier, o.arbitrage_rank), reverse=True)
    return results[: config.max_per_scan]


async def _scan_one(
    hs_code: str,
    market: str,
    config: TradeMinConfig,
    min_rank: float,
) -> EnrichedOpportunity | None:
    try:
        phase1 = await _phase1_historical_demand(hs_code, market, config)
        phase2 = await _phase2_live_signals(hs_code, market, phase1, config)
        tariff = await _phase3_tariff_gate(
            hs_code, market, phase1.gross_price_gap_pct, config
        )
        net_gap = phase1.gross_price_gap_pct - tariff.total_landed_cost_add_pct
        if net_gap < config.tariff_hard_discard_floor:
            return None
        sellers = await _phase4_seller_discovery(hs_code, config)
        return await _phase5_assemble_and_persist(
            hs_code, market, phase1, phase2, tariff, sellers, config
        )
    except Exception as exc:
        logger.exception("Unhandled error in _scan_one(%s×%s): %s", hs_code, market, exc)
        return None


async def list_opportunities(
    limit: int = 100,
    min_rank: float = 0.0,
    urgency_tier: int | None = None,
) -> list[EnrichedOpportunity]:
    repo = await get_repository()
    opps = await repo.list_active(limit=limit * 2, min_rank=min_rank)
    if urgency_tier is not None:
        opps = [o for o in opps if o.urgency_tier == urgency_tier]
    return opps[:limit]


async def get_opportunity(opportunity_id: str) -> EnrichedOpportunity | None:
    repo = await get_repository()
    return await repo.get(opportunity_id)


async def get_match_seed(opportunity_id: str) -> MatchEngineSeed | None:
    """Return the pre-seeded Match Engine values for an opportunity.

    The Deals service or DealMaker agent calls this endpoint when creating a
    deal from an opportunity to avoid redundant Volza / IndiaMART API calls.
    """
    opp = await get_opportunity(opportunity_id)
    if opp is None:
        return None
    return _build_match_seed(opp)


def _build_match_seed(opp: EnrichedOpportunity) -> MatchEngineSeed:
    # Best buyer unit price from Volza manifests (true CIF price buyers paid)
    buyer_unit_prices = [
        b.unit_price_usd for b in opp.buyer_signals
        if b.unit_price_usd and b.unit_price_usd > 0
    ]
    buyer_target_price = (
        round(sum(buyer_unit_prices) / len(buyer_unit_prices), 4)
        if buyer_unit_prices else None
    )

    top_sellers = [
        {
            "supplier_id":        s.candidate_id,
            "company_name":       s.company_name,
            "location":           f"{s.location_city}, {s.location_state}",
            "fob_price_usd":      s.estimated_fob_price_usd,
            "supplier_to_port_km": s.supplier_to_port_km,
            "annual_capacity_mt": s.annual_capacity_mt,
            "source":             s.source,
            "listing_url":        s.listing_url,
            "contact_id":         s.contact_id,
        }
        for s in opp.seller_candidates[:5]
    ]

    return MatchEngineSeed(
        opportunity_id=opp.opportunity_id,
        hs_code=opp.hs_code,
        market=opp.market,
        buyer_target_price_usd=buyer_target_price,
        top_sellers=top_sellers,
        net_price_gap_pct=opp.net_price_gap_pct,
        urgency_tier=opp.urgency_tier,
        confidence_score=opp.confidence_score,
        tariff_data=opp.tariff_data,
        buyer_signals_count=len(opp.buyer_signals),
        seller_candidates_count=len(opp.seller_candidates),
    )


async def mark_opportunity_converted(opportunity_id: str) -> bool:
    repo = await get_repository()
    return await repo.mark_converted(opportunity_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
