"""Curated mock connector responses for ``TRADE_MIN_MOCK_MODE`` (dev/CI).

Bypasses every live trade-intelligence connector (Comtrade, Census, Alibaba
RFQ, GDELT, EU TED, WITS/Access2Markets, IndiaMART, TradeIndia) so the real
5-phase pipeline in ``service.py`` — ranking, tariff gating, confidence
scoring — still runs unmodified, just against canned instead of live data.

Three scenarios, selected by HS code, are designed to land in different
branches of the deal state machine once carried through the rest of the
pipeline by an end-to-end test:

  090930  Cumin Seeds (market US)      — strong ACTIVE opportunity, high
                                          confidence, urgency tier 3 (active
                                          RFQs) — intended to sail to PAID.
  100630  Basmati Rice (market DE)     — thin post-tariff margin → MARGINAL,
                                          low confidence — intended to be
                                          driven into a negotiation that
                                          never converges (NEGOTIATION_FAILED).
  121190  Botanical Extracts (market AE) — ACTIVE with a live supply
                                          disruption → urgency tier 4
                                          (highest) — intended to reach
                                          QC_FAILED and exercise the
                                          Exception Resolver.

Each HS code's 4-digit prefix (0909, 1006, 1211) is deliberately absent from
``shared.connectors.trade_intel._HS_TO_KEYWORD``, so Phase 2's keyword
derivation (``_HS_TO_KEYWORD.get(hs_code[:4], hs_code)``) falls back to the
literal HS code — the same string used as the ``query``/``product_keyword``
request field for the Alibaba RFQ, GDELT, and EU TED connectors. That lets a
single lookup key match every connector's request shape without needing to
mirror trade_min's per-connector request construction here.
"""
from __future__ import annotations

import hashlib

_SCENARIOS: dict[str, dict] = {
    "090930": {
        "market": "US",
        "comtrade": {"total_records": 300, "fob": 1200.0, "cif": 1500.0},
        "rfq_count": 14,
        "tender_count": 3,
        "disruption": None,
        "tariff": {"applied_duty_pct": 3.0, "ntb_flags": []},
        "sellers": [
            {"company_name": "Ratan Agro Exports", "city": "Unjha", "state": "Gujarat",
             "min_price": 1100, "max_price": 1250, "annual_capacity_mt": 500},
            {"company_name": "Sona Masala Traders", "city": "Jodhpur", "state": "Rajasthan",
             "min_price": 1150, "max_price": 1300, "annual_capacity_mt": 300},
            {"company_name": "Global Spice House", "city": "Chennai", "state": "Tamil Nadu",
             "min_price": 1180, "max_price": 1320, "annual_capacity_mt": 450},
        ],
    },
    "100630": {
        "market": "DE",
        "comtrade": {"total_records": 80, "fob": 1400.0, "cif": 1560.0},
        "rfq_count": 0,
        "tender_count": 0,
        "disruption": None,
        "tariff": {"applied_duty_pct": 2.0, "ntb_flags": []},
        "sellers": [
            {"company_name": "Haryana Basmati Mills", "city": "Karnal", "state": "Haryana",
             "min_price": 1250, "max_price": 1380, "annual_capacity_mt": 200},
            {"company_name": "Kohinoor Rice Co", "city": "Amritsar", "state": "Punjab",
             "min_price": 1300, "max_price": 1420, "annual_capacity_mt": 180},
        ],
    },
    "121190": {
        "market": "AE",
        "comtrade": {"total_records": 150, "fob": 900.0, "cif": 1200.0},
        "rfq_count": 5,
        "tender_count": 0,
        "disruption": {"signal_strength": "HIGH", "article_count": 12},
        "tariff": {"applied_duty_pct": 5.0, "ntb_flags": []},
        "sellers": [
            {"company_name": "Himalaya Botanics", "city": "Dehradun", "state": "Uttarakhand",
             "min_price": 820, "max_price": 950, "annual_capacity_mt": 120},
            {"company_name": "Kerala Herbal Exports", "city": "Kochi", "state": "Kerala",
             "min_price": 850, "max_price": 980, "annual_capacity_mt": 100},
            {"company_name": "Deccan Naturals", "city": "Hyderabad", "state": "Telangana",
             "min_price": 870, "max_price": 1000, "annual_capacity_mt": 90},
        ],
    },
}


def _generic_scenario(hs_code: str, market: str) -> dict:
    """Deterministic scenario for any hs_code not in the curated set above.

    Scanning with the app's default HS codes (or any HS code a user types in)
    must still produce a usable, fully-populated opportunity — sellers
    included — not just the 3 hand-authored scenarios. Same deterministic-
    hash approach as ranking.simulated_demand_index, so a given hs_code always
    reproduces the same mock data.
    """
    seed = int(hashlib.sha256(f"{hs_code}:{market}".encode()).hexdigest()[:8], 16)
    fob = round(700.0 + (seed % 600), 2)
    return {
        "market": market,
        "comtrade": {"total_records": 40 + seed % 250, "fob": fob, "cif": round(fob * 1.22, 2)},
        "rfq_count": seed % 9,
        "tender_count": seed % 4,
        "disruption": None,
        "tariff": {"applied_duty_pct": round(2.0 + (seed % 6), 2), "ntb_flags": []},
        "sellers": [
            {
                "company_name": f"{hs_code} Exporters {i + 1}", "city": "Mumbai", "state": "Maharashtra",
                "min_price": round(fob - 40, 2), "max_price": round(fob + 60, 2),
                "annual_capacity_mt": 150 + i * 50,
            }
            for i in range(3)
        ],
    }


def _scenario_key(req: dict) -> tuple[str, str]:
    """(hs_code-or-keyword, market) from any connector's request shape — the
    Apify actor connectors pass ``products``/``keyword`` and ``markets``."""
    products = req.get("products") or []
    key = (
        req.get("hs_code") or req.get("product_keyword") or req.get("query") or req.get("keyword")
        or (products[0] if products else None) or "UNKNOWN"
    )
    markets = req.get("markets") or []
    market = (
        req.get("market") or req.get("country") or req.get("buyer_country")
        or req.get("reporter_iso2") or (markets[0] if markets else None) or "US"
    )
    return str(key), str(market)


def _match_scenario(req: dict) -> dict:
    key, market = _scenario_key(req)
    return _SCENARIOS.get(key) or _generic_scenario(key, market)


def mock_quote(name: str, req: dict) -> dict | None:
    """Return a normalized connector-shaped payload, or None (connector skip).

    Mirrors exactly what a live connector returns after its own parsing —
    the shapes ``service.py``'s phase extractors expect (see
    ``_extract_comtrade``, ``_phase2_live_signals``, ``_phase3_tariff_gate``,
    ``_phase4_seller_discovery``) — not raw upstream provider payloads.
    """
    scenario = _match_scenario(req)

    if name == "comtrade":
        ct = scenario["comtrade"]
        records = [{"fob_value_usd": ct["fob"], "cif_value_usd": ct["cif"]} for _ in range(8)]
        return {"records": records, "total_records": ct["total_records"], "period": "2025"}

    if name == "census":
        # Real CENSUS_API_KEY is never configured in this project — mirror
        # that absence in mock mode too, so Phase 1's active_connectors count
        # (and therefore its confidence-score contribution) stays realistic.
        return None

    if name == "alibaba_rfq":
        count = scenario["rfq_count"]
        rfqs = [{"buyer_country": scenario["market"]} for _ in range(count)]
        return {"rfqs": rfqs, "total": count}

    if name == "gdelt_disruption":
        disruption = scenario.get("disruption")
        if not disruption:
            return {"disruption_detected": False}
        return {
            "disruption_detected": True,
            "signal_strength": disruption["signal_strength"],
            "article_count": disruption["article_count"],
        }

    if name == "eu_ted":
        count = scenario["tender_count"]
        tenders = [
            {
                "buyer_name": f"EU Institution {i + 1}",
                "buyer_country": scenario["market"],
                "contract_value": 50_000 + i * 5_000,
                "notice_url": None,
            }
            for i in range(count)
        ]
        return {"tenders": tenders, "total": count}

    if name == "access2markets":
        # No Access2Markets scenario data is modeled — always fall through to
        # WITS, mirroring live behaviour when that endpoint isn't configured.
        return {"applied_duty_pct": None}

    if name == "wits_tariff":
        return dict(scenario["tariff"])

    if name == "indiamart":
        return {
            "sellers": [{"company_id": f"IM-{i + 1}", **s} for i, s in enumerate(scenario["sellers"])]
        }

    if name == "tradeindia":
        return {"sellers": []}

    # ── PortLess-owned Apify actors (shared/connectors/apify.py) ─────────
    # Shapes mirror each connector's normalised quote() output exactly, so the
    # pipeline exercises the same extraction code paths as a live run.
    ct = scenario["comtrade"]
    key, requested_market = _scenario_key(req)
    # Curated scenarios pin their market; generic ones follow the request.
    market = scenario["market"] if key in _SCENARIOS else requested_market

    if name == "apify_export_markets":
        return {
            "markets": [{
                "market": market,
                "market_name": market,
                "hs_code": key,
                "latest_period": "2025",
                "import_value_usd": float(ct["total_records"]) * 250_000.0,
                "growth_1y_pct": 12.5,
                "exporter_value_usd": float(ct["total_records"]) * 40_000.0,
                "exporter_share_pct": 16.0,
                "unit_value_usd_per_kg": round(ct["cif"] / 1000.0, 4),
                "unit_value_usd_per_mt": float(ct["cif"]),
                "exporter_unit_value_usd_per_mt": float(ct["fob"]),
                "exporter_world_unit_value_usd_per_mt": float(ct["fob"]),
                "top_suppliers": [],
                "score": min(100, 30 + ct["total_records"] // 4),
                "flags": ["fast_growing"],
                "summary": "",
                "next_step": "",
                "url": None,
                "mirror": False,
            }],
            "total": 1,
            "actor": "mock~export-market-finder-ai",
        }

    if name == "apify_alibaba_rfq":
        count = min(scenario["rfq_count"], 5)
        return {
            "rfqs": [{
                "rfq_id": f"RFQ-{key}-{i + 1}",
                "title": f"Need {10 + i * 5} MT of HS {key}",
                "buyer_name": f"{market} Importer {i + 1}",
                "buyer_country": market,
                "quantity": 10.0 + i * 5,
                "quantity_unit": "tons",
                "posted_at": "2026-10-20T09:00:00Z",
                "quotes_left": 5,
                "unit_price_usd": float(ct["cif"]),
                "price_unit": "USD/MT",
                "contact_url": f"https://sourcing.alibaba.com/rfq/{key}-{i + 1}",
                "fit_score": 80 - i * 5,
                "fit_reason": "matched product line",
                "quote_reply": None,
            } for i in range(count)],
            "total": count,
            "actor": "mock~alibaba-rfq-leads-ai",
        }

    if name == "apify_us_importers":
        if market != "US":
            return {"buyers": [], "total": 0}
        return {
            "buyers": [{
                "buyer_name": f"US Importer {i + 1} Inc",
                "buyer_country": "US",
                "company_id": f"iy:us-importer-{key}-{i + 1}",
                "signal_type": "manifest",
                "source": "apify_us_importers",
                "last_import_date": "2026-09-15",
                "annual_volume_mt": 120.0 - i * 20,
                "estimated_order_value_usd": (120.0 - i * 20) * ct["cif"],
                "unit_price_usd": float(ct["cif"]),
                "price_unit": "USD/MT",
                "shipments_12m": 12 - i,
                "website": f"https://us-importer-{i + 1}.example.com",
                "phone": None,
                "contact_url": f"https://us-importer-{i + 1}.example.com",
                "address": "Newark, NJ",
                "hs_codes": [key],
                "fit_score": 85 - i * 10,
                "fit_reason": "imports this HS code from Asia every month",
                "pitch_angle": "You already buy this product from Vietnam; India ships 2 weeks faster.",
            } for i in range(3)],
            "total": 3,
            "actor": "mock~us-importer-finder-ai",
        }

    if name == "apify_india_suppliers":
        return {
            "sellers": [{
                "company_id": f"APIFY-IN-{i + 1}",
                "company_name": s["company_name"],
                "city": s["city"],
                "state": s["state"],
                "product_name": f"HS {key}",
                "hs_code": key,
                "min_price": s["min_price"],
                "max_price": s["max_price"],
                "price_unit": "USD/MT",
                "min_order_qty": "20 MT",
                "annual_capacity_mt": s["annual_capacity_mt"],
                "listing_url": f"https://www.indiamart.com/{s['company_name'].lower().replace(' ', '-')}",
                "contact_id": None,
                "email": f"sales@{s['company_name'].lower().replace(' ', '')}.in",
                "phone": "+919876543210",
                "website": f"https://{s['company_name'].lower().replace(' ', '')}.in",
                "gstin": None,
                "iec": None,
                "fit_score": 75,
            } for i, s in enumerate(scenario["sellers"][:2])],
            "total": min(2, len(scenario["sellers"])),
            "actor": "mock~india-supplier-finder-ai",
        }

    return None
