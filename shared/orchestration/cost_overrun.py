"""Actual provider costs above what the price assumed (DEF-19).

The deal was priced on the cost sheet's estimate. When the booked freight (or inspection)
order reports its real fee and it runs more than ``COST_OVERRUN_PCT`` (10) % above the
sheet's line × quantity, the margin is smaller than everyone thinks: ``deal.exception``
(category ``MARGIN_BREACH``, stage ``COSTS:<line>_over_estimate``) once per line, with the
estimate, the actual and the margin points lost. Called when freight is booked and when a QC
booking is made; never raises.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_FEE_KEYS = {"ocean_freight": ("total_freight_usd", "total_usd", "amount_usd"), "inspection": ("fee_usd", "total_usd")}
_ORDER_TYPE = {"ocean_freight": "FREIGHT", "inspection": "QC"}
_alerted: set[str] = set()


def _pct() -> float:
    try:
        return float(os.getenv("COST_OVERRUN_PCT", "10"))
    except ValueError:
        return 10.0


def compare(line: str, estimate_usd: float, actual_usd: float, principal: float | None) -> dict[str, Any] | None:
    """Pure: the overrun, or None while within the tolerance."""
    if estimate_usd <= 0 or actual_usd <= estimate_usd * (1 + _pct() / 100.0):
        return None
    over = round(actual_usd - estimate_usd, 2)
    return {"line": line, "estimate_usd": round(estimate_usd, 2), "actual_usd": round(actual_usd, 2), "over_usd": over,
            "over_pct": round(over / estimate_usd * 100.0, 1),
            "margin_points_lost": round(over / principal * 100.0, 2) if principal else None}


async def check(deal_id: str, line: str = "ocean_freight") -> dict[str, Any] | None:
    try:
        from agents.negotiation_agent.repository import get_repository as neg_repo  # type: ignore[import]
        session = await neg_repo().get_session_by_deal(deal_id)
        sheet = getattr(session, "cost_sheet", None) or {}
        qty = float(getattr(session, "quantity_mt", None) or sheet.get("quantity_mt") or 0)
        row = next((ln for ln in sheet.get("lines") or [] if ln.get("key") == line), None)
        if not row or qty <= 0:
            return None
        estimate = float(row.get("usd_per_mt") or 0) * qty
        from services.shield_link.service import list_deal_orders  # type: ignore[import]
        actual = 0.0
        for o in (await list_deal_orders(deal_id)).orders:
            if str(o.order_type).upper() != _ORDER_TYPE[line] or o.status in ("FAILED", "CANCELLED"):
                continue
            payload = getattr(o, "provider_payload", None) or {}
            fee = next((float(payload[k]) for k in _FEE_KEYS[line] if isinstance(payload.get(k), (int, float))), 0.0)
            actual += fee
        principal = None
        try:
            from services.escrow_guard.repository import get_repository as eg_repo  # type: ignore[import]
            hold = await eg_repo().get_hold_by_deal(deal_id)
            principal = float(hold.amount) if hold else None
        except Exception:  # noqa: BLE001
            principal = None
        found = compare(line, estimate, actual, principal)
    except Exception as exc:  # noqa: BLE001
        logger.debug("CostOverrun: %s/%s not checked: %s", deal_id, line, exc)
        return None
    if found and f"{deal_id}:{line}" not in _alerted:
        _alerted.add(f"{deal_id}:{line}")
        from shared.bus import get_publisher
        from shared.domain.events import DomainEvent, EventTopic
        await get_publisher().publish(DomainEvent(topic=EventTopic.DEAL_EXCEPTION, deal_id=deal_id, payload={
            "source": "cost_overrun", "stage": f"COSTS:{line}_over_estimate", "category": "MARGIN_BREACH",
            "error": (f"{line.replace('_', ' ')} costs {found['actual_usd']:,.2f} USD against {found['estimate_usd']:,.2f} "
                      f"priced in (+{found['over_pct']} %"
                      + (f", {found['margin_points_lost']} margin points" if found["margin_points_lost"] else "") + ")"),
            "action_required": "review_the_margin_and_the_freight_estimate_for_this_lane", **found,
        }))
    return found


def reset_for_tests() -> None:
    _alerted.clear()
