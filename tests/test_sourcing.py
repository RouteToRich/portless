"""Tests for the Sourcing service.

Coverage:
  • RFQ dispatch          — connector success, portal fallback, PARTIAL/FAILED status,
                            persistence + ``quote.requested`` event, message building.
  • Supplier verification — GSTN/DGFT/MCA/Udyam happy path, GST-suspended, denied-party,
                            conservative pass-through defaults, no-credential skip.
  • Quote intake pipeline — SOURCE → transition, DISCARD → no transition, non-MATCHING
                            guard, deal-absent persistence, verification-failure isolation,
                            re-score skip without buyer price, ``quote.received`` event.
  • Fetch / list          — not-found errors, deal-scoped listing.

All connector and inter-service seams are mocked, so these run fully in-memory
with no MongoDB, no network, and no sibling services.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from shared.domain.events import EventTopic

from services.sourcing.repository import InMemorySourcingRepository
from services.sourcing.schemas import (
    DispatchResult,
    QuoteIntakeRequest,
    RfqRequest,
    SupplierRef,
    VerificationResult,
)
from services.sourcing.service import (
    QuoteNotFoundError,
    RfqNotFoundError,
    _build_rfq_message,
    dispatch_rfq,
    get_quote,
    get_rfq,
    list_quotes,
    list_rfqs,
    receive_quote,
    verify_supplier,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _repo(monkeypatch) -> InMemorySourcingRepository:
    """Inject a fresh in-memory repository for every test."""
    repo = InMemorySourcingRepository()
    monkeypatch.setattr("services.sourcing.service.get_repository", lambda: repo)
    return repo


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _supplier(
    sid: str = "SUP-1",
    source: str = "indiamart",
    contact_id: str | None = "C-1",
    name: str = "Sunrise Agro",
) -> SupplierRef:
    return SupplierRef(
        supplier_id=sid, source=source, contact_id=contact_id, company_name=name
    )


def _rfq_request(**overrides) -> RfqRequest:
    defaults = dict(
        deal_id="DEAL-SRC",
        hs_code="1006",
        suppliers=[_supplier()],
        quantity_mt=100.0,
        target_price_usd=500.0,
    )
    defaults.update(overrides)
    return RfqRequest(**defaults)


def _quote_request(**overrides) -> QuoteIntakeRequest:
    defaults = dict(
        rfq_id="RFQ-1",
        deal_id="DEAL-SRC",
        supplier_id="SUP-1",
        company_name="Sunrise Agro",
        unit_price_usd=450.0,
        quantity_mt=100.0,
        gstin="33AAAAA0000A1Z5",
        iec_number="0388000000",
        cin_number="U01100TN2010PTC000000",
        udyam_number="UDYAM-TN-01-0000123",
    )
    defaults.update(overrides)
    return QuoteIntakeRequest(**defaults)


def _verification(**overrides) -> VerificationResult:
    defaults = dict(
        supplier_id="SUP-1",
        gst_verified=True,
        iec_active=True,
        dgft_denied=False,
        business_age_years=5.0,
        udyam_registered=True,
        annual_shipment_count=0,
        verified_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return VerificationResult(**defaults)


def _fake_safe_call(mapping: dict[tuple[str, str], dict | None]):
    """Return an async stand-in for ``service._safe_call`` keyed by (name, method)."""

    async def _call(name: str, method: str, request: dict) -> dict | None:
        return mapping.get((name, method))

    return _call


def _matching_deal(**overrides) -> dict:
    d = {"status": "MATCHING", "buyer_target_price_usd": 500.0, "match_results": []}
    d.update(overrides)
    return d


def _source_rescore(**overrides) -> dict:
    d = {
        "match_id": "M-1",
        "composite_score": 88.0,
        "gatekeeper_passed": True,
        "decision": "SOURCE",
    }
    d.update(overrides)
    return d


# ---------------------------------------------------------------------------
# RFQ dispatch
# ---------------------------------------------------------------------------

def test_dispatch_rfq_all_success_via_connectors():
    booked = _fake_safe_call({
        ("indiamart", "book"): {"rfq_id": "IM-EXT-9"},
        ("tradeindia", "book"): {"rfq_id": "TI-EXT-9"},
    })
    with (
        patch("services.sourcing.service._safe_call", booked),
        patch("services.sourcing.service._emit_event", AsyncMock()),
    ):
        resp = run(dispatch_rfq(_rfq_request(suppliers=[
            _supplier("SUP-A", "indiamart", "C-A"),
            _supplier("SUP-B", "tradeindia", "C-B"),
        ])))

    assert resp.status == "COMPLETE"
    assert resp.dispatched_count == 2
    assert resp.failed_count == 0
    assert {d.channel for d in resp.dispatch_results} == {"indiamart", "tradeindia"}
    im = next(d for d in resp.dispatch_results if d.supplier_id == "SUP-A")
    assert im.ext_rfq_id == "IM-EXT-9"
    assert all(d.success for d in resp.dispatch_results)


def test_dispatch_rfq_portal_fallback_when_connector_returns_none():
    # Connector call returns None → treated as queued-for-manual, not failed.
    with (
        patch("services.sourcing.service._safe_call", _fake_safe_call({})),
        patch("services.sourcing.service._emit_event", AsyncMock()),
    ):
        resp = run(dispatch_rfq(_rfq_request()))

    assert resp.status == "COMPLETE"
    assert resp.dispatched_count == 1
    assert resp.dispatch_results[0].channel == "portal"
    assert resp.dispatch_results[0].success is True


def test_dispatch_rfq_portal_fallback_when_no_contact_id():
    with (
        patch("services.sourcing.service._safe_call", _fake_safe_call({})),
        patch("services.sourcing.service._emit_event", AsyncMock()),
    ):
        resp = run(dispatch_rfq(_rfq_request(suppliers=[_supplier(contact_id=None)])))

    assert resp.dispatch_results[0].channel == "portal"
    assert resp.status == "COMPLETE"


def test_dispatch_rfq_partial_when_one_supplier_errors():
    async def _one(rfq_id, supplier, message, payload):
        if supplier.supplier_id == "SUP-BAD":
            raise RuntimeError("connector exploded")
        return DispatchResult(
            supplier_id=supplier.supplier_id, channel="indiamart", success=True
        )

    with (
        patch("services.sourcing.service._dispatch_one", _one),
        patch("services.sourcing.service._emit_event", AsyncMock()),
    ):
        resp = run(dispatch_rfq(_rfq_request(suppliers=[
            _supplier("SUP-OK"), _supplier("SUP-BAD"),
        ])))

    assert resp.status == "PARTIAL"
    assert resp.dispatched_count == 1
    assert resp.failed_count == 1
    bad = next(d for d in resp.dispatch_results if d.supplier_id == "SUP-BAD")
    assert bad.success is False
    assert bad.channel == "failed"


def test_dispatch_rfq_failed_when_all_error():
    async def _one(rfq_id, supplier, message, payload):
        raise RuntimeError("boom")

    with (
        patch("services.sourcing.service._dispatch_one", _one),
        patch("services.sourcing.service._emit_event", AsyncMock()),
    ):
        resp = run(dispatch_rfq(_rfq_request()))

    assert resp.status == "FAILED"
    assert resp.dispatched_count == 0
    assert resp.failed_count == 1


def test_dispatch_rfq_persists_and_emits(_repo):
    emit = AsyncMock()
    with (
        patch("services.sourcing.service._safe_call", _fake_safe_call({})),
        patch("services.sourcing.service._emit_event", emit),
    ):
        resp = run(dispatch_rfq(_rfq_request()))

    stored = run(_repo.get_rfq(resp.rfq_id))
    assert stored is not None
    assert stored.deal_id == "DEAL-SRC"
    assert stored.status == "COMPLETE"

    emit.assert_awaited_once()
    assert emit.call_args.args[0] == EventTopic.QUOTE_REQUESTED
    kwargs = emit.call_args.kwargs
    assert kwargs["deal_id"] == "DEAL-SRC"
    assert kwargs["payload"]["rfq_id"] == resp.rfq_id


# ---------------------------------------------------------------------------
# RFQ message building
# ---------------------------------------------------------------------------

def test_build_rfq_message_custom_passthrough():
    assert _build_rfq_message(_rfq_request(message="Custom hello")) == "Custom hello"


def test_build_rfq_message_uses_template():
    msg = _build_rfq_message(_rfq_request(hs_code="1006", quantity_mt=100.0))
    assert "1006" in msg
    assert "100" in msg
    assert "DEAL-SRC" in msg


# ---------------------------------------------------------------------------
# Supplier verification
# ---------------------------------------------------------------------------

def test_verify_supplier_all_active():
    calls = _fake_safe_call({
        ("gstn", "quote"): {"status": "ACTIVE"},
        ("dgft", "quote"): {"status": "ACTIVE"},
        ("dgft", "book"): {"on_denied_list": False},
        ("mca", "quote"): {"age_years": 7.5},
        ("udyam", "quote"): {"registered": True},
    })
    with patch("services.sourcing.service._safe_call", calls):
        res = run(verify_supplier("SUP-1", gstin="G", iec="I", cin="C", udyam_number="U"))

    assert res.gst_verified is True
    assert res.iec_active is True
    assert res.dgft_denied is False
    assert res.business_age_years == 7.5
    assert res.udyam_registered is True


def test_verify_supplier_gst_suspended_marks_unverified():
    calls = _fake_safe_call({
        ("gstn", "quote"): {"status": "SUSPENDED"},
        ("dgft", "quote"): {"status": "ACTIVE"},
        ("dgft", "book"): {"on_denied_list": False},
    })
    with patch("services.sourcing.service._safe_call", calls):
        res = run(verify_supplier("SUP-1", gstin="G", iec="I"))

    assert res.gst_verified is False
    assert res.iec_active is True


def test_verify_supplier_flags_denied_party():
    calls = _fake_safe_call({
        ("dgft", "quote"): {"status": "ACTIVE"},
        ("dgft", "book"): {"on_denied_list": True},
    })
    with patch("services.sourcing.service._safe_call", calls):
        res = run(verify_supplier("SUP-1", iec="I"))

    assert res.dgft_denied is True


def test_verify_supplier_conservative_defaults_when_apis_unavailable():
    # Every connector returns None (missing creds / API down). This documents the
    # current graceful-degradation behaviour: an unverifiable supplier passes.
    with patch("services.sourcing.service._safe_call", _fake_safe_call({})):
        res = run(verify_supplier("SUP-1", gstin="G", iec="I", cin="C", udyam_number="U"))

    assert res.gst_verified is True
    assert res.iec_active is True
    assert res.dgft_denied is False
    assert res.business_age_years == 0.0
    assert res.udyam_registered is False


def test_verify_supplier_no_credentials_skips_all_calls():
    safe = AsyncMock()
    with patch("services.sourcing.service._safe_call", safe):
        res = run(verify_supplier("SUP-1"))

    safe.assert_not_called()
    assert res.raw == {}
    assert res.supplier_id == "SUP-1"


# ---------------------------------------------------------------------------
# Quote intake pipeline
# ---------------------------------------------------------------------------

def test_receive_quote_sources_and_transitions(_repo, monkeypatch):
    monkeypatch.setenv("MATCHMAKING_ENABLED", "false")  # legacy: a SOURCE quote opens the negotiation itself
    transition = AsyncMock()
    financials = AsyncMock()
    with (
        patch("services.sourcing.service._verify_supplier", AsyncMock(return_value=_verification())),
        patch("services.sourcing.service._fetch_deal", AsyncMock(return_value=_matching_deal())),
        patch("services.sourcing.service._rescore", AsyncMock(return_value=_source_rescore())),
        patch("services.sourcing.service._update_deal_financials", financials),
        patch("services.sourcing.service._transition_to_negotiating", transition),
        patch("services.sourcing.service._emit_event", AsyncMock()),
    ):
        quote = run(receive_quote(_quote_request()))

    assert quote.status == "VERIFIED"
    assert quote.re_scored_decision == "SOURCE"
    assert quote.re_scored_cms == 88.0
    financials.assert_awaited_once()
    transition.assert_awaited_once()
    assert run(_repo.get_quote(quote.quote_id)) is not None


def test_receive_quote_discard_does_not_transition():
    transition = AsyncMock()
    with (
        patch("services.sourcing.service._verify_supplier", AsyncMock(return_value=_verification())),
        patch("services.sourcing.service._fetch_deal", AsyncMock(return_value=_matching_deal())),
        patch("services.sourcing.service._rescore",
              AsyncMock(return_value=_source_rescore(composite_score=30.0, decision="DISCARD"))),
        patch("services.sourcing.service._update_deal_financials", AsyncMock()),
        patch("services.sourcing.service._transition_to_negotiating", transition),
        patch("services.sourcing.service._emit_event", AsyncMock()),
    ):
        quote = run(receive_quote(_quote_request()))

    assert quote.re_scored_decision == "DISCARD"
    transition.assert_not_awaited()


def test_receive_quote_no_transition_when_deal_not_matching():
    transition = AsyncMock()
    with (
        patch("services.sourcing.service._verify_supplier", AsyncMock(return_value=_verification())),
        patch("services.sourcing.service._fetch_deal",
              AsyncMock(return_value=_matching_deal(status="NEGOTIATING"))),
        patch("services.sourcing.service._rescore", AsyncMock(return_value=_source_rescore())),
        patch("services.sourcing.service._update_deal_financials", AsyncMock()),
        patch("services.sourcing.service._transition_to_negotiating", transition),
        patch("services.sourcing.service._emit_event", AsyncMock()),
    ):
        run(receive_quote(_quote_request()))

    # SOURCE decision, but the deal is no longer MATCHING → guard blocks transition.
    transition.assert_not_awaited()


def test_receive_quote_persists_when_deal_absent():
    rescore = AsyncMock()
    with (
        patch("services.sourcing.service._verify_supplier", AsyncMock(return_value=_verification())),
        patch("services.sourcing.service._fetch_deal", AsyncMock(return_value=None)),
        patch("services.sourcing.service._rescore", rescore),
        patch("services.sourcing.service._emit_event", AsyncMock()),
    ):
        quote = run(receive_quote(_quote_request()))

    assert quote.status == "VERIFIED"        # verification still ran
    assert quote.re_scored_decision is None  # no deal → no re-score
    rescore.assert_not_called()


def test_receive_quote_isolates_verification_failure():
    with (
        patch("services.sourcing.service._verify_supplier",
              AsyncMock(side_effect=RuntimeError("gstn 500"))),
        patch("services.sourcing.service._fetch_deal", AsyncMock(return_value=_matching_deal())),
        patch("services.sourcing.service._emit_event", AsyncMock()),
    ):
        quote = run(receive_quote(_quote_request()))

    assert quote.status == "RECEIVED"     # verification failed → downgraded status
    assert quote.verification is None
    assert quote.re_scored_decision is None


def test_receive_quote_skips_rescore_without_buyer_price():
    rescore = AsyncMock()
    with (
        patch("services.sourcing.service._verify_supplier", AsyncMock(return_value=_verification())),
        patch("services.sourcing.service._fetch_deal",
              AsyncMock(return_value=_matching_deal(buyer_target_price_usd=None))),
        patch("services.sourcing.service._rescore", rescore),
        patch("services.sourcing.service._emit_event", AsyncMock()),
    ):
        quote = run(receive_quote(_quote_request()))

    rescore.assert_not_called()
    assert quote.re_scored_cms is None


def test_receive_quote_emits_quote_received():
    emit = AsyncMock()
    with (
        patch("services.sourcing.service._verify_supplier", AsyncMock(return_value=_verification())),
        patch("services.sourcing.service._fetch_deal", AsyncMock(return_value=_matching_deal())),
        patch("services.sourcing.service._rescore", AsyncMock(return_value=_source_rescore())),
        patch("services.sourcing.service._update_deal_financials", AsyncMock()),
        patch("services.sourcing.service._transition_to_negotiating", AsyncMock()),
        patch("services.sourcing.service._emit_event", emit),
    ):
        quote = run(receive_quote(_quote_request()))

    emit.assert_awaited_once()
    assert emit.call_args.args[0] == EventTopic.QUOTE_RECEIVED
    payload = emit.call_args.kwargs["payload"]
    assert payload["quote_id"] == quote.quote_id
    assert payload["re_scored_decision"] == "SOURCE"


# ---------------------------------------------------------------------------
# Fetch / list + error paths
# ---------------------------------------------------------------------------

def test_get_rfq_not_found_raises():
    with pytest.raises(RfqNotFoundError):
        run(get_rfq("RFQ-GHOST"))


def test_get_quote_not_found_raises():
    with pytest.raises(QuoteNotFoundError):
        run(get_quote("QUOTE-GHOST"))


def test_list_rfqs_after_dispatch():
    with (
        patch("services.sourcing.service._safe_call", _fake_safe_call({})),
        patch("services.sourcing.service._emit_event", AsyncMock()),
    ):
        run(dispatch_rfq(_rfq_request(deal_id="DEAL-L")))
        run(dispatch_rfq(_rfq_request(deal_id="DEAL-L")))

    assert len(run(list_rfqs("DEAL-L"))) == 2


def test_list_quotes_filters_by_deal():
    with (
        patch("services.sourcing.service._verify_supplier", AsyncMock(return_value=_verification())),
        patch("services.sourcing.service._fetch_deal", AsyncMock(return_value=None)),
        patch("services.sourcing.service._emit_event", AsyncMock()),
    ):
        run(receive_quote(_quote_request(deal_id="DEAL-Q1", supplier_id="S1")))
        run(receive_quote(_quote_request(deal_id="DEAL-Q2", supplier_id="S2")))

    q1 = run(list_quotes(deal_id="DEAL-Q1"))
    assert len(q1) == 1
    assert q1[0].deal_id == "DEAL-Q1"
