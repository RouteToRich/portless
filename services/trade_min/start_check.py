"""What can stop a deal, known before "Start prospect" — and the ready-to-start checklist.

``annotate(opp)`` adds what the deal would otherwise only learn after it was created:

* **export policy** — India's policy for the HS code (ITC-HS seed + operator
  confirmations, ``compliance.check_hs``) and the destination check
  (``compliance.check_destination``: embargoed / high-risk); a block never lists;
* **market news** — the go / caution / avoid verdict of ``market_check`` (news + ITC-HS
  policy), run at scan / rescore (``refresh=True``), read from its cache otherwise;
* **destination requirements** — from the catalogue product line.

``checklist(opp)`` turns everything known into one line of ok / warn / fail checks:
reachable buyers, reachable suppliers, export policy, price checked, margin at the
floor, duty verified, news, no running deal. "Start prospect" names every fail.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from .schemas import EnrichedOpportunity

logger = logging.getLogger(__name__)

EXPORT_BLOCKED = "export_blocked"
EXPORT_REVIEW = "export_review"
NEWS_AVOID = "market_news_avoid"
NEWS_CAUTION = "market_news_caution"


async def export_policy(hs_code: str, market: str) -> dict[str, Any]:
    try:
        from shared.orchestration import compliance
        hs = await compliance.check_hs(hs_code)
        dest = compliance.check_destination(market)
    except Exception as exc:  # noqa: BLE001 - the radar loads without it
        logger.debug("Start check: export policy unavailable for %s: %s", hs_code, exc)
        return {}
    status = (hs.data or {}).get("status", "unverified")
    severity = hs.severity
    if dest is not None and dest.severity == "block":
        severity = "block"
    elif dest is not None and severity == "info":
        severity = dest.severity
    return {
        "status": status, "severity": severity, "message": hs.message,
        "destination_severity": dest.severity if dest else None,
        "destination_message": dest.message if dest else None,
    }


async def market_news(hs_code: str, market: str, *, refresh: bool) -> dict[str, Any] | None:
    try:
        from shared.orchestration import market_check
        if not market_check.enabled():
            return None
        key = f"{hs_code}:{market.upper()}"
        check = await market_check._cached(key)  # noqa: SLF001 - its own cache
        if check is None and refresh:
            check = await market_check.run(hs_code, market)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Start check: market check unavailable for %s×%s: %s", hs_code, market, exc)
        return None
    if check is None:
        return None
    return {"verdict": check.verdict, "summary": check.summary,
            "checked_at": check.checked_at.isoformat() if check.checked_at else None}


def import_requirements(hs_code: str, market: str) -> list[str]:
    try:
        from shared import catalogue
        return catalogue.import_requirements_for(hs_code, market)
    except Exception:  # noqa: BLE001
        return []


async def annotate(opp: EnrichedOpportunity, *, refresh_news: bool = False) -> EnrichedOpportunity:
    """Fill export policy, news verdict and destination requirements (never raises)."""
    try:
        opp.export_policy = await export_policy(opp.hs_code, opp.market) or None
        news = await market_news(opp.hs_code, opp.market, refresh=refresh_news)
        if news is not None:
            opp.market_check = news
        opp.import_requirements = import_requirements(opp.hs_code, opp.market)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Start check: annotate failed for %s: %s", opp.opportunity_id, exc)
    return opp


def blocked(opp: EnrichedOpportunity) -> bool:
    """An opportunity the export policy forbids (prohibited product / embargoed market)."""
    return bool(opp.export_policy and opp.export_policy.get("severity") == "block")


def _outreach_needed() -> int:
    try:
        return max(1, int(os.getenv("OUTREACH_PARALLEL", "") or 2))
    except ValueError:
        return 2


def _reachable_buyer(b: Any) -> bool:
    return bool(b.email) and (b.email_status or "ok") not in ("invalid", "bounced") \
        and "role_suspect" not in (b.flags or []) and not (b.validation and b.validation.status == "failed")


def _reachable_supplier(s: Any) -> bool:
    return bool(s.email or s.phone) and "product_mismatch" not in (s.flags or []) \
        and not (s.validation and s.validation.status == "failed")


def checklist(opp: EnrichedOpportunity) -> list[dict[str, Any]]:
    """The ready-to-start checks, in the order the deal meets them."""
    out: list[dict[str, Any]] = []

    def add(key: str, label: str, status: str, detail: str) -> None:
        out.append({"key": key, "label": label, "status": status, "detail": detail})

    need = _outreach_needed()
    buyers = sum(1 for b in opp.buyer_signals if _reachable_buyer(b))
    add("buyers", "Reachable buyers", "ok" if buyers >= need else "warn" if buyers else "fail",
        f"{buyers} buyer(s) with a usable e-mail (outreach writes to {need} at a time)"
        + ("" if buyers else " — the prospect would send nothing; find contacts first (Rescore)"))
    sellers = sum(1 for s in opp.seller_candidates if _reachable_supplier(s))
    add("suppliers", "Reachable suppliers", "ok" if sellers >= 2 else "warn" if sellers else "fail",
        f"{sellers} supplier(s) with an e-mail or phone for the RFQ")

    ep = opp.export_policy or {}
    sev = ep.get("severity")
    if not ep:
        add("export_policy", "Export policy", "warn", "not checked")
    elif sev == "block":
        add("export_policy", "Export policy", "fail", ep.get("destination_message") if ep.get("destination_severity") == "block" else ep.get("message", ""))
    elif sev == "review":
        add("export_policy", "Export policy", "warn",
            (ep.get("message") or "") + " — confirm once (APPROVE records it) before the PI")
    else:
        add("export_policy", "Export policy", "ok", ep.get("message") or "free")

    flags = set(opp.flags or [])
    add("price", "Price checked", "warn" if "price_check_needed" in flags else "ok",
        opp.gap_note or "gap within the plausible range")

    p = opp.profit
    if p is None or p.margin_pct is None:
        add("margin", "Margin at the floor", "warn", "needs a buyer and a supplier price")
    elif p.margin_usd_per_mt is not None and p.margin_usd_per_mt <= 0:
        add("margin", "Margin at the floor", "fail", f"loss of {abs(p.margin_usd_per_mt):,.0f} USD/MT at these prices")
    elif not p.meets_floor:
        add("margin", "Margin at the floor", "warn", f"{p.margin_pct:.1f}% is under the {p.margin_floor_pct or 0:.0f}% floor")
    else:
        add("margin", "Margin at the floor", "ok", f"{p.margin_pct:.1f}% (floor {p.margin_floor_pct or 0:.0f}%)")

    t = opp.tariff_data
    add("duty", "Duty verified", "ok" if (t is None or t.verified) else "warn",
        "duty confirmed" if (t is None or t.verified) else "no tariff source answered — 0 % is unconfirmed")

    news = opp.market_check or {}
    verdict = news.get("verdict")
    if verdict == "avoid":
        add("news", "Market news", "fail", news.get("summary") or "the news check says avoid")
    elif verdict == "caution":
        add("news", "Market news", "warn", news.get("summary") or "the news check says caution")
    elif verdict == "go":
        add("news", "Market news", "ok", news.get("summary") or "no blocking news")
    else:
        add("news", "Market news", "warn", "not checked yet (runs at scan / rescore)")

    running = opp.corridor_deals or []
    add("running_deal", "No deal already running", "warn" if running else "ok",
        ", ".join(f"{d['deal_id']} {d['status']}" for d in running) if running else "none")
    if opp.import_requirements:
        add("destination", "Destination requirements", "warn", "needs: " + "; ".join(opp.import_requirements))
    return out
