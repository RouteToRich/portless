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
from datetime import datetime, timezone
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
    return done


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
    wire = amount * 100 if hold.provider == "stripe" else amount
    res = await eg.handle_escrow_funded_webhook(EscrowFundedWebhookPayload(
        ext_ref=hold.ext_ref, status="FUNDED", amount=wire, currency=hold.currency, event_id=event_id,
        payer_name=payer, payer_country=country,
    ), {}, b"")
    await settle()
    return res


async def to_qc_ready(s: str, deal_id: str, supplier_ack=None) -> None:
    """PO gate → PO → (supplier acknowledges the PO) → QC booked → T1 tranche.
    ``supplier_ack`` plays the supplier's acknowledgement by e-mail; without it
    an operator records the acknowledgement, as the staff endpoint does."""
    say(f"\n[{s}] PO gate → PO → PO acknowledged → QC booked → T1 tranche")
    await approve(deal_id, "ISSUE_PO")
    po = await dm_repo().get_po_by_deal(deal_id)
    check(s, "PO issued", po is not None, f"{po.po_id} total={po.total_value}" if po else "")
    from shared.orchestration import supplier_commitment
    if supplier_commitment.po_ack_required() and await status(deal_id) == "PO_ISSUED":
        qc_before = [o for o in await sl_repo().list_by_deal(deal_id) if o.order_type == "QC"]
        check(s, "no inspection is booked before the supplier acknowledges the PO", not qc_before, str(len(qc_before)))
        if supplier_ack is not None:
            await supplier_ack()
        else:
            await supplier_commitment.acknowledge_po(deal_id, by="operator@qa.invalid: acknowledged by phone")
        await settle()
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
    check(s, "5 export documents rendered", len(docs) == 5, str(sorted(str(getattr(d.doc_type, 'value', d.doc_type)) for d in docs)))
    say(f"   gates: {[g.action for g in await pending(deal_id)]}")
    await approve(deal_id, "RELEASE_ESCROW_TRANCHE")
    await approve(deal_id, "BOOK_FREIGHT")
    check(s, "deal reaches IN_TRANSIT", await status(deal_id) == "IN_TRANSIT", await status(deal_id))
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
    user = f"The e-mail you received:\n---\n{thread[-1800:]}\n---\n\nYour instruction: {instruction}"
    for attempt in range(4):
        try:
            response = await LLMClient().chat(
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                caller="qa_counterparty", temperature=0.4, max_tokens=1500,
            )
            text = (response.content or "").strip()
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
        f"LLM gateway unavailable: the simulated {role} at {company} could not write a reply after 4 attempts "
        f"(instruction: {instruction[:120]}). The journey stopped here; rerun when the gateway is healthy."
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
    other = ([who["supplier_company"], who["supplier_mail"], f"{ask:,.0f}", f"{ask:.0f}"] if party == "buyer"
             else [who["buyer_company"], who["buyer_mail"]])
    return [w for w in other if w.lower() in text.lower()]


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
        await reply(deal_id, who, "buyer", "Our destination port is Jebel Ali, UAE. Say clearly: we accept your offer and your terms and wish to proceed. Say that you choose SGS as the inspection agency. Do NOT write any price or number in your reply, and repeat the sentence about the destination port word for word.")
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
    check(s, "funding by the buyer shows no payer warning", bool(po_gate) and not [f for f in po_gate[0].risk_flags if "payer_" in f],
          str(po_gate[0].risk_flags if po_gate else "no gate"))

    async def supplier_acknowledges_po():
        await reply(deal_id, who, "supplier", "Say clearly: we acknowledge the purchase order and will start production on Monday.")

    await base.to_qc_ready(s, deal_id, supplier_ack=supplier_acknowledges_po)
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


def _trust_webhooks() -> None:
    """The journeys hand provider results straight to the services; signatures have their own cases."""
    from shared.connectors import escrow, payments, qc_providers, shields

    async def verified(*_args: Any, **_kwargs: Any) -> bool:
        return True

    for module in (escrow, payments, qc_providers, shields):
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
