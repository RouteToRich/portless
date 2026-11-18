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
