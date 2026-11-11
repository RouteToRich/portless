"""The supplier commits before PortLess commits to the buyer.

Before this module a buyer's agreement went straight to the Proforma Invoice:
the supplier's price was only something it had *offered* (and possibly
something our own counters had rejected), so PortLess could promise a buyer a
price and a delivery no manufacturer had confirmed. Now:

1. **Firm order request** — when the buyer agrees (``issue_pi_for_deal``), the
   supplier gets a templated order request at the price it itself stated
   (``supplier_last_quote``, else its RFQ quote). The PI waits.
2. **Confirmation** — the supplier's reply on the negotiation thread is read
   deterministically: a plain "we confirm" opens ``CONFIRM_SUPPLIER_ORDER``
   (policy default L0); APPROVE records the commitment and issues the PI. A
   decline moves to the next supplier; anything else (a new price, a question)
   goes to an operator.
3. **Standby supplier** — a decline, or no reply within
   ``SUPPLIER_CONFIRMATION_HOURS`` (default 24, deal watcher), sends the same
   firm request to the next-ranked supplier (Match Engine SOURCE results and
   received quotes, best first) whose own quoted price still clears the margin
   floor against the price the buyer agreed. Only one supplier at a time holds
   an open request, so two suppliers never both believe they have the order.
   When none is left → ``deal.exception`` (``SUPPLIER:no_confirmation``).
4. **PO acknowledgement** — after the PO, the inspection is booked only once the
   supplier acknowledges it (e-mail reply, or an operator recording it), so an
   inspector is never sent to a factory that has not accepted the order.
5. **Goods-ready date** — the inspection is booked for the day the supplier says
   the lot is 100 % produced and packed (``shared/orchestration/qc_schedule.py``).
   The date is read from the PO acknowledgement; without one the supplier is
   asked (``qc_readiness_request``) and its next reply naming a date books the
   inspection. ``POST /shield-link/deals/{id}/qc-readiness`` records a date an
   operator heard by phone.

Switches: ``SUPPLIER_CONFIRMATION_REQUIRED``, ``PO_ACK_REQUIRED`` and
``QC_READINESS_DATE_REQUIRED`` (default on; ``tests/conftest.py`` pins them off
for the older pipeline tests).
Storage: Mongo ``supplier_commitments`` when ``MONGO_URI`` is set, else in memory.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ACTION = "CONFIRM_SUPPLIER_ORDER"
_COLLECTION = "supplier_commitments"


def _on(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off")


def enabled() -> bool:
    return _on("SUPPLIER_CONFIRMATION_REQUIRED")


def po_ack_required() -> bool:
    return _on("PO_ACK_REQUIRED")


def confirm_hours() -> float:
    try:
        return float(os.getenv("SUPPLIER_CONFIRMATION_HOURS", "24"))
    except ValueError:
        return 24.0


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class Attempt(BaseModel):
    supplier_id: str
    company_name: str = ""
    email: str | None = None
    price_usd_per_mt: float
    requested_at: datetime
    outcome: str = "requested"
    """requested | confirmed | declined | no_reply | replaced"""
    outcome_at: datetime | None = None
    note: str | None = None


class Commitment(BaseModel):
    deal_id: str
    agreed_price: float
    quantity_mt: float
    market: str | None = None
    status: str = "requested"
    """requested | confirmed | exhausted"""
    attempts: list[Attempt] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    """Standby suppliers passed over (no address / price above the margin)."""
    confirmed_supplier_id: str | None = None
    confirmed_price: float | None = None
    confirmed_at: datetime | None = None
    po_ack: str | None = None
    """None | awaiting | acknowledged"""
    po_ack_at: datetime | None = None
    po_ack_by: str | None = None
    qc_ready_status: str | None = None
    """None | awaiting (asked for the goods-ready date) | received"""
    qc_ready_date: str | None = None
    """ISO date the supplier said the lot is produced and packed for inspection."""
    qc_ready_by: str | None = None
    qc_ready_at: datetime | None = None
    qc_ready_asked_at: datetime | None = None
    qc_ready_produced_pct: float | None = None
    qc_ready_packed_pct: float | None = None

    @property
    def current(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts else None


_memory: dict[str, Commitment] = {}


def reset_for_tests() -> None:
    _memory.clear()


def _mongo():
    try:
        from shared.config import settings
        if not settings.mongo_uri:
            return None
        import motor.motor_asyncio as motor  # type: ignore[import]
        return motor.AsyncIOMotorClient(settings.mongo_uri)[settings.mongo_db_name][_COLLECTION]
    except Exception as exc:  # noqa: BLE001
        logger.warning("SupplierCommitment: Mongo unavailable (%s) — in memory only", exc)
        return None


async def get(deal_id: str) -> Commitment | None:
    col = _mongo()
    if col is not None:
        try:
            doc = await col.find_one({"deal_id": deal_id})
            if doc:
                doc.pop("_id", None)
                return Commitment(**doc)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("SupplierCommitment: read failed for %s: %s", deal_id, exc)
    return _memory.get(deal_id)


async def _save(c: Commitment) -> None:
    col = _mongo()
    if col is not None:
        try:
            await col.replace_one({"deal_id": c.deal_id}, c.model_dump(mode="python"), upsert=True)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("SupplierCommitment: write failed for %s: %s", c.deal_id, exc)
    _memory[c.deal_id] = c


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Who the supplier is, and who stands by
# ---------------------------------------------------------------------------

async def _session(deal_id: str):
    from agents.negotiation_agent.repository import get_repository  # type: ignore[import]
    return await get_repository().get_session_by_deal(deal_id)


def _supplier_party(deal: Any):
    return next((p for p in (getattr(deal, "parties", None) or [])
                 if (getattr(getattr(p, "role", ""), "value", getattr(p, "role", "")) or "").lower() == "supplier"), None)


def _net_margin_pct(session: Any, buyer_price: float, supplier_price: float) -> float:
    if getattr(session, "cost_sheet", None):
        from shared.orchestration import trade_costs
        return trade_costs.net_margin_pct(session.cost_sheet, buyer_price, supplier_price)
    env = session.cost_envelope.total() if getattr(session, "cost_envelope", None) else 0.0
    costs = float(getattr(session, "logistics_cost_usd", None) or 0.0) + float(env)
    return (buyer_price - supplier_price - costs) / buyer_price * 100.0 if buyer_price else -100.0


async def standby_suppliers(deal: Any) -> list[dict[str, Any]]:
    """Every other supplier with a price on this deal, best first: Match Engine
    SOURCE results by score, then received quotes. One row per supplier."""
    rows: dict[str, dict[str, Any]] = {}
    for m in sorted(getattr(deal, "match_results", None) or [], key=lambda m: -(m.composite_score or 0)):
        if getattr(m, "decision", "") != "SOURCE" or not m.fob_price_usd:
            continue
        rows.setdefault(m.supplier_id, {"supplier_id": m.supplier_id, "company_name": m.company_name,
                                        "price": float(m.fob_price_usd), "email": None,
                                        "score": float(m.composite_score or 0)})
    for o in getattr(deal, "supplier_offers", None) or []:
        if getattr(o, "verification_failed", False) or not o.fob_price_usd_per_mt:
            continue
        row = rows.setdefault(o.supplier_id, {"supplier_id": o.supplier_id, "company_name": o.company_name,
                                              "price": float(o.fob_price_usd_per_mt), "email": None, "score": 0.0})
        row["email"] = row["email"] or o.email
        row["company_name"] = row["company_name"] or o.company_name
    out = list(rows.values())
    for row in out:
        if not row["email"]:
            try:
                from services.deals.service import _supplier_contact  # type: ignore[import]
                info = await _supplier_contact(deal, row["supplier_id"])
                row["email"] = info.get("email")
                row["company_name"] = row["company_name"] or info.get("legal_name", "")
            except Exception as exc:  # noqa: BLE001
                logger.debug("SupplierCommitment: contact lookup for %s failed: %s", row["supplier_id"], exc)
    return out


# ---------------------------------------------------------------------------
# (a) Before the PI
# ---------------------------------------------------------------------------

async def ready_for_pi(deal_id: str, *, agreed_price: float, quantity_mt: float | None,
                       market: str | None) -> bool:
    """True when the PI may go out. Otherwise makes sure the supplier has a firm
    order request in front of it and returns False (the PI follows its confirmation)."""
    if not enabled():
        return True
    rec = await get(deal_id)
    if rec is not None and rec.status == "confirmed":
        return True
    if rec is not None and rec.status == "requested" and rec.current and rec.current.outcome == "requested":
        logger.info("SupplierCommitment: deal %s still waits for %s to confirm", deal_id, rec.current.supplier_id)
        return False
    if rec is not None and rec.status == "exhausted":
        return False

    from services.deals.service import get_deal  # type: ignore[import]
    deal = await get_deal(deal_id)
    session = await _session(deal_id)
    if session is None:
        logger.warning("SupplierCommitment: deal %s has no negotiation session — PI not held", deal_id)
        return True
    party = _supplier_party(deal)
    price = float(session.supplier_last_quote or session.supplier_quote_price or 0)
    if not price:
        logger.warning("SupplierCommitment: deal %s has no supplier price — PI not held", deal_id)
        return True
    rec = Commitment(deal_id=deal_id, agreed_price=float(agreed_price),
                     quantity_mt=float(quantity_mt or session.quantity_mt or 0), market=market)
    await _request(rec, deal, supplier_id=session.seller_id,
                   company=getattr(party, "legal_name", "") or "", email=session.seller_email,
                   price=price, introduced=True)
    return False


async def _request(rec: Commitment, deal: Any, *, supplier_id: str, company: str, email: str | None,
                   price: float, introduced: bool) -> None:
    from shared.orchestration import notifications
    rec.status = "requested"
    rec.attempts.append(Attempt(supplier_id=supplier_id, company_name=company, email=email,
                                price_usd_per_mt=price, requested_at=_now()))
    await _save(rec)
    await notifications.notify_party(
        deal.deal_id, "supplier", "supplier_order_request",
        {"supplier_name": company, "price": price, "quantity_mt": rec.quantity_mt,
         "hs_code": getattr(deal, "hs_code", ""), "hours": confirm_hours(), "introduced": introduced},
        suffix=f"{supplier_id}-{len(rec.attempts)}", deal=deal,
    )
    logger.info("SupplierCommitment: firm order request to %s (%s) at %.2f for deal %s",
                company or supplier_id, email, price, deal.deal_id)


# ---------------------------------------------------------------------------
# Reading the supplier's reply
# ---------------------------------------------------------------------------

_DECLINE = re.compile(
    r"\b(?:cannot|can't|can not|unable|not able|regret|decline|no longer|not possible|sold out|"
    r"not available|unavailable|withdraw)\b", re.IGNORECASE)
_CONFIRM = re.compile(
    r"\b(?:we\s+(?:hereby\s+)?confirm|i\s+confirm|confirmed|we\s+accept|accepted|agreed|go\s+ahead|"
    r"please\s+(?:proceed|issue)|happy\s+to\s+proceed|we\s+can\s+proceed)\b", re.IGNORECASE)
_PO_ACK = re.compile(
    r"\b(?:acknowledg\w*|confirm\w*|received\s+(?:the|your)\s+(?:purchase\s+order|po)|we\s+accept|"
    r"(?:start|begin|commence)\w*\s+production|production\s+(?:will|has|is)\b)", re.IGNORECASE)


def _new_text(body: str) -> str:
    try:
        from agents.negotiation_agent.guards import _new_reply_text  # type: ignore[import]
        return _new_reply_text(body) or body
    except Exception:  # noqa: BLE001
        return body


async def read_order_reply(body: str, requested_price: float) -> tuple[str, float | None]:
    """→ ("confirmed" | "declined" | "unclear", other price named or None)."""
    text = _new_text(body)
    from services.email_processor.service import _extract_price  # type: ignore[import]
    named = await _extract_price(text, ignore_price=requested_price)
    other = named if named is not None and abs(named - requested_price) > 0.01 else None
    if _DECLINE.search(text) and not _CONFIRM.search(text):
        return "declined", other
    if _CONFIRM.search(text) and other is None:
        return "confirmed", None
    return "unclear", other


def _from_current(rec: Commitment, from_address: str | None) -> bool:
    """True when the e-mail comes from the supplier that holds the order request.

    After a switch to a standby supplier the first supplier's address is still a
    known supplier address of the negotiation thread, so the sender check alone
    would let its late "we confirm" stand for the standby's confirmation. No
    address to compare (an operator relay, an older caller) → True."""
    cur = rec.current
    if not from_address or cur is None or not cur.email:
        return True
    from agents.negotiation_agent.email_safety import same_owner  # type: ignore[import]
    return same_owner(from_address, cur.email)


async def sent_by_replaced_supplier(deal_id: str, from_address: str | None) -> bool:
    """The sender is a supplier this deal asked earlier and has since moved on from."""
    rec = await get(deal_id)
    if rec is None or not from_address or _from_current(rec, from_address):
        return False
    from agents.negotiation_agent.email_safety import same_owner  # type: ignore[import]
    return any(a.email and same_owner(from_address, a.email) for a in rec.attempts[:-1])


async def handle_supplier_reply(deal_id: str, deal_status: str, body: str, inbound_id: str,
                                from_address: str | None = None) -> str | None:
    """The supplier wrote after the agreement. Returns a short outcome for the
    caller, or None when this module has nothing to say (the caller then routes
    the e-mail to an operator)."""
    rec = await get(deal_id)
    if rec is None:
        return None
    if not _from_current(rec, from_address):
        logger.warning("SupplierCommitment: deal %s — reply from %s, but %s holds the order request; "
                       "left for an operator", deal_id, from_address, rec.current.supplier_id if rec.current else None)
        return None
    if deal_status == "NEGOTIATION_AGREED" and rec.status == "requested" and rec.current:
        cur = rec.current
        verdict, other = await read_order_reply(body, cur.price_usd_per_mt)
        if verdict == "confirmed":
            await _gate_confirmation(rec, inbound_id, body)
            return f"Supplier confirmed the order at USD {cur.price_usd_per_mt:,.2f}/MT; approval requested."
        if verdict == "declined":
            await mark_and_advance(deal_id, "declined", note=body[:300])
            return "Supplier declined the order; the next supplier is asked."
        return None  # a new price or a question — a person reads it
    if deal_status == "PO_ISSUED":
        from shared.orchestration import qc_schedule
        text = _new_text(body)
        ready = qc_schedule.parse_ready_date(text)
        if rec.po_ack == "awaiting":
            if _PO_ACK.search(text) and not _DECLINE.search(text):
                tpo = await acknowledge_po(deal_id, by=f"supplier e-mail {inbound_id}", ready_date=ready)
                if tpo is not None:
                    when = getattr(tpo, "inspection_date", None)
                    return "Supplier acknowledged the purchase order; inspection booked" + (f" for {when}." if when else ".")
                return "Supplier acknowledged the purchase order; asked for the date the goods are ready for inspection."
            return None
        if rec.qc_ready_status == "awaiting" and ready is not None and not _DECLINE.search(text):
            result = await record_qc_readiness(deal_id, ready, by=f"supplier e-mail {inbound_id}")
            return f"Supplier named {ready.isoformat()} as the goods-ready date; inspection booked for {result.get('inspection_date')}."
        return None
    return None


async def _gate_confirmation(rec: Commitment, inbound_id: str, body: str) -> None:
    from shared.orchestration.continuations import _create_gate
    cur = rec.current
    assert cur is not None
    await _create_gate(
        rec.deal_id, ACTION,
        summary=(f"{cur.company_name or cur.supplier_id} confirms {rec.quantity_mt:g} MT at "
                 f"USD {cur.price_usd_per_mt:,.2f}/MT FOB — approve to send the buyer the Proforma Invoice "
                 f"at USD {rec.agreed_price:,.2f}/MT. Reply: {_new_text(body)[:160]}"),
        financial_impact_usd=round(cur.price_usd_per_mt * rec.quantity_mt, 2),
        callback_payload={"deal_id": rec.deal_id, "supplier_id": cur.supplier_id,
                          "price": cur.price_usd_per_mt, "attempt": len(rec.attempts), "inbound_id": inbound_id},
        idempotency_key=f"supplier-confirm-{rec.deal_id}-{len(rec.attempts)}",
        risk_flags=["supplier_confirmation"],
    )


async def record_confirmed(deal_id: str, *, supplier_id: str, price: float) -> Any:
    """APPROVE of CONFIRM_SUPPLIER_ORDER: the price becomes the supplier's
    committed price (the PO is written at it) and the PI goes out."""
    rec = await get(deal_id)
    if rec is None:
        raise RuntimeError(f"no supplier order request on deal {deal_id}")
    cur = rec.current
    if cur is None or cur.supplier_id != supplier_id:
        raise RuntimeError(f"deal {deal_id}: {supplier_id} does not hold the open order request")
    cur.outcome, cur.outcome_at = "confirmed", _now()
    rec.status, rec.confirmed_supplier_id, rec.confirmed_price, rec.confirmed_at = (
        "confirmed", supplier_id, float(price), _now())
    await _save(rec)
    session = await _session(deal_id)
    if session is not None:
        from agents.negotiation_agent.repository import get_repository  # type: ignore[import]
        session = session.model_copy(update={"supplier_last_quote": float(price), "current_ask": float(price)})
        await get_repository().save_session(session)
    from shared.orchestration.continuations import pi_after_compliance
    return await pi_after_compliance(deal_id, agreed_price=rec.agreed_price, quantity_mt=rec.quantity_mt,
                                     market=rec.market)


# ---------------------------------------------------------------------------
# (c) Standby supplier
# ---------------------------------------------------------------------------

async def mark_and_advance(deal_id: str, outcome: str, note: str | None = None) -> str:
    """The current supplier declined / stayed silent → ask the next one.
    Returns "requested" (a standby was asked) or "exhausted"."""
    rec = await get(deal_id)
    if rec is None or rec.status != "requested" or rec.current is None:
        return rec.status if rec else "none"
    rec.current.outcome, rec.current.outcome_at, rec.current.note = outcome, _now(), note
    await _save(rec)

    from services.deals.service import get_deal, set_supplier_party  # type: ignore[import]
    deal = await get_deal(deal_id)
    session = await _session(deal_id)
    tried = {a.supplier_id for a in rec.attempts}
    floor = float(session.policy.margin_floor_pct) if session is not None else 8.0
    for row in await standby_suppliers(deal):
        if row["supplier_id"] in tried or any(s.get("supplier_id") == row["supplier_id"] for s in rec.skipped):
            continue
        margin = _net_margin_pct(session, rec.agreed_price, row["price"]) if session is not None else 0.0
        if not row["email"]:
            rec.skipped.append({**row, "reason": "no e-mail address"})
            continue
        if margin < floor:
            rec.skipped.append({**row, "reason": f"margin {margin:.1f}% below the {floor:g}% floor"})
            continue
        updated = await set_supplier_party(deal_id, row["supplier_id"], company_name=row["company_name"],
                                           email=row["email"])
        party = _supplier_party(updated)
        if party is None or party.party_id != row["supplier_id"]:
            rec.skipped.append({**row, "reason": "supplier party is operator-set — not replaced automatically"})
            break
        if session is not None:
            from agents.negotiation_agent.repository import get_repository  # type: ignore[import]
            session = session.model_copy(update={
                "seller_id": row["supplier_id"], "seller_email": row["email"],
                "supplier_quote_price": row["price"], "supplier_last_quote": row["price"],
                "current_ask": row["price"], "last_offer_to_supplier": None,
            })
            await get_repository().save_session(session)
        await _request(rec, updated, supplier_id=row["supplier_id"], company=row["company_name"],
                       email=row["email"], price=row["price"], introduced=False)
        logger.info("SupplierCommitment: deal %s moved to standby supplier %s (%.1f%% margin)",
                    deal_id, row["supplier_id"], margin)
        return "requested"

    rec.status = "exhausted"
    await _save(rec)
    from shared.orchestration.continuations import _publish_exception
    await _publish_exception(
        deal_id, "SUPPLIER:no_confirmation",
        "no supplier confirmed the order — Proforma Invoice withheld",
        {"category": "NEGOTIATION_FAILED", "action_required": "find_supplier_or_cancel",
         "attempts": [a.model_dump(mode="json") for a in rec.attempts], "skipped": rec.skipped},
    )
    return "exhausted"


async def sweep(now: datetime | None = None) -> int:
    """Deal watcher: an order request unanswered past the deadline → next supplier."""
    if not enabled():
        return 0
    now = now or _now()
    limit = timedelta(hours=confirm_hours())
    moved = 0
    rows: list[Commitment] = []
    col = _mongo()
    if col is not None:
        try:
            async for doc in col.find({"status": "requested"}):
                doc.pop("_id", None)
                rows.append(Commitment(**doc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("SupplierCommitment: sweep read failed: %s", exc)
    rows += [c for c in _memory.values() if c.status == "requested" and c.deal_id not in {r.deal_id for r in rows}]
    for rec in rows:
        cur = rec.current
        if cur and cur.outcome == "requested" and now - (_aware(cur.requested_at) or now) >= limit:
            await mark_and_advance(rec.deal_id, "no_reply", note=f"no reply within {confirm_hours():g} h")
            moved += 1
    return moved


# ---------------------------------------------------------------------------
# (b) PO acknowledgement before the inspection
# ---------------------------------------------------------------------------

async def await_po_ack(deal_id: str, quantity_mt: float) -> None:
    rec = await get(deal_id) or Commitment(deal_id=deal_id, agreed_price=0.0, quantity_mt=quantity_mt,
                                           status="confirmed")
    if rec.po_ack != "acknowledged":
        rec.po_ack = "awaiting"
        await _save(rec)


async def acknowledge_po(deal_id: str, *, by: str, ready_date: Any = None) -> Any:
    """The supplier accepted the PO → book the inspection for its goods-ready
    date (asked for when the acknowledgement does not name one)."""
    rec = await get(deal_id)
    if rec is not None and rec.po_ack == "acknowledged":
        return None
    if rec is None:
        rec = Commitment(deal_id=deal_id, agreed_price=0.0, quantity_mt=0.0, status="confirmed")
    rec.po_ack, rec.po_ack_at, rec.po_ack_by = "acknowledged", _now(), by
    if ready_date is not None:
        _set_ready(rec, ready_date, by)
    await _save(rec)
    return await book_qc_when_ready(deal_id)


# ---------------------------------------------------------------------------
# (c) Goods-ready date for the inspection
# ---------------------------------------------------------------------------

def _set_ready(rec: Commitment, ready_date: Any, by: str, produced_pct: float | None = None,
               packed_pct: float | None = None) -> None:
    from shared.orchestration import qc_schedule
    d = qc_schedule.to_date(ready_date)
    rec.qc_ready_date = d.isoformat() if d else None
    rec.qc_ready_status = "received"
    rec.qc_ready_by, rec.qc_ready_at = by, _now()
    rec.qc_ready_produced_pct, rec.qc_ready_packed_pct = produced_pct, packed_pct


async def request_qc_readiness(deal_id: str, quantity_mt: float = 0.0) -> bool:
    """Ask the supplier when the lot will be ready for inspection (once)."""
    rec = await get(deal_id) or Commitment(deal_id=deal_id, agreed_price=0.0, quantity_mt=quantity_mt, status="confirmed")
    if rec.qc_ready_status in ("awaiting", "received"):
        return False
    rec.qc_ready_status, rec.qc_ready_asked_at = "awaiting", _now()
    await _save(rec)
    try:
        from services.deals.service import get_deal  # type: ignore[import]
        from shared.orchestration import notifications, qc_schedule
        deal = await get_deal(deal_id)
        await notifications.notify_party(deal_id, "supplier", "qc_readiness_request", {
            "supplier_name": notifications._party_name(deal, "supplier"),
            "lead_days": qc_schedule.lead_business_days(),
            "packed_pct": qc_schedule.min_packed_pct(),
            "earliest": qc_schedule.earliest_inspection_date().isoformat(),
        }, deal=deal)
    except Exception as exc:  # noqa: BLE001 — the watcher's PO reminder asks again
        logger.error("SupplierCommitment: readiness request for %s not sent: %s", deal_id, exc)
    return True


async def book_qc_when_ready(deal_id: str) -> Any:
    """Book the inspection now if the goods-ready date is known (or not
    required); otherwise ask the supplier for it. Returns the QC order or None."""
    from shared.orchestration import qc_schedule
    rec = await get(deal_id)
    if qc_schedule.readiness_required() and not (rec and rec.qc_ready_date):
        await request_qc_readiness(deal_id, rec.quantity_mt if rec else 0.0)
        logger.info("SupplierCommitment: deal %s — inspection waits for the supplier's goods-ready date", deal_id)
        return None
    from shared.orchestration.continuations import book_qc_for_deal
    return await book_qc_for_deal(deal_id)


async def record_qc_readiness(deal_id: str, ready_date: Any, *, by: str, produced_pct: float | None = None,
                              packed_pct: float | None = None, note: str | None = None) -> dict[str, Any]:
    """The supplier's goods-ready date: stored, and the inspection booked for it
    while the deal is at PO_ISSUED (a date given before the PO acknowledgement
    counts as the acknowledgement — a supplier naming a ready date has accepted
    the order)."""
    from services.deals.service import get_deal  # type: ignore[import]
    from shared.orchestration import qc_schedule
    try:
        deal = await get_deal(deal_id)
    except Exception as exc:
        from services.shield_link.service import DealNotFoundError  # type: ignore[import]
        raise DealNotFoundError(deal_id) from exc
    rec = await get(deal_id) or Commitment(deal_id=deal_id, agreed_price=0.0, quantity_mt=0.0, status="confirmed")
    _set_ready(rec, ready_date, by, produced_pct, packed_pct)
    status = str(getattr(deal.status, "value", deal.status))
    if status == "PO_ISSUED" and rec.po_ack != "acknowledged":
        rec.po_ack, rec.po_ack_at, rec.po_ack_by = "acknowledged", _now(), f"{by} (goods-ready date given)"
    await _save(rec)
    out: dict[str, Any] = {"deal_id": deal_id, "ready_date": rec.qc_ready_date, "status": "recorded",
                           "inspection_date": qc_schedule.inspection_date_for(rec.qc_ready_date).isoformat(), "note": note}
    if status == "PO_ISSUED":
        tpo = await book_qc_when_ready(deal_id)
        if tpo is not None:
            out.update(status="booked", tpo_id=tpo.tpo_id, provider=tpo.provider, inspection_date=tpo.inspection_date)
    return out
