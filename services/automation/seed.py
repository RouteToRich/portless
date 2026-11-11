"""Builds a deal at any stage of the workflow, the way the catalogue's fixtures describe.

The companies look like real trade counterparties, but every address is on the
reserved ``.invalid`` domain, which cannot receive mail.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

FIXTURES: dict[str, tuple[float, float, float]] = {
    "FX-STD": (1250.0, 1000.0, 20.0),
    "FX-THIN": (1100.0, 1000.0, 20.0),
    "FX-BELOW-FLOOR": (1050.0, 1000.0, 20.0),
}
HS_CODE, MARKET = "10063020", "AE"
_ORDER = ["DRAFT", "MATCHING", "NEGOTIATING", "NEGOTIATION_AGREED", "PI_SENT", "PI_ACCEPTED", "ESCROW_PENDING",
          "ESCROW_FUNDED", "PO_ISSUED", "SUPPLIER_DISPATCH_READY", "QC_PASSED", "IN_TRANSIT", "CLEARED", "PAID"]
_RANK_OF_SIDE_STATUS = {"QC_FAILED": "SUPPLIER_DISPATCH_READY", "DISPUTE": "SUPPLIER_DISPATCH_READY",
                        "REFUNDED": "SUPPLIER_DISPATCH_READY", "CANCELLED": "ESCROW_FUNDED",
                        "NEGOTIATION_FAILED": "NEGOTIATING"}
DEFAULT_RELEASED = {"SUPPLIER_DISPATCH_READY": ["T1"], "QC_PASSED": ["T1"], "IN_TRANSIT": ["T1", "T2"],
                    "CLEARED": ["T1", "T2"], "PAID": ["T1", "T2", "T3", "T4"]}
_hs_confirmed = False
DIRECTORY: dict[str, list[Any]] = {"buyer": [], "supplier": []}
_turn = {"buyer": 0, "supplier": 0}


async def load_directory() -> dict[str, int]:
    """The buyers and suppliers the live scan found. Deals are built with these companies."""
    from services.trade_min.directory import list_counterparties

    for role in ("buyer", "supplier"):
        found = [r for r in await list_counterparties(role) if r.name and len(r.name) > 2]
        wanted = MARKET if role == "buyer" else "IN"
        DIRECTORY[role] = [r for r in found if (r.country or "").upper() == wanted] or found
    return {role: len(rows) for role, rows in DIRECTORY.items()}


def company(role: str, fallback: str) -> Any:
    """The next real company for this role; a stand-in only when no scan has run."""
    from types import SimpleNamespace

    rows = DIRECTORY[role]
    if not rows:
        return SimpleNamespace(name=fallback, website=None, phone=None)
    _turn[role] += 1
    return rows[_turn[role] % len(rows)]


def _stored(name: str) -> str:
    """A small file on disk, so document downloads have something to serve."""
    folder = Path(tempfile.gettempdir()) / "portless-automation" / "documents"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.html"
    path.write_text(f"<html><body>{name}</body></html>", encoding="utf-8")
    return path.as_uri()


async def _thread(ids: dict[str, Any], deal_id: str, now: datetime) -> None:
    """The conversation that came before this stage, in the order it happened."""
    if "N" not in ids:
        return
    await write_conversation(ids["N"], deal_id, ids["buyer_mail"], ids["supplier_mail"], ids["buyer_price"], ids["supplier_price"], ids["qty"], now)


async def write_conversation(negotiation_id: str, deal_id: str, buyer_mail: str, supplier_mail: str,
                             buyer_price: float, supplier_price: float, qty: float, now: datetime) -> None:
    """A deal built directly at a later stage still shows how it got there:
    introduction, RFQ, quote, counter-offers and agreement, oldest first. These
    are written from templates; the LLM journeys have real drafted threads."""
    from agents.negotiation_agent.email_history import EmailDirection, EmailHistoryRecord, EmailStatus, get_email_repository

    trade = "trade@portless-qa.invalid"
    token = f"[NEG:{negotiation_id}]"
    steps = [
        (6.0, "", "outbound", "buyer", "outreach_intro", "Milled rice from India, inspected before shipment",
         f"Dear team,\n\nWe supply milled rice from verified Indian mills, with independent inspection and payment through a neutral escrow. Would {qty:g} MT be of interest?\n\nBest regards,\nPortLess Trade Operations"),
        (5.5, "", "inbound", "buyer", "outreach_reply", "Re: Milled rice from India, inspected before shipment",
         f"Yes, we are interested. Please send your offer for {qty:g} MT, delivered to Jebel Ali."),
        (5.0, "", "outbound", "supplier", "rfq", f"Quotation request: {qty:g} MT milled rice (HS {HS_CODE})",
         f"Dear team,\n\nPlease quote your best FOB price for {qty:g} MT of milled rice, and your lead time.\n\nBest regards,\nPortLess Trade Operations"),
        (4.5, "", "inbound", "supplier", "rfq_reply", f"Re: Quotation request: {qty:g} MT milled rice (HS {HS_CODE})",
         f"We can supply at USD {supplier_price * 1.02:,.2f} per MT FOB Chennai, 21 days from order."),
        (4.0, negotiation_id, "outbound", "buyer", "negotiation", f"Offer for milled rice {token}",
         f"Dear team,\n\nWe can offer {qty:g} MT at USD {buyer_price * 1.04:,.2f} per MT CIF Jebel Ali.\n\nBest regards,\nPortLess Trade Operations"),
        (3.5, negotiation_id, "outbound", "supplier", "negotiation", f"Order for milled rice {token}",
         f"Dear team,\n\nWe can place the order at USD {supplier_price * 0.96:,.2f} per MT FOB. The purchase order comes only after the buyer has paid into escrow.\n\nBest regards,\nPortLess Trade Operations"),
        (3.0, negotiation_id, "inbound", "buyer", "negotiation", f"Re: Offer for milled rice {token}",
         f"That is above our budget. We can do USD {buyer_price * 0.98:,.2f} per MT."),
        (2.5, negotiation_id, "inbound", "supplier", "negotiation", f"Re: Order for milled rice {token}",
         f"Our best price is USD {supplier_price:,.2f} per MT FOB Chennai."),
        (2.0, negotiation_id, "outbound", "buyer", "negotiation", f"Revised offer for milled rice {token}",
         f"Dear team,\n\nOur best price is USD {buyer_price:,.2f} per MT CIF Jebel Ali for {qty:g} MT.\n\nBest regards,\nPortLess Trade Operations"),
        (1.5, negotiation_id, "outbound", "supplier", "negotiation", f"Confirming your price {token}",
         f"Dear team,\n\nWe confirm USD {supplier_price:,.2f} per MT FOB Chennai for {qty:g} MT.\n\nBest regards,\nPortLess Trade Operations"),
        (1.0, negotiation_id, "inbound", "buyer", "negotiation", f"Re: Revised offer for milled rice {token}",
         f"We accept USD {buyer_price:,.2f} per MT. We choose SGS for the inspection."),
    ]
    repo = get_email_repository()
    for days_ago, neg, direction, party, kind, subject, body in steps:
        when = now - timedelta(days=days_ago)
        outbound = direction == "outbound"
        address = buyer_mail if party == "buyer" else supplier_mail
        await repo.save(EmailHistoryRecord(
            email_id=f"EMAIL-{uuid4().hex[:10].upper()}", negotiation_id=neg, deal_id=deal_id, round_no=0 if not neg else 1,
            direction=EmailDirection(direction), party=party, to_address=address if outbound else trade, from_address=trade if outbound else address,
            subject=subject, body=body, status=EmailStatus.SENT if outbound else EmailStatus.RECEIVED, kind=kind,
            sent_at=when if outbound else None, created_at=when, updated_at=when,
        ))


async def sent_mail(negotiation_id: str, deal_id: str, party: str, to: str, now: datetime) -> None:
    from agents.negotiation_agent.email_history import EmailDirection, EmailHistoryRecord, EmailStatus, get_email_repository
    await get_email_repository().save(EmailHistoryRecord(
        email_id=f"EMAIL-{uuid4().hex[:10].upper()}", negotiation_id=negotiation_id, deal_id=deal_id, round_no=1, direction=EmailDirection.OUTBOUND,
        party=party, to_address=to, from_address="trade@portless-qa.invalid", subject=f"Offer [NEG:{negotiation_id}]",
        body="Dear team, here is our offer. Best regards, PortLess Trade Operations", status=EmailStatus.SENT, sent_at=now, created_at=now, updated_at=now,
    ))


async def add_payout(ids: dict[str, Any], amount: float, status: str) -> None:
    from services.escrow_guard.repository import get_repository
    from services.escrow_guard.schemas import PayoutRecord
    now = datetime.now(timezone.utc)
    ref = f"PAY-{uuid4().hex[:8]}"
    await get_repository().save_payout(PayoutRecord(
        payout_id=ref, deal_id=ids["D"], payee_id=f"SUP-{ids['uid']}", amount=amount, currency="USD", status=status, ext_ref=f"EXT-{ref}",
        created_at=now, updated_at=now, completed_at=now if status == "COMPLETED" else None, idempotency_key=f"seed-{ref}",
        total_owed=ids["supplier_price"] * ids["qty"],
    ))
    ids["PAY"], ids["PAY_REF"] = ref, f"EXT-{ref}"


def rank(status: str) -> int:
    return _ORDER.index(_RANK_OF_SIDE_STATUS.get(status, status))


async def confirm_hs() -> None:
    global _hs_confirmed
    if _hs_confirmed:
        return
    from shared.orchestration import compliance
    await compliance.set_hs_policy(compliance.HsPolicyEntry(hs_code=HS_CODE, status="free", note="automation fixture"))
    _hs_confirmed = True


def parties(uid: str, market: str = MARKET) -> tuple[Any, Any, dict[str, str]]:
    from shared.domain.enums import PartyRole
    from shared.domain.models import Party

    now = datetime.now(timezone.utc).isoformat()
    real_buyer = company("buyer", f"Gulf Staples Trading {uid} LLC")
    real_supplier = company("supplier", f"Cauvery Rice Mills {uid} Pvt Ltd")
    # The company is real; the mailbox never is, so no test can reach it.
    who = {"buyer_company": real_buyer.name, "buyer_mail": "buyer@buyer.invalid",
           "supplier_company": real_supplier.name, "supplier_mail": "sales@supplier.invalid"}
    buyer = Party(role=PartyRole.BUYER, legal_name=who["buyer_company"], country=market, email=who["buyer_mail"],
                  address="Office 1204, Al Khaleej Road, Dubai", phone=real_buyer.phone or "+971 4 000 0000", website=real_buyer.website,
                  sanctions_status="clear", sanctions_checked_at=now)
    supplier = Party(role=PartyRole.SUPPLIER, legal_name=who["supplier_company"], country="IN", email=who["supplier_mail"],
                     address="14 Mill Road, Thanjavur, Tamil Nadu", phone=real_supplier.phone or "+91 44 0000 0000", website=real_supplier.website,
                     gstin="33AAAAA0000A1Z5", iec="0400000000", sanctions_status="clear", sanctions_checked_at=now)
    return buyer, supplier, who


async def seed_deal(status: str, fixture: str = "FX-STD", *, released: list[str] | None = None, org_id: str = "qa-org-a",
                    market: str = MARKET, provider: str = "tazapay", funded: float | None = None, loading: bool | None = None,
                    window_open: bool = False, po: bool = True, target_price: float | None | str = "fixture",
                    qc_provider: str = "sgs", disputed: bool = False, settled: bool = False,
                    pi_key: str | None = None, escrow_key: str | None = None, with_hold: bool = True,
                    buyer_price: float | None = None, supplier_price: float | None = None) -> dict[str, Any]:
    """Create the deal and every record that exists at ``status``. Returns its ids."""
    from agents.negotiation_agent.repository import get_repository as neg_repo
    from agents.negotiation_agent.schemas import NegotiationPolicy, NegotiationSession
    from services.dealmaker.repository import get_repository as dm_repo
    from services.dealmaker.schemas import PiRecord, PoRecord
    from services.deals.repository import get_repository as deals_repo
    from services.deals.schemas import CreateDealRequest, MatchResult
    from services.deals.service import create_deal, get_deal
    from services.escrow_guard.repository import get_repository as eg_repo
    from services.escrow_guard.schemas import EscrowHoldRecord, TrancheSpec
    from services.escrow_guard.service import DEFAULT_TRANCHES
    from services.shield_link.repository import get_repository as sl_repo
    from services.shield_link.schemas import ThirdPartyOrderRecord
    from shared.domain.enums import DealStatus
    from shared.orchestration import compliance

    # ``buyer_price`` / ``supplier_price`` override the fixture's prices on every
    # record that carries them (deal target, negotiation, PI, escrow, PO).
    fx_buyer, fx_supplier, qty = FIXTURES[fixture]
    buyer_price = fx_buyer if buyer_price is None else buyer_price
    supplier_price = fx_supplier if supplier_price is None else supplier_price
    uid = uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    buyer, supplier, who = parties(uid, market)
    before = os.environ.get("EVENT_CONTINUATION_ENABLED")
    os.environ["EVENT_CONTINUATION_ENABLED"] = "false"
    try:
        created = await create_deal(CreateDealRequest(
            operator_id="qa-staff", hs_code=HS_CODE, market=market, margin_floor_pct=8.0, parties=[buyer, supplier],
        ), org_id=org_id)
    finally:
        if before is None:
            os.environ.pop("EVENT_CONTINUATION_ENABLED", None)
        else:
            os.environ["EVENT_CONTINUATION_ENABLED"] = before
    deal_id = created.deal_id
    pi_key = f"{pi_key}-{uid}" if pi_key else None
    escrow_key = f"{escrow_key}-{uid}" if escrow_key else None
    ids: dict[str, Any] = {"D": deal_id, "uid": uid, **who, "buyer_price": buyer_price, "supplier_price": supplier_price, "qty": qty}
    record = await get_deal(deal_id)
    match = MatchResult(match_id=f"M-{uid}", supplier_id=f"SUP-{uid}", company_name=who["supplier_company"],
                        composite_score=85.0, gatekeeper_passed=True, arbitrage_score=80.0, proximity_score=80.0,
                        reliability_score=80.0, decision="SOURCE", fob_price_usd=supplier_price)
    await deals_repo().save(record.model_copy(update={
        "status": DealStatus(status), "match_results": [match], "winning_supplier_id": f"SUP-{uid}", "quantity_mt": qty,
        "buyer_target_price_usd": buyer_price if target_price == "fixture" else target_price,
    }))

    level = rank(status)
    if level >= rank("NEGOTIATION_AGREED"):
        neg_id = str(uuid4())
        ids["N"] = neg_id
        await neg_repo().save_session(NegotiationSession(
            negotiation_id=neg_id, deal_id=deal_id, trade_match_id=f"TM-{uid}", buyer_id=f"BUY-{uid}", seller_id=f"SUP-{uid}",
            product_code=HS_CODE, buyer_country_code=market, buyer_target_price=buyer_price, supplier_quote_price=supplier_price,
            policy=NegotiationPolicy(), quantity_mt=qty, buyer_email=who["buyer_mail"], seller_email=who["supplier_mail"],
            status="AGREED", round_no=3, current_bid=buyer_price, current_ask=supplier_price, buyer_last_offer=buyer_price,
            supplier_last_quote=supplier_price, agreed_price=buyer_price, qc_provider=qc_provider, created_at=now, updated_at=now,
        ))
    if level >= rank("PI_SENT") and po is not None:
        pi = PiRecord(
            pi_id=f"PI-{uid}", deal_id=deal_id, status="SENT" if level == rank("PI_SENT") else "ACCEPTED", agreed_price=buyer_price,
            currency="USD", quantity_mt=qty, total_value=buyer_price * qty, incoterm="CIF", port_of_destination=f"Jebel Ali, {market}",
            payment_terms="Neutral escrow, staged release", validity_days=7, buyer_name=who["buyer_company"],
            buyer_address=buyer.address or "", buyer_vat_number=None, qc_provider=qc_provider, escrow_provider=provider,
            storage_uri=_stored(f"PI-{uid}"), content_hash=f"hash-{uid}", issued_at=now, expires_at=now + timedelta(days=7), idempotency_key=pi_key,
        )
        await dm_repo().save_pi(pi)
        ids["PI"] = pi.pi_id
    if level >= rank("ESCROW_PENDING") and with_hold:
        amount = buyer_price * qty
        tranches = [TrancheSpec(tranche_id=t, trigger=trg, pct=pct, amount=round(amount * pct, 2)) for t, trg, pct in DEFAULT_TRANCHES]
        names = DEFAULT_RELEASED.get(status, []) if released is None else released
        total = 0.0
        for tranche in tranches:
            if tranche.tranche_id in names:
                tranche.status, tranche.released_at, tranche.proof_refs = "RELEASED", now, [f"proof-{uid}"]
                total += tranche.amount or 0.0
        is_funded = level >= rank("ESCROW_FUNDED")
        got = funded if funded is not None else (amount if is_funded else None)
        hold_status = "PENDING"
        if status == "REFUNDED":
            hold_status = "REFUNDED"
        elif got is not None:
            hold_status = "PARTIALLY_FUNDED" if got < amount - 0.01 else "FUNDED"
        if settled:
            hold_status = "RELEASED"
        hold = EscrowHoldRecord(
            escrow_id=f"ESC-{uid}", deal_id=deal_id, provider=provider, amount=amount, currency="USD", status=hold_status,
            ext_ref=f"EXT-{uid}", funded_at=now if got is not None else None, funded_amount=got,
            shortfall=round(amount - got, 2) if got is not None and got < amount - 0.01 else None,
            created_at=now, updated_at=now, idempotency_key=escrow_key, qc_provider=qc_provider, tranches=tranches, released_total=total, fee_pct=1.0,
            fees_total=round(total * 0.01, 2), disputed=disputed, disputed_at=now if disputed else None,
            refunded_total=amount - total if status == "REFUNDED" else 0.0,
            inspection_deadline=now + timedelta(hours=72) if window_open else None,
            inspection_opened_at=now if window_open else None,
            delivery_confirmed_at=now if window_open else None,
            settled_at=now if settled else None,
        )
        await eg_repo().save_hold(hold)
        ids["ESC"], ids["ESC_REF"] = hold.escrow_id, hold.ext_ref
    if level >= rank("PO_ISSUED") and po:
        record_po = PoRecord(
            po_id=f"PO-{uid}", deal_id=deal_id, status="ISSUED", agreed_price=supplier_price, currency="USD", quantity_mt=qty,
            total_value=supplier_price * qty, incoterm="FOB", port_of_loading="Chennai, IN", delivery_deadline=date(2027, 1, 31),
            payment_terms="Staged from escrow", supplier_name=who["supplier_company"], supplier_address=supplier.address or "",
            supplier_gstin=supplier.gstin, supplier_iec=supplier.iec, quality_spec=None,
            net_margin_pct=(buyer_price - supplier_price) / buyer_price * 100, margin_floor_pct=8.0, storage_uri=_stored(f"PO-{uid}"),
            content_hash=f"hash-po-{uid}", issued_at=now,
        )
        await dm_repo().save_po(record_po)
        ids["PO"] = record_po.po_id
    if level >= rank("SUPPLIER_DISPATCH_READY"):
        done = level >= rank("QC_PASSED") or status == "QC_FAILED"
        qc = ThirdPartyOrderRecord(
            tpo_id=f"TPO-QC-{uid}", deal_id=deal_id, order_type="QC", provider=qc_provider, status="COMPLETED" if done else "BOOKED",
            ext_ref=f"QC-{uid}", qc_result=("PASS" if level >= rank("QC_PASSED") else "FAIL") if done else None,
            created_at=now, updated_at=now,
        )
        await sl_repo().save(qc)
        ids["QC"], ids["QC_REF"] = qc.tpo_id, qc.ext_ref
    if level >= rank("IN_TRANSIT"):
        freight = ThirdPartyOrderRecord(
            tpo_id=f"TPO-FRT-{uid}", deal_id=deal_id, order_type="FREIGHT", provider="cogoport", status="BOOKED", ext_ref=f"FRT-{uid}",
            destination_port=f"Jebel Ali, {market}", created_at=now, updated_at=now,
        )
        await sl_repo().save(freight)
        ids["FRT"], ids["FRT_REF"] = freight.tpo_id, freight.ext_ref
    await _thread(ids, deal_id, now)
    if settled or status == "PAID":
        await add_payout(ids, supplier_price * qty, "COMPLETED")
    wants_loading = loading if loading is not None else level >= rank("QC_PASSED")
    if wants_loading:
        await compliance.record_loading(deal_id, compliance.LoadingRecord(
            container_numbers=["CONT-1"], seal_numbers=["SEAL-1"], loaded_quantity_mt=qty, hs_code=HS_CODE,
            inspector_ref=f"QC-{uid}", photos=["https://photos.invalid/1.jpg"], source="inspector",
        ))
    return ids
