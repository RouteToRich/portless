"""Agent scorecard (todo 23.2, P2): one row per agent — what it did, how people judged it, the
money it moved, what it cost — and the autonomy level its own record supports.

An "agent" is the group of gates one automated step opens (``AGENTS``). From the approvals in
the window: gates opened, approved / rejected by a person, auto-approved (L2 / L3, value
threshold), timed out, edited before approval (``modified_body``), still pending; the money its
approved gates moved (``financial_impact_usd``); the share a person agreed with
(approved ÷ decided). The recommendation is only evidence — never applied: L2 needs at least
``SCORECARD_MIN_DECISIONS`` (30) human decisions with ≥ 95 % approved and ≤ 5 % edited, L1 at
≥ 85 %; money agents never go above ``AUTONOMY_MONEY_CEILING``. Run costs (LLM / Apify) are the
platform-wide figures of shared/cost_meter.py for the same window.
``GET /hitl-approval/scorecard?days=30``.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

AGENTS: dict[str, tuple[str, ...]] = {
    "negotiation": ("SEND_NEGOTIATION_EMAIL", "CONFIRM_NEGOTIATION_AGREEMENT"),
    "buyer_outreach": ("SEND_BUYER_OUTREACH",),
    "matchmaking": ("CONFIRM_MATCH", "SEND_INDICATIVE_OFFER", "SEND_BEST_AND_FINAL"),
    "sourcing": ("DISPATCH_RFQ", "CONFIRM_SUPPLIER_SELECTION", "CONFIRM_SUPPLIER_ORDER"),
    "reply_reading": ("CONFIRM_PI_ACCEPTANCE", "CONFIRM_BUYER_ACCEPTANCE", "PARSE_INBOUND_EMAIL"),
    "exception_resolver": ("RESOLVE_EXCEPTION",),
    "notifications": ("SEND_DEAL_NOTIFICATION",),
    "logistics": ("ISSUE_PO", "BOOK_FREIGHT", "FILE_CUSTOMS", "SUBMIT_VGM", "APPROVE_TRANSPORT_DOCUMENT"),
    "money": ("RELEASE_ESCROW_TRANCHE", "RELEASE_SUPPLIER_PAYOUT", "REFUND_ESCROW"),
    "compliance": ("CONFIRM_TRADE_COMPLIANCE", "CONFIRM_COUNTERPARTY_RISK"),
}
MONEY_AGENTS = {"money"}


def _aware(dt: Any) -> datetime | None:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def agent_of(action: str) -> str:
    return next((name for name, actions in AGENTS.items() if action in actions), "other")


def recommend(row: dict[str, Any], agent: str) -> tuple[str, str]:
    try:
        need = int(os.getenv("SCORECARD_MIN_DECISIONS", "30"))
    except ValueError:
        need = 30
    decided = row["approved"] + row["rejected"]
    if decided < need:
        return "L0", f"{decided} human decisions so far — {need} needed before any change"
    agree = row["approved"] / decided
    edited = row["edited"] / max(1, row["approved"])
    level = "L2" if agree >= 0.95 and edited <= 0.05 else "L1" if agree >= 0.85 else "L0"
    if agent in MONEY_AGENTS:
        ceiling = os.getenv("AUTONOMY_MONEY_CEILING", "L1").upper()
        level = min(level, ceiling)
    return level, f"{agree:.0%} approved, {edited:.0%} edited over {decided} decisions"


def tally(approvals: list[Any], *, since: datetime | None = None) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for a in approvals:
        created = _aware(getattr(a, "created_at", None))
        if since is not None and created is not None and created < since:
            continue
        agent = agent_of(str(getattr(a, "action", "")))
        r = rows.setdefault(agent, {"actions": set(), "gates": 0, "approved": 0, "rejected": 0, "auto_approved": 0,
                                    "timed_out": 0, "pending": 0, "edited": 0, "money_moved_usd": 0.0})
        r["actions"].add(str(a.action))
        r["gates"] += 1
        st = str(getattr(a, "status", ""))
        if st == "AUTO_APPROVED":
            r["auto_approved"] += 1
        elif st == "TIMED_OUT":
            r["timed_out"] += 1
        elif st == "PENDING":
            r["pending"] += 1
        elif st == "APPROVED":
            r["approved"] += 1
            if getattr(a, "modified_body", None):
                r["edited"] += 1
        elif st == "REJECTED":
            r["rejected"] += 1
        if st in ("APPROVED", "AUTO_APPROVED") and getattr(a, "financial_impact_usd", None):
            r["money_moved_usd"] += float(a.financial_impact_usd)
    for agent, r in rows.items():
        r["actions"] = sorted(r["actions"])
        r["money_moved_usd"] = round(r["money_moved_usd"], 2)
        decided = r["approved"] + r["rejected"]
        r["agreement_pct"] = round(100.0 * r["approved"] / decided, 1) if decided else None
        r["recommended_level"], r["recommendation_basis"] = recommend(r, agent)
    return rows


async def scorecard(days: int = 30, now: datetime | None = None) -> dict[str, Any]:
    from services.hitl_approval.repository import get_repository  # type: ignore[import]
    now = now or datetime.now(timezone.utc)
    days = max(1, min(int(days), 366))
    approvals = await get_repository().list_by_status(None)
    rows = tally(approvals, since=now - timedelta(days=days))
    costs = None
    try:
        from shared import cost_meter
        costs = (await cost_meter.report(days, now=now))["totals"]
    except Exception:  # noqa: BLE001
        pass
    return {"days": days, "agents": dict(sorted(rows.items(), key=lambda kv: -kv[1]["gates"])),
            "platform_costs": costs,
            "note": "Recommendations are evidence only; levels change on the Autonomy Settings page."}
