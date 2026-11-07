"""Right e-mail, right counterparty — with many deals running at once.

Covers agents/negotiation_agent/email_safety.py and the paths that use it:
send-time preflight (send_approved_email), draft-time leak flags, inbound
sender verification (mapper + processor), RFQ / outreach sender matching and
memory scoping for the drafting agents.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agents.negotiation_agent import email_safety as es
from agents.negotiation_agent.email_history import (
    EmailDirection,
    EmailHistoryRecord,
    EmailStatus,
    InMemoryEmailHistoryRepository,
)
from agents.negotiation_agent.repository import InMemoryNegotiationRepository
from agents.negotiation_agent.schemas import NegotiationPolicy, NegotiationSession

NOW = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
NEG = "11111111-2222-3333-4444-555555555555"
BUYER = "m.turre@glaconchemie.de"
SUPPLIER = "export@swadeshindia.in"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _session(**kw) -> NegotiationSession:
    d = dict(negotiation_id=NEG, deal_id="DEAL-R1", trade_match_id="TM", buyer_id="BUYER-DE", seller_id="SUP-1",
             product_code="1520", buyer_country_code="DE", buyer_target_price=980.0, supplier_quote_price=760.0,
             policy=NegotiationPolicy(), buyer_email=BUYER, seller_email=SUPPLIER, status="ROUND",
             created_at=NOW, updated_at=NOW)
    d.update(kw)
    return NegotiationSession(**d)


def _deal(**kw):
    parties = [SimpleNamespace(role="buyer", legal_name="Glaconchemie GmbH", email=BUYER),
               SimpleNamespace(role="supplier", legal_name="Swadesh India Chemical Pvt Ltd", email=SUPPLIER)]
    return SimpleNamespace(deal_id="DEAL-R1", parties=kw.get("parties", parties), buyer_candidates=[],
                           supplier_offers=[])


def _rec(**kw) -> EmailHistoryRecord:
    d = dict(email_id="EMAIL-AAA111", negotiation_id=NEG, deal_id="DEAL-R1", round_no=1,
             direction=EmailDirection.OUTBOUND, party="buyer", to_address=BUYER, from_address="trade@portless.io",
             subject="Crude glycerine — our offer", body="We can offer 20 MT at USD 980 per MT CIF Hamburg.",
             status=EmailStatus.PENDING_APPROVAL, created_at=NOW, updated_at=NOW)
    d.update(kw)
    return EmailHistoryRecord(**d)


@pytest.fixture
def repos(monkeypatch):
    email_repo = InMemoryEmailHistoryRepository()
    neg_repo = InMemoryNegotiationRepository()
    monkeypatch.setattr("agents.negotiation_agent.email_history._email_repo", email_repo)
    monkeypatch.setattr("agents.negotiation_agent.repository._repo", neg_repo)
    run(neg_repo.save_session(_session()))
    return SimpleNamespace(email=email_repo, neg=neg_repo)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_same_owner_never_matches_on_a_free_mail_domain():
    assert es.same_owner("ravi@kerala.in", "sales@kerala.in")
    assert not es.same_owner("a.supplier@gmail.com", "other.buyer@gmail.com")
    assert es.same_owner("Ravi <RAVI@gmail.com>", "ravi@gmail.com")
    assert not es.is_real_address("BUYER-DE@portless.placeholder")


def test_classify_sender():
    addrs = es.party_addresses(_session(), _deal())
    assert es.classify_sender("purchase@glaconchemie.de", "buyer", addrs) == "verified"
    assert es.classify_sender(SUPPLIER, "buyer", addrs) == "other_party"
    assert es.classify_sender("x@stranger.com", "buyer", addrs) == "unknown"
    # both sides on one company domain: the exact address decides, a colleague is ambiguous
    shared = {"buyer": {"buyer@example.com"}, "supplier": {"supplier@example.com"}}
    assert es.classify_sender("buyer@example.com", "buyer", shared) == "verified"
    assert es.classify_sender("supplier@example.com", "buyer", shared) == "other_party"
    assert es.classify_sender("ops@example.com", "buyer", shared) == "ambiguous"


# ---------------------------------------------------------------------------
# Outbound: send-time preflight
# ---------------------------------------------------------------------------

def test_preflight_blocks_the_other_partys_address():
    reasons = es.preflight(_rec(to_address=SUPPLIER), _session(), _deal(), [])
    assert any("is the supplier, but this e-mail was written for the buyer" in r for r in reasons)


def test_preflight_blocks_a_recipient_that_is_no_longer_the_contact():
    # draft written while candidate A was the buyer; the session was re-addressed to B since
    s = _session(buyer_email="buyer@newco.com")
    deal = _deal(parties=[SimpleNamespace(role="buyer", legal_name="NewCo", email="buyer@newco.com"),
                          SimpleNamespace(role="supplier", legal_name="Swadesh", email=SUPPLIER)])
    reasons = es.preflight(_rec(), s, deal, [])
    assert any("no longer a contact of the buyer" in r for r in reasons)


def test_preflight_blocks_body_naming_the_other_side_and_shared_addresses():
    body = "Our supplier Swadesh India Chemical Pvt Ltd (export@swadeshindia.in) confirms."
    assert any("body mentions the other side" in r for r in es.preflight(_rec(body=body), _session(), _deal(), []))
    same = _session(seller_email=BUYER)
    assert any("share the address" in r for r in es.preflight(_rec(), same, None, []))


def test_preflight_blocks_a_stale_draft_but_not_a_notification():
    reply = _rec(email_id="EMAIL-IN1", direction=EmailDirection.INBOUND, status=EmailStatus.RECEIVED,
                 from_address=BUYER, to_address="trade@portless.io", created_at=NOW + timedelta(hours=1))
    assert any("replied after this draft" in r for r in es.preflight(_rec(), _session(), _deal(), [reply]))
    note = _rec(email_id="EMAIL-N0123456789AB", kind="notification")
    assert es.preflight(note, _session(), _deal(), [reply]) == []


def test_preflight_passes_a_correct_draft():
    assert es.preflight(_rec(), _session(), _deal(), []) == []


def test_send_refuses_wrong_recipient_and_never_sends_twice(repos):
    from agents.negotiation_agent.service import EmailBlockedError, send_approved_email
    run(repos.email.save(_rec(email_id="EMAIL-BAD1", to_address=SUPPLIER)))
    smtp = AsyncMock()
    with patch("services.email_gateway.sender.send_email", smtp):
        with pytest.raises(EmailBlockedError):
            run(send_approved_email("EMAIL-BAD1"))
    smtp.assert_not_awaited()
    blocked = run(repos.email.get("EMAIL-BAD1"))
    assert blocked.status == EmailStatus.BLOCKED and "supplier" in blocked.block_reason

    run(repos.email.save(_rec(email_id="EMAIL-OK1")))
    with patch("services.email_gateway.sender.send_email", smtp):
        run(send_approved_email("EMAIL-OK1"))
        run(send_approved_email("EMAIL-OK1"))  # callback retry / redelivery
    assert smtp.await_count == 1
    assert run(repos.email.get("EMAIL-OK1")).status == EmailStatus.SENT


# ---------------------------------------------------------------------------
# Drafting: the other side's name / price forces human review
# ---------------------------------------------------------------------------

def test_leak_findings_catch_the_other_sides_price_and_name():
    s = _session()
    found = es.leak_findings("We buy at USD 760 per MT from Swadesh India.", "buyer", s, _deal())
    assert any("price 760" in f for f in found) and any("name" in f for f in found)
    assert es.leak_findings("We can offer USD 980 per MT CIF Hamburg.", "buyer", s, _deal()) == []
    assert any("price 980" in f for f in es.leak_findings("Buyer pays 980.", "supplier", s, _deal()))


def test_leaking_draft_is_forced_to_l0(repos):
    from agents.negotiation_agent.service import _create_and_gate_email
    created = AsyncMock(return_value=SimpleNamespace(approval_id="APPR-1"))
    with patch("services.hitl_approval.service.create_approval", created), \
         patch("agents.negotiation_agent.service._load_deal_quiet", AsyncMock(return_value=_deal())), \
         patch("agents.negotiation_agent.service._resolve_gate_level", return_value="L2"):
        run(_create_and_gate_email(_session(round_no=3), "buyer", BUYER, "s", "Supplier price is USD 760 per MT."))
    req = created.await_args.args[0]
    assert "counterparty_leak" in req.risk_flags and req.autonomy_level == "L0"
    assert req.summary.startswith("⛔ Mentions the other side")


# ---------------------------------------------------------------------------
# Inbound: party from the exact e-mail answered, sender verified
# ---------------------------------------------------------------------------

def _in_reply_to(email_id: str) -> str:
    return f"<{email_id}.{NEG}@portless.io>"


def test_party_comes_from_the_email_they_answered(repos):
    from services.email_processor.mapper import map_email
    run(repos.email.save(_rec(email_id="EMAIL-SUP1", party="supplier", to_address=SUPPLIER, status=EmailStatus.SENT)))
    with patch("services.deals.service.get_deal", AsyncMock(return_value=_deal())):
        # a colleague at the supplier answers our supplier e-mail from another mailbox
        m = run(map_email("ravi@swadeshindia.in", "Re: x", _in_reply_to("EMAIL-SUP1"), []))
        assert m.party == "supplier" and m.sender_check == "verified"
        # the BUYER writes on the supplier's thread → other party → held
        m2 = run(map_email(BUYER, "Re: x", _in_reply_to("EMAIL-SUP1"), []))
        assert m2.party == "supplier" and m2.sender_check == "other_party"


def test_unverified_sender_is_held_and_never_reaches_the_agent(repos, monkeypatch):
    import services.email_processor.repository as repo_mod
    from services.email_processor.repository import InMemoryEmailProcessorRepository
    from services.email_processor.schemas import InboundEmailSource, InboundEmailStatus, InboundRawEmail
    from services.email_processor.service import process_inbound
    repo_mod._repository = InMemoryEmailProcessorRepository()
    run(repos.email.save(_rec(email_id="EMAIL-SUP2", party="supplier", to_address=SUPPLIER, status=EmailStatus.SENT)))
    run_round = AsyncMock()
    monkeypatch.setattr("agents.negotiation_agent.service.run_round", run_round)
    hitl = AsyncMock(return_value=SimpleNamespace(approval_id="APPR-H"))
    raw = InboundRawEmail(from_address="someone@gmail.com", to_address="trade@portless.io",
                          subject="Re: order", body_text="We can do 700 per MT", in_reply_to=_in_reply_to("EMAIL-SUP2"),
                          source=InboundEmailSource.IMAP, received_at=NOW)
    try:
        with patch("services.deals.service.get_deal", AsyncMock(return_value=_deal())), \
             patch("services.hitl_approval.service.create_approval", hitl):
            res = run(process_inbound(raw))
    finally:
        repo_mod._repository = None
    assert res.status == InboundEmailStatus.NEEDS_OPERATOR and "not verified" in res.message
    run_round.assert_not_awaited()
    history = run(repos.email.list_by_negotiation(NEG))
    assert not [r for r in history if r.direction == EmailDirection.INBOUND]   # not in the agent's history
    assert "sender_not_verified" in hitl.await_args.args[0].risk_flags


# ---------------------------------------------------------------------------
# RFQ and outreach replies
# ---------------------------------------------------------------------------

def test_rfq_reply_is_never_guessed():
    from services.sourcing.service import rfq_sender_supplier
    rfq = SimpleNamespace(dispatch_results=[
        SimpleNamespace(supplier_id="SUP-A", email="a.spices@gmail.com"),
        SimpleNamespace(supplier_id="SUP-B", email="b.spices@gmail.com"),
        SimpleNamespace(supplier_id="SUP-C", email="sales@kerala.in"),
    ], supplier_ids=["SUP-A", "SUP-B", "SUP-C"])
    assert rfq_sender_supplier(rfq, "b.spices@gmail.com") == "SUP-B"
    assert rfq_sender_supplier(rfq, "c.spices@gmail.com") is None      # gmail is not a company
    assert rfq_sender_supplier(rfq, "ravi@kerala.in") == "SUP-C"       # colleague at the same company
    single = SimpleNamespace(dispatch_results=[SimpleNamespace(supplier_id="SUP-C", email="sales@kerala.in")],
                             supplier_ids=["SUP-C"])
    assert rfq_sender_supplier(single, "stranger@other.com") is None   # no "only one supplier" guess


def test_outreach_reply_from_someone_else_is_not_the_candidate():
    from shared.orchestration.outreach import sender_matches
    cand = SimpleNamespace(email="purchase@glaconchemie.de", reply_address=None)
    assert sender_matches(cand, "m.turre@glaconchemie.de")
    assert not sender_matches(cand, "agent@forwarded-to.com")
    assert not sender_matches(SimpleNamespace(email="x@gmail.com", reply_address=None), "y@gmail.com")


# ---------------------------------------------------------------------------
# Memory scoping
# ---------------------------------------------------------------------------

def test_knowledge_filter_keeps_only_this_counterparty():
    accept = es.knowledge_filter("buyer", {BUYER})
    assert accept({"metadata": {"kind": "email", "counterparty_address": "purchase@glaconchemie.de"}})
    assert not accept({"metadata": {"kind": "email", "counterparty_address": SUPPLIER}})
    assert not accept({"metadata": {"kind": "email", "counterparty_address": "other@buyer2.com"}})
    assert not accept({"metadata": {"kind": "email", "party": "supplier", "counterparty_domain": "glaconchemie.de"}})
    assert not accept({"metadata": {"kind": "deal_summary"}})           # margins + the other side's price
    assert not accept({"source": "knowledge-graph", "metadata": {}})    # cannot be vetted
    assert accept({"metadata": {"kind": "note", "subject_type": "buyer"}})
    assert not accept({"metadata": {"kind": "note", "subject_type": "supplier"}})
    assert accept({"metadata": {"kind": "product"}})
