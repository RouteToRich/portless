"""Tests for the Deals service — the orchestration hub of the pipeline.

Coverage:
  • create_deal      — no-opportunity DRAFT path, opportunity seeding → MATCHING,
                       highest-SOURCE winner selection, no-SOURCE (still MATCHING,
                       no winner), seed-miss → DRAFT, scoring-error isolation,
                       deal.created emission, opportunity conversion.
  • update_status    — valid transition + history, mapped-event emission,
                       invalid-transition rejection, unknown-deal 404.
  • update_financials— partial field patch, unknown-deal 404.
  • get / list       — not-found, fetch-after-create, status/operator filters.

The Trade-Min seed fetch, Match-Engine scoring, opportunity conversion, and
event publisher are all mocked, so these run fully in-memory.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from shared.domain.enums import DealStatus
from shared.domain.events import EventTopic

from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import (
    CreateDealRequest,
    DealRecord,
    MatchResult,
    StatusHistoryEntry,
    UpdateFinancialsRequest,
    UpdateStatusRequest,
)
from services.deals.service import (
    DealNotFoundError,
    InvalidTransitionError,
    create_deal,
    get_deal,
    list_deals,
    update_financials,
    update_status,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _repo(monkeypatch) -> InMemoryFallbackRepository:
    repo = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.service.get_repository", lambda: repo)
    return repo


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


_ORG = "acme.com"


def _create_request(**overrides) -> CreateDealRequest:
    defaults = dict(operator_id="OP-1", hs_code="1006", market="ES")
    defaults.update(overrides)
    return CreateDealRequest(**defaults)


def _create(payload: CreateDealRequest, org_id: str = _ORG):
    return create_deal(payload, org_id=org_id)


def _seed(**overrides) -> dict:
    d = {
        "buyer_target_price_usd": 500.0,
        "top_sellers": [
            {"supplier_id": "S1", "company_name": "Alpha", "fob_price_usd": 420.0,
             "supplier_to_port_km": 100.0},
            {"supplier_id": "S2", "company_name": "Beta", "fob_price_usd": 400.0,
             "supplier_to_port_km": 200.0},
        ],
    }
    d.update(overrides)
    return d


def _match(supplier_id: str, cms: float, decision: str = "SOURCE") -> MatchResult:
    return MatchResult(
        match_id=f"MATCH-{supplier_id}",
        supplier_id=supplier_id,
        company_name=supplier_id,
        composite_score=cms,
        gatekeeper_passed=(decision == "SOURCE"),
        arbitrage_score=cms,
        proximity_score=cms,
        reliability_score=cms,
        decision=decision,
        fob_price_usd=420.0,
        supplier_to_port_km=100.0,
    )


def _make_deal(status: DealStatus = DealStatus.DRAFT, **overrides) -> DealRecord:
    now = datetime.now(timezone.utc)
    defaults = dict(
        deal_id="DEAL-X",
        operator_id="OP-1",
        org_id=_ORG,
        hs_code="1006",
        market="ES",
        status=status,
        created_at=now,
        updated_at=now,
        status_history=[StatusHistoryEntry(status=status, transitioned_at=now, reason="seed")],
    )
    defaults.update(overrides)
    return DealRecord(**defaults)


# ---------------------------------------------------------------------------
# create_deal
# ---------------------------------------------------------------------------

def test_create_deal_without_opportunity_is_draft(_repo):
    with patch("services.deals.service._emit_event", AsyncMock()):
        deal = run(_create(_create_request()))

    assert deal.status == DealStatus.DRAFT
    assert deal.match_results == []
    assert deal.winning_supplier_id is None
    assert run(_repo.get(deal.deal_id)) is not None


def test_create_deal_with_opportunity_seeds_matching():
    convert = AsyncMock()
    with (
        patch("services.deals.service._fetch_match_seed", AsyncMock(return_value=_seed())),
        patch("services.deals.service._score_seller",
              AsyncMock(side_effect=[_match("S1", 80.0), _match("S2", 90.0)])),
        patch("services.deals.service._mark_opportunity_converted", convert),
        patch("services.deals.service._emit_event", AsyncMock()),
    ):
        deal = run(_create(_create_request(opportunity_id="OPP-1")))

    assert deal.status == DealStatus.MATCHING
    assert len(deal.match_results) == 2
    assert deal.winning_supplier_id == "S2"   # highest composite among SOURCE
    assert deal.winning_cms == 90.0
    assert deal.buyer_target_price_usd == 500.0
    convert.assert_awaited_once_with("OPP-1")


def test_create_deal_picks_highest_source_ignoring_discard():
    # DISCARD has the top score but must never win.
    with (
        patch("services.deals.service._fetch_match_seed", AsyncMock(return_value=_seed())),
        patch("services.deals.service._score_seller",
              AsyncMock(side_effect=[_match("S1", 85.0, "SOURCE"),
                                     _match("S2", 99.0, "DISCARD")])),
        patch("services.deals.service._mark_opportunity_converted", AsyncMock()),
        patch("services.deals.service._emit_event", AsyncMock()),
    ):
        deal = run(_create(_create_request(opportunity_id="OPP-1")))

    assert deal.winning_supplier_id == "S1"
    assert deal.winning_cms == 85.0


def test_create_deal_no_source_is_matching_without_winner():
    with (
        patch("services.deals.service._fetch_match_seed", AsyncMock(return_value=_seed())),
        patch("services.deals.service._score_seller",
              AsyncMock(side_effect=[_match("S1", 40.0, "DISCARD"),
                                     _match("S2", 30.0, "DISCARD")])),
        patch("services.deals.service._mark_opportunity_converted", AsyncMock()),
        patch("services.deals.service._emit_event", AsyncMock()),
    ):
        deal = run(_create(_create_request(opportunity_id="OPP-1")))

    # Seed present → MATCHING regardless of SOURCE; no winner selected.
    assert deal.status == DealStatus.MATCHING
    assert deal.winning_supplier_id is None
    assert deal.winning_cms is None
    assert len(deal.match_results) == 2


def test_create_deal_seed_miss_falls_back_to_draft():
    convert = AsyncMock()
    with (
        patch("services.deals.service._fetch_match_seed", AsyncMock(return_value=None)),
        patch("services.deals.service._mark_opportunity_converted", convert),
        patch("services.deals.service._emit_event", AsyncMock()),
    ):
        deal = run(_create(_create_request(opportunity_id="OPP-GONE")))

    assert deal.status == DealStatus.DRAFT
    assert deal.match_results == []
    convert.assert_awaited_once()   # conversion is best-effort, still attempted


def test_create_deal_isolates_scoring_error():
    with (
        patch("services.deals.service._fetch_match_seed", AsyncMock(return_value=_seed())),
        patch("services.deals.service._score_seller",
              AsyncMock(side_effect=[_match("S1", 70.0), RuntimeError("ME down")])),
        patch("services.deals.service._mark_opportunity_converted", AsyncMock()),
        patch("services.deals.service._emit_event", AsyncMock()),
    ):
        deal = run(_create(_create_request(opportunity_id="OPP-1")))

    # The failed seller is dropped; the deal still forms with the good score.
    assert len(deal.match_results) == 1
    assert deal.winning_supplier_id == "S1"


def test_create_deal_emits_deal_created():
    emit = AsyncMock()
    with patch("services.deals.service._emit_event", emit):
        deal = run(_create(_create_request()))

    emit.assert_awaited_once()
    assert emit.call_args.args[0] == EventTopic.DEAL_CREATED
    payload = emit.call_args.kwargs["payload"]
    assert payload["status"] == DealStatus.DRAFT.value
    assert payload["hs_code"] == "1006"


# ---------------------------------------------------------------------------
# update_status
# ---------------------------------------------------------------------------

def test_update_status_valid_transition_records_history(_repo):
    run(_repo.save(_make_deal(DealStatus.DRAFT)))
    with patch("services.deals.service._emit_event", AsyncMock()):
        deal = run(update_status("DEAL-X", UpdateStatusRequest(
            target_status=DealStatus.MATCHING, reason="seeded")))

    assert deal.status == DealStatus.MATCHING
    assert deal.status_history[-1].status == DealStatus.MATCHING
    assert deal.status_history[-1].reason == "seeded"


def test_update_status_emits_mapped_event(_repo):
    run(_repo.save(_make_deal(DealStatus.MATCHING)))
    emit = AsyncMock()
    with patch("services.deals.service._emit_event", emit):
        run(update_status("DEAL-X", UpdateStatusRequest(
            target_status=DealStatus.NEGOTIATING, reason="go")))

    emit.assert_awaited_once()
    assert emit.call_args.args[0] == EventTopic.NEGOTIATION_STARTED


def test_update_status_unmapped_status_emits_nothing(_repo):
    # DRAFT → MATCHING is valid but MATCHING has no mapped domain event.
    run(_repo.save(_make_deal(DealStatus.DRAFT)))
    emit = AsyncMock()
    with patch("services.deals.service._emit_event", emit):
        run(update_status("DEAL-X", UpdateStatusRequest(target_status=DealStatus.MATCHING)))

    emit.assert_not_awaited()


def test_update_status_invalid_transition_raises(_repo):
    run(_repo.save(_make_deal(DealStatus.DRAFT)))
    with pytest.raises(InvalidTransitionError):
        run(update_status("DEAL-X", UpdateStatusRequest(target_status=DealStatus.PAID)))


def test_update_status_unknown_deal_raises():
    with pytest.raises(DealNotFoundError):
        run(update_status("DEAL-GHOST", UpdateStatusRequest(target_status=DealStatus.MATCHING)))


# ---------------------------------------------------------------------------
# update_financials
# ---------------------------------------------------------------------------

def test_update_financials_patches_only_supplied_fields(_repo):
    run(_repo.save(_make_deal(DealStatus.MATCHING, buyer_target_price_usd=500.0)))
    deal = run(update_financials("DEAL-X", UpdateFinancialsRequest(
        winning_supplier_id="S9", winning_cms=77.0)))

    assert deal.winning_supplier_id == "S9"
    assert deal.winning_cms == 77.0
    assert deal.buyer_target_price_usd == 500.0   # untouched


def test_update_financials_partial_single_field(_repo):
    run(_repo.save(_make_deal(DealStatus.MATCHING)))
    deal = run(update_financials("DEAL-X", UpdateFinancialsRequest(est_margin_usd=1234.5)))

    assert deal.est_margin_usd == 1234.5
    assert deal.realized_margin_usd is None


def test_update_financials_unknown_deal_raises():
    with pytest.raises(DealNotFoundError):
        run(update_financials("DEAL-GHOST", UpdateFinancialsRequest(winning_cms=1.0)))


# ---------------------------------------------------------------------------
# get / list
# ---------------------------------------------------------------------------

def test_get_deal_unknown_raises():
    with pytest.raises(DealNotFoundError):
        run(get_deal("DEAL-GHOST"))


def test_get_deal_after_create():
    with patch("services.deals.service._emit_event", AsyncMock()):
        created = run(_create(_create_request()))
    fetched = run(get_deal(created.deal_id))
    assert fetched.deal_id == created.deal_id


def test_list_deals_filters_by_status_and_operator(_repo):
    run(_repo.save(_make_deal(DealStatus.DRAFT, deal_id="D1", operator_id="OP-A")))
    run(_repo.save(_make_deal(DealStatus.MATCHING, deal_id="D2", operator_id="OP-A")))
    run(_repo.save(_make_deal(DealStatus.MATCHING, deal_id="D3", operator_id="OP-B")))

    matching = run(list_deals(status=DealStatus.MATCHING))
    assert {d.deal_id for d in matching} == {"D2", "D3"}

    op_a = run(list_deals(operator_id="OP-A"))
    assert {d.deal_id for d in op_a} == {"D1", "D2"}


def test_list_deals_filters_by_org(_repo):
    run(_repo.save(_make_deal(DealStatus.DRAFT, deal_id="D1", org_id="acme.com")))
    run(_repo.save(_make_deal(DealStatus.DRAFT, deal_id="D2", org_id="acme.com")))
    run(_repo.save(_make_deal(DealStatus.DRAFT, deal_id="D3", org_id="other.com")))

    acme = run(list_deals(org_id="acme.com"))
    assert {d.deal_id for d in acme} == {"D1", "D2"}

    unrestricted = run(list_deals(org_id=None))
    assert {d.deal_id for d in unrestricted} == {"D1", "D2", "D3"}
