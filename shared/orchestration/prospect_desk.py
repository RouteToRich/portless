"""The prospect desk — every open prospect summarised the way a trader works them.

A prospect (deal at DRAFT / MATCHING) is a bet that one of its buyers and one of its
suppliers will trade at a margin. The desk answers, per prospect:

* **What is it worth?** The best buyer × supplier pair of the match board: margin per
  MT, chance of closing, expected value, firm or estimated prices — next to what the
  radar expected when it was started (``radar_estimate``).
* **Whose move is it?** ``waiting_on``: ``us`` (a firm pair to confirm, an offer to send,
  a price to push), ``buyers`` (introductions out, no reply), ``suppliers`` (quote
  requests out, no quote), ``buyer`` (indicative offer out, no reaction) — and for how
  many days since the last thing happened.
* **What should I do next?** A short ranked list of trader moves: confirm, send the
  indicative offer, push the supplier to the walk-away price, play the best quote
  against the others, ask for a reconfirmation before a quote expires, contact more
  buyers / suppliers, or park / drop a prospect that is not worth the effort.

``priority`` orders the desk: expected value when prices exist, else the radar's
expected profit scaled by how far the prospect has come.

Never raises for one prospect: a board that cannot be built gives a minimal row.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_PROSPECT_STATUSES = ("DRAFT", "MATCHING")
_INTERESTED = ("interested", "selected")
_CONTACTED = ("contacted", "followed_up")


def stall_days() -> int:
    try:
        return max(1, int(os.getenv("PROSPECT_STALL_DAYS", "") or 5))
    except ValueError:
        return 5


def drop_days() -> int:
    try:
        return max(stall_days() + 1, int(os.getenv("PROSPECT_DROP_DAYS", "") or 21))
    except ValueError:
        return 21


class DeskAction(BaseModel):
    kind: str
    """confirm | send_offer | push_supplier | push_buyer | best_and_final | reconfirm_quote |
    contact_buyers | chase_quotes | wait | park | drop"""
    text: str
    owner: str = "us"


class ProspectSummary(BaseModel):
    deal_id: str
    hs_code: str
    market: str | None = None
    status: str
    created_at: datetime | None = None
    age_days: int | None = None
    last_activity_at: datetime | None = None
    idle_days: int | None = None
    stalled: bool = False
    waiting_on: str = "us"
    waiting_detail: str = ""
    # funnel
    buyers_total: int = 0
    buyers_contacted: int = 0
    buyers_interested: int = 0
    buyers_declined: int = 0
    quotes: int = 0
    best_quote_usd_per_mt: float | None = None
    quote_spread_pct: float | None = None
    quotes_expiring: int = 0
    quotes_expired: int = 0
    undelivered: int = 0
    """Buyer introductions that were only logged / failed (no mailbox) — nothing reached them."""
    # best pair
    best_buyer: str | None = None
    best_supplier: str | None = None
    margin_usd_per_mt: float | None = None
    margin_pct: float | None = None
    probability: float | None = None
    expected_value_usd: float | None = None
    firm: bool = False
    quantity_mt: float | None = None
    max_supplier_fob_usd_per_mt: float | None = None
    min_buyer_price_usd_per_mt: float | None = None
    supplier_cut_needed_usd_per_mt: float | None = None
    # radar
    radar_expected_profit_usd: float | None = None
    radar_margin_usd_per_mt: float | None = None
    # effort spent on it (e-mails, replies, operator decisions, Apify money)
    emails_sent: int = 0
    replies: int = 0
    operator_touches: int = 0
    """Gates a person resolved for this prospect (auto-approved ones are not counted)."""
    apify_cost_usd: float | None = None
    """What the scans that found it cost (the opportunity's scan cost meter)."""
    busy_not_moving: bool = False
    """Much effort, no interested buyer and no quote — park or drop it."""
    # parked from the desk (prospect_actions)
    parked: bool = False
    parked_until: datetime | None = None
    parked_reason: str | None = None
    # trader view
    priority: float = 0.0
    actions: list[DeskAction] = Field(default_factory=list)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _days(since: datetime | None, now: datetime) -> int | None:
    since = _aware(since)
    return None if since is None else max(0, (now - since).days)


def _status(deal: Any) -> str:
    return str(getattr(getattr(deal, "status", None), "value", getattr(deal, "status", "")))


def effort_high() -> int:
    try:
        return max(1, int(os.getenv("PROSPECT_EFFORT_HIGH", "12")))
    except ValueError:
        return 12


async def _effort(deal: Any) -> dict[str, Any]:
    """E-mails out / in, decisions a person made and the scan cost — never raises."""
    out: dict[str, Any] = {"emails_sent": 0, "replies": 0, "operator_touches": 0, "apify_cost_usd": None}
    try:
        from agents.negotiation_agent.email_history import get_email_repository  # type: ignore[import]
        for e in await get_email_repository().list_by_deal(deal.deal_id):
            direction = str(getattr(getattr(e, "direction", ""), "value", getattr(e, "direction", ""))).lower()
            if direction == "outbound":
                out["emails_sent"] += 1
            elif direction == "inbound":
                out["replies"] += 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("Prospect desk: e-mails of %s unavailable: %s", deal.deal_id, exc)
    try:
        from services.hitl_approval.service import list_approvals  # type: ignore[import]
        for a in await list_approvals(deal_id=deal.deal_id):
            who = getattr(a, "resolved_by", None) or ""
            if who and not who.startswith("system") and getattr(a, "status", "") != "AUTO_APPROVED":
                out["operator_touches"] += 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("Prospect desk: approvals of %s unavailable: %s", deal.deal_id, exc)
    if getattr(deal, "opportunity_id", None):
        try:
            from services.trade_min.repository import get_repository as trade_repo  # type: ignore[import]
            opp = await (await trade_repo()).get(deal.opportunity_id)
            cost = (getattr(opp, "scan_cost", None) or {}) if opp is not None else {}
            if cost.get("usd") is not None:
                out["apify_cost_usd"] = round(float(cost["usd"]), 2)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Prospect desk: scan cost of %s unavailable: %s", deal.deal_id, exc)
    return out


def summarise(deal: Any, board: Any | None, *, now: datetime | None = None) -> ProspectSummary:
    """Pure: one prospect + its match board → the desk row."""
    now = now or datetime.now(timezone.utc)
    cands = list(getattr(deal, "buyer_candidates", None) or [])
    statuses = [getattr(c, "status", "pending") for c in cands]
    offers = list(getattr(deal, "supplier_offers", None) or [])
    radar = getattr(deal, "radar_estimate", None) or {}

    # last thing that happened on either side
    stamps = [getattr(deal, "updated_at", None), getattr(deal, "created_at", None)]
    for c in cands:
        stamps += [getattr(c, "contacted_at", None), getattr(c, "followed_up_at", None), getattr(c, "replied_at", None)]
        io = getattr(c, "indicative_offer", None)
        if io is not None:
            stamps += [getattr(io, "sent_at", None), getattr(io, "responded_at", None)]
    stamps += [getattr(o, "received_at", None) for o in offers]
    last = max((_aware(t) for t in stamps if t), default=None)

    out = ProspectSummary(
        deal_id=deal.deal_id, hs_code=deal.hs_code, market=getattr(deal, "market", None), status=_status(deal),
        created_at=getattr(deal, "created_at", None), age_days=_days(getattr(deal, "created_at", None), now),
        last_activity_at=last, idle_days=_days(last, now),
        buyers_total=len(cands), buyers_contacted=sum(1 for s in statuses if s in _CONTACTED + _INTERESTED + ("declined", "no_reply")),
        buyers_interested=sum(1 for s in statuses if s in _INTERESTED),
        buyers_declined=sum(1 for s in statuses if s in ("declined", "no_reply")),
        quotes=len({o.supplier_id for o in offers}),
        quantity_mt=getattr(deal, "quantity_mt", None),
        radar_expected_profit_usd=radar.get("expected_profit_usd"),
        radar_margin_usd_per_mt=radar.get("margin_usd_per_mt"),
    )
    pairs = list(getattr(board, "pairs", None) or []) if board is not None else []
    best = next((p for p in pairs if p.feasible), None)
    if board is not None:
        out.best_quote_usd_per_mt = getattr(board, "best_quote_usd_per_mt", None)
        out.quote_spread_pct = getattr(board, "quote_spread_pct", None)
        out.quotes_expired = len(getattr(board, "expired_offers", None) or [])
        out.quotes_expiring = len({p.supplier_id for p in pairs
                                   if p.quote_expires_in_days is not None and 0 <= p.quote_expires_in_days <= 3})
    if best is not None:
        out.best_buyer, out.best_supplier = best.buyer_name, best.supplier_name
        out.margin_usd_per_mt, out.margin_pct = best.margin_usd_per_mt, best.margin_pct
        out.probability, out.expected_value_usd, out.firm = best.probability, best.expected_value_usd, best.firm
        out.quantity_mt = best.quantity_mt
        out.max_supplier_fob_usd_per_mt = best.max_supplier_fob_usd_per_mt
        out.min_buyer_price_usd_per_mt = best.min_buyer_price_usd_per_mt
        out.supplier_cut_needed_usd_per_mt = best.supplier_cut_needed_usd_per_mt

    out.undelivered = sum(1 for c in cands if getattr(c, "send_error", None)
                          and getattr(c, "status", "pending") == "pending")
    _actions(out, deal, board, best, cands)
    # idle on either side is a stall: on ours it means the ball was dropped
    out.stalled = (out.idle_days or 0) >= stall_days()

    # priority: firm expected value first, then estimated value, then the radar's number
    stage = 0.2 + 0.4 * (1 if out.buyers_interested else 0) + 0.4 * (1 if out.quotes else 0)
    if out.expected_value_usd:
        out.priority = round(out.expected_value_usd * (1.5 if out.firm else 1.0), 2)
    elif out.radar_expected_profit_usd:
        out.priority = round(max(0.0, out.radar_expected_profit_usd) * stage, 2)
    if out.stalled:
        out.priority = round(out.priority * 0.7, 2)
    return out


def _actions(out: ProspectSummary, deal: Any, board: Any | None, best: Any | None, cands: list[Any]) -> None:
    acts: list[DeskAction] = []
    statuses = [getattr(c, "status", "pending") for c in cands]
    pending = sum(1 for s in statuses if s == "pending")
    min_p = getattr(board, "min_probability", 0.45) if board is not None else 0.45

    if best is not None and best.firm and best.probability >= min_p and not best.supplier_cut_needed_usd_per_mt:
        acts.append(DeskAction(kind="confirm", text=f"Confirm {best.buyer_name} × {best.supplier_name}: "
                               f"{best.margin_usd_per_mt:,.0f}/MT, {best.probability:.0%} likely — it becomes the deal"))
        out.waiting_on, out.waiting_detail = "us", "a firm pair is ready to confirm"
    if best is not None and best.supplier_price.basis == "quoted" and (best.supplier_cut_needed_usd_per_mt or 0) > 0:
        acts.append(DeskAction(kind="push_supplier", text=(
            f"Push {best.supplier_name} to ≤ {best.max_supplier_fob_usd_per_mt:,.0f} FOB "
            f"(−{best.supplier_cut_needed_usd_per_mt:,.0f}/MT) to reach the margin floor"
            + (f", or the buyer to ≥ {best.min_buyer_price_usd_per_mt:,.0f}" if best.min_buyer_price_usd_per_mt else ""))))
    if board is not None and (getattr(board, "quote_spread_pct", None) or 0) >= 3 and out.quotes >= 2:
        acts.append(DeskAction(kind="best_and_final", text=(
            f"Best quote {board.best_quote_usd_per_mt:,.0f} FOB, spread {board.quote_spread_pct:.0f}%: "
            f"ask the other suppliers for their best and final price")))
    if best is not None and best.supplier_price.basis == "quoted" and best.buyer_status in _INTERESTED \
            and not best.firm and getattr(best, "next_step", "").startswith("Send an indicative offer"):
        acts.append(DeskAction(kind="send_offer", text=f"Send {best.buyer_name} an indicative offer — "
                               f"their reaction gives their price (our minimum {best.landed.min_offer_usd_per_mt:,.0f})"
                               if best.landed else f"Send {best.buyer_name} an indicative offer"))
    if out.quotes_expiring:
        acts.append(DeskAction(kind="reconfirm_quote", text=f"{out.quotes_expiring} quote(s) expire within 3 days — "
                               "ask the supplier(s) to reconfirm before you commit the buyer"))
    if out.quotes_expired:
        acts.append(DeskAction(kind="reconfirm_quote", text=f"{out.quotes_expired} quote(s) expired — ask for a fresh price"))

    # whose move, when no firm action is ours
    if not any(a.kind in ("confirm",) for a in acts):
        if not out.buyers_interested:
            if out.buyers_contacted and not pending:
                out.waiting_on, out.waiting_detail = "buyers", f"{out.buyers_contacted} buyer(s) contacted, nobody said yes yet"
            elif out.buyers_contacted:
                out.waiting_on, out.waiting_detail = "buyers", f"{out.buyers_contacted} contacted, {pending} more to contact"
            else:
                out.waiting_on, out.waiting_detail = "us", "no buyer contacted yet"
                acts.append(DeskAction(kind="contact_buyers", text="Contact the buyers (Contact next buyers)"))
        elif not out.quotes:
            out.waiting_on, out.waiting_detail = "suppliers", "a buyer said yes — waiting for supplier quotes"
            acts.append(DeskAction(kind="chase_quotes", text="Chase the quote requests or add a supplier you know (phone quote)",
                                   owner="us"))
        elif best is not None and best.buyer_status in _INTERESTED and getattr(best, "next_step", "").startswith("Indicative offer sent"):
            out.waiting_on, out.waiting_detail = "buyer", f"indicative offer sent to {best.buyer_name}"
        elif any(a.kind in ("push_supplier", "send_offer", "best_and_final") for a in acts):
            out.waiting_on, out.waiting_detail = "us", "a price move is ours to make"
        else:
            out.waiting_on, out.waiting_detail = "us", "review the match board"

    if pending and out.buyers_contacted and not out.buyers_interested and (out.idle_days or 0) >= stall_days():
        acts.append(DeskAction(kind="contact_buyers", text=f"No reply for {out.idle_days} days — contact the next {pending} buyer(s)"))
    # park / drop: no interest and nothing left, or an old prospect that cannot clear the floor
    exhausted = cands and not pending and not out.buyers_interested and out.buyers_declined >= len(cands)
    if exhausted:
        acts.append(DeskAction(kind="drop", text="Every buyer declined or stayed silent — drop it, or add buyers / widen the scan"))
    elif (out.age_days or 0) >= drop_days() and not out.buyers_interested:
        acts.append(DeskAction(kind="park", text=f"{out.age_days} days old with no buyer interest — park it and free the effort"))
    elif best is not None and best.firm and (best.margin_usd_per_mt or 0) <= 0 and (out.age_days or 0) >= stall_days():
        acts.append(DeskAction(kind="drop", text="Firm prices leave no margin — drop unless a cheaper supplier appears"))
    if out.undelivered:
        acts.insert(0, DeskAction(kind="fix_mailbox", text=(
            f"{out.undelivered} buyer introduction(s) were never delivered (no outbound mailbox) — set up "
            "GMAIL_SENDER_ADDRESS / app password, then Contact next buyers")))
        if not out.buyers_contacted:
            out.waiting_on, out.waiting_detail = "us", "introductions not delivered — mailbox not set up"
            acts = [a for a in acts if a.kind != "contact_buyers"]
    if not acts:
        acts.append(DeskAction(kind="wait", text=out.waiting_detail or "Nothing to do yet", owner=out.waiting_on))
    out.actions = acts


async def _opportunity_profit(opportunity_id: str | None) -> dict[str, Any] | None:
    """The radar's profit block of the opportunity, for prospects started before the
    deal kept its own snapshot (``radar_estimate``)."""
    if not opportunity_id:
        return None
    try:
        from services.trade_min.repository import get_repository as trade_repo  # type: ignore[import]
        opp = await (await trade_repo()).get(opportunity_id)
        if opp is None:
            return None
        if opp.profit is None:
            # converted before the radar computed profit: compute it now (read-only)
            from services.trade_min import profit as trade_profit  # type: ignore[import]
            await trade_profit.attach(opp)
        return opp.profit.model_dump(mode="json") if opp.profit is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Prospect desk: opportunity %s unavailable: %s", opportunity_id, exc)
        return None


async def _store_radar_estimate(deal: Any) -> dict[str, Any] | None:
    """Prospects started before the deal kept its own snapshot (2026-11-17): take the
    opportunity's profit block once and store it on the deal, marked ``backfilled_at`` —
    so estimate → outcome (trade_min.accuracy) covers them and later reads are stable."""
    radar = await _opportunity_profit(getattr(deal, "opportunity_id", None))
    if not radar:
        return None
    radar = {**radar, "backfilled_at": datetime.now(timezone.utc).isoformat()}
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        await get_repository().set_fields(deal.deal_id, {"radar_estimate": radar})
    except Exception as exc:  # noqa: BLE001 - shown anyway, stored next time
        logger.warning("Prospect desk: radar estimate of %s not stored: %s", getattr(deal, "deal_id", "?"), exc)
    return radar


async def backfill_radar_estimates(deals: list[Any]) -> dict[str, int]:
    """Every deal from an opportunity without a ``radar_estimate`` (any status — settled deals
    are what the accuracy check compares) gets the snapshot once."""
    counts = {"checked": 0, "backfilled": 0, "no_opportunity_data": 0}
    for d in deals:
        if getattr(d, "radar_estimate", None) or not getattr(d, "opportunity_id", None):
            continue
        counts["checked"] += 1
        if await _store_radar_estimate(d):
            counts["backfilled"] += 1
        else:
            counts["no_opportunity_data"] += 1
    return counts


async def desk(deals: list[Any]) -> list[ProspectSummary]:
    """Every open prospect, highest priority first."""
    from shared.orchestration import matchmaking
    rows: list[ProspectSummary] = []
    for d in deals:
        if _status(d) not in _PROSPECT_STATUSES:
            continue
        try:
            board = await matchmaking.build_board(d)
        except Exception as exc:  # noqa: BLE001 - one bad prospect never hides the desk
            logger.warning("Prospect desk: board failed for %s: %s", getattr(d, "deal_id", "?"), exc)
            board = None
        if not getattr(d, "radar_estimate", None):
            radar = await _store_radar_estimate(d)
            if radar:
                d = d.model_copy(update={"radar_estimate": radar}) if hasattr(d, "model_copy") else d
        row = summarise(d, board)
        effort = await _effort(d)
        busy = (effort["emails_sent"] + effort["operator_touches"] >= effort_high()
                and not row.buyers_interested and not row.quotes)
        row = row.model_copy(update={**effort, "busy_not_moving": busy})
        if busy:
            row.actions.insert(0, DeskAction(kind="park", text=(
                f"Much effort, nothing moving ({effort['emails_sent']} e-mails, {effort['operator_touches']} decisions, "
                "no interested buyer, no quote) — park or drop it")))
        from shared.orchestration.prospect_actions import is_parked
        if is_parked(d):
            row = row.model_copy(update={
                "parked": True, "parked_until": getattr(d, "parked_until", None),
                "parked_reason": getattr(d, "parked_reason", None), "stalled": False,
                "actions": [DeskAction(kind="unpark", text="Parked — unpark to work it again")],
            })
        rows.append(row)
    rows.sort(key=lambda r: (not r.parked, r.waiting_on == "us", r.priority), reverse=True)
    return rows
