"""Running a deal with more than one supplier (todo 13.4).

Everything after the match used to assume one supplier party. A composed deal
(composition.py) has **supplier legs** on ``DealRecord.supplier_legs``: supplier, quantity,
price, basis, port of loading, ready date, facility address, PO, QC orders, status. Money, QC
and documents read the legs; a one-supplier deal keeps behaving exactly as before:

- **Data model / migration** — ``legs_for(deal)`` returns the stored legs, or one leg derived
  from the supplier party (an old deal is never touched until ``migrate`` stores that leg;
  ``POST /deals/legs/migrate`` runs it over every deal).
- **PO per leg** — ``issue_pos`` writes one PO per live leg at that leg's price and quantity,
  naming only that supplier; the margin guard runs on each leg (DealMaker) and on the total
  here (``total_margin_pct``). The first leg's PO moves the deal to PO_ISSUED.
- **PO acknowledgement, goods-ready date, facility address per leg** — recorded on the leg
  (``acknowledge_po``, ``record_ready_date``, from the ``[LEG:…]`` thread or staff); the deal
  watcher reminds each leg that has not acknowledged (``sweep``).
- **QC per facility** — ``book_qc`` books one inspection per leg at its own site; the deal's QC
  passes only when every live leg passed (``on_leg_qc_result``); a failed leg holds the deal
  (no DISPUTE) until it is re-inspected, replaced, or its quantity cancelled with the buyer's
  consent (``cancel_leg``).
- **Money** — the buyer still has one escrow. What each supplier is owed is its own PO
  (``owed_by_leg``); released funds are split pro rata by leg value (``payout_plan``); a payout
  never exceeds that leg's due; the deal is PAID only when every leg is paid in full
  (``every_leg_paid``). Four-eyes and the compliance money block apply to each payout gate.
- **Documents** — the buyer side is unchanged (one PI / Sales Contract / CI / PL at the
  combined quantity, no supplier named); the CI / PL list every leg's containers and seals
  (``loading_merged``); each PO names only its own leg.
- **Compliance** — related-party checks between the legs and with the buyer, a failed leg
  supplier blocks (``compliance_findings``); the shipment check applies the quantity tolerance
  to the total loaded against the PI and to each leg against its PO (``shipment_findings``).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

LIVE = ("proposed", "requested", "confirmed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _role(p: Any) -> str:
    r = getattr(p, "role", "")
    return str(getattr(r, "value", r) or "").lower()


# ---------------------------------------------------------------------------
# Reading legs
# ---------------------------------------------------------------------------

def derived_leg(deal: Any) -> Any | None:
    """One leg from the supplier party of an old (one-supplier) deal."""
    from services.deals.schemas import SupplierLeg  # type: ignore[import]
    party = next((p for p in getattr(deal, "parties", None) or [] if _role(p) == "supplier"), None)
    supplier_id = getattr(deal, "winning_supplier_id", None) or (getattr(party, "party_id", None) if party else None)
    if not supplier_id:
        return None
    match = next((m for m in getattr(deal, "match_results", None) or [] if m.supplier_id == supplier_id), None)
    return SupplierLeg(
        leg_id=f"LEG-{str(deal.deal_id).replace('DEAL-', '')[:8].upper()}",
        supplier_id=supplier_id, company_name=getattr(party, "legal_name", "") if party else (match.company_name if match else ""),
        email=getattr(party, "email", None) if party else None,
        quantity_mt=float(getattr(deal, "quantity_mt", None) or 0.0),
        price_usd_per_mt=float(getattr(match, "fob_price_usd", None) or 0.0),
        facility_address=getattr(party, "address", None) if party else None, status="confirmed",
    )


def legs_for(deal: Any) -> list[Any]:
    stored = list(getattr(deal, "supplier_legs", None) or [])
    if stored:
        return stored
    one = derived_leg(deal)
    return [one] if one is not None else []


def active_legs(deal: Any) -> list[Any]:
    return [x for x in getattr(deal, "supplier_legs", None) or [] if x.status in LIVE]


def is_multi(deal: Any) -> bool:
    return len(active_legs(deal)) > 1


def leg(deal: Any, leg_id: str) -> Any | None:
    return next((x for x in getattr(deal, "supplier_legs", None) or [] if x.leg_id == leg_id), None)


def leg_of_supplier(deal: Any, supplier_id: str) -> Any | None:
    return next((x for x in active_legs(deal) if x.supplier_id == supplier_id), None)


# ---------------------------------------------------------------------------
# Writing legs
# ---------------------------------------------------------------------------

async def _mutate(deal_id: str, fn) -> Any:
    from services.deals.repository import get_repository  # type: ignore[import]
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        raise ValueError(f"deal {deal_id} not found")
    legs = fn(list(deal.supplier_legs))
    updated = deal.model_copy(update={"supplier_legs": legs, "updated_at": _now()})
    await repo.save(updated)
    return updated


async def set_leg(deal_id: str, leg_id: str, fields: dict[str, Any]) -> Any:
    def _f(legs):
        out = []
        for x in legs:
            out.append(x.model_copy(update=fields) if x.leg_id == leg_id else x)
        return out
    return await _mutate(deal_id, _f)


async def add_legs(deal_id: str, new_legs: list[Any]) -> Any:
    return await _mutate(deal_id, lambda legs: legs + list(new_legs))


async def add_qc_order(deal_id: str, leg_id: str, tpo_id: str) -> None:
    def _f(legs):
        return [x.model_copy(update={"qc_order_ids": list(dict.fromkeys([*x.qc_order_ids, tpo_id]))})
                if x.leg_id == leg_id else x for x in legs]
    try:
        await _mutate(deal_id, _f)
    except Exception as exc:  # noqa: BLE001 — the booking itself stands
        logger.warning("SupplierLegs: QC order %s not linked to %s/%s: %s", tpo_id, deal_id, leg_id, exc)


async def migrate(deal: Any) -> list[Any]:
    """Store the derived leg of an old one-supplier deal (idempotent)."""
    if getattr(deal, "supplier_legs", None):
        return list(deal.supplier_legs)
    one = derived_leg(deal)
    if one is None:
        return []
    await _mutate(deal.deal_id, lambda legs: legs or [one])
    return [one]


async def migrate_all(limit: int = 100_000) -> dict[str, int]:
    from services.deals.repository import get_repository  # type: ignore[import]
    counts = {"migrated": 0, "skipped": 0}
    for deal in await get_repository().list_all(limit=limit) if hasattr(get_repository(), "list_all") else \
            await get_repository().list_active(limit=limit):
        if getattr(deal, "supplier_legs", None) or derived_leg(deal) is None:
            counts["skipped"] += 1
            continue
        await migrate(deal)
        counts["migrated"] += 1
    return counts


# ---------------------------------------------------------------------------
# PO per leg
# ---------------------------------------------------------------------------

def po_floor_pct() -> float:
    try:
        from services.dealmaker.config import settings  # type: ignore[import]
        return float(settings.po_margin_floor_pct)
    except Exception:  # noqa: BLE001
        return 8.0


def total_margin_pct(legs: list[Any], buyer_price: float) -> float:
    qty = sum(x.quantity_mt for x in legs)
    if not qty or not buyer_price:
        return -100.0
    cost = sum(x.price_usd_per_mt * x.quantity_mt for x in legs)
    return round((buyer_price * qty - cost) / (buyer_price * qty) * 100.0, 2)


async def issue_pos(deal_id: str, base: dict[str, Any]) -> dict[str, Any]:
    """ISSUE_PO APPROVE on a multi-supplier deal: one PO per live leg (the base payload's
    delivery deadline / payment terms; each leg's own price, quantity, supplier, port)."""
    from services.dealmaker.repository import get_repository as dm_repo  # type: ignore[import]
    from services.dealmaker.schemas import IssuePoRequest  # type: ignore[import]
    from services.dealmaker.service import issue_po  # type: ignore[import]
    from services.deals.service import get_deal  # type: ignore[import]
    deal = await get_deal(deal_id)
    legs = active_legs(deal)
    pi = await dm_repo().get_pi_by_deal(deal_id)
    buyer_price = (pi.agreed_price if pi else None) or getattr(deal, "buyer_target_price_usd", None) or 0.0
    total = total_margin_pct(legs, float(buyer_price))
    if total < po_floor_pct():
        raise ValueError(f"total margin of the legs {total:.2f} % is below the {po_floor_pct():g} % PO floor")
    issued = []
    for x in sorted(legs, key=lambda x: -x.quantity_mt):  # the largest leg moves the deal
        req = IssuePoRequest(**{
            **{k: v for k, v in base.items() if k in IssuePoRequest.model_fields},
            "deal_id": deal_id, "agreed_price": x.price_usd_per_mt, "quantity_mt": x.quantity_mt,
            "incoterm": x.incoterm or "FOB", "port_of_loading": x.port_of_loading or base.get("port_of_loading") or "",
            "supplier_name": x.company_name or None, "supplier_address": x.facility_address or None,
            "leg_id": x.leg_id, "idempotency_key": f"auto-po-{deal_id}-{x.leg_id}",
        })
        res = await issue_po(req)
        issued.append({"leg_id": x.leg_id, "po_id": res.po_id})
        if x.po_ack is None:
            await set_leg(deal_id, x.leg_id, {"po_ack": "awaiting"})
    return {"pos": issued, "total_margin_pct": total}


async def owed_by_leg(deal_id: str, deal: Any | None = None) -> dict[str, float]:
    """leg_id → what that supplier is owed (its PO value, else quantity × price)."""
    from services.dealmaker.repository import get_repository as dm_repo  # type: ignore[import]
    if deal is None:
        from services.deals.service import get_deal  # type: ignore[import]
        deal = await get_deal(deal_id)
    pos = {p.leg_id: p for p in await dm_repo().list_pos_by_deal(deal_id) if p.leg_id}
    out = {}
    for x in active_legs(deal):
        po = pos.get(x.leg_id)
        out[x.leg_id] = round(float(po.total_value) if po else x.price_usd_per_mt * x.quantity_mt, 2)
    return out


async def notify_pos(deal_id: str) -> int:
    """Each leg's supplier gets its own PO on its ``[LEG:…]`` thread — its quantity and price
    only, never another leg or the buyer."""
    from services.dealmaker.repository import get_repository as dm_repo  # type: ignore[import]
    from services.deals.service import get_deal  # type: ignore[import]
    from shared import company_profile as cp
    from shared.orchestration.composition import leg_token
    from shared.orchestration.outreach import _product_label
    from shared.orchestration.unsubscribe import supplier_opt_out_line
    deal = await get_deal(deal_id)
    pos = {p.leg_id: p for p in await dm_repo().list_pos_by_deal(deal_id) if p.leg_id}
    sent = 0
    for x in active_legs(deal):
        po = pos.get(x.leg_id)
        if po is None or not x.email:
            continue
        body = ((f"Dear {x.company_name} team," if x.company_name else "Dear Sir / Madam,")
                + f"\n\nPlease find our purchase order {po.po_id}: {po.quantity_mt:g} MT {_product_label(deal)} at "
                f"USD {po.agreed_price:,.2f} per MT {po.incoterm} {po.port_of_loading}, total USD {po.total_value:,.2f}.\n\n"
                "Please acknowledge it by replying to this e-mail, with the date the goods are fully produced and "
                "packed, and the full address of the facility where the inspector will come.\n\n"
                + supplier_opt_out_line(x.email) + cp.signature())
        from agents.negotiation_agent.email_safety import prospect_leak_findings  # type: ignore[import]
        if prospect_leak_findings(body, x.email, "supplier", deal):
            logger.error("SupplierLegs: PO notice for leg %s refused by the privacy check", x.leg_id)
            continue
        from services.email_gateway.schemas import SendEmailRequest  # type: ignore[import]
        from services.email_gateway.sender import send_email  # type: ignore[import]
        subject = f"{leg_token(deal_id, x.leg_id)} Purchase order {po.po_id}"
        result = await send_email(SendEmailRequest(
            to_address=x.email, subject=subject, body=body, negotiation_id="", round_no=0,
            email_id=f"LEGPO{deal_id.replace('-', '')[-6:]}{x.leg_id.replace('-', '')[-8:]}",
        ))
        outcome = str(getattr(getattr(result, "outcome", None), "value", getattr(result, "outcome", "sent")))
        from agents.negotiation_agent.email_history import record_deal_email  # type: ignore[import]
        await record_deal_email(deal_id=deal_id, direction="outbound", party="supplier", to_address=x.email,
                                from_address="PortLess", subject=subject, body=body, kind="leg_po",
                                delivered=outcome != "logged")
        sent += 1
    return sent


# ---------------------------------------------------------------------------
# PO acknowledgement / ready date / reminders per leg
# ---------------------------------------------------------------------------

async def acknowledge_po(deal_id: str, leg_id: str, *, by: str, ready_date: str | None = None) -> Any:
    fields: dict[str, Any] = {"po_ack": "acknowledged", "po_ack_at": _now()}
    if ready_date:
        fields["ready_date"] = str(ready_date)
    deal = await set_leg(deal_id, leg_id, fields)
    logger.info("SupplierLegs: leg %s of %s acknowledged the PO (%s)", leg_id, deal_id, by)
    await book_qc_when_ready(deal_id)
    return deal


async def record_ready_date(deal_id: str, leg_id: str, ready_date: str, *, by: str) -> Any:
    deal = await set_leg(deal_id, leg_id, {"ready_date": str(ready_date)})
    await book_qc_when_ready(deal_id)
    return deal


def reminder_hours() -> float:
    try:
        return float(os.getenv("DEAL_PO_ACK_REMINDER_AFTER_HOURS", "24"))
    except ValueError:
        return 24.0


def reminder_max() -> int:
    try:
        return int(os.getenv("DEAL_REMINDER_MAX", "3"))
    except ValueError:
        return 3


async def _remind(deal: Any, x: Any) -> bool:
    if not x.email:
        return False
    from shared import company_profile as cp
    from shared.orchestration.composition import leg_token
    from shared.orchestration.outreach import _product_label
    from shared.orchestration.unsubscribe import supplier_opt_out_line
    missing = []
    if x.po_ack != "acknowledged":
        missing.append("acknowledge our purchase order")
    if not x.ready_date:
        missing.append("tell us the date the goods are fully produced and packed")
    if not x.facility_address:
        missing.append("send the full address of the facility for the inspection")
    if not missing:
        return False
    body = ((f"Dear {x.company_name} team," if x.company_name else "Dear Sir / Madam,")
            + f"\n\nAbout our order of {x.quantity_mt:g} MT {_product_label(deal)}: could you please "
            + "; ".join(missing) + "?\n\n" + supplier_opt_out_line(x.email) + cp.signature())
    from agents.negotiation_agent.email_safety import prospect_leak_findings  # type: ignore[import]
    if prospect_leak_findings(body, x.email, "supplier", deal):
        return False
    from services.email_gateway.schemas import SendEmailRequest  # type: ignore[import]
    from services.email_gateway.sender import send_email  # type: ignore[import]
    subject = f"Re: {leg_token(deal.deal_id, x.leg_id)} Purchase order — {_product_label(deal)}"
    result = await send_email(SendEmailRequest(
        to_address=x.email, subject=subject, body=body, negotiation_id="", round_no=0,
        email_id=f"LEGREM{deal.deal_id.replace('-', '')[-6:]}{x.leg_id.replace('-', '')[-8:]}{x.reminders + 1}",
    ))
    outcome = str(getattr(getattr(result, "outcome", None), "value", getattr(result, "outcome", "sent")))
    from agents.negotiation_agent.email_history import record_deal_email  # type: ignore[import]
    await record_deal_email(deal_id=deal.deal_id, direction="outbound", party="supplier", to_address=x.email,
                            from_address="PortLess", subject=subject, body=body, kind="leg_reminder",
                            delivered=outcome != "logged")
    return True


async def sweep(now: datetime | None = None) -> dict[str, int]:
    """Multi-supplier deals after the PO: remind each leg that has not acknowledged / given its
    ready date / facility address (every ``DEAL_PO_ACK_REMINDER_AFTER_HOURS``, at most
    ``DEAL_REMINDER_MAX``)."""
    counts = {"reminded": 0}
    now = now or _now()
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deals = await get_repository().list_active(limit=1000)
    except Exception as exc:  # noqa: BLE001
        logger.error("SupplierLegs: could not list deals: %s", exc)
        return counts
    gap = timedelta(hours=reminder_hours())
    for deal in deals:
        if str(getattr(deal.status, "value", deal.status)) not in ("PO_ISSUED", "SUPPLIER_DISPATCH_READY") or not is_multi(deal):
            continue
        for x in active_legs(deal):
            if not x.po_id or x.reminders >= reminder_max():
                continue
            since = _aware(x.last_reminder_at) or _aware(x.requested_at) or _aware(deal.updated_at)
            if since and now - since < gap:
                continue
            try:
                if await _remind(deal, x):
                    await set_leg(deal.deal_id, x.leg_id, {"reminders": x.reminders + 1, "last_reminder_at": now})
                    counts["reminded"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("SupplierLegs: reminder %s/%s failed: %s", deal.deal_id, x.leg_id, exc)
    return counts


# ---------------------------------------------------------------------------
# QC per facility
# ---------------------------------------------------------------------------

def site_facts(deal: Any, leg_id: str) -> dict[str, Any] | None:
    """The inspection site of one leg (the inspector goes to that supplier's facility)."""
    import re
    x = leg(deal, leg_id)
    if x is None:
        return None
    address = x.facility_address or ""
    parts = [s.strip() for s in address.split(",") if s.strip()]
    pin = next(iter(re.findall(r"\b\d{6}\b", address)), None)
    return {"name": x.company_name, "contact_name": x.company_name, "email": x.email, "phone": None,
            "address": address or None, "city": parts[-2] if len(parts) >= 2 else None, "postcode": pin,
            "country_code": "IN"}


def _ack_required() -> bool:
    from shared.orchestration import supplier_commitment
    return supplier_commitment.po_ack_required()


async def book_qc(deal_id: str) -> list[str]:
    """Book one PSI per live leg that is ready (PO acknowledged when that is required)."""
    from services.deals.service import get_deal  # type: ignore[import]
    from services.shield_link.schemas import QcBookingRequest  # type: ignore[import]
    from services.shield_link.service import book_qc as sl_book  # type: ignore[import]
    deal = await get_deal(deal_id)
    booked = []
    for x in sorted(active_legs(deal), key=lambda x: -x.quantity_mt):
        if x.qc_order_ids:
            continue
        if _ack_required() and x.po_ack != "acknowledged":
            continue
        tpo = await sl_book(QcBookingRequest(
            deal_id=deal_id, quantity_mt=x.quantity_mt, hs_code=deal.hs_code, goods_ready_date=x.ready_date,
            location=x.facility_address, leg_id=x.leg_id, idempotency_key=f"auto-qc-{deal_id}-{x.leg_id}",
        ))
        booked.append(tpo.tpo_id)
    return booked


async def book_qc_when_ready(deal_id: str) -> list[str]:
    try:
        from services.deals.service import get_deal  # type: ignore[import]
        deal = await get_deal(deal_id)
        if not is_multi(deal) or str(getattr(deal.status, "value", deal.status)) not in ("PO_ISSUED", "SUPPLIER_DISPATCH_READY"):
            return []
        return await book_qc(deal_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("SupplierLegs: QC booking for %s failed: %s", deal_id, exc)
        from shared.bus import get_publisher
        from shared.domain.events import DomainEvent, EventTopic
        await get_publisher().publish(DomainEvent(
            topic=EventTopic.DEAL_EXCEPTION, deal_id=deal_id,
            payload={"source": "supplier_legs", "stage": "PO_ISSUED→QC_BOOKED:leg", "error": str(exc),
                     "action_required": "manual_retry_or_intervention"}))
        return []


def loading_merged(deal: Any) -> dict[str, Any]:
    """Every leg's containers, seals and loaded quantity in one loading record (CI / PL)."""
    containers: list[str] = []
    seals: list[str] = []
    loaded = 0.0
    known = True
    for x in active_legs(deal):
        containers += [c for c in x.containers if c not in containers]
        seals += [s for s in x.seals if s not in seals]
        if x.loaded_mt is None:
            known = False
        else:
            loaded += float(x.loaded_mt)
    return {"container_numbers": containers, "seal_numbers": seals,
            "loaded_quantity_mt": round(loaded, 3) if known else None,
            "legs": [{"leg_id": x.leg_id, "containers": x.containers, "seals": x.seals, "loaded_mt": x.loaded_mt}
                     for x in active_legs(deal)]}


async def on_leg_qc_result(deal_id: str, leg_id: str, result: str, *, loading: dict[str, Any] | None = None,
                           notes: str | None = None, tpo_id: str | None = None) -> dict[str, Any]:
    """One leg's PSI result. → ``{"proceed": True, "loading": merged}`` once every live leg
    passed (the deal moves to QC_PASSED); otherwise the deal waits. A FAIL marks the leg and
    raises ``QC_FAILED:leg`` for a person (re-inspect, replace, or cancel that quantity)."""
    loading = loading or {}
    fields: dict[str, Any] = {"qc_result": result}
    if loading.get("container_numbers"):
        fields["containers"] = list(loading["container_numbers"])
    if loading.get("seal_numbers"):
        fields["seals"] = list(loading["seal_numbers"])
    if loading.get("loaded_quantity_mt") is not None:
        fields["loaded_mt"] = float(loading["loaded_quantity_mt"])
    deal = await set_leg(deal_id, leg_id, fields)
    x = leg(deal, leg_id)
    if result == "FAIL":
        from shared.bus import get_publisher
        from shared.domain.events import DomainEvent, EventTopic
        await get_publisher().publish(DomainEvent(
            topic=EventTopic.DEAL_EXCEPTION, deal_id=deal_id,
            payload={"source": "supplier_legs", "stage": "QC_FAILED:leg", "category": "QC_FAILED",
                     "leg_id": leg_id, "tpo_id": tpo_id,
                     "error": f"leg {leg_id} ({getattr(x, 'company_name', '')}, {getattr(x, 'quantity_mt', 0):g} MT) failed "
                              f"its inspection: {notes or ''}".strip(),
                     "action_required": "reinspect_replace_or_cancel_the_leg_with_buyer_consent"}))
        return {"proceed": False, "waiting": "leg failed", "summary": qc_summary(deal)}
    if all(y.qc_result == "PASS" for y in active_legs(deal)):
        merged = loading_merged(deal)
        merged.update({k: v for k, v in loading.items() if k not in merged})
        return {"proceed": True, "loading": merged, "summary": qc_summary(deal)}
    return {"proceed": False, "waiting": "other legs", "summary": qc_summary(deal)}


def qc_summary(deal: Any) -> str | None:
    legs = active_legs(deal)
    if len(legs) < 2:
        return None
    passed = sum(1 for x in legs if x.qc_result == "PASS")
    failed = sum(1 for x in legs if x.qc_result == "FAIL")
    return f"QC {passed} of {len(legs)} passed" + (f", {failed} failed" if failed else "")


async def cancel_leg(deal_id: str, leg_id: str, *, by: str, reason: str, buyer_consent: bool) -> dict[str, Any]:
    """Cancel one leg's quantity (a failed leg the buyer agrees to drop). Needs the buyer's
    consent; the remaining legs carry the deal and pass QC on their own."""
    if not buyer_consent:
        raise ValueError("cancelling a leg's quantity needs the buyer's consent")
    if not reason.strip():
        raise ValueError("a reason is required")
    deal = await set_leg(deal_id, leg_id, {"status": "cancelled", "note": f"cancelled by {by}: {reason}"})
    from shared.audit_log import record as audit
    try:
        await audit("supplier_leg.cancelled", by, deal_id, {"leg_id": leg_id, "reason": reason, "buyer_consent": True})
    except Exception as exc:  # noqa: BLE001
        logger.warning("SupplierLegs: audit of leg cancel failed: %s", exc)
    remaining = active_legs(deal)
    out: dict[str, Any] = {"cancelled": leg_id, "remaining": [x.leg_id for x in remaining]}
    status = str(getattr(deal.status, "value", deal.status))
    if remaining and status == "SUPPLIER_DISPATCH_READY" and all(x.qc_result == "PASS" for x in remaining):
        from services.shield_link import service as sl  # type: ignore[import]
        from shared.domain.events import EventTopic
        await sl._transition_deal(deal_id, "QC_PASSED", f"remaining legs passed QC (leg {leg_id} cancelled)")
        await sl._emit(EventTopic.QC_PASSED, deal_id, {"tpo_id": "", "ext_ref": f"legs-{deal_id}", **loading_merged(deal)})
        out["qc_passed"] = True
    return out


# ---------------------------------------------------------------------------
# Money per leg
# ---------------------------------------------------------------------------

def paid_by_leg(deal: Any, payouts: list[Any]) -> dict[str, tuple[float, float]]:
    """leg_id → (completed, pending) payouts to that leg's supplier."""
    out: dict[str, tuple[float, float]] = {}
    for x in active_legs(deal):
        payee = f"SUPPLIER-{x.supplier_id}"
        done = sum(float(p.amount) for p in payouts if p.payee_id == payee and p.status == "COMPLETED")
        pend = sum(float(p.amount) for p in payouts if p.payee_id == payee and p.status in ("REQUESTED", "PROCESSING"))
        out[x.leg_id] = (round(done, 2), round(pend, 2))
    return out


def payout_plan(owed: dict[str, float], paid: dict[str, tuple[float, float]], available: float) -> dict[str, float]:
    """Released funds split pro rata by leg value, never above a leg's own due."""
    total = sum(owed.values())
    if total <= 0 or available <= 0:
        return {k: 0.0 for k in owed}
    plan: dict[str, float] = {}
    for k, v in owed.items():
        done, pend = paid.get(k, (0.0, 0.0))
        due = max(0.0, v - done - pend)
        plan[k] = round(min(due, available * v / total), 2)
    return plan


def every_leg_paid(deal: Any, payouts: list[Any], owed: dict[str, float], tolerance: float = 0.01) -> bool:
    paid = paid_by_leg(deal, payouts)
    return all(paid.get(k, (0.0, 0.0))[0] >= v - tolerance for k, v in owed.items())


# ---------------------------------------------------------------------------
# Compliance per leg
# ---------------------------------------------------------------------------

def compliance_findings(deal: Any) -> list[Any]:
    """Every leg screened (a failed supplier blocks) and no two parties related."""
    from shared.orchestration.compliance import Finding
    from shared.orchestration.composition import related
    legs = active_legs(deal)
    if len(legs) < 2:
        return []
    out = []
    failed = {o.supplier_id for o in getattr(deal, "supplier_offers", None) or [] if getattr(o, "verification_failed", False)}
    for x in legs:
        if x.supplier_id in failed:
            out.append(Finding(code="leg_supplier_failed", severity="block",
                               message=f"leg {x.leg_id}: {x.company_name or x.supplier_id} failed its check",
                               data={"leg_id": x.leg_id}))

    class _P:
        def __init__(self, name, email, pid):
            self.company_name, self.email, self.counterparty_id = name, email, pid

    buyer = next((p for p in getattr(deal, "parties", None) or [] if _role(p) == "buyer"), None)
    people = [_P(x.company_name, x.email, x.supplier_id) for x in legs]
    if buyer is not None:
        people.append(_P(getattr(buyer, "legal_name", ""), getattr(buyer, "email", None), "buyer"))
    for i in range(len(people)):
        for j in range(i + 1, len(people)):
            why = related(people[i], people[j])
            if why:
                out.append(Finding(code="leg_related_parties", severity="review", message=why))
    return out


def shipment_findings(deal: Any, tolerance: float) -> list[Any]:
    """Each leg's loaded quantity against its own PO quantity."""
    from shared.orchestration.compliance import Finding
    legs = active_legs(deal)
    if len(legs) < 2:
        return []
    out = []
    for x in legs:
        if x.loaded_mt is None:
            out.append(Finding(code="shipment_leg_qty_unknown", severity="review",
                               message=f"leg {x.leg_id}: the inspector did not record the loaded quantity"))
        elif x.quantity_mt and abs(x.loaded_mt - x.quantity_mt) > x.quantity_mt * tolerance:
            out.append(Finding(code="shipment_leg_qty_mismatch", severity="review",
                               message=f"leg {x.leg_id} ({x.company_name}): {x.loaded_mt:g} MT loaded, "
                                       f"{x.quantity_mt:g} MT ordered (tolerance {tolerance:.0%})",
                               data={"leg_id": x.leg_id, "loaded": x.loaded_mt, "ordered": x.quantity_mt}))
    return out


def summary(deal: Any) -> list[dict[str, Any]]:
    """Per-leg status for the deal board / timeline / evidence pack."""
    return [{"leg_id": x.leg_id, "supplier_id": x.supplier_id, "company_name": x.company_name,
             "quantity_mt": x.quantity_mt, "price_usd_per_mt": x.price_usd_per_mt, "status": x.status,
             "po_id": x.po_id, "po_ack": x.po_ack, "qc_result": x.qc_result, "loaded_mt": x.loaded_mt,
             "containers": x.containers, "seals": x.seals, "paid_usd": x.paid_usd}
            for x in getattr(deal, "supplier_legs", None) or []]
