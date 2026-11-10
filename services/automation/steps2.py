"""Catalogue steps for prospects, negotiation, compliance and money set-ups."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

from . import seed as fx
from .engine import Ctx, NotAutomated, step
from .steps import _approve, _deal, _deal_id, _gate_exists, _gates, _hold, _one_gate, _reply, _signed, _value


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Negotiation
# ---------------------------------------------------------------------------

async def make_session(ctx: Ctx, status: str, **over: Any) -> str:
    from agents.negotiation_agent.repository import get_repository
    from agents.negotiation_agent.schemas import NegotiationPolicy, NegotiationSession

    uid, now = ctx.ids["uid"], _now()
    fields: dict[str, Any] = dict(
        negotiation_id=str(uuid4()), deal_id=_deal_id(ctx), trade_match_id=f"TM-{uid}", buyer_id=f"BUY-{uid}", seller_id=f"SUP-{uid}",
        product_code=fx.HS_CODE, buyer_country_code=fx.MARKET, buyer_target_price=ctx.ids["buyer_price"],
        supplier_quote_price=ctx.ids["supplier_price"], policy=NegotiationPolicy(), quantity_mt=ctx.ids["qty"],
        buyer_email=ctx.ids["buyer_mail"], seller_email=ctx.ids["supplier_mail"], status=status, round_no=2,
        current_bid=ctx.ids["buyer_price"], current_ask=ctx.ids["supplier_price"], last_offer_to_buyer=ctx.ids["buyer_price"],
        last_offer_to_supplier=ctx.ids["supplier_price"], qc_provider="sgs", created_at=now, updated_at=now,
    )
    fields.update(over)
    session = NegotiationSession(**fields)
    await get_repository().save_session(session)
    ctx.ids["N"] = session.negotiation_id
    await fx.sent_mail(session.negotiation_id, _deal_id(ctx), "buyer", ctx.ids["buyer_mail"], now)
    await fx.sent_mail(session.negotiation_id, _deal_id(ctx), "supplier", ctx.ids["supplier_mail"], now)
    return session.negotiation_id


@step(r'a negotiation "([^"]+)" in status "([^"]+)"( idle past its limit)?')
async def negotiation_in(ctx: Ctx, _alias: str, status: str, idle: str | None) -> None:
    over: dict[str, Any] = {"updated_at": _now() - timedelta(days=90)} if idle else {}
    await make_session(ctx, status, **over)
    if status == "STARTED":
        ctx.extra["round0"] = True


async def _session(ctx: Ctx) -> Any:
    from agents.negotiation_agent.repository import get_repository
    if "N" in ctx.ids:
        return await get_repository().get_session(ctx.ids["N"])
    return await get_repository().get_session_by_deal(_deal_id(ctx))


@step(r'the negotiation(?: "[^"]+")? has status "([^"]+)"')
async def negotiation_status(ctx: Ctx, status: str) -> None:
    session = await _session(ctx)
    assert session is not None and session.status == status, f"negotiation is {session.status if session else None}"


@step(r'the negotiation is finalized at "([\d.]+)"')
async def finalized(ctx: Ctx, price: str) -> None:
    from agents.negotiation_agent.service import finalize_agreement
    neg = ctx.ids.get("N") or await make_session(ctx, "HITL_PENDING", agreed_price=float(price))
    await finalize_agreement(neg, float(price))
    await ctx.settle()


@step(r'HS code "([^"]+)" is confirmed "([^"]+)"')
async def hs_confirmed(ctx: Ctx, hs: str, status: str) -> None:
    from shared.orchestration import compliance
    real = compliance.hs_policy_status

    async def confirmed(code: str) -> tuple[str, str]:
        return (status, "automation fixture") if code == hs else await real(code)

    ctx.patch(compliance, "hs_policy_status", confirmed)


@step(r"the LLM gateway is not configured")
async def llm_off(ctx: Ctx) -> None:
    from agents.negotiation_agent import graph

    async def unavailable(_system: str, _payload: Any, _caller: str, fallback: dict[str, Any]) -> dict[str, Any]:
        return {**fallback, "__degraded__": True}

    ctx.patch(graph, "_chat_json", unavailable)
    from agents.negotiation_agent import service as neg
    from shared.llm_gateway.client import LLMConfigurationError
    ctx.patch(graph, "get_llm_client", lambda: (_ for _ in ()).throw(LLMConfigurationError("automation: gateway off")))
    ctx.extra["llm_off"] = neg


def _scripted_drafts(ctx: Ctx, *, price: float | None = None, message: str | None = None) -> None:
    from agents.negotiation_agent import graph

    async def scripted(system: str, payload: Any, caller: str, fallback: dict[str, Any]) -> dict[str, Any]:
        if "buyer_agent" not in caller:
            return fallback
        answer = dict(fallback)
        if price is not None:
            answer["counter_price_usd"] = price
        if message is not None:
            answer["outbound_message"] = message
        answer.pop("__degraded__", None)
        return answer

    ctx.patch(graph, "_chat_json", scripted)


@step(r"the LLM proposes a price below the margin floor")
async def llm_low(ctx: Ctx) -> None:
    ctx.extra["recommended_hint"] = True
    _scripted_drafts(ctx, price=500.0)


@step(r"the draft to the buyer contains the supplier's (name|address|domain)")
async def draft_leak(ctx: Ctx, what: str) -> None:
    leak = {"name": ctx.ids["supplier_company"], "address": ctx.ids["supplier_mail"], "domain": ctx.ids["supplier_mail"].split("@")[1]}[what]
    _scripted_drafts(ctx, message=f"Dear team, our supplier {leak} is ready. Best regards, PortLess Trade Operations")


@step(r"a round is drafted")
async def round_drafted(ctx: Ctx) -> None:
    await ctx.call("POST", f"/negotiation-agent/negotiations/{ctx.ids['N']}/round",
                   json_body={"counter_price": ctx.ids["buyer_price"] - 50, "party": "buyer"})
    assert ctx.response is not None and ctx.response.status_code == 200, ctx.response.text[:300]


@step(r"the drafted e-mail shows the corrected price")
async def corrected_price(ctx: Ctx) -> None:
    from agents.negotiation_agent.email_history import get_email_repository
    mails = await get_email_repository().list_by_deal(_deal_id(ctx))
    drafts = [m for m in mails if _value(m.direction) == "outbound" and m.party == "buyer"]
    assert drafts, "no draft to the buyer"
    assert "500" not in drafts[-1].body, f"the bad price is in the draft: {drafts[-1].body[:200]}"


@step(r"the buyer accepts the current price by e-mail")
async def buyer_accepts_price(ctx: Ctx) -> None:
    session = await _session(ctx)
    await _reply(ctx, ctx.ids["buyer_mail"], f"Re: offer [NEG:{ctx.ids['N']}]",
                 f"We accept your price of USD {session.last_offer_to_buyer:.2f} per MT and wish to proceed.")


@step(r'a negotiation draft saying "([^"]+)"')
async def draft_saying(ctx: Ctx, text: str) -> None:
    await make_session(ctx, "ROUND")
    _scripted_drafts(ctx, message=f"Dear team. {text} Best regards, PortLess Trade Operations")
    await round_drafted(ctx)


async def _draft_record(ctx: Ctx, *, to: str, party: str = "buyer", status: str = "pending_approval") -> Any:
    from agents.negotiation_agent.email_history import EmailDirection, EmailHistoryRecord, EmailStatus, get_email_repository
    now = _now()
    record = EmailHistoryRecord(
        email_id=f"EMAIL-{uuid4().hex[:10].upper()}", negotiation_id=ctx.ids["N"], deal_id=_deal_id(ctx), round_no=2,
        direction=EmailDirection.OUTBOUND, party=party, to_address=to, from_address="trade@portless-qa.invalid",
        subject=f"Offer [NEG:{ctx.ids['N']}]", body="Dear team, here is our offer. Best regards, PortLess Trade Operations",
        status=EmailStatus(status), created_at=now, updated_at=now,
    )
    await get_email_repository().save(record)
    ctx.extra["email"] = record.email_id
    return record


@step(r"an approved e-mail draft where (.+)")
async def approved_draft(ctx: Ctx, condition: str) -> None:
    from services.deals.repository import get_repository
    await make_session(ctx, "ROUND")
    recipients = {"the recipient is the other party": ctx.ids["supplier_mail"], "the recipient is not a current contact": "stale@elsewhere.invalid"}
    if condition in recipients:
        await _draft_record(ctx, to=recipients[condition])
    elif condition == "the buyer and supplier addresses are the same":
        deal = await _deal(ctx)
        for party in deal.parties:
            party.email = ctx.ids["buyer_mail"]
        await get_repository().save(deal)
        await make_session(ctx, "ROUND", seller_email=ctx.ids["buyer_mail"])
        await _draft_record(ctx, to=ctx.ids["buyer_mail"])
    else:
        raise NotAutomated(f"Draft condition not automated: {condition}")


@step(r"the e-mail is sent")
async def email_sent(ctx: Ctx) -> None:
    from agents.negotiation_agent.service import EmailBlockedError, send_approved_email
    try:
        await send_approved_email(ctx.extra["email"])
    except EmailBlockedError as exc:
        ctx.extra["blocked"] = str(exc)
        from shared.bus import get_publisher
        from shared.domain.events import DomainEvent, EventTopic
        await get_publisher().publish(DomainEvent(topic=EventTopic.DEAL_EXCEPTION, deal_id=_deal_id(ctx), payload={"source": "hitl_approval.callback", "error": str(exc)}))
    await ctx.settle()


async def _email(ctx: Ctx) -> Any:
    from agents.negotiation_agent.email_history import get_email_repository
    return await get_email_repository().get(ctx.extra["email"])


@step(r'the e-mail has status "([^"]+)"')
async def email_status(ctx: Ctx, status: str) -> None:
    assert _value((await _email(ctx)).status) == status, f"e-mail is {_value((await _email(ctx)).status)}"


@step(r"the e-mail has a block reason")
async def email_reason(ctx: Ctx) -> None:
    assert (await _email(ctx)).block_reason, "no block reason"


@step(r"an e-mail that was already sent")
async def already_sent(ctx: Ctx) -> None:
    await make_session(ctx, "ROUND")
    await _draft_record(ctx, to=ctx.ids["buyer_mail"], status="sent")


@step(r"the send is repeated")
async def send_repeat(ctx: Ctx) -> None:
    from agents.negotiation_agent.service import send_approved_email
    await send_approved_email(ctx.extra["email"])
    await ctx.settle()


@step(r"no second e-mail is composed")
async def no_second(ctx: Ctx) -> None:
    assert not ctx.emails, f"a mail was composed: {ctx.emails[0]['subject']}"


# ---------------------------------------------------------------------------
# Inbound mail
# ---------------------------------------------------------------------------

@step(r'an e-mail arrives from "([^"]+)" with subject "([^"]+)"')
async def mail_arrives(ctx: Ctx, sender: str, subject: str) -> None:
    from agents.negotiation_agent.email_history import get_email_repository
    sent = [m for m in await get_email_repository().list_by_negotiation(ctx.ids["N"]) if m.party == "buyer" and _value(m.direction) == "outbound"]
    answered = f"<{sent[-1].email_id}.{ctx.ids['N']}@portless.io>" if sent else ""
    await _reply(ctx, sender, ctx.fill(subject), "We can do 1200 per MT", in_reply_to=answered)


async def _inbound_record(ctx: Ctx) -> Any:
    from services.email_processor.repository import get_repository
    return await get_repository().get(ctx.extra["inbound"].inbound_id) if ctx.extra.get("inbound") else None


async def _inbound_from_response(ctx: Ctx) -> Any:
    if ctx.extra.get("inbound"):
        return await _inbound_record(ctx)
    from services.email_processor.repository import get_repository
    return await get_repository().get(ctx.response.json()["inbound_id"])  # type: ignore[union-attr]


def _sender_check(record: Any) -> str:
    """The processor keeps the verdict only as its refusal text."""
    return "unknown" if "not verified" in str(record.error or "") else "verified"


@step(r'the inbound record has status "([^"]+)"')
async def inbound_status(ctx: Ctx, status: str) -> None:
    record = await _inbound_from_response(ctx)
    assert _value(record.status).lower() == status.lower(), f"inbound is {_value(record.status)}"


@step(r'the inbound record has sender check "([^"]+)"')
async def inbound_check(ctx: Ctx, check: str) -> None:
    assert _sender_check(await _inbound_from_response(ctx)) == check, f"sender check: {_sender_check(await _inbound_from_response(ctx))}"


@step(r'the inbound record does not have sender check "([^"]+)"')
async def inbound_not_check(ctx: Ctx, check: str) -> None:
    assert _sender_check(await _inbound_from_response(ctx)) != check, f"sender check was {check}"


@step(r"the e-mail is not in the agent's history")
async def not_in_history(ctx: Ctx) -> None:
    from agents.negotiation_agent.email_history import get_email_repository
    mails = await get_email_repository().list_by_negotiation(ctx.ids["N"])
    assert not [m for m in mails if _value(m.direction) == "inbound"], "the e-mail entered the agent's history"


@step(r"the buyer's address is on a free-mail domain")
async def freemail(ctx: Ctx) -> None:
    from agents.negotiation_agent.repository import get_repository
    from services.deals.repository import get_repository as deals
    session = await _session(ctx)
    await get_repository().save_session(session.model_copy(update={"buyer_email": "buyer.qa@gmail.com"}))
    deal = await _deal(ctx)
    for party in deal.parties:
        if _value(party.role).lower() == "buyer":
            party.email = "buyer.qa@gmail.com"
    await deals().save(deal)


@step(r'an e-mail arrives from another address on that domain with subject "([^"]+)"')
async def other_freemail(ctx: Ctx, subject: str) -> None:
    await _reply(ctx, "someone.else@gmail.com", ctx.fill(subject), "We can do 1200 per MT")


@step(r"the same message id arrives twice")
async def same_message(ctx: Ctx) -> None:
    from services.email_processor.schemas import InboundEmailSource, InboundRawEmail
    from services.email_processor.service import process_inbound
    message = InboundRawEmail(from_address=ctx.ids["buyer_mail"], to_address="trade@portless-qa.invalid", subject=f"Re: offer [NEG:{ctx.ids['N']}]",
                              body_text="We can do 1200 per MT", message_id=f"<{uuid4().hex}@qa.invalid>", source=InboundEmailSource.IMAP, received_at=_now())
    await process_inbound(message)
    await process_inbound(message)
    await ctx.settle()


# ---------------------------------------------------------------------------
# Prospects: outreach funnel, RFQ, match board
# ---------------------------------------------------------------------------

async def add_candidates(ctx: Ctx, count: int, *, email: bool = True) -> None:
    from services.deals.repository import get_repository
    from services.deals.schemas import BuyerCandidate
    deal = await _deal(ctx)
    cands = [BuyerCandidate(candidate_id=f"C{i}", legal_name=fx.company("buyer", f"Emirates Foods {i} LLC").name, country="AE",
                            email=f"buyer{i}@emiratesfoods{i}.invalid" if email else None, status="pending", source="automation")
             for i in range(1, count + 1)]
    parties = [p for p in deal.parties if _value(p.role).lower() != "buyer"]
    await get_repository().save(deal.model_copy(update={"buyer_candidates": cands, "parties": parties}))
    ctx.ids["CAND"] = "C1"


async def _candidate(ctx: Ctx, cid: str) -> Any:
    deal = await _deal(ctx)
    return next(c for c in deal.buyer_candidates if c.candidate_id == cid)


async def _update_candidate(ctx: Ctx, cid: str, **fields: Any) -> None:
    from services.deals.service import update_buyer_candidate
    await update_buyer_candidate(_deal_id(ctx), cid, fields)


@step(r'outreach was sent to candidate "([^"]+)"(?: (\d+) days ago)?')
async def outreach_sent(ctx: Ctx, cid: str, days: str | None) -> None:
    await _update_candidate(ctx, cid, status="contacted", contacted_at=_now() - timedelta(days=int(days or 0)))


@step(r"the follow-up was sent (\d+) days ago")
async def followup_sent(ctx: Ctx, days: str) -> None:
    await _update_candidate(ctx, "C1", status="followed_up", followed_up_at=_now() - timedelta(days=int(days)))


async def _outreach_subject(ctx: Ctx, cid: str) -> str:
    from shared.orchestration import outreach
    return f"Re: introduction {outreach.subject_token(_deal_id(ctx), cid)}"


@step(r'"([^"]+)" replies "([^"]+)" from its own address(?: with the "\[BUY:" subject token)?')
async def candidate_replies(ctx: Ctx, cid: str, text: str) -> None:
    cand = await _candidate(ctx, cid)
    await _reply(ctx, cand.email, await _outreach_subject(ctx, cid), text)


@step(r'"([^"]+)" replies "([^"]+)" with the "\[BUY:" subject token for "([^"]+)"')
async def stranger_replies(ctx: Ctx, sender: str, text: str, cid: str) -> None:
    await _reply(ctx, sender, await _outreach_subject(ctx, cid), text)


@step(r'candidate "([^"]+)" has status "([^"]+)"')
async def cand_status(ctx: Ctx, cid: str, status: str) -> None:
    cand = await _candidate(ctx, cid)
    assert cand.status == status, f"{cid} is {cand.status}"


@step(r'candidate "([^"]+)" does not have status "([^"]+)"')
async def cand_not_status(ctx: Ctx, cid: str, status: str) -> None:
    assert (await _candidate(ctx, cid)).status != status


@step(r'candidate "([^"]+)" has a send error')
async def cand_error(ctx: Ctx, cid: str) -> None:
    assert (await _candidate(ctx, cid)).send_error, "no send error"


@step(r"the next pending candidate is contacted")
async def next_contacted(ctx: Ctx) -> None:
    deal = await _deal(ctx)
    assert any(c.status in ("contacted", "followed_up") for c in deal.buyer_candidates if c.candidate_id != "C1"), [c.status for c in deal.buyer_candidates]


@step(r"every candidate has declined")
async def all_declined(ctx: Ctx) -> None:
    for cand in (await _deal(ctx)).buyer_candidates:
        await _update_candidate(ctx, cand.candidate_id, status="declined")


@step(r"the funnel is evaluated")
async def funnel(ctx: Ctx) -> None:
    from shared.orchestration import outreach
    await outreach.advance(_deal_id(ctx))
    await ctx.settle()


def _party(deal: Any, role: str) -> Any:
    return next((p for p in deal.parties if _value(p.role).lower() == role), None)


@step(r'the buyer party e-mail is "([^"]+)"')
async def buyer_email_is(ctx: Ctx, email: str) -> None:
    assert _party(await _deal(ctx), "buyer").email == email


@step(r'the buyer party contact source is "([^"]+)"')
async def buyer_source(ctx: Ctx, source: str) -> None:
    party = _party(await _deal(ctx), "buyer")
    assert party.contact_source == source, f"source is {party.contact_source}"


@step(r'the buyer party e-mail is candidate "([^"]+)"\'s address')
async def buyer_is_candidate_mail(ctx: Ctx, cid: str) -> None:
    deal = await _deal(ctx)
    cand = next(c for c in deal.buyer_candidates if c.candidate_id == cid)
    assert _party(deal, "buyer").email == cand.email


@step(r"the buyer party is not set")
async def no_buyer(ctx: Ctx) -> None:
    assert _party(await _deal(ctx), "buyer") is None


@step(r"the mail transport is log-only")
async def log_only(ctx: Ctx) -> None:
    from services.email_gateway import sender
    from services.email_gateway.schemas import SendEmailResult, SendOutcome

    async def logged(req: Any) -> Any:
        return SendEmailResult(outcome=SendOutcome.LOGGED, message_id="<qa>", detail="Gmail not configured")

    ctx.patch(sender, "send_email", logged)


async def _rfq(ctx: Ctx, alias: str, supplier: str, email: str | None) -> None:
    from services.sourcing.repository import get_repository
    from services.sourcing.schemas import DispatchResult, RfqRecord
    now = _now()
    rfq_id = f"RFQ-{uuid4().hex[:12].upper()}"
    await get_repository().save_rfq(RfqRecord(
        rfq_id=rfq_id, deal_id=_deal_id(ctx), hs_code=fx.HS_CODE, supplier_ids=[supplier],
        dispatch_results=[DispatchResult(supplier_id=supplier, channel="email", success=True, email=email)],
        quantity_mt=20.0, target_price_usd=1250.0, status="COMPLETE", dispatched_at=now, expires_at=now + timedelta(days=7),
    ))
    ctx.ids[alias] = rfq_id
    ctx.ids["RFQ"] = rfq_id
    ctx.ids["rfq_supplier_mail"] = email


@step(r'an RFQ "([^"]+)" was dispatched to supplier "([^"]+)"')
async def rfq_supplier(ctx: Ctx, alias: str, supplier: str) -> None:
    await _rfq(ctx, alias, supplier, f"{supplier.lower()}@mill.invalid")


@step(r'an RFQ "([^"]+)" was dispatched to "([^"@]+@[^"]+)"')
async def rfq_address(ctx: Ctx, alias: str, email: str) -> None:
    await _rfq(ctx, alias, email.split("@")[0].upper(), email)


@step(r'"([^"]+@[^"]+)" replies with a price of "([\d.]+)" and the "\[RFQ:" subject token')
async def rfq_price(ctx: Ctx, sender: str, price: str) -> None:
    await _reply(ctx, sender, f"Re: quotation [RFQ:{ctx.ids['RFQ']}]", f"Our price is USD {price} per MT FOB Chennai. GSTIN 33AAAAA0000A1Z5 IEC 0400000000")


@step(r'"([^"]+@[^"]+)" replies "([^"]+)" with the "\[RFQ:" subject token')
async def rfq_text(ctx: Ctx, sender: str, text: str) -> None:
    await _reply(ctx, sender, f"Re: quotation [RFQ:{ctx.ids['RFQ']}]", text)


@step(r"the matching feature is off")
async def matching_off(ctx: Ctx) -> None:
    ctx.set_env("MATCHMAKING_ENABLED", "false")


async def _quote(ctx: Ctx, verified: bool) -> None:
    from services.sourcing import service as sourcing
    await _rfq(ctx, "R", "S1", "s1@mill.invalid")
    state = "ACTIVE" if verified else "INACTIVE"
    ctx.patch(sourcing, "_safe_call", AsyncMock(return_value={"status": state, "on_denied_list": False, "age_years": 5, "registered": True}))
    await ctx.call("POST", "/sourcing/quotes", json_body={"rfq_id": ctx.ids["R"], "deal_id": _deal_id(ctx), "supplier_id": "S1", "unit_price_usd": 1000, "gstin": "33AAAAA0000A1Z5", "iec_number": "0400000000"})


@step(r'the verification connector answers "gst_verified=false"')
async def verification_false(ctx: Ctx) -> None:
    ctx.extra["verified"] = False


@step(r'a quote from supplier "S1" is received')
async def quote_received(ctx: Ctx) -> None:
    await _quote(ctx, ctx.extra.get("verified", True))


@step(r"a verified quote inside the buyer's corridor is received")
async def verified_quote(ctx: Ctx) -> None:
    await _quote(ctx, True)


async def add_board(ctx: Ctx) -> None:
    from services.deals.repository import get_repository
    from services.deals.schemas import BuyerRequirement, SupplierOffer
    await add_candidates(ctx, 1)
    deal = await _deal(ctx)
    cand = deal.buyer_candidates[0].model_copy(update={"status": "interested", "requirement": BuyerRequirement(
        quantity_mt=20.0, target_price_usd_per_mt=1250.0, target_price_incoterm="CIF", destination_port="Jebel Ali", source="operator")})
    offer = SupplierOffer(offer_id="OF1", supplier_id="mill.invalid", company_name=ctx.ids["supplier_company"], email="sales@mill.invalid", fob_price_usd_per_mt=1000.0,
                          moq_mt=5.0, capacity_mt_per_month=500.0, lead_time_days=20, gst_verified=True, iec_active=True, source="quote")
    await get_repository().save(deal.model_copy(update={"buyer_candidates": [cand], "supplier_offers": [offer]}))
    ctx.ids["OFFER"] = "OF1"


async def _save_board(ctx: Ctx, *, cand: dict[str, Any] | None = None, requirement: dict[str, Any] | None = None, offer: dict[str, Any] | None = None) -> None:
    from services.deals.repository import get_repository
    deal = await _deal(ctx)
    c = deal.buyer_candidates[0]
    if requirement is not None:
        c = c.model_copy(update={"requirement": c.requirement.model_copy(update=requirement)})
    if cand:
        c = c.model_copy(update=cand)
    o = deal.supplier_offers[0].model_copy(update=offer or {})
    await get_repository().save(deal.model_copy(update={"buyer_candidates": [c], "supplier_offers": [o]}))


@step(r'pair "C1" × "OF1" has a chance of closing above the threshold')
async def pair_likely(ctx: Ctx) -> None:
    ctx.set_env("MATCH_MIN_PROBABILITY", "0.01")


@step(r'buyer "C1" is interested but has stated no price')
async def buyer_no_price(ctx: Ctx) -> None:
    await _save_board(ctx, requirement={"target_price_usd_per_mt": None})


@step(r'pair "C1" × "OF1" has no buyer price')
async def pair_no_price(ctx: Ctx) -> None:
    await _save_board(ctx, requirement={"target_price_usd_per_mt": None})


@step(r'pair "C1" × "OF1" has the condition "([^"]+)"')
async def pair_condition(ctx: Ctx, condition: str) -> None:
    rules = {"supplier failed GST verification": {"offer": {"verification_failed": True, "gst_verified": False}},
             "buyer quantity below the supplier's MOQ": {"offer": {"moq_mt": 500.0}}}
    if condition not in rules:
        raise NotAutomated(f"Pair condition not automated: {condition}")
    await _save_board(ctx, **rules[condition])


@step(r'offer "OF1" has only an estimated price')
async def offer_estimated(ctx: Ctx) -> None:
    await _save_board(ctx, offer={"fob_price_usd_per_mt": None})


@step(r'offer "OF1" has a quoted price')
async def offer_quoted(ctx: Ctx) -> None:
    await _save_board(ctx, offer={"fob_price_usd_per_mt": 1000.0})


@step(r'candidate "C1" has no e-mail address')
async def cand_no_mail(ctx: Ctx) -> None:
    await _save_board(ctx, cand={"email": None})


@step(r'a pending gate "CONFIRM_MATCH" exists for pair "C1" × "OF1"')
async def pending_match(ctx: Ctx) -> None:
    await ctx.call("POST", "/deals/{D}/match/confirm", json_body={"candidate_id": "C1", "offer_id": "OF1"})
    assert ctx.response is not None and ctx.response.status_code < 300, ctx.response.text[:300]


@step(r'an indicative offer was sent to "C1"')
async def offer_sent(ctx: Ctx) -> None:
    from services.deals.schemas import IndicativeOffer
    await _save_board(ctx, cand={"status": "interested", "indicative_offer": IndicativeOffer(offer_id="OF1", price_low_usd_per_mt=1200.0, price_high_usd_per_mt=1300.0, sent_at=_now())})


@step(r'the indicative offer has outcome "([^"]+)"')
async def offer_outcome(ctx: Ctx, outcome: str) -> None:
    cand = await _candidate(ctx, "C1")
    assert cand.indicative_offer and cand.indicative_offer.response == outcome, str(cand.indicative_offer)


@step(r'the buyer party is candidate "C1"')
async def buyer_is_c1(ctx: Ctx) -> None:
    deal = await _deal(ctx)
    assert _party(deal, "buyer") is not None and _party(deal, "buyer").legal_name == deal.buyer_candidates[0].legal_name


@step(r'the supplier party is offer "OF1"')
async def supplier_is_of1(ctx: Ctx) -> None:
    party = _party(await _deal(ctx), "supplier")
    assert party is not None and ctx.ids["supplier_company"] in party.legal_name


# ---------------------------------------------------------------------------
# PI, escrow, QC, documents, gates
# ---------------------------------------------------------------------------

@step(r'the buyer has written "([^"]+)"')
async def buyer_wrote(ctx: Ctx, text: str) -> None:
    from agents.negotiation_agent.email_history import EmailDirection, EmailHistoryRecord, EmailStatus, get_email_repository
    now = _now()
    neg = ctx.ids.get("N", "none")
    await get_email_repository().save(EmailHistoryRecord(
        email_id=f"EMAIL-{uuid4().hex[:10]}", negotiation_id=neg, deal_id=_deal_id(ctx), round_no=2, direction=EmailDirection.INBOUND, party="buyer",
        to_address="trade@portless-qa.invalid", from_address=ctx.ids["buyer_mail"], subject="Re: offer", body=text, status=EmailStatus.RECEIVED, created_at=now, updated_at=now,
    ))


@step(r"the Proforma Invoice is issued")
async def pi_issued(ctx: Ctx) -> None:
    from shared.orchestration import continuations
    pi = await continuations.issue_pi_for_deal(_deal_id(ctx), agreed_price=ctx.ids["buyer_price"], quantity_mt=ctx.ids["qty"], market=fx.MARKET)
    await ctx.settle()
    import httpx
    ctx.response = httpx.Response(200, json=pi.model_dump(mode="json"))


@step(r"the escrow amount is ([\d.]+)")
async def escrow_amount(ctx: Ctx, amount: str) -> None:
    assert abs((await _hold(ctx)).amount - float(amount)) < 0.01


@step(r"exactly (\d+) escrow exists for the deal")
async def one_escrow(ctx: Ctx, n: str) -> None:
    from services.escrow_guard.repository import get_repository
    holds = [h for h in await get_repository().list_holds() if h.deal_id == _deal_id(ctx)]
    assert len(holds) == int(n), f"{len(holds)} escrows"


@step(r'a reminder is composed for the "([^"]+)"')
async def reminder(ctx: Ctx, party: str) -> None:
    address = ctx.ids["buyer_mail" if party == "buyer" else "supplier_mail"]
    assert [m for m in ctx.emails if m["to"] == address], f"no reminder; composed: {[m['subject'] for m in ctx.emails]}"


@step(r'the reminder count never exceeds "([^"]+)"')
async def reminder_cap(ctx: Ctx, name: str) -> None:
    deal = await _deal(ctx)
    from os import environ
    assert len(ctx.emails) <= int(environ.get(name, "3")), f"{len(ctx.emails)} reminders on {_value(deal.status)}"


@step(r"the stored PI file has been removed")
async def pi_file_removed(ctx: Ctx) -> None:
    from services.dealmaker.repository import get_repository
    repo = get_repository()
    pi = await repo.get_pi(ctx.ids["PI"])
    await repo.save_pi(pi.model_copy(update={"storage_uri": "file:///C:/does-not-exist/qa-pi.html"}))


async def _fund(ctx: Ctx, payer: str | None, country: str | None, amount: float | None = None) -> None:
    hold = await _hold(ctx)
    body: dict[str, Any] = {"ext_ref": hold.ext_ref, "status": "FUNDED", "amount": amount or hold.amount, "currency": "USD", "event_id": f"ev-{uuid4().hex[:8]}"}
    if payer:
        body["payer_name"] = payer
    if country:
        body["payer_country"] = country
    raw = json.dumps(body).encode()
    await ctx.call("POST", "/escrow-guard/webhooks/escrow-funded", content=raw, persona="anonymous",
                   headers={"content-type": "application/json", **_signed("tazapay", raw, "valid")})


@step(r"the buyer funds the escrow in full as the payer")
async def funds_full(ctx: Ctx) -> None:
    await _fund(ctx, ctx.ids["buyer_company"], fx.MARKET)


@step(r"the escrow is funded in full with (.+)")
async def funds_with(ctx: Ctx, what: str) -> None:
    m = re.match(r'payer "([^"]+)"(?: from "([^"]+)")?', what)
    if m:
        await _fund(ctx, m.group(1), m.group(2) or fx.MARKET)
    elif what == "no payer reported":
        await _fund(ctx, None, None)
    else:
        raise NotAutomated(f"Funding variant not automated: {what}")


@step(r'the pending gate "([^"]+)" has playbook "([^"]+)"')
async def gate_playbook(ctx: Ctx, action: str, playbook: str) -> None:
    gates = await _gates(ctx, action, "PENDING")
    assert gates and gates[0].callback_payload.get("playbook") == playbook, str([g.callback_payload for g in gates])[:300]


@step(r'a pending gate "RESOLVE_EXCEPTION" has playbook "([^"]+)"')
async def given_playbook(ctx: Ctx, playbook: str) -> None:
    await _gate_exists(ctx, "RESOLVE_EXCEPTION", payload={"deal_id": _deal_id(ctx), "playbook": playbook, "exception_id": f"EXC-{uuid4().hex[:6]}", "category": "PAYMENT_FAILED"})


@step(r'the loading is recorded with (.+)')
async def loading_with(ctx: Ctx, what: str) -> None:
    body: dict[str, Any] = {"container_numbers": ["CONT-1"], "seal_numbers": ["SEAL-1"], "loaded_quantity_mt": ctx.ids["qty"], "hs_code": fx.HS_CODE, "photos": ["https://photos.invalid/1.jpg"], "source": "inspector"}
    if (m := re.fullmatch(r'HS "([^"]+)"', what)):
        body["hs_code"] = m.group(1)
    elif (m := re.fullmatch(r"([\d.]+) MT(?: loaded against [\d.]+ MT)?", what)):
        body["loaded_quantity_mt"] = float(m.group(1))
    elif what == "no loaded quantity":
        body.pop("loaded_quantity_mt")
    elif what == "no photos":
        body["photos"] = []
    else:
        raise NotAutomated(f"Loading variant not automated: {what}")
    await ctx.call("POST", f"/shield-link/deals/{_deal_id(ctx)}/loading", json_body=body)


@step(r'the inspector reports PASS with container "([^"]+)", seal "([^"]+)", ([\d.]+) MT, HS "([^"]+)" and a photo')
async def inspector_full(ctx: Ctx, container: str, seal: str, mt: str, hs: str) -> None:
    from .steps import _qc_webhook
    await _qc_webhook(ctx, "PASS", container_numbers=[container], seal_numbers=[seal], loaded_quantity_mt=float(mt), loaded_hs_code=hs, photos=["https://photos.invalid/1.jpg"])


@step(r"(\d+) QC orders exist for the deal")
async def qc_orders(ctx: Ctx, n: str) -> None:
    from .steps import _orders
    found = [o for o in await _orders(ctx) if o.order_type == "QC"]
    assert len(found) == int(n), f"{len(found)} QC orders"


@step(r'a document "([^"]+)" exists for the deal')
async def doc_exists(ctx: Ctx, alias: str) -> None:
    await ctx.call("POST", "/docugen/documents", json_body={"deal_id": _deal_id(ctx), "doc_type": "CI"})
    assert ctx.response is not None and ctx.response.status_code == 201, ctx.response.text[:300]
    ctx.ids[alias] = ctx.response.json()["doc_id"]


@step(r'the clearance is recorded with the staff user\'s e-mail')
async def clearance_by(ctx: Ctx) -> None:
    from shared.orchestration import compliance
    from .safety import STAFF_EMAILS
    record = await compliance.get_record(_deal_id(ctx))
    assert record.hold_clearances and record.hold_clearances[-1].cleared_by == STAFF_EMAILS[0], str(record.hold_clearances)


@step(r'"([^"]+)" tries to approve the pending "([^"]+)" gate')
async def tries_approve(ctx: Ctx, persona: str, action: str) -> None:
    if not await _gates(ctx, action, "PENDING"):
        hold = await _hold(ctx)
        await _gate_exists(ctx, action, amount=100.0, payload={"deal_id": _deal_id(ctx), "escrow_id": hold.escrow_id, "tranche_id": "T3", "amount": 100.0, "idempotency_key": uuid4().hex})
    await _approve(ctx, persona, action)


@step(r'an exception of category "([^"]+)" is raised')
async def exception_raised(ctx: Ctx, category: str) -> None:
    await ctx.call("POST", "/exception-resolver/exceptions", json_body={"deal_id": _deal_id(ctx), "source_event": "automation", "source_service": "automation", "raw_detail": "automation", "context": {"category": category}})
    if ctx.response is not None and ctx.response.status_code < 300:
        ctx.ids["EXC"] = ctx.response.json().get("exception_id", "")


@step(r'the exception status is "([^"]+)"')
async def exception_status_is(ctx: Ctx, status: str) -> None:
    from services.exception_resolver.repository import get_repository
    rows = await get_repository().list_by_deal(_deal_id(ctx))
    assert any(_value(r.status) == status for r in rows), [_value(r.status) for r in rows]


@step(r'the exception decision is "([^"]+)"')
async def exception_decision(ctx: Ctx, decision: str) -> None:
    from services.exception_resolver.repository import get_repository
    rows = await get_repository().list_by_deal(_deal_id(ctx))
    assert any(r.analysis and _value(r.analysis.decision) == decision for r in rows), [r.analysis for r in rows]


@step(r'the response contains deal "([^"]+)"')
async def response_has(ctx: Ctx, alias: str) -> None:
    assert ctx.response is not None and ctx.ids[alias] in ctx.response.text, "deal not listed"


@step(r'the response does not contain deal "([^"]+)"')
async def response_lacks(ctx: Ctx, alias: str) -> None:
    assert ctx.response is not None and ctx.ids[alias] not in ctx.response.text, "deal listed"


@step(r"a refund for the deal has been requested")
async def refund_requested(ctx: Ctx) -> None:
    await ctx.call("POST", "/escrow-guard/escrows/{ESC}/request-refund", json_body={"reason": "automation"})
    assert ctx.response is not None and ctx.response.status_code == 202, ctx.response.text[:300]


__all__ = ["_one_gate"]
