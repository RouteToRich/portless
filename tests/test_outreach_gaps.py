"""Outreach paths that had no test: matching a reply to a candidate, the reply with no match or
from another sender, a buyer becoming interested / declining without the match board, the
negotiation re-addressed or opened, operator skip, send-failed and exhausted exceptions
(todo: product-code coverage back to 95 %)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.orchestration import outreach as o


def _cand(cid, email, status="contacted"):
    return SimpleNamespace(candidate_id=cid, legal_name=f"Buyer {cid}", email=email, status=status, country="AE",
                           website=None, phone=None, source="scan", send_error="SMTP 550")


def _deal(cands, status="MATCHING", parties=()):
    return SimpleNamespace(deal_id="DEAL-O1", buyer_candidates=list(cands), status=SimpleNamespace(value=status),
                           parties=list(parties))


def test_find_by_id_exact_address_and_domain_but_never_ambiguous():
    deal = _deal([_cand("C1", "buy@gulf.ae"), _cand("C2", "ops@emirates.ae"), _cand("C3", "sales@emirates.ae")])
    assert o._find(deal, "C2", None).candidate_id == "C2"
    assert o._find(deal, None, "BUY@gulf.ae").candidate_id == "C1"
    assert o._find(deal, None, "ceo@gulf.ae").candidate_id == "C1"          # same company domain
    assert o._find(deal, None, "x@emirates.ae") is None                    # two on one domain → operator
    assert o._find(deal, None, None) is None


@pytest.fixture
def svc(monkeypatch):
    import services.deals.service as ds
    from agents.negotiation_agent import email_history
    from shared.orchestration import matchmaking
    state = {"deal": _deal([_cand("C1", "buy@gulf.ae")])}
    monkeypatch.setattr(ds, "get_deal", AsyncMock(side_effect=lambda deal_id: state["deal"]))
    monkeypatch.setattr(ds, "update_buyer_candidate", AsyncMock())
    monkeypatch.setattr(ds, "update_party", AsyncMock(side_effect=lambda *a, **k: state["deal"]))
    monkeypatch.setattr(email_history, "record_deal_email", AsyncMock())
    monkeypatch.setattr(matchmaking, "enabled", lambda: False)
    return SimpleNamespace(ds=ds, state=state)


async def test_reply_matching_no_candidate_or_from_someone_else(svc):
    out = await o.handle_reply("DEAL-O1", None, "stranger@other.com", "interested")
    assert out["matched"] is False and out["candidate_id"] is None
    out = await o.handle_reply("DEAL-O1", "C1", "stranger@other.com", "yes please send prices")
    assert out["sender_mismatch"] is True
    svc.ds.update_buyer_candidate.assert_not_awaited()


async def test_interested_buyer_becomes_the_party(svc, monkeypatch):
    confirmed = AsyncMock()
    monkeypatch.setattr(o, "_buyer_confirmed", confirmed)
    monkeypatch.setattr(o, "classify_reply", lambda body: "interested")
    out = await o.handle_reply("DEAL-O1", "C1", "buy@gulf.ae", "Yes, interested — send your best price")
    assert out == {"verdict": "interested", "candidate_id": "C1", "matched": True, "buyer_set": True}
    fields = svc.ds.update_party.await_args.args[2]
    assert fields["email"] == "buy@gulf.ae" and fields["party_id"] == "C1"
    confirmed.assert_awaited_once()


async def test_declined_buyer_goes_on_the_list_and_the_funnel_moves(svc, monkeypatch):
    monkeypatch.setattr(o, "classify_reply", lambda body: "declined")
    dnc, nxt = AsyncMock(), AsyncMock(return_value={"advanced": 1, "exhausted": False})
    monkeypatch.setattr(o, "_mark_do_not_contact", dnc)
    monkeypatch.setattr(o, "advance", nxt)
    out = await o.handle_reply("DEAL-O1", "C1", "buy@gulf.ae", "not interested, thanks")
    assert out["verdict"] == "declined" and out["advanced"] == 1
    dnc.assert_awaited_once()


async def test_unclear_reply_is_only_recorded(svc, monkeypatch):
    monkeypatch.setattr(o, "classify_reply", lambda body: "unclear")
    out = await o.handle_reply("DEAL-O1", "C1", "buy@gulf.ae", "who are you?")
    assert out["verdict"] == "unclear" and svc.ds.update_buyer_candidate.await_args.args[2]["reply_preview"] == "who are you?"


async def test_buyer_confirmed_readdresses_or_opens_the_negotiation(monkeypatch):
    import agents.negotiation_agent.repository as neg
    session = SimpleNamespace(buyer_email="buyer@portless.placeholder", negotiation_id="NEG-1", updated_at=None)
    repo = SimpleNamespace(get_session_by_deal=AsyncMock(return_value=session), save_session=AsyncMock())
    monkeypatch.setattr(neg, "get_repository", lambda: repo)
    await o._buyer_confirmed(_deal([], status="NEGOTIATING"), "buy@gulf.ae")
    assert session.buyer_email == "buy@gulf.ae" and repo.save_session.await_count == 1
    repo.get_session_by_deal = AsyncMock(return_value=None)
    from shared.orchestration import continuations
    started = AsyncMock()
    monkeypatch.setattr(continuations, "on_negotiation_started", started)
    await o._buyer_confirmed(_deal([], status="NEGOTIATING"), "buy@gulf.ae")
    assert started.await_args.args[0].payload == {"resumed": "buyer_interested"}


async def test_skip_and_exceptions(monkeypatch):
    import services.deals.service as ds
    monkeypatch.setattr(ds, "update_buyer_candidate", AsyncMock())
    monkeypatch.setattr(o, "advance", AsyncMock(return_value={"advanced": 0, "exhausted": True}))
    assert (await o.skip_candidate("DEAL-O1", "C1"))["exhausted"] is True
    assert ds.update_buyer_candidate.await_args.args[2]["status"] == "declined"
    events = []

    class _Pub:
        async def publish(self, e):
            events.append(e)
    monkeypatch.setattr("shared.bus.get_publisher", lambda: _Pub())
    await o._send_failed_exception("DEAL-O1", _cand("C1", "buy@gulf.ae"), 3)
    assert events[0].payload["stage"] == "OUTREACH:send_failed" and "3 tries" in events[0].payload["error"]


async def test_advance_reports_exhausted_once_every_buyer_is_done(monkeypatch):
    import services.deals.service as ds
    deal = _deal([_cand("C1", "a@a.ae", "declined"), _cand("C2", "b@b.ae", "no_reply")])
    monkeypatch.setattr(ds, "get_deal", AsyncMock(return_value=deal))
    monkeypatch.setattr(o, "start_outreach", AsyncMock(return_value=0))
    events = []

    class _Pub:
        async def publish(self, e):
            events.append(e)
    monkeypatch.setattr("shared.bus.get_publisher", lambda: _Pub())
    out = await o.advance("DEAL-O1")
    assert out == {"advanced": 0, "exhausted": True} and events[0].payload["stage"] == "OUTREACH:exhausted"
    selected = _deal([_cand("C1", "a@a.ae", "selected")])
    monkeypatch.setattr(ds, "get_deal", AsyncMock(return_value=selected))
    assert await o.advance("DEAL-O1") == {"advanced": 0, "exhausted": False}
