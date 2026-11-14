"""Email Processor — post-agreement replies.

Once a negotiation is AGREED, the buyer's reply to the Proforma Invoice email
threads back via the same [NEG:...] token. It must never run a negotiation
round; an unconditional acceptance becomes a CONFIRM_PI_ACCEPTANCE approval,
anything else a PARSE_INBOUND_EMAIL approval for a human. FAILED sessions stay
unmatched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.negotiation_agent.email_history import (
    EmailDirection,
    EmailHistoryRecord,
    EmailStatus,
    InMemoryEmailHistoryRepository,
)
from agents.negotiation_agent.repository import InMemoryNegotiationRepository
from agents.negotiation_agent.schemas import NegotiationPolicy, NegotiationSession
from services.email_processor.repository import InMemoryEmailProcessorRepository
from services.email_processor.schemas import InboundEmailSource, InboundEmailStatus, InboundRawEmail
from services.email_processor.service import process_inbound

DEAL = "DEAL-PA-1"
NEG = "4c02932b-d7fa-4042-aae9-cf4ac412e212"
BUYER = "buyer@example.com"


def _session(status: str = "AGREED") -> NegotiationSession:
    now = datetime.now(timezone.utc)
    return NegotiationSession(
        negotiation_id=NEG, deal_id=DEAL, trade_match_id="TM-1",
        buyer_id="BYR-1", seller_id="SUP-1", product_code="1006",
        buyer_country_code="DE", buyer_target_price=1255.0,
        supplier_quote_price=1004.0, policy=NegotiationPolicy(),
        status=status, round_no=3, agreed_price=1200.0,
        buyer_email=BUYER, seller_email="supplier@example.com",
        created_at=now, updated_at=now,
    )


def _outbound_to_buyer() -> EmailHistoryRecord:
    now = datetime.now(timezone.utc)
    return EmailHistoryRecord(
        email_id="EMAIL-NABC123", negotiation_id=NEG, deal_id=DEAL, round_no=3,
        direction=EmailDirection.OUTBOUND, party="buyer", to_address=BUYER,
        from_address="portless-trade@portless.io", subject="Proforma Invoice PI-1",
        body="...", status=EmailStatus.SENT, created_at=now, updated_at=now,
    )


def _raw(body: str, from_address: str = BUYER) -> InboundRawEmail:
    return InboundRawEmail(
        from_address=from_address, to_address="portless-trade@portless.io",
        subject="Re: Proforma Invoice PI-1", body_text=body,
        in_reply_to=f"<EMAIL-NABC123.{NEG}@portless.io>",
        source=InboundEmailSource.IMAP, received_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def wiring(monkeypatch):
    import services.email_processor.repository as ep_repo_mod
    ep_repo_mod._repository = InMemoryEmailProcessorRepository()
    neg_repo = InMemoryNegotiationRepository()
    email_repo = InMemoryEmailHistoryRepository()
    monkeypatch.setattr("agents.negotiation_agent.repository._repo", neg_repo)
    monkeypatch.setattr("agents.negotiation_agent.email_history._email_repo", email_repo)

    run_round = AsyncMock()
    monkeypatch.setattr("agents.negotiation_agent.service.run_round", run_round)
    create_approval = AsyncMock(return_value=SimpleNamespace(approval_id="APPR-1"))
    monkeypatch.setattr("services.hitl_approval.service.create_approval", create_approval)
    monkeypatch.setattr(
        "services.deals.service.get_deal",
        AsyncMock(return_value=SimpleNamespace(deal_id=DEAL, status=SimpleNamespace(value="PI_SENT"))),
    )
    dm_repo = AsyncMock()
    dm_repo.get_pi_by_deal = AsyncMock(return_value=SimpleNamespace(
        pi_id="PI-1", status="SENT", total_value=30000.0, currency="USD",
        expires_at=__import__("datetime").datetime(2099, 1, 1, tzinfo=__import__("datetime").timezone.utc),
    ))
    monkeypatch.setattr("services.dealmaker.repository.get_repository", lambda: dm_repo)
    yield SimpleNamespace(
        neg=neg_repo, email=email_repo, run_round=run_round,
        create_approval=create_approval, dm_repo=dm_repo,
    )
    ep_repo_mod._repository = None


@pytest.mark.asyncio
async def test_buyer_acceptance_creates_confirm_pi_approval(wiring):
    await wiring.neg.save_session(_session())
    await wiring.email.save(_outbound_to_buyer())

    result = await process_inbound(_raw("Dear team, we accept the Proforma Invoice. Regards"))

    assert result.status == InboundEmailStatus.PROCESSED
    assert "PI-1" in result.message
    wiring.run_round.assert_not_awaited()
    req = wiring.create_approval.await_args.args[0]
    assert req.action == "CONFIRM_PI_ACCEPTANCE"
    assert req.deal_id == DEAL
    assert req.callback_payload["pi_id"] == "PI-1"
    assert req.callback_payload["buyer_email"] == BUYER
    assert req.idempotency_key == "pi-accept-PI-1"
    assert req.financial_impact_usd == 30000.0


@pytest.mark.asyncio
async def test_conditional_reply_goes_to_operator(wiring):
    await wiring.neg.save_session(_session())
    await wiring.email.save(_outbound_to_buyer())

    result = await process_inbound(_raw("We can accept if you ship by 15 November. Please confirm."))

    assert result.status == InboundEmailStatus.NEEDS_OPERATOR
    wiring.run_round.assert_not_awaited()
    req = wiring.create_approval.await_args.args[0]
    assert req.action == "PARSE_INBOUND_EMAIL"
    assert "post_agreement_reply" in req.risk_flags


@pytest.mark.asyncio
async def test_acceptance_when_deal_not_pi_sent_goes_to_operator(wiring, monkeypatch):
    monkeypatch.setattr(
        "services.deals.service.get_deal",
        AsyncMock(return_value=SimpleNamespace(deal_id=DEAL, status=SimpleNamespace(value="ESCROW_PENDING"))),
    )
    await wiring.neg.save_session(_session())
    await wiring.email.save(_outbound_to_buyer())

    result = await process_inbound(_raw("Agreed, we accept."))

    assert result.status == InboundEmailStatus.NEEDS_OPERATOR
    assert wiring.create_approval.await_args.args[0].action == "PARSE_INBOUND_EMAIL"


@pytest.mark.asyncio
async def test_supplier_reply_never_accepts_pi(wiring):
    await wiring.neg.save_session(_session())
    now = datetime.now(timezone.utc)
    await wiring.email.save(EmailHistoryRecord(
        email_id="EMAIL-NABC123", negotiation_id=NEG, deal_id=DEAL, round_no=3,
        direction=EmailDirection.OUTBOUND, party="supplier", to_address="supplier@example.com",
        from_address="portless-trade@portless.io", subject="PO", body="...",
        status=EmailStatus.SENT, created_at=now, updated_at=now,
    ))
    result = await process_inbound(_raw("We accept the PO and will proceed.", from_address="supplier@example.com"))
    assert result.status == InboundEmailStatus.NEEDS_OPERATOR
    assert wiring.create_approval.await_args.args[0].action == "PARSE_INBOUND_EMAIL"


@pytest.mark.asyncio
async def test_failed_session_stays_unmatched(wiring):
    await wiring.neg.save_session(_session(status="FAILED"))
    await wiring.email.save(_outbound_to_buyer())
    result = await process_inbound(_raw("We accept."))
    assert result.status == InboundEmailStatus.UNMATCHED
    wiring.create_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_session_still_runs_a_round(wiring):
    await wiring.neg.save_session(_session(status="ROUND"))
    await wiring.email.save(_outbound_to_buyer())
    wiring.run_round.return_value = SimpleNamespace(round_no=4, status="ROUND")
    result = await process_inbound(_raw("We can do $1,180 per MT."))
    assert result.status == InboundEmailStatus.PROCESSED
    wiring.run_round.assert_awaited_once()


@pytest.mark.asyncio
async def test_parse_inbound_email_is_a_valid_hitl_action(monkeypatch):
    """Regression: PARSE_INBOUND_EMAIL was created by the processor but was not
    in ApprovalAction, so create_approval raised InvalidAction and the operator
    never saw the email."""
    from services.hitl_approval.repository import InMemoryHitlApprovalRepository
    from services.hitl_approval.schemas import CreateApprovalRequest
    from services.hitl_approval.service import create_approval
    import services.hitl_approval.repository as hitl_mod
    monkeypatch.setattr(hitl_mod, "_repository", InMemoryHitlApprovalRepository())
    monkeypatch.setattr("services.hitl_approval.service.notify", AsyncMock())
    monkeypatch.setattr("services.hitl_approval.service._emit", AsyncMock())
    appr = await create_approval(CreateApprovalRequest(
        deal_id=DEAL, action="PARSE_INBOUND_EMAIL", autonomy_level="L0",
    ))
    assert appr.status == "PENDING"


def _priced_pi(wiring):
    from datetime import datetime as _dt, timezone as _tz
    wiring.dm_repo.get_pi_by_deal.return_value = SimpleNamespace(
        pi_id="PI-1", status="SENT", agreed_price=1225.0, total_value=30625.0, currency="USD",
        expires_at=_dt(2099, 1, 1, tzinfo=_tz.utc),
    )


@pytest.mark.asyncio
async def test_acceptance_naming_another_price_is_forced_to_review(wiring, monkeypatch):
    """UC-15 / TC-RSK-024: "we accept the PI … at USD 1,180" for a PI at USD 1,225
    is not a plain acceptance — the gate is L0 with flag pi_price_mismatch."""
    monkeypatch.setenv("AUTONOMY_CONFIRM_PI_ACCEPTANCE", "L2")  # no policy lifts it
    _priced_pi(wiring)
    await wiring.neg.save_session(_session())
    await wiring.email.save(_outbound_to_buyer())

    await process_inbound(_raw(
        "Dear team, we accept the Proforma Invoice PI-1 at USD 1,180 per MT. Please send the escrow details."))

    req = wiring.create_approval.await_args.args[0]
    assert req.action == "CONFIRM_PI_ACCEPTANCE"
    assert req.autonomy_level == "L0"
    assert "pi_price_mismatch" in req.risk_flags
    assert "1,180.00" in req.summary and "1,225.00" in req.summary


@pytest.mark.asyncio
async def test_acceptance_repeating_the_pi_price_and_total_is_accepted(wiring):
    _priced_pi(wiring)
    await wiring.neg.save_session(_session())
    await wiring.email.save(_outbound_to_buyer())

    result = await process_inbound(_raw(
        "We accept the Proforma Invoice at USD 1,225 per MT, total USD 30,625.00. Regards"))

    assert result.status == InboundEmailStatus.PROCESSED
    req = wiring.create_approval.await_args.args[0]
    assert req.action == "CONFIRM_PI_ACCEPTANCE"
    assert "pi_price_mismatch" not in req.risk_flags


@pytest.mark.asyncio
async def test_rupee_figure_is_compared_after_conversion(wiring, monkeypatch):
    _priced_pi(wiring)
    monkeypatch.setattr("services.email_processor.service._usd_inr_rate", AsyncMock(return_value=80.0))
    await wiring.neg.save_session(_session())
    await wiring.email.save(_outbound_to_buyer())

    ok = await process_inbound(_raw("We accept the Proforma Invoice at INR 98,000 per MT."))  # = USD 1,225
    assert ok.status == InboundEmailStatus.PROCESSED

    wiring.create_approval.reset_mock()
    from services.email_processor.service import _pi_price_mismatch
    pi = wiring.dm_repo.get_pi_by_deal.return_value
    assert await _pi_price_mismatch("We accept the PI at Rs. 90,000 per MT.", pi) == 1125.0
    assert await _pi_price_mismatch("We accept the PI.", pi) is None
    assert await _pi_price_mismatch(
        "Accepted.\n\nOn Mon, PortLess wrote:\n> Our offer is USD 1,300 per MT", pi) is None
