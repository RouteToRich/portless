"""Supplier sourcing agent — stalled RFQs (todo 23.2).

When a prospect has an interested buyer but no usable supplier quote ``PROSPECT_STALL_DAYS``
after it entered MATCHING (or after the last widening), the search widens on its own instead of
waiting for a person: directory suppliers of the same product — and of neighbouring HS codes
(same 4-digit heading) — that have not been asked yet, are contactable (an e-mail, not
do-not-contact, not failed the check, not flagged ``product_mismatch``) get the quote request
(``continuations.rfq_for_supplier`` → a ``DISPATCH_RFQ`` gate per supplier, policy level as
usual), best first: traded → verified → manual (staff-added) → score. At most
``SOURCING_AGENT_MAX_NEW`` (3) per widening; the deal records ``sourcing_widened_at`` so it
waits another stall period before widening again. ``SOURCING_AGENT_ENABLED`` (default on).

Code decides who is asked; nothing here spends money beyond the e-mails the gates send.
Runs from the deal watcher; ``POST /deals/{id}/sourcing/widen`` runs it for one prospect now.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return os.getenv("SOURCING_AGENT_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def max_new() -> int:
    try:
        return max(1, int(os.getenv("SOURCING_AGENT_MAX_NEW", "3")))
    except ValueError:
        return 3


def _status(x: Any) -> str:
    s = getattr(x, "status", "")
    return str(getattr(s, "value", s) or "")


def _aware(dt: Any) -> datetime | None:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def supply_short(deal: Any) -> float:
    """MT the interested buyers want beyond what the quoting suppliers can supply (0 when covered or
    unknown). Only counted with ``DEAL_COMPOSITION_ENABLED`` — partial suppliers are kept then."""
    from shared.orchestration.outreach import composition_enabled
    if not composition_enabled():
        return 0.0
    want = 0.0
    for c in getattr(deal, "buyer_candidates", None) or []:
        if _status(c) in ("interested", "selected"):
            req = getattr(c, "requirement", None)
            want += float(getattr(req, "quantity_mt", None) or getattr(c, "quantity_mt", None) or 0)
    if not want:
        return 0.0
    have = 0.0
    for o in getattr(deal, "supplier_offers", None) or []:
        if not getattr(o, "fob_price_usd_per_mt", None) or getattr(o, "verification_failed", False):
            continue
        cap = getattr(o, "capacity_mt_per_month", None) or getattr(o, "max_quantity_mt", None)
        if not cap:
            return 0.0  # a quote without a stated capacity may cover it all — ask it first
        have += float(cap)
    return round(max(0.0, want - have), 2) if have else 0.0


def stalled(deal: Any, now: datetime) -> bool:
    """An interested buyer, no quoted supplier price (or, with composition on, quotes that cover only
    part of the buyers' quantity), and a stall period since the last move."""
    if _status(deal) != "MATCHING":
        return False
    if not any(_status(c) in ("interested", "selected") for c in getattr(deal, "buyer_candidates", None) or []):
        return False
    if supply_short(deal) <= 0 and any(
            getattr(o, "fob_price_usd_per_mt", None) and getattr(o, "price_basis", "quoted") == "quoted"
            and not getattr(o, "verification_failed", False) for o in getattr(deal, "supplier_offers", None) or []):
        return False
    from shared.orchestration.prospect_desk import stall_days
    since = _aware(getattr(deal, "sourcing_widened_at", None)) or _aware(getattr(deal, "created_at", None))
    return since is not None and now - since >= timedelta(days=stall_days())


def _asked(deal: Any) -> tuple[set[str], set[str]]:
    ids, mails = set(), set()
    for o in getattr(deal, "supplier_offers", None) or []:
        ids.add(str(getattr(o, "supplier_id", "") or ""))
        if getattr(o, "email", None):
            mails.add(o.email.lower())
    for m in getattr(deal, "match_results", None) or []:
        ids.add(str(getattr(m, "supplier_id", "") or ""))
    for p in getattr(deal, "parties", None) or []:
        if getattr(p, "email", None):
            mails.add(p.email.lower())
    return ids, mails


async def candidates(deal: Any) -> list[Any]:
    """Directory suppliers for the prospect not asked yet, best first."""
    from services.trade_min import directory  # type: ignore[import]
    from services.trade_min.markets import contactable  # type: ignore[import]
    store = await directory.get_directory()
    hs = "".join(ch for ch in str(getattr(deal, "hs_code", "") or "") if ch.isdigit())
    ids, mails = _asked(deal)
    rows = []
    for rec in await store.list("supplier", limit=100_000):
        codes = ["".join(ch for ch in h if ch.isdigit()) for h in rec.hs_codes if h]
        if not any(c[:4] == hs[:4] for c in codes if len(c) >= 4):
            continue
        if not contactable(rec) or "product_mismatch" in (rec.flags or []):
            continue
        if rec.id in ids or (rec.email or "").lower() in mails:
            continue
        same_code = any(c[:6] == hs[:6] for c in codes)
        verified = rec.validation is not None and rec.validation.status == "verified"
        traded = bool((rec.product_facts or {}).get(hs[:6], {}).get("facts", {}).get("traded"))
        rows.append(((not traded, not verified, not same_code, rec.source != "manual", -(rec.fit_score or 0)), rec))
    rows.sort(key=lambda t: t[0])
    return [r for _, r in rows]


async def widen(deal_id: str, *, now: datetime | None = None, force: bool = False) -> dict[str, Any]:
    from services.deals.repository import get_repository  # type: ignore[import]
    from services.deals.service import get_deal  # type: ignore[import]
    from shared.orchestration.continuations import rfq_for_supplier
    now = now or datetime.now(timezone.utc)
    deal = await get_deal(deal_id)
    if not force and not stalled(deal, now):
        return {"deal_id": deal_id, "asked": [], "skipped": "not stalled"}
    asked = []
    for rec in (await candidates(deal))[: max_new()]:
        gate = await rfq_for_supplier(deal, rec.id, rec.name, rec.email)
        if gate:
            asked.append({"supplier_id": rec.id, "name": rec.name, "approval_id": gate})
    await get_repository().set_fields(deal_id, {"sourcing_widened_at": now})
    if asked:
        logger.info("SourcingAgent: deal %s stalled without a quote — RFQ to %d more supplier(s)", deal_id, len(asked))
    return {"deal_id": deal_id, "asked": asked}


async def sweep(now: datetime | None = None) -> dict[str, int]:
    counts = {"widened": 0, "asked": 0}
    if not enabled():
        return counts
    now = now or datetime.now(timezone.utc)
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deals = await get_repository().list_active(limit=1000)
    except Exception as exc:  # noqa: BLE001
        logger.error("SourcingAgent: could not list deals: %s", exc)
        return counts
    from shared.orchestration.prospect_actions import is_parked
    for deal in deals:
        if is_parked(deal, now) or not stalled(deal, now):
            continue
        try:
            res = await widen(deal.deal_id, now=now)
            counts["widened"] += 1
            counts["asked"] += len(res.get("asked") or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("SourcingAgent: deal %s not widened: %s", deal.deal_id, exc)
    return counts
