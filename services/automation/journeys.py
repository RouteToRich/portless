"""Whole-workflow journeys: the real LLM drafts PortLess's e-mails and plays the counterparties.

Every journey runs inside the automation worker, so mail stops at the composed
record, addresses are on ``.invalid`` and the data lands in ``portless_automation``.
"""
from __future__ import annotations

import asyncio
import os
import re
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from agents.negotiation_agent.email_history import get_email_repository
from agents.negotiation_agent.repository import get_repository as neg_repo
from agents.negotiation_agent.schemas import NegotiationPolicy, NegotiationSession
from agents.negotiation_agent.service import finalize_agreement
from services.dealmaker.repository import get_repository as dm_repo
from services.dealmaker.schemas import AcceptPiRequest
from services.dealmaker.service import accept_pi
from services.deals.repository import get_repository as deals_repo
from services.docugen.repository import get_repository as dg_repo
from services.exception_resolver.repository import get_repository as er_repo
from services.deals.schemas import CreateDealRequest, MatchResult, UpdateStatusRequest
from services.deals.service import create_deal, get_deal, update_status
from services.email_processor.schemas import InboundEmailSource, InboundRawEmail
from services.email_processor.service import process_inbound
from services.escrow_guard import service as eg
from services.escrow_guard.journal import build_journal
from services.escrow_guard.repository import get_repository as eg_repo
from services.escrow_guard.schemas import (
    AcceptEscrowRequest, DisputeEscrowRequest, EscrowFundedWebhookPayload, PayoutWebhookPayload, ReleaseTrancheRequest,
)
from services.hitl_approval.schemas import ResolveApprovalRequest
from services.hitl_approval.service import list_approvals, resolve_approval
from services.shield_link import service as sl
from services.shield_link.repository import get_repository as sl_repo
from services.shield_link.schemas import FreightMilestonePayload, QcWebhookPayload
from shared.domain.enums import DealStatus, PartyRole
from shared.domain.models import Party
from shared.llm_gateway import LLMClient
from shared.orchestration import compliance, evidence

from . import seed as fx
from .journeys_index import JOURNEYS

EMAILS: list[dict[str, Any]] = []
CHECKS: list[dict[str, Any]] = []
TRANSCRIPT: list[str] = []
LLM_REPLIES = {"ok": 0, "failed": 0}
_background: set[asyncio.Task] = set()


def say(text: str = "") -> None:
    print(text, flush=True)


def note(text: str) -> None:
    TRANSCRIPT.append(text)


def check(scenario: str, what: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append({"text": what, "ok": bool(ok), "detail": detail[:400]})
    say(f"   [{'PASS' if ok else 'FAIL'}] {what}{(' - ' + detail) if detail else ''}")
    return bool(ok)


async def settle(timeout: float = 45.0) -> None:
    me = asyncio.current_task()
    loop = asyncio.get_running_loop()
    end, quiet = loop.time() + timeout, 0
    while loop.time() < end and quiet < 3:
        await asyncio.sleep(0.02)
        quiet = quiet + 1 if not [t for t in asyncio.all_tasks() if t is not me and t not in _background and not t.done()] else 0


async def status(deal_id: str) -> str:
    d = await get_deal(deal_id)
    return d.status.value if hasattr(d.status, "value") else str(d.status)


async def pending(deal_id: str, action: str | None = None) -> list:
    rows = await list_approvals(status="PENDING", deal_id=deal_id)
    return [r for r in rows if action is None or r.action == action]


async def approve(deal_id: str, action: str, by: str = "qa-operator") -> list:
    """APPROVE every pending gate of one action, as an operator would."""
    done = []
    for gate in await pending(deal_id, action):
        say(f"   gate {gate.action} level={gate.autonomy_level} amount={gate.financial_impact_usd} "
            f"flags={gate.risk_flags}")
        await resolve_approval(gate.approval_id, ResolveApprovalRequest(decision="APPROVE", resolved_by=by))
        done.append(gate)
        await settle()
        if action in ("RELEASE_ESCROW_TRANCHE", "CONFIRM_BUYER_ACCEPTANCE"):
            await read_escrow_provider(deal_id)
    return done


async def read_escrow_provider(deal_id: str) -> None:
    """A provider escrow (Tazapay / Escrow.com) only takes the proof on a
    release; the payout is booked when PortLess reads the provider, which the
    escrow watcher does in production (the watcher is off in automation)."""
    hold = await eg_repo().get_hold_by_deal(deal_id)
    from shared.connectors.escrow import in_simulator
    if hold is not None and in_simulator(hold.provider):
        await eg.poll_provider(hold.escrow_id, source="poll")
        await settle()


async def seed(name: str, buyer_price: float, supplier_price: float, qty: float = 20.0,
               hs: str = "10063020", market: str = "AE") -> str:
    """A deal whose negotiation just succeeded, as the user asked us to assume."""
    os.environ["EVENT_CONTINUATION_ENABLED"] = "false"
    deal = await create_deal(CreateDealRequest(
        operator_id="qa-operator", hs_code=hs, market=market, margin_floor_pct=8.0,
        parties=[
            Party(role=PartyRole.BUYER, legal_name=fx.company("buyer", f"Gulf Staples Trading {name} LLC").name, country=market,
                  email=f"buyer.{name}@gulfstaples-{name}.invalid", address="1 Test Road, Test City",
                  phone="+0000000001", sanctions_status="clear",
                  sanctions_checked_at=datetime.now(timezone.utc).isoformat()),
            Party(role=PartyRole.SUPPLIER, legal_name=fx.company("supplier", f"Cauvery Rice Mills {name} Pvt Ltd").name, country="IN",
                  email=f"sales.{name}@cauverymills-{name}.invalid", address="2 Sample Street, Chennai",
                  phone="+0000000002", gstin="00AAAAA0000A0Z0", iec="0000000000",
                  sanctions_status="clear",
                  sanctions_checked_at=datetime.now(timezone.utc).isoformat()),
        ],
    ), org_id="qa-org-a")
    deal_id = deal.deal_id
    await update_status(deal_id, UpdateStatusRequest(target_status=DealStatus.MATCHING, reason="automation seed"))
    await update_status(deal_id, UpdateStatusRequest(target_status=DealStatus.NEGOTIATING, reason="automation seed"))
    rec = await get_deal(deal_id)
    await deals_repo().save(rec.model_copy(update={
        "winning_supplier_id": f"SUP-{name}", "quantity_mt": qty, "buyer_target_price_usd": buyer_price,
    }))
    now = datetime.now(timezone.utc)
    neg_id = str(__import__("uuid").uuid4())
    await neg_repo().save_session(NegotiationSession(
        negotiation_id=neg_id, deal_id=deal_id, trade_match_id=f"TM-{name}",
        buyer_id=f"BUY-{name}", seller_id=f"SUP-{name}", product_code=hs, buyer_country_code=market,
        buyer_target_price=buyer_price, supplier_quote_price=supplier_price, policy=NegotiationPolicy(),
        quantity_mt=qty, buyer_email=f"buyer.{name}@gulfstaples-{name}.invalid",
        seller_email=f"sales.{name}@cauverymills-{name}.invalid", status="HITL_PENDING", round_no=3,
        current_bid=buyer_price, current_ask=supplier_price, buyer_last_offer=buyer_price,
        supplier_last_quote=supplier_price, agreed_price=buyer_price, qc_provider="sgs",
        created_at=now, updated_at=now,
    ))
    await fx.write_conversation(neg_id, deal_id, f"buyer.{name}@gulfstaples-{name}.invalid", f"sales.{name}@cauverymills-{name}.invalid",
                                buyer_price, supplier_price, qty, now)
    await settle()
    os.environ["EVENT_CONTINUATION_ENABLED"] = "true"
    await finalize_agreement(neg_id, buyer_price)  # negotiation = success
    await settle()
    return deal_id


async def to_pi_sent(s: str, deal_id: str) -> None:
    say(f"\n[{s}] Negotiation agreed → compliance → PI")
    st = await status(deal_id)
    say(f"   deal after agreement: {st}; gates: {[g.action for g in await pending(deal_id)]}")
    await approve(deal_id, "CONFIRM_COUNTERPARTY_RISK")
    await approve(deal_id, "CONFIRM_TRADE_COMPLIANCE")
    from shared.orchestration import supplier_commitment
    rec = await supplier_commitment.get(deal_id)
    if rec is not None and rec.status == "requested" and rec.current is not None:
        # The supplier confirms by phone; the operator relays it (staff endpoint behaviour).
        await supplier_commitment._gate_confirmation(rec, "operator:qa", "Supplier confirmed the order by phone.")
        await approve(deal_id, "CONFIRM_SUPPLIER_ORDER")
        await settle()
    check(s, "deal reaches PI_SENT", await status(deal_id) == "PI_SENT", await status(deal_id))


async def to_funded(s: str, deal_id: str, payer: str | None = None) -> None:
    say(f"\n[{s}] Buyer accepts PI → escrow opens → buyer funds")
    pi = await dm_repo().get_pi_by_deal(deal_id)
    check(s, "PI record exists", pi is not None, f"{pi.pi_id} total={pi.total_value}" if pi else "")
    await accept_pi(pi.pi_id, AcceptPiRequest(accepted_by="qa-buyer"))
    await settle()
    check(s, "deal reaches ESCROW_PENDING", await status(deal_id) == "ESCROW_PENDING", await status(deal_id))
    hold = await eg_repo().get_hold_by_deal(deal_id)
    say(f"   escrow {hold.escrow_id} provider={hold.provider} amount={hold.amount} "
        f"tranches={[(t.tranche_id, t.pct, t.trigger) for t in hold.tranches]}")
    check(s, "escrow sized from the PI", abs(hold.amount - pi.total_value) < 0.01)
    deal = await get_deal(deal_id)
    buyer = next(p for p in deal.parties if p.role == PartyRole.BUYER)
    await fund(deal_id, hold.amount, payer or buyer.legal_name, buyer.country, f"evt-fund-{deal_id}")
    check(s, "deal reaches ESCROW_FUNDED", await status(deal_id) == "ESCROW_FUNDED", await status(deal_id))


async def fund(deal_id: str, amount: float, payer: str, country: str, event_id: str) -> dict:
    hold = await eg_repo().get_hold_by_deal(deal_id)
    from shared.connectors.escrow import in_simulator
    if in_simulator(hold.provider):
        # The buyer pays on the provider (simulator); PortLess learns it by reading
        # the provider, exactly as from a real Tazapay / Escrow.com webhook.
        from services.escrow_sandbox.main import buyer_pays
        await buyer_pays(hold.provider, hold.ext_ref, None if amount >= hold.amount - 0.01 else amount)
        updated = await eg.poll_provider(hold.escrow_id, source="webhook")
        await settle()
        return {"status": "ok" if updated.status == "FUNDED" else updated.status.lower(),
                "escrow_id": hold.escrow_id, "deal_id": deal_id}
    wire = amount * 100 if hold.provider == "stripe" else amount
    res = await eg.handle_escrow_funded_webhook(EscrowFundedWebhookPayload(
        ext_ref=hold.ext_ref, status="FUNDED", amount=wire, currency=hold.currency, event_id=event_id,
        payer_name=payer, payer_country=country,
    ), {}, b"")
    await settle()
    return res


async def to_qc_ready(s: str, deal_id: str, supplier_ack=None, ready_date=None) -> None:
    """PO gate → PO → (supplier acknowledges the PO and names its goods-ready
    date) → QC booked for that date → T1 tranche. ``supplier_ack`` plays the
    supplier's acknowledgement by e-mail; without it an operator records the
    acknowledgement, as the staff endpoint does. When the acknowledgement names
    no date, the operator records ``ready_date`` (default: ten days out)."""
    say(f"\n[{s}] PO gate → PO → PO acknowledged + goods-ready date → QC booked → T1 tranche")
    await approve(deal_id, "ISSUE_PO")
    po = await dm_repo().get_po_by_deal(deal_id)
    check(s, "PO issued", po is not None, f"{po.po_id} total={po.total_value}" if po else "")
    from shared.orchestration import qc_schedule, supplier_commitment
    if supplier_commitment.po_ack_required() and await status(deal_id) == "PO_ISSUED":
        qc_before = [o for o in await sl_repo().list_by_deal(deal_id) if o.order_type == "QC"]
        check(s, "no inspection is booked before the supplier acknowledges the PO", not qc_before, str(len(qc_before)))
        if supplier_ack is not None:
            await supplier_ack()
        else:
            await supplier_commitment.acknowledge_po(deal_id, by="operator@qa.invalid: acknowledged by phone")
        await settle()
    if qc_schedule.readiness_required() and await status(deal_id) == "PO_ISSUED":
        rec = await supplier_commitment.get(deal_id)
        asked = [m for m in EMAILS if "ready for inspection" in m["subject"].lower()]
        check(s, "the supplier is asked for its goods-ready date before any inspection is booked",
              bool(rec and rec.qc_ready_status == "awaiting") and bool(asked),
              f"status={rec.qc_ready_status if rec else None}, asked={len(asked)}")
        when = ready_date or (qc_schedule.today() + timedelta(days=10))
        await supplier_commitment.record_qc_readiness(deal_id, when, by="operator@qa.invalid: date given by phone")
        await settle()
    rec = await supplier_commitment.get(deal_id)
    qc = [o for o in await sl_repo().list_by_deal(deal_id) if o.order_type == "QC"]
    if qc and rec and rec.qc_ready_date:
        check(s, "the inspection is booked for the supplier's goods-ready date (or the first day the agency can attend)",
              qc[-1].inspection_date == qc_schedule.inspection_date_for(rec.qc_ready_date).isoformat(),
              f"ready {rec.qc_ready_date}, booked {qc[-1].inspection_date}")
    check(s, "deal reaches SUPPLIER_DISPATCH_READY", await status(deal_id) == "SUPPLIER_DISPATCH_READY",
          await status(deal_id))
    await approve(deal_id, "RELEASE_ESCROW_TRANCHE")
    await show_tranches(deal_id)


async def show_tranches(deal_id: str) -> None:
    hold = await eg_repo().get_hold_by_deal(deal_id)
    say(f"   tranches: {[(t.tranche_id, t.status, t.amount) for t in hold.tranches]}")


async def qc_order(deal_id: str):
    return next(o for o in await sl_repo().list_by_deal(deal_id) if o.order_type == "QC")


async def freight_order(deal_id: str):
    return next(o for o in await sl_repo().list_by_deal(deal_id) if o.order_type.upper() == "FREIGHT")


async def qc_result(deal_id: str, result: str, qty: float = 20.0, hs: str = "10063020",
                    seals: tuple[str, ...] = ("SEAL-SBX-001",)) -> None:
    o = await qc_order(deal_id)
    await sl.handle_qc_result(o.ext_ref, QcWebhookPayload(
        ext_ref=o.ext_ref, result=result, report_url="https://sandbox.invalid/report.pdf",
        crf_hash="sandbox-crf-hash", notes="sandbox inspection",
        container_numbers=["SBXU0000001"] if result == "PASS" else [],
        seal_numbers=list(seals) if result == "PASS" else [],
        loaded_quantity_mt=qty if result == "PASS" else None, loaded_hs_code=hs if result == "PASS" else None,
        goods_description="sandbox goods", photos=["https://sandbox.invalid/seal.jpg"] if result == "PASS" else [],
    ), {}, b"")
    await settle()


async def to_in_transit(s: str, deal_id: str) -> None:
    say(f"\n[{s}] QC pass + sealed container → documents → T2 → freight → customs")
    await qc_result(deal_id, "PASS")
    check(s, "deal reaches QC_PASSED", await status(deal_id) == "QC_PASSED", await status(deal_id))
    docs = await dg_repo().list_by_deal(deal_id)
    kinds = {str(getattr(d.doc_type, "value", d.doc_type)) for d in docs if d.status == "RENDERED"}
    expected = {"SALES_CONTRACT", "INSPECTION_ORDER", "CI", "PL", "CoO", "SDF", "SHIPPING_BILL"}
    check(s, "sales contract, inspection order and the 5 export documents rendered", expected <= kinds,
          f"missing {sorted(expected - kinds)}; have {sorted(kinds)}")
    say(f"   gates: {[g.action for g in await pending(deal_id)]}")
    await approve(deal_id, "RELEASE_ESCROW_TRANCHE")
    await approve(deal_id, "BOOK_FREIGHT")
    check(s, "deal reaches IN_TRANSIT", await status(deal_id) == "IN_TRANSIT", await status(deal_id))
    # A Maersk booking (the default carrier) asks for the VGM and the draft B/L once confirmed.
    await approve(deal_id, "SUBMIT_VGM")
    await approve(deal_id, "APPROVE_TRANSPORT_DOCUMENT")
    await approve(deal_id, "FILE_CUSTOMS")
    orders = await sl_repo().list_by_deal(deal_id)
    check(s, "customs (CHA) order filed", any(o.order_type.upper() == "CHA" for o in orders),
          str([(o.order_type, o.status) for o in orders]))
    await show_tranches(deal_id)


async def milestone(deal_id: str, name: str, seals: tuple[str, ...] = ()) -> None:
    o = await freight_order(deal_id)
    await sl.handle_freight_milestone(o.ext_ref, FreightMilestonePayload(
        ext_ref=o.ext_ref, milestone=name, location="Sandbox Port", seal_numbers=list(seals),
    ), {}, b"")
    await settle()


async def complete_payouts(deal_id: str) -> int:
    n = 0
    for p in await eg_repo().list_payouts_by_deal(deal_id):
        if p.status != "COMPLETED":
            await eg.handle_payout_webhook(PayoutWebhookPayload(
                ext_ref=p.ext_ref, status="COMPLETED", amount=p.amount, currency=p.currency,
                event_id=f"evt-payout-{p.payout_id}",
            ), {}, b"")
            n += 1
            await settle()
    return n


async def exceptions_of(deal_id: str) -> list:
    repo = er_repo()
    fn = getattr(repo, "list_by_deal", None)
    return list(await fn(deal_id)) if fn else []


base = SimpleNamespace(EMAILS=EMAILS, eg=eg, dm_repo=dm_repo, approve=approve, complete_payouts=complete_payouts, exceptions_of=exceptions_of, fund=fund, milestone=milestone, pending=pending, seed=seed, status=status, to_funded=to_funded, to_in_transit=to_in_transit, to_pi_sent=to_pi_sent, to_qc_ready=to_qc_ready)


def note(text: str) -> None:
    TRANSCRIPT.append(text)


async def slow_settle() -> None:
    await settle(timeout=150.0)


class CounterpartyUnavailable(RuntimeError):
    """The LLM that plays the buyer / supplier could not write a reply."""


def _scripted_prices(instruction: str) -> list[str]:
    """Prices the instruction tells the persona to state ("USD 1,010" → "1010")."""
    return [m.replace(",", "") for m in re.findall(r"USD\s*([\d,]+(?:\.\d+)?)", instruction)]


async def persona_reply(role: str, company: str, thread: str, instruction: str) -> str:
    """The LLM plays the buyer or supplier. Raises CounterpartyUnavailable if it fails:
    mailing the bare instruction ("Say the price is too high…") would test nothing real."""
    system = (
        f"You are the {role} at {company}, in an international commodity trade by e-mail. "
        "Write ONLY the body of your reply e-mail, in natural business English, under 90 words, "
        "signed with a first name. Do not write a subject line. Do not use markdown. Follow the "
        "instruction exactly; state prices as, for example, USD 1,180 per MT."
    )
    for attempt in range(4):
        # A long template (the Proforma Invoice) has come back blank from the gateway:
        # retries show less of it and allow more tokens.
        shown = thread[-1800:] if attempt == 0 else thread[:700]
        user = f"The e-mail you received:\n---\n{shown}\n---\n\nYour instruction: {instruction}"
        try:
            response = await LLMClient().chat(
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                caller="qa_counterparty", temperature=0.4 + 0.1 * attempt, max_tokens=1500 if attempt == 0 else 3000,
            )
            text = (response.content or "").strip()
            if len(re.findall(r"[A-Za-z]{2,}", text)) < 4:  # "-P", a bare name: not an e-mail
                say(f"   (LLM reply too short to be an e-mail: {text[:40]!r}, attempt {attempt + 1})")
                await asyncio.sleep(3 * (attempt + 1))
                continue
            missing = [p for p in _scripted_prices(instruction) if p not in text.replace(",", "")]
            if text and not missing:
                LLM_REPLIES["ok"] += 1
                return text
            if text:  # the persona improvised a price: the journey would test the wrong deal
                say(f"   (LLM reply left out the scripted price {missing}, attempt {attempt + 1})")
                continue
            say(f"   (blank LLM reply, attempt {attempt + 1})")
        except Exception as exc:  # noqa: BLE001
            say(f"   (LLM reply failed, attempt {attempt + 1}: {type(exc).__name__}: {str(exc)[:100]})")
        await asyncio.sleep(3 * (attempt + 1))
    LLM_REPLIES["failed"] += 1
    raise CounterpartyUnavailable(
        f"No usable LLM reply: the simulated {role} at {company} did not write the scripted reply after 4 attempts "
        "(gateway errors, blank or too-short replies, or a missing scripted price — see the attempt lines) "
        f"(instruction: {instruction[:120]}). The journey stopped here."
    )


async def open_negotiation(name: str, buyer=1250.0, supplier=1000.0, qty=20.0) -> tuple[str, dict]:
    os.environ["EVENT_CONTINUATION_ENABLED"] = "false"
    hs, market, now = "10063020", "AE", datetime.now(timezone.utc).isoformat()
    who = {
        "name": name,
        "buyer_company": fx.company("buyer", f"Gulf Staples Trading {name} LLC").name, "buyer_mail": f"buyer.{name}@gulfstaples-{name}.invalid",
        "supplier_company": fx.company("supplier", f"Cauvery Mills {name} Pvt Ltd").name, "supplier_mail": f"sales.{name}@cauvery-{name}.invalid",
        "supplier_ask": supplier,
    }
    deal = await create_deal(CreateDealRequest(operator_id="qa", hs_code=hs, market=market, margin_floor_pct=8.0, parties=[
        Party(role=PartyRole.BUYER, legal_name=who["buyer_company"], country=market, email=who["buyer_mail"],
              address="1 Test Road, Dubai", phone="+0000000001", sanctions_status="clear", sanctions_checked_at=now),
        Party(role=PartyRole.SUPPLIER, legal_name=who["supplier_company"], country="IN", email=who["supplier_mail"],
              address="2 Sample Street, Chennai", phone="+0000000002", gstin="00AAAAA0000A0Z0", iec="0000000000",
              sanctions_status="clear", sanctions_checked_at=now),
    ]), org_id="qa-org-a")
    deal_id = deal.deal_id
    await update_status(deal_id, UpdateStatusRequest(target_status=DealStatus.MATCHING, reason="use case"))
    record = await get_deal(deal_id)
    match = MatchResult(match_id=f"M-{name}", supplier_id=f"SUP-{name}", company_name=who["supplier_company"],
                        composite_score=85.0, gatekeeper_passed=True, arbitrage_score=80.0, proximity_score=80.0,
                        reliability_score=80.0, decision="SOURCE", fob_price_usd=supplier)
    await deals_repo().save(record.model_copy(update={
        "match_results": [match], "winning_supplier_id": f"SUP-{name}", "quantity_mt": qty, "buyer_target_price_usd": buyer,
    }))
    await settle()
    os.environ["EVENT_CONTINUATION_ENABLED"] = "true"
    note(f"\n\n# Use case {name}\n\nDeal `{deal_id}` · buyer target {buyer} · supplier ask {supplier} · {qty} MT\n")
    await update_status(deal_id, UpdateStatusRequest(target_status=DealStatus.NEGOTIATING, reason="use case"))
    await slow_settle()
    session = await neg_repo().get_session_by_deal(deal_id)
    who["negotiation_id"] = session.negotiation_id if session else None
    return deal_id, who


async def drafts(who) -> list:
    return await get_email_repository().list_by_negotiation(who["negotiation_id"])


async def send_pending_drafts(deal_id: str, who, decide="APPROVE") -> int:
    """Operator handles every waiting e-mail draft. Mail stops at the log."""
    count = 0
    for _ in range(4):
        gates = await pending(deal_id, "SEND_NEGOTIATION_EMAIL")
        if not gates:
            return count
        for gate in gates:
            record = next((e for e in await drafts(who) if e.email_id == gate.callback_payload.get("email_id")), None)
            if record is not None:
                note(f"\n**PortLess → {record.to_address}** (LLM draft, gate {gate.autonomy_level}, "
                     f"flags {gate.risk_flags}, decision {decide})\n\n> {record.subject}\n\n{record.body}\n")
            await resolve_approval(gate.approval_id, ResolveApprovalRequest(decision=decide, resolved_by="operator@qa.invalid"))
            count += 1
            await slow_settle()
    return count


async def last_sent(who, party: str):
    sent = [e for e in await drafts(who)
            if str(getattr(e.status, "value", e.status)) in ("sent", "modified_sent", "logged_only") and e.party == party
            and str(getattr(e.direction, "value", e.direction)).lower() == "outbound"]
    return sent[-1] if sent else None


async def reply(deal_id: str, who, party: str, instruction: str, from_address: str | None = None):
    role = "purchasing manager" if party == "buyer" else "export sales manager"
    company = who["buyer_company"] if party == "buyer" else who["supplier_company"]
    known = who["buyer_mail"] if party == "buyer" else who["supplier_mail"]
    last = await last_sent(who, party)
    thread = last.body if last else ""
    subject = last.subject if last else "Your e-mail"
    body = await persona_reply(role, company, thread, instruction)
    sender = from_address or known
    note(f"\n**{sender} → PortLess** (LLM playing the {party}; instruction: {instruction})\n\n{body}\n")
    result = await process_inbound(InboundRawEmail(
        from_address=sender, to_address="trade@portless-qa.invalid",
        subject=f"Re: {subject} [NEG:{who['negotiation_id']}]", body_text=body,
        message_id=f"<{uuid.uuid4().hex}@qa.invalid>", source=InboundEmailSource.IMAP,
        received_at=datetime.now(timezone.utc),
    ))
    await slow_settle()
    note(f"\n_System: {result.status.value} — {result.message[:220]}_\n")
    say(f"   inbound from {party}: {result.status.value} — {result.message[:130]}")
    return result, body


def leak_free(text: str, who, party: str) -> list[str]:
    ask = who.get("supplier_ask", 1000.0)
    names = [who["supplier_company"], who["supplier_mail"]] if party == "buyer" else [who["buyer_company"], who["buyer_mail"]]
    found = [w for w in names if w.lower() in text.lower()]
    if party == "buyer":
        # The supplier's price as a whole amount: "900" or "900.00", never the tail of "4,900.00".
        for amount in dict.fromkeys((f"{ask:,.0f}", f"{ask:.0f}")):
            if re.search(rf"(?<![\d,.]){re.escape(amount)}(?![\d,]|\.\d*[1-9])", text):
                found.append(amount)
    return found


async def assert_outbound_clean(s: str, who):
    outbound = [e for e in await drafts(who)
                if str(getattr(e.direction, "value", e.direction)).lower() == "outbound" and e.body]
    leaks = [(e.party, leak_free(e.body, who, e.party)) for e in outbound if leak_free(e.body, who, e.party)]
    check(s, f"{len(outbound)} drafted e-mail(s) name nothing of the other side", not leaks, str(leaks)[:300])
    junk = [e.email_id for e in outbound if "**" in e.body or e.body.lstrip().lower().startswith("subject:") or len(e.body) < 40]
    check(s, "drafts are clean plain text", not junk, str(junk))


async def session_of(who):
    return await neg_repo().get_session(who["negotiation_id"])


async def uc_whole_deal():
    s = "UC-01"
    say(f"\n{'=' * 78}\n{s}: LLM negotiation, counter-offers, agreement, then the whole deal to cash\n{'=' * 78}")
    # Real economics: with every PortLess cost (freight, inspection, customs, documents, insurance,
    # escrow fee) a 900-910 FOB rice lot sells profitably near 1,225 CIF; 1,000-1,010 would not clear 8 %.
    deal_id, who = await open_negotiation("uc01", supplier=900.0)
    check(s, "opening the deal drafts a first e-mail for approval", bool(await pending(deal_id, "SEND_NEGOTIATION_EMAIL")),
          str([g.autonomy_level for g in await pending(deal_id)]))
    check(s, "the first e-mail waits for a human (L0)", all(g.autonomy_level == "L0" for g in await pending(deal_id, "SEND_NEGOTIATION_EMAIL")))
    before = len(base.EMAILS)
    await send_pending_drafts(deal_id, who)
    check(s, "the approved draft is composed, never delivered", len(base.EMAILS) > before and all(e["to"].endswith(".invalid") for e in base.EMAILS[before:]))

    plan = [
        ("buyer", "Say the price is too high for your market and counter at USD 1,200 per MT for 20 MT. Our destination port is Jebel Ali, UAE — repeat that sentence word for word."),
        ("supplier", "Say you can improve slightly and offer USD 910 per MT FOB Chennai."),
        ("buyer", "Say you will meet them closer and counter at USD 1,225 per MT."),
    ]
    agreed_gate = False
    for party, instruction in plan:
        # A party can only answer an e-mail PortLess actually sent it. Never
        # put one party's words on the other party's thread: the system would
        # read the supplier's price as the buyer's (or the reverse).
        written_to = bool(await last_sent(who, party))
        check(s, f"PortLess wrote to the {party} before the {party} replies", written_to)
        if not written_to:
            continue
        await reply(deal_id, who, party, instruction)
        await send_pending_drafts(deal_id, who)
        if await pending(deal_id, "CONFIRM_NEGOTIATION_AGREEMENT"):
            agreed_gate = True
            break
    if not agreed_gate:
        await reply(deal_id, who, "buyer", "Our destination port is Jebel Ali, UAE. Say clearly: we accept your offer and your terms and wish to proceed. Say that the goods must be packed in 25 kg PP woven bags with our company name printed on each bag. Do NOT write any price in your reply, and repeat the sentences about the destination port and the packing word for word.")
        await send_pending_drafts(deal_id, who)
    session = await session_of(who)
    say(f"   negotiation: status={session.status} round={session.round_no} bid={session.current_bid} ask={session.current_ask} agreed={session.agreed_price}")
    gates = await pending(deal_id, "CONFIRM_NEGOTIATION_AGREEMENT")
    check(s, "acceptance waits for the operator; the deal has not moved", bool(gates) and await status(deal_id) == "NEGOTIATING",
          f"{await status(deal_id)}; negotiation {session.status}")
    await assert_outbound_clean(s, who)
    if not gates:
        return
    await approve(deal_id, "CONFIRM_NEGOTIATION_AGREEMENT", by="operator@qa.invalid")
    await slow_settle()
    await approve(deal_id, "CONFIRM_TRADE_COMPLIANCE", by="operator@qa.invalid")
    await slow_settle()

    # The supplier commits before the buyer gets a PI.
    from shared.orchestration import supplier_commitment
    commitment = await supplier_commitment.get(deal_id)
    request_mail = next((e for e in base.EMAILS if e["to"] == who["supplier_mail"] and "Order confirmation" in e["subject"]), None)
    check(s, "the buyer's agreement first sends the supplier a firm order request, no PI yet",
          commitment is not None and commitment.status == "requested" and request_mail is not None
          and await status(deal_id) == "NEGOTIATION_AGREED", f"{await status(deal_id)}; {commitment.status if commitment else None}")
    if request_mail:
        note(f"\n**PortLess → {request_mail['to']}** (template)\n\n> {request_mail['subject']}\n\n{request_mail['body']}\n")
    agreed_session = await session_of(who)
    check(s, "the buyer's packing was read from its own e-mail",
          bool(agreed_session.packing) and agreed_session.packing.get("unit_weight_kg") == 25,
          str(agreed_session.packing))
    check(s, "the supplier's order request states the buyer's packing and asks for a price including it",
          bool(request_mail) and "25 kg" in request_mail["body"] and "Your price must include this packing" in request_mail["body"],
          (request_mail or {}).get("body", "")[:300])
    if commitment is None or commitment.current is None:
        return
    asked = commitment.current.price_usd_per_mt
    check(s, "the order request is at the supplier's own stated price", abs(asked - ((await session_of(who)).supplier_last_quote or 0)) < 0.01,
          f"asked {asked}")
    await reply(deal_id, who, "supplier", f"Say clearly: we confirm the order at USD {asked:,.0f} per MT FOB. "
                                          "Port of loading Chennai, ready 25 days after the purchase order.")
    check(s, "the supplier's confirmation needs an operator's approval", bool(await pending(deal_id, "CONFIRM_SUPPLIER_ORDER")),
          str([g.action for g in await pending(deal_id)]))
    await approve(deal_id, "CONFIRM_SUPPLIER_ORDER", by="operator@qa.invalid")
    await slow_settle()
    check(s, "agreement leads to the PI", await status(deal_id) == "PI_SENT", await status(deal_id))
    pi = await base.dm_repo().get_pi_by_deal(deal_id)
    session = await session_of(who)
    check(s, "PI price is the agreed price", pi is not None and abs(pi.agreed_price - session.agreed_price) < 0.01,
          f"PI {pi.agreed_price if pi else None}, agreed {session.agreed_price}")
    check(s, "the PI prints the port the buyer named", pi is not None and pi.port_of_destination == "Jebel Ali, AE", pi.port_of_destination if pi else None)
    premature = [e.email_id for e in await drafts(who) if e.party == "supplier" and str(getattr(e.direction, "value", e.direction)).lower() == "outbound"
                 and __import__("agents.negotiation_agent.service", fromlist=["x"]).claims_funds_already_held(e.body)
                 and str(getattr(e.status, "value", e.status)).lower() not in ("rejected",)]
    say(f"   supplier drafts claiming escrow money is already there: {len(premature)}")
    pi_mail = next((e for e in base.EMAILS if e["to"] == who["buyer_mail"] and "Proforma Invoice" in e["subject"]), None)
    if pi_mail:
        note(f"\n**PortLess → {pi_mail['to']}** (template)\n\n> {pi_mail['subject']}\n\n{pi_mail['body']}\n")

    await reply(deal_id, who, "buyer", "Reply exactly in substance: We accept the Proforma Invoice. Do not add conditions.")
    check(s, "the buyer's e-mailed PI acceptance needs confirmation", bool(await pending(deal_id, "CONFIRM_PI_ACCEPTANCE")) and await status(deal_id) == "PI_SENT",
          await status(deal_id))
    await approve(deal_id, "CONFIRM_PI_ACCEPTANCE", by="operator@qa.invalid")
    await slow_settle()
    check(s, "escrow opens at the PI value", await status(deal_id) == "ESCROW_PENDING", await status(deal_id))
    hold = await eg_repo().get_hold_by_deal(deal_id)
    await base.fund(deal_id, hold.amount, who["buyer_company"], "AE", f"ev-{s}")
    await slow_settle()
    po_gate = (await pending(deal_id, "ISSUE_PO"))
    final = await session_of(who)
    stated = final.supplier_last_quote or final.supplier_quote_price
    check(s, "the PO price is one the supplier itself stated", bool(po_gate) and abs(po_gate[0].callback_payload["agreed_price"] - stated) < 0.01,
          f"PO {po_gate[0].callback_payload['agreed_price'] if po_gate else None}, supplier stated {stated}, our last counter {final.current_ask}")
    # Tazapay does not report the paying account, so a person confirms the payer
    # (payer_unknown); Escrow.com takes money only from the buyer's account.
    payer_flags = [f for f in (po_gate[0].risk_flags if po_gate else []) if "payer_" in f]
    allowed = {"compliance:payer_unknown"} if hold.provider == "tazapay" else set()
    check(s, "funding by the buyer raises no payer mismatch", bool(po_gate) and set(payer_flags) <= allowed,
          f"{hold.provider}: {payer_flags if po_gate else 'no gate'}")

    ready_on = datetime.now(timezone.utc).date() + timedelta(days=12)
    ready_text = f"{ready_on.day} {ready_on:%B %Y}"

    async def supplier_acknowledges_po():
        await reply(deal_id, who, "supplier",
                    "Say clearly: we acknowledge the purchase order and will start production on Monday. Also say "
                    f"that the goods will be fully produced and packed, ready for inspection on {ready_text} — write "
                    "that date exactly as given.")

    await base.to_qc_ready(s, deal_id, supplier_ack=supplier_acknowledges_po)
    from shared.orchestration import supplier_commitment as _sc
    rec = await _sc.get(deal_id)
    check(s, "the supplier's own e-mail gave the goods-ready date the inspection is booked for",
          bool(rec and rec.qc_ready_date == ready_on.isoformat() and "e-mail" in (rec.qc_ready_by or "")),
          f"ready {rec.qc_ready_date if rec else None} by {rec.qc_ready_by if rec else None}")
    await base.to_in_transit(s, deal_id)
    await base.milestone(deal_id, "ARRIVED")
    await slow_settle()
    await approve(deal_id, "RELEASE_ESCROW_TRANCHE", by="operator@qa.invalid")
    for _ in range(5):
        if not await approve(deal_id, "RELEASE_SUPPLIER_PAYOUT", by="operator@qa.invalid"):
            break
        await base.complete_payouts(deal_id)
    await slow_settle()
    check(s, "supplier is not fully paid before the buyer accepts", await status(deal_id) == "CLEARED", await status(deal_id))
    await base.milestone(deal_id, "DELIVERED")
    await slow_settle()
    await approve(deal_id, "SUBMIT_VGM", by="operator@qa.invalid")
    await approve(deal_id, "APPROVE_TRANSPORT_DOCUMENT", by="operator@qa.invalid")
    await reply(deal_id, who, "buyer", "Say the cargo arrived in good condition and state plainly: we accept the goods.")
    check(s, "acceptance of the goods needs confirmation", bool(await pending(deal_id, "CONFIRM_BUYER_ACCEPTANCE")),
          str([g.action for g in await pending(deal_id)]))
    await approve(deal_id, "CONFIRM_BUYER_ACCEPTANCE", by="operator@qa.invalid")
    await slow_settle()
    for _ in range(5):
        if not await approve(deal_id, "RELEASE_SUPPLIER_PAYOUT", by="operator@qa.invalid"):
            break
        await base.complete_payouts(deal_id)
    await slow_settle()
    final = await status(deal_id)
    ledger = await base.eg.get_deal_ledger(deal_id)
    check(s, "deal ends PAID with the books reconciled", final == "PAID" and ledger.reconciled, f"{final}, reconciled={ledger.reconciled}")
    check(s, "supplier received exactly the PO value", abs(ledger.payouts_total - ledger.supplier_owed) < 0.01,
          f"paid {ledger.payouts_total} of {ledger.supplier_owed}; margin {ledger.realized_margin}")
    check(s, "no gate left waiting", not await pending(deal_id), str([g.action for g in await pending(deal_id)]))
    say(f"   final: PI {pi.total_value}, supplier {ledger.supplier_owed}, fees {ledger.fees_total}, margin {ledger.realized_margin}")


async def negotiated(name: str, first_party="buyer"):
    deal_id, who = await open_negotiation(name)
    await send_pending_drafts(deal_id, who)
    return deal_id, who


async def uc_question():
    s = "UC-02"
    say(f"\n{s}: buyer asks a question and names no price")
    deal_id, who = await negotiated("uc02")
    round_before = (await session_of(who)).round_no
    result, _ = await reply(deal_id, who, "buyer", "Ask what moisture level and packaging you offer. Do NOT mention any number, price or quantity.")
    check(s, "a reply with no price goes to a human", result.status.value == "needs_operator" and bool(await pending(deal_id, "PARSE_INBOUND_EMAIL")), result.status.value)
    check(s, "the round does not advance", (await session_of(who)).round_no == round_before)


async def uc_decline():
    s = "UC-03"
    say(f"\n{s}: buyer walks away")
    deal_id, who = await negotiated("uc03")
    await reply(deal_id, who, "buyer", "Politely say you are not interested and have bought elsewhere. Do NOT mention any number or price.")
    check(s, "a refusal never becomes an agreement", not await pending(deal_id, "CONFIRM_NEGOTIATION_AGREEMENT") and await status(deal_id) in ("NEGOTIATING", "NEGOTIATION_FAILED"), await status(deal_id))


async def uc_stranger():
    s = "UC-04"
    say(f"\n{s}: a stranger answers on the buyer's thread with an acceptance")
    deal_id, who = await negotiated("uc04")
    round_before = (await session_of(who)).round_no
    result, _ = await reply(deal_id, who, "buyer", "Say clearly we accept your price of USD 1,250 per MT and want to proceed.",
                            from_address="someone@unrelated-trader.invalid")
    check(s, "an unknown sender is held for a human", result.status.value == "needs_operator", result.status.value)
    gates = await pending(deal_id, "PARSE_INBOUND_EMAIL")
    check(s, "the gate says the sender is not verified", bool(gates) and "sender_not_verified" in gates[0].risk_flags, str(gates[0].risk_flags if gates else ""))
    check(s, "no agreement and no round advance", not await pending(deal_id, "CONFIRM_NEGOTIATION_AGREEMENT") and (await session_of(who)).round_no == round_before)


async def uc_other_side():
    s = "UC-05"
    say(f"\n{s}: the supplier writes on the buyer's thread")
    deal_id, who = await negotiated("uc05")
    last = await last_sent(who, "buyer")
    check(s, "the opening went to the buyer", last is not None)
    result, _ = await reply(deal_id, who, "buyer", "Say you can do USD 1,100 per MT.", from_address=who["supplier_mail"])
    check(s, "the other party's mail on this thread is held", result.status.value == "needs_operator", result.status.value)


async def uc_reject_draft():
    s = "UC-06"
    say(f"\n{s}: the operator rejects the LLM draft")
    deal_id, who = await open_negotiation("uc06")
    before = len(base.EMAILS)
    await send_pending_drafts(deal_id, who, decide="REJECT")
    await slow_settle()
    statuses = [str(getattr(e.status, "value", e.status)) for e in await drafts(who)]
    check(s, "a rejected draft is not composed for sending", len(base.EMAILS) == before and "rejected" in statuses, str(statuses))
    check(s, "no deal exception on a soft reject", not await base.exceptions_of(deal_id))


async def uc_absurd_price():
    s = "UC-07"
    say(f"\n{s}: a reply carries an absurd price")
    deal_id, who = await negotiated("uc07")
    session = await session_of(who)
    bid = session.current_bid
    await reply(deal_id, who, "buyer", "Say your offer is USD 95,000 per MT.")
    session = await session_of(who)
    check(s, "the absurd price is not taken as the bid", session.current_bid == bid or session.current_bid < 5000, f"bid {session.current_bid}")
    check(s, "it stays with a human", session.hitl_required or bool(await pending(deal_id)), f"status {session.status}")


async def uc_below_floor():
    s = "UC-08"
    say(f"\n{s}: the buyer offers below the margin floor and calls it final")
    deal_id, who = await negotiated("uc08")
    await reply(deal_id, who, "buyer", "Say USD 1,010 per MT is your final price and you accept the deal at that price.")
    await send_pending_drafts(deal_id, who)
    session = await session_of(who)
    agreed = await pending(deal_id, "CONFIRM_NEGOTIATION_AGREEMENT")
    price = agreed[0].callback_payload.get("agreed_price") if agreed else None
    check(s, "no agreement below the margin floor", price is None or (price - 1000) / price * 100 >= 8.0, f"gate price {price}; negotiation {session.status}")


async def uc_hesitant():
    s = "UC-09"
    say(f"\n{s}: a warm but non-committal reply")
    deal_id, who = await negotiated("uc09")
    await reply(deal_id, who, "buyer", "Say the price of USD 1,250 per MT looks reasonable but you must check with your director first and will revert. Do not accept.")
    check(s, "interest is not read as acceptance", not await pending(deal_id, "CONFIRM_NEGOTIATION_AGREEMENT"), str([g.action for g in await pending(deal_id)]))


async def post_agreement(name: str, status_target: str):
    deal_id = await base.seed(name, 1250, 1000)
    await base.to_pi_sent(name, deal_id)
    company, mail = f"Gulf Staples Trading {name} LLC", f"buyer.{name}@gulfstaples-{name}.invalid"
    session = await neg_repo().get_session_by_deal(deal_id)
    who = {"name": name, "buyer_company": company, "buyer_mail": mail, "supplier_company": f"Cauvery Rice Mills {name} Pvt Ltd",
           "supplier_mail": f"sales.{name}@cauverymills-{name}.invalid", "negotiation_id": session.negotiation_id}
    note(f"\n\n# Use case {name}\n\nDeal `{deal_id}` (negotiation already agreed)\n")
    if status_target == "CLEARED":
        await base.to_funded(name, deal_id)
        await base.to_qc_ready(name, deal_id)
        await base.to_in_transit(name, deal_id)
        await base.milestone(deal_id, "ARRIVED")
        await approve(deal_id, "RELEASE_ESCROW_TRANCHE")
        await base.milestone(deal_id, "DELIVERED")
        await slow_settle()
    return deal_id, who


async def uc_conditional_pi():
    s = "uc10"
    say(f"\nUC-10: a conditional answer to the Proforma Invoice")
    deal_id, who = await post_agreement(s, "PI_SENT")
    await reply(deal_id, who, "buyer", "Say you would accept the Proforma Invoice only if the delivery date moves two weeks earlier.")
    check("UC-10", "a conditional answer is not acceptance", not await pending(deal_id, "CONFIRM_PI_ACCEPTANCE") and await status(deal_id) == "PI_SENT", await status(deal_id))
    check("UC-10", "a human reads it", bool(await pending(deal_id, "PARSE_INBOUND_EMAIL")), str([g.action for g in await pending(deal_id)]))


async def uc_complaint():
    s = "uc11"
    say(f"\nUC-11: buyer complains about the delivered goods")
    deal_id, who = await post_agreement(s, "CLEARED")
    await reply(deal_id, who, "buyer", "Say part of the cargo arrived damaged and wet, and you dispute the shipment.")
    hold = await eg_repo().get_hold_by_deal(deal_id)
    check("UC-11", "a complaint freezes the final tranche", hold.disputed and next(t for t in hold.tranches if t.trigger == "ACCEPTED").status == "PENDING", f"disputed={hold.disputed}")
    gates = await pending(deal_id, "PARSE_INBOUND_EMAIL")
    check("UC-11", "the complaint reaches a human", bool(gates) and "buyer_complaint" in gates[0].risk_flags, str(gates[0].risk_flags if gates else ""))
    check("UC-11", "no acceptance gate on a complaint", not await pending(deal_id, "CONFIRM_BUYER_ACCEPTANCE"))


async def uc_bypass_attempts():
    """The fixes, end to end, on a real deal: a staff member tries to go around the gates."""
    from fastapi.testclient import TestClient
    from services.escrow_guard.router import router as escrow_router
    from services.deals.router import router as deals_router
    from fastapi import FastAPI
    from shared.auth.dependencies import AuthUser, get_current_user
    import httpx

    s = "UC-12"
    say(f"\n{s}: a staff member tries to go around the gates on a funded deal")
    deal_id = await base.seed("uc12", 1250, 1000)
    await base.to_pi_sent(s, deal_id)
    await base.to_funded(s, deal_id)
    await base.to_qc_ready(s, deal_id)
    await base.to_in_transit(s, deal_id)
    await base.milestone(deal_id, "ARRIVED")
    hold = await eg_repo().get_hold_by_deal(deal_id)
    app = FastAPI()
    app.include_router(escrow_router)
    app.include_router(deals_router)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id="U", email="staff@qa.invalid", is_portless=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sandbox") as api:
        release = await api.post(f"/escrow-guard/escrows/{hold.escrow_id}/release", json={"tranche_id": "T3", "proof_refs": ["x"]})
        check(s, "direct tranche release is refused", release.status_code == 403, str(release.status_code))
        payout = await api.post("/escrow-guard/payouts", json={"deal_id": deal_id, "payee_id": "S", "amount": 1000, "idempotency_key": "bypass"})
        check(s, "direct payout is refused", payout.status_code == 403, str(payout.status_code))
        accept = await api.post(f"/escrow-guard/escrows/{hold.escrow_id}/accept", json={})
        check(s, "direct acceptance is refused", accept.status_code == 403, str(accept.status_code))
        paid = await api.patch(f"/deals/{deal_id}/status", json={"target_status": "PAID"})
        check(s, "marking the deal PAID by hand is refused", paid.status_code == 422 and await status(deal_id) == "CLEARED", f"{paid.status_code}, {await status(deal_id)}")
        refund = await api.post(f"/escrow-guard/escrows/{hold.escrow_id}/refund", json={"reason": "x"})
        check(s, "direct refund is refused", refund.status_code == 403, str(refund.status_code))
        relay = await api.post(f"/escrow-guard/escrows/{hold.escrow_id}/request-acceptance", json={"notes": "buyer phoned"})
        check(s, "relaying acceptance opens a gate and moves nothing", relay.status_code == 202 and bool(await pending(deal_id, "CONFIRM_BUYER_ACCEPTANCE")), str(relay.status_code))
    await approve(deal_id, "RELEASE_ESCROW_TRANCHE")
    hold = await eg_repo().get_hold_by_deal(deal_id)
    check(s, "the normal gate still releases", next(t for t in hold.tranches if t.trigger == "CLEARED").status == "RELEASED")
    from shared.orchestration import kill_switch
    from services.escrow_guard.schemas import RefundRequest
    await kill_switch.pause("staff@qa.invalid", "use-case check")
    sent_before = len(base.EMAILS)
    waiting = await pending(deal_id, "RELEASE_SUPPLIER_PAYOUT")
    refused = False
    try:
        await base.eg.refund(hold.escrow_id, RefundRequest(reason="x"))
    except kill_switch.PlatformPaused:
        refused = True
    check(s, "while paused a refund is refused", refused)
    check(s, "while paused nothing was e-mailed", len(base.EMAILS) == sent_before)
    await kill_switch.resume("staff@qa.invalid", "use-case check done")
    check(s, "after resume the waiting gates are still there", bool(waiting) and bool(await pending(deal_id, "RELEASE_SUPPLIER_PAYOUT")))
    opened = await base.eg.request_refund(hold.escrow_id, RefundRequest(reason="use-case check", requested_by="staff@qa.invalid"))
    check(s, "a refund request opens a gate and moves nothing", bool(await pending(deal_id, "REFUND_ESCROW")) and (await eg_repo().get_hold_by_deal(deal_id)).refunded_total == 0, str(opened))


# ---------------------------------------------------------------------------
# Supplier commitment: the firm order request, the standby supplier, nobody left
# ---------------------------------------------------------------------------

async def agreed_with_standbys(name: str, standbys: list[dict[str, Any]]) -> tuple[str, dict, dict[str, dict]]:
    """A negotiation agreed at 1,250 with the first supplier at 1,000, plus standby
    suppliers (Match Engine SOURCE results, best first, and their received quotes).
    Returns the deal, the ``who`` map for replies, and every supplier by key."""
    from services.deals.schemas import SupplierOffer
    deal_id = await base.seed(name, 1250, 1000)
    first = {"key": "A", "supplier_id": f"SUP-{name}", "price": 1000.0,
             "company": f"Cauvery Rice Mills {name} Pvt Ltd", "mail": f"sales.{name}@cauverymills-{name}.invalid"}
    suppliers = {"A": first}
    matches, offers = [], []
    for row in standbys:
        sid = f"SUP-{name}-{row['key']}"
        mail = f"export.{name}{row['key'].lower()}@{row['domain']}-{name}.invalid" if row.get("mail", True) else None
        suppliers[row["key"]] = {"key": row["key"], "supplier_id": sid, "price": row["price"], "company": row["company"], "mail": mail}
        matches.append(MatchResult(match_id=f"M-{sid}", supplier_id=sid, company_name=row["company"],
                                   composite_score=row["score"], gatekeeper_passed=True, arbitrage_score=80.0,
                                   proximity_score=80.0, reliability_score=80.0, decision="SOURCE", fob_price_usd=row["price"]))
        if mail:
            offers.append(SupplierOffer(offer_id=f"OFF-{sid}", supplier_id=sid, company_name=row["company"], email=mail,
                                        fob_price_usd_per_mt=row["price"], gst_verified=True, iec_active=True,
                                        source="quote", received_at=datetime.now(timezone.utc)))
    record = await get_deal(deal_id)
    await deals_repo().save(record.model_copy(update={"match_results": matches, "supplier_offers": offers}))
    note(f"\n\n# Use case {name}\n\nDeal `{deal_id}` agreed at 1,250 with {first['company']} (1,000). Standby suppliers: "
         + ", ".join(f"{s['company']} {s['price']:,.0f}{'' if s['mail'] else ' (no e-mail)'}" for k, s in suppliers.items() if k != "A") + "\n")
    await approve(deal_id, "CONFIRM_COUNTERPARTY_RISK")
    await approve(deal_id, "CONFIRM_TRADE_COMPLIANCE")
    await slow_settle()
    session = await neg_repo().get_session_by_deal(deal_id)
    who = {"name": name, "buyer_company": f"Gulf Staples Trading {name} LLC", "buyer_mail": f"buyer.{name}@gulfstaples-{name}.invalid",
           "supplier_company": first["company"], "supplier_mail": first["mail"], "supplier_ask": 1000.0,
           "negotiation_id": session.negotiation_id if session else None}
    return deal_id, who, suppliers


def as_supplier(who: dict, supplier: dict) -> dict:
    """The ``who`` map with one supplier speaking (its company, its mailbox)."""
    return {**who, "supplier_company": supplier["company"], "supplier_mail": supplier["mail"], "supplier_ask": supplier["price"]}


def order_requests(who: dict) -> list[dict[str, Any]]:
    """Firm order requests composed for this deal's suppliers, oldest first."""
    deal_ref = who["name"]
    return [e for e in base.EMAILS if "Order confirmation" in e.get("subject", "") and deal_ref in e.get("to", "")]


async def commitment_of(deal_id: str):
    from shared.orchestration import supplier_commitment
    return await supplier_commitment.get(deal_id)


async def holder(deal_id: str) -> str | None:
    rec = await commitment_of(deal_id)
    return rec.current.supplier_id if rec is not None and rec.current is not None and rec.status == "requested" else None


def requests_clean(s: str, who: dict, mails: list[dict[str, Any]]) -> None:
    leaks = [m["to"] for m in mails if any(w.lower() in m["body"].lower() for w in (who["buyer_company"], who["buyer_mail"], "1,250"))]
    check(s, "no order request names the buyer or the buyer's price", not leaks, str(leaks))


async def uc_supplier_declines():
    s = "UC-13"
    say(f"\n{s}: the supplier declines the firm order — standby suppliers until one confirms")
    deal_id, who, sup = await agreed_with_standbys("uc13", [
        {"key": "B", "company": "Kaveri Agro Exports uc13", "domain": "kaveriagro", "price": 1240.0, "score": 90},   # breaks the 8 % floor
        {"key": "C", "company": "Thanjavur Grains uc13", "domain": "tanjoregrains", "price": 900.0, "score": 85},
        {"key": "E", "company": "Delta Paddy Traders uc13", "domain": "deltapaddy", "price": 880.0, "score": 80, "mail": False},
        {"key": "D", "company": "Madurai Rice Exports uc13", "domain": "madurairice", "price": 890.0, "score": 75},
    ])
    A, B, C, D, E = (sup[k] for k in "ABCDE")
    rec = await commitment_of(deal_id)
    check(s, "the buyer's agreement sends the chosen supplier a firm order request, no PI yet",
          rec is not None and await holder(deal_id) == A["supplier_id"] and await status(deal_id) == "NEGOTIATION_AGREED",
          f"{await status(deal_id)}; holder {await holder(deal_id)}")
    if rec is None:
        return

    # 1. The chosen supplier declines → the best standby that keeps the margin is asked.
    await reply(deal_id, as_supplier(who, A), "supplier",
                "Say you regret that you cannot supply this order now because your stock is already committed. "
                "Do NOT use the words confirm, accept, agree or proceed, and do not mention any price.")
    rec = await commitment_of(deal_id)
    skipped = {x["supplier_id"]: x["reason"] for x in rec.skipped}
    check(s, "the decline is recorded against the first supplier", rec.attempts[0].outcome == "declined", rec.attempts[0].outcome)
    check(s, "the standby whose price breaks the margin floor is skipped, with the reason", "floor" in skipped.get(B["supplier_id"], ""), str(skipped))
    check(s, "the best standby that keeps the margin now holds the request", await holder(deal_id) == C["supplier_id"], str(await holder(deal_id)))
    deal = await get_deal(deal_id)
    party = next((p for p in deal.parties if (getattr(p.role, "value", p.role) or "").lower() == "supplier"), None)
    session = await neg_repo().get_session_by_deal(deal_id)
    check(s, "the deal's supplier and the negotiation's seller switch to the standby",
          party is not None and party.party_id == C["supplier_id"] and session.seller_email == C["mail"],
          f"party {party.party_id if party else None}, seller {session.seller_email}")
    check(s, "no PI while the standby has not confirmed", await status(deal_id) == "NEGOTIATION_AGREED"
          and await base.dm_repo().get_pi_by_deal(deal_id) is None, await status(deal_id))

    # 2. The first supplier changes its mind after the order moved: a person decides.
    result, _ = await reply(deal_id, as_supplier(who, A), "supplier",
                            "Say you have reconsidered and state clearly: we confirm the order at USD 1,000 per MT FOB.",
                            from_address=A["mail"])
    late = await pending(deal_id, "PARSE_INBOUND_EMAIL")
    check(s, "a late confirmation from the replaced supplier opens no order confirmation",
          not await pending(deal_id, "CONFIRM_SUPPLIER_ORDER") and await holder(deal_id) == C["supplier_id"],
          f"holder {await holder(deal_id)}; gates {[g.action for g in await pending(deal_id)]}")
    check(s, "it goes to an operator, marked as the replaced supplier",
          result.status.value == "needs_operator" and any("supplier_replaced" in g.risk_flags or "sender_not_verified" in g.risk_flags for g in late),
          f"{result.status.value}; flags {[g.risk_flags for g in late]}")
    for gate in late:  # the operator reads it and closes it
        await resolve_approval(gate.approval_id, ResolveApprovalRequest(decision="REJECT", resolved_by="operator@qa.invalid"))
    await settle()

    # 3. The standby declines too → a standby with no address is passed over → the next one is asked.
    await reply(deal_id, as_supplier(who, C), "supplier",
                "Say you regret you cannot take this order this month because your mill is fully booked. "
                "Do NOT use the words confirm, accept, agree or proceed, and do not mention any price.")
    rec = await commitment_of(deal_id)
    skipped = {x["supplier_id"]: x["reason"] for x in rec.skipped}
    check(s, "the second decline moves on again", [a.outcome for a in rec.attempts][:2] == ["declined", "declined"],
          str([a.outcome for a in rec.attempts]))
    check(s, "a standby with no e-mail address is skipped", "e-mail" in skipped.get(E["supplier_id"], ""), str(skipped))
    check(s, "the third supplier holds the request", await holder(deal_id) == D["supplier_id"], str(await holder(deal_id)))

    # 4. The third supplier confirms → the operator approves → the PI goes out.
    asked = rec.current.price_usd_per_mt if rec.current else None
    check(s, "each request is at that supplier's own price", asked == D["price"], f"asked {asked}")
    await reply(deal_id, as_supplier(who, D), "supplier",
                f"Say clearly: we confirm the order at USD {D['price']:,.0f} per MT FOB. Port of loading Chennai, ready 25 days after the purchase order.")
    gates = await pending(deal_id, "CONFIRM_SUPPLIER_ORDER")
    check(s, "the standby's confirmation needs an operator's approval",
          bool(gates) and gates[0].callback_payload.get("supplier_id") == D["supplier_id"], str([g.callback_payload for g in gates]))
    await approve(deal_id, "CONFIRM_SUPPLIER_ORDER", by="operator@qa.invalid")
    await slow_settle()
    # A newly named supplier is screened again before the PI; the operator clears what it raises.
    await approve(deal_id, "CONFIRM_TRADE_COMPLIANCE", by="operator@qa.invalid")
    await approve(deal_id, "CONFIRM_COUNTERPARTY_RISK", by="operator@qa.invalid")
    await slow_settle()
    rec = await commitment_of(deal_id)
    pi = await base.dm_repo().get_pi_by_deal(deal_id)
    session = await neg_repo().get_session_by_deal(deal_id)
    check(s, "the PI goes out at the price the buyer agreed", await status(deal_id) == "PI_SENT" and pi is not None
          and abs(pi.agreed_price - 1250) < 0.01, f"{await status(deal_id)}; PI {pi.agreed_price if pi else None}")
    check(s, "the committed supplier and price are the standby's (the PO is written at it)",
          rec.status == "confirmed" and rec.confirmed_supplier_id == D["supplier_id"] and rec.confirmed_price == D["price"]
          and session.supplier_last_quote == D["price"],
          f"{rec.status} {rec.confirmed_supplier_id} {rec.confirmed_price}; session {session.supplier_last_quote}")

    mails = order_requests(who)
    sent_to = [m["to"] for m in mails]
    check(s, "order requests went to the first supplier and the two standbys asked, in order — never to a skipped one",
          sent_to == [A["mail"], C["mail"], D["mail"]], str(sent_to))
    check(s, "each request quotes that supplier's own price",
          all(f"{p:,.0f}" in m["body"] for m, p in zip(mails, (A["price"], C["price"], D["price"]))), "")
    requests_clean(s, who, mails)
    for m in mails:
        note(f"\n**PortLess → {m['to']}** (template)\n\n> {m['subject']}\n")


async def uc_supplier_silent():
    s = "UC-14"
    say(f"\n{s}: the supplier stays silent — the standby is asked, then nobody is left")
    from shared.orchestration import supplier_commitment
    deal_id, who, sup = await agreed_with_standbys("uc14", [
        {"key": "B", "company": "Kaveri Agro Exports uc14", "domain": "kaveriagro", "price": 1240.0, "score": 90},   # breaks the 8 % floor
        {"key": "C", "company": "Thanjavur Grains uc14", "domain": "tanjoregrains", "price": 900.0, "score": 85},
    ])
    A, C = sup["A"], sup["C"]
    check(s, "the chosen supplier holds the firm order request", await holder(deal_id) == A["supplier_id"], str(await holder(deal_id)))
    hours = supplier_commitment.confirm_hours()
    now = datetime.now(timezone.utc)

    # 1. Inside the reply window nothing moves; past it the standby is asked.
    await supplier_commitment.sweep(now + timedelta(hours=1))
    await settle()
    check(s, f"an hour of silence changes nothing (window {hours:g} h)", await holder(deal_id) == A["supplier_id"], str(await holder(deal_id)))
    await supplier_commitment.sweep(now + timedelta(hours=hours + 1))
    await slow_settle()
    rec = await commitment_of(deal_id)
    check(s, "silence past the window is recorded as no reply", rec.attempts[0].outcome == "no_reply", rec.attempts[0].outcome)
    check(s, "the standby that keeps the margin is asked", await holder(deal_id) == C["supplier_id"], str(await holder(deal_id)))
    check(s, "the standby that breaks the floor is skipped", any(x["supplier_id"] == sup["B"]["supplier_id"] for x in rec.skipped), str(rec.skipped))

    # 2. The standby is silent too → nobody is left: one exception, the PI stays back.
    await supplier_commitment.sweep(now + timedelta(hours=2 * hours + 2))
    await slow_settle()
    rec = await commitment_of(deal_id)
    excs = [e for e in await base.exceptions_of(deal_id) if "no_confirmation" in str(e.model_dump())]
    check(s, "with every standby tried the request is exhausted", rec.status == "exhausted" and [a.outcome for a in rec.attempts] == ["no_reply", "no_reply"],
          f"{rec.status}; {[a.outcome for a in rec.attempts]}")
    check(s, "exactly one SUPPLIER:no_confirmation exception is raised", len(excs) == 1, str(len(excs)))
    check(s, "no PI is issued and the deal stays agreed", await status(deal_id) == "NEGOTIATION_AGREED"
          and await base.dm_repo().get_pi_by_deal(deal_id) is None, await status(deal_id))

    # 3. Another sweep does not raise it again.
    await supplier_commitment.sweep(now + timedelta(hours=4 * hours))
    await slow_settle()
    excs = [e for e in await base.exceptions_of(deal_id) if "no_confirmation" in str(e.model_dump())]
    check(s, "a later sweep does not raise it twice", len(excs) == 1, str(len(excs)))

    # 4. The standby confirms after the request lapsed: a person decides, no PI.
    result, _ = await reply(deal_id, as_supplier(who, C), "supplier",
                            f"Apologise for the late reply and state clearly: we confirm the order at USD {C['price']:,.0f} per MT FOB.",
                            from_address=C["mail"])
    check(s, "a confirmation after the request lapsed opens no order confirmation and no PI",
          not await pending(deal_id, "CONFIRM_SUPPLIER_ORDER") and await status(deal_id) == "NEGOTIATION_AGREED",
          f"{await status(deal_id)}; gates {[g.action for g in await pending(deal_id)]}")
    check(s, "it goes to an operator", result.status.value == "needs_operator", result.status.value)
    mails = order_requests(who)
    check(s, "order requests went to the first supplier and the standby only", [m["to"] for m in mails] == [A["mail"], C["mail"]],
          str([m["to"] for m in mails]))
    requests_clean(s, who, mails)


def _trust_webhooks() -> None:
    """The journeys hand provider results straight to the services; signatures have their own cases."""
    from shared.connectors import escrow, maersk, payments, qc_providers, shields

    async def verified(*_args: Any, **_kwargs: Any) -> bool:
        return True

    for module in (escrow, maersk, payments, qc_providers, shields):
        for name in dir(module):
            cls = getattr(module, name)
            if isinstance(cls, type) and "webhook_verify" in vars(cls):
                is_async = asyncio.iscoroutinefunction(vars(cls)["webhook_verify"])
                setattr(cls, "webhook_verify", verified if is_async else (lambda *_a, **_k: True))


async def run_all(sink: Any, mail: list[dict[str, Any]], guard: Any, background: set[asyncio.Task], only: str = "") -> None:
    global EMAILS
    _background.clear()
    _background.update(background)
    _trust_webhooks()
    EMAILS = mail
    base.EMAILS = mail
    wanted = [p for p in only.split(",") if p]
    for journey in JOURNEYS:
        if wanted and not any(journey["id"].startswith(p) for p in wanted):
            continue
        CHECKS.clear()
        TRANSCRIPT.clear()
        start = len(mail)
        began = asyncio.get_running_loop().time()
        status, error = "passed", None
        try:
            await globals()[journey["fn"]]()
        except CounterpartyUnavailable as exc:
            status, error = "failed", str(exc)
            say(f"   [STOPPED] {exc}")
        except Exception:  # noqa: BLE001 — one broken journey never stops the run
            status, error = "failed", traceback.format_exc()[-1500:]
        failed = [c for c in CHECKS if not c["ok"]]
        if failed and status == "passed":
            status, error = "failed", "; ".join(f"{c['text']} ({c['detail']})" for c in failed)[:900]
        await sink.result({
            "tc_id": journey["id"], "iteration": 0, "row": {}, "status": status, "error": error,
            "phase": "llm-journeys", "layer": "e2e", "steps": list(CHECKS), "emails": mail[start:][:60],
            "transcript": "\n".join(TRANSCRIPT)[:60000],
            "duration_ms": int((asyncio.get_running_loop().time() - began) * 1000),
        })


# Registered last: it builds on the helpers above.
from .journeys_full import uc_full_flow  # noqa: E402,F401
