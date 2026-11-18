"""OUTREACH_VERIFIED_ONLY / RFQ_VERIFIED_ONLY: only checked counterparties are written to
(default off — today's behaviour)."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.deals.schemas import BuyerCandidate, DealRecord, DealStatus
from shared.orchestration import continuations, outreach

NOW = datetime.now(UTC)


def _deal(cands=(), results=()):
    return DealRecord(deal_id="P1", operator_id="op", org_id="o", hs_code="090422", market="AE",
                      status=DealStatus.MATCHING, created_at=NOW, updated_at=NOW,
                      buyer_candidates=list(cands), match_results=list(results))


def _cand(cid, status):
    return BuyerCandidate(candidate_id=cid, legal_name=cid, country="AE", email=f"b@{cid}", validation_status=status)


@pytest.fixture
def gates(monkeypatch):
    gate = AsyncMock(return_value="APPR")
    monkeypatch.setattr(outreach, "_gate", gate)
    monkeypatch.setattr(outreach, "_blocked", AsyncMock(return_value=set()))
    monkeypatch.setattr(outreach, "_suspect", AsyncMock(return_value=set()))
    monkeypatch.setenv("OUTREACH_PARALLEL", "5")
    return gate


async def test_outreach_default_writes_to_unverified_buyers_too(gates):
    assert await outreach.start_outreach(_deal([_cand("a.ae", "verified"), _cand("b.ae", "unverified")])) == 2


async def test_outreach_verified_only(gates, monkeypatch):
    monkeypatch.setenv("OUTREACH_VERIFIED_ONLY", "true")
    assert await outreach.start_outreach(_deal([_cand("a.ae", "verified"), _cand("b.ae", "unverified"),
                                                _cand("c.ae", None)])) == 1
    assert gates.await_args.args[1].candidate_id == "a.ae"


async def test_rfq_verified_only(monkeypatch):
    from services.deals.schemas import MatchResult
    from tests.factories import make
    results = [make(MatchResult, supplier_id="S1", company_name="One", decision="SOURCE", composite_score=80, verification="verified"),
               make(MatchResult, supplier_id="S2", company_name="Two", decision="SOURCE", composite_score=90, verification="unverified")]
    monkeypatch.setattr(continuations, "_rank_for_rfq", AsyncMock(side_effect=lambda d, c: c))
    deal = _deal(results=results)
    assert {c["supplier_id"] for c in await continuations._rfq_candidates(deal)} == {"S1", "S2"}
    monkeypatch.setenv("RFQ_VERIFIED_ONLY", "true")
    assert [c["supplier_id"] for c in await continuations._rfq_candidates(deal)] == ["S1"]


async def test_hand_added_suppliers_join_the_rfq(monkeypatch):
    from services.deals.schemas import MatchResult, SupplierOffer
    from shared.domain.models import Party
    from tests.factories import make
    monkeypatch.setattr(continuations, "_rank_for_rfq", AsyncMock(side_effect=lambda d, c: c))
    deal = _deal(results=[make(MatchResult, supplier_id="S1", company_name="One", decision="SOURCE", composite_score=80)])
    deal = deal.model_copy(update={
        "parties": [Party(role="supplier", party_id="S9", legal_name="Party Mill", country="IN", email="p@mill.in")],
        "supplier_offers": [
            SupplierOffer(offer_id="O1", supplier_id="D1", company_name="Directory Mill", email="d@mill.in"),
            SupplierOffer(offer_id="O2", supplier_id="Q1", company_name="Quoted Mill", email="q@mill.in", fob_price_usd_per_mt=700.0),
            SupplierOffer(offer_id="O3", supplier_id="N1", company_name="No Mail"),
        ]})
    ids = [c["supplier_id"] for c in await continuations._rfq_candidates(deal)]
    assert ids == ["S1", "S9", "D1"]  # the quoted one and the one without an address are not asked


async def test_traded_and_likely_buyers_are_written_to_first(monkeypatch):
    from services.trade_min.relationships import keys_for
    view = [(keys_for("b@traded.ae", "traded.ae"), {"deals_won": 1, "stage": "traded", "score": 40}),
            (keys_for("b@replies.ae", "replies.ae"), {"reply_rate": 0.8, "score": 60, "verified": True}),
            (keys_for("b@cold.ae", "cold.ae"), {"reply_rate": 0.0, "score": 90, "verified": True})]
    monkeypatch.setattr(outreach, "_directory_view", AsyncMock(return_value=view))
    cands = [_cand("cold.ae", "verified"), _cand("new.ae", None), _cand("replies.ae", "verified"), _cand("traded.ae", None)]
    ranked = await outreach.rank_candidates(cands)
    assert [c.candidate_id for c in ranked] == ["traded.ae", "replies.ae", "cold.ae", "new.ae"]


async def test_traded_suppliers_always_get_the_rfq_first(monkeypatch):
    from services.trade_min import directory as dr
    from services.trade_min.relationships import record_keys
    from services.trade_min.schemas import CounterpartyRecord
    dr.reset_directory()
    store = await dr.get_directory()
    rec = await store.upsert(CounterpartyRecord(
        id="supplier:oldmill.in", role="supplier", name="Old Mill", website="https://oldmill.in", email="s@oldmill.in",
        source="manual", hs_codes=["090422"], first_seen_at=NOW, last_seen_at=NOW))
    monkeypatch.setattr(outreach, "_directory_view", AsyncMock(return_value=[(record_keys(rec), {"deals_won": 2})]))
    out = await continuations._traded_suppliers_first(_deal(), [{"supplier_id": "S1", "company_name": "One", "email": "a@one.in"}])
    assert [c["supplier_id"] for c in out] == ["supplier:oldmill.in", "S1"] and out[0]["traded"]
    dr.reset_directory()


# ---------------------------------------------------------------------------
# RFQ slots (review board B-15): traded suppliers never crowd out the deal's own
# ---------------------------------------------------------------------------

def _c(sid, traded=False):
    return {"supplier_id": sid, "company_name": sid, "email": f"s@{sid}.in", "traded": traded}


def test_traded_suppliers_take_at_most_their_share(monkeypatch):
    monkeypatch.delenv("RFQ_TRADED_MAX", raising=False)
    ranked = [_c(f"T{i}", True) for i in range(5)] + [_c("S1"), _c("S2")]
    out = continuations._cap_rfq(_deal(), ranked)
    assert [c["supplier_id"] for c in out] == ["T0", "T1", "T2", "S1", "S2"]   # spare slot back to traded, ranking order kept


def test_own_suppliers_fill_when_there_are_enough(monkeypatch):
    monkeypatch.setenv("RFQ_TRADED_MAX", "1")
    ranked = [_c("T0", True), _c("T1", True)] + [_c(f"S{i}") for i in range(6)]
    out = continuations._cap_rfq(_deal(), ranked)
    assert [c["supplier_id"] for c in out] == ["T0", "S0", "S1", "S2", "S3"]


def test_no_traded_suppliers_keeps_the_ranking(monkeypatch):
    ranked = [_c(f"S{i}") for i in range(7)]
    assert [c["supplier_id"] for c in continuations._cap_rfq(_deal(), ranked)] == ["S0", "S1", "S2", "S3", "S4"]


async def test_a_traded_company_already_on_the_deal_keeps_the_deals_contact(monkeypatch):
    """B-15 follow-up (automation UC-15): the directory's traded record and the deal's own match
    result are one company — the RFQ goes to the deal's contact under the deal's supplier id."""
    from types import SimpleNamespace

    from shared.orchestration import outreach
    rec = SimpleNamespace(id="supplier:mill.in", email="old@mill.in", do_not_contact=False, hs_codes=["100630"],
                          name="Mill One", estimated_fob_price_usd=700.0, validation=None, website="https://mill.in")

    class _Store:
        async def list(self, role, limit=0):
            return [rec]

    async def view(role):
        from services.trade_min.relationships import record_keys
        return [(record_keys(rec), {"deals_won": 1, "stage": "traded"})]

    async def store():
        return _Store()
    monkeypatch.setattr(outreach, "_directory_view", view)
    monkeypatch.setattr("services.trade_min.directory.get_directory", store)
    deal = SimpleNamespace(deal_id="D1", hs_code="10063020")
    own = [{"supplier_id": "S1", "company_name": "Mill One", "email": "sales@mill.in", "fob_price_usd": 690.0}]
    out = await continuations._traded_suppliers_first(deal, own)
    assert len(out) == 1 and out[0]["traded"] is True
    assert out[0]["supplier_id"] == "S1" and out[0]["email"] == "sales@mill.in" and out[0]["fob_price_usd"] == 690.0
