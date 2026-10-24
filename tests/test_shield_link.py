"""Tests for Shield-Link service: QC booking, QC webhook, freight, CHA filing."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from services.shield_link.repository import InMemoryShieldLinkRepository, init_repository
from services.shield_link.schemas import (
    ChaFilingRequest,
    FreightBookingRequest,
    FreightMilestonePayload,
    FreightQuoteRequest,
    QcBookingRequest,
    QcWebhookPayload,
)
from services.shield_link.service import (
    ConnectorCallError,
    DealNotFoundError,
    InvalidDealState,
    TpoNotFoundError,
    WebhookVerifyError,
    book_freight,
    book_qc,
    file_cha,
    get_tpo,
    handle_freight_milestone,
    handle_qc_result,
    list_deal_orders,
    quote_freight,
)


# ---------------------------------------------------------------------------
# Freight quoting (read-only, powers negotiation logistics_cost)
# ---------------------------------------------------------------------------

class TestFreightQuote:
    @pytest.mark.asyncio
    async def test_quote_single_container_per_unit(self):
        resp = await quote_freight(FreightQuoteRequest(quantity_mt=20.0))
        assert resp.provider == "cogoport"
        assert resp.containers == 1
        assert resp.total_freight_usd == resp.rate_usd_per_container
        assert resp.logistics_cost_per_unit_usd == round(resp.total_freight_usd / 20.0, 4)
        assert resp.stub is True

    @pytest.mark.asyncio
    async def test_quote_multi_container_rounds_up(self):
        resp = await quote_freight(FreightQuoteRequest(quantity_mt=60.0))
        assert resp.containers == 3  # ceil(60 / 25 MT-per-40HC)
        assert resp.total_freight_usd == resp.rate_usd_per_container * 3
        assert resp.logistics_cost_per_unit_usd == round(resp.total_freight_usd / 60.0, 4)

    @pytest.mark.asyncio
    async def test_quote_without_quantity_omits_per_unit(self):
        resp = await quote_freight(FreightQuoteRequest())
        assert resp.containers == 1
        assert resp.logistics_cost_per_unit_usd is None

    @pytest.mark.asyncio
    async def test_quote_alternate_provider(self):
        resp = await quote_freight(FreightQuoteRequest(provider="freightos", quantity_mt=25.0))
        assert resp.provider == "freightos"
        assert resp.rate_usd_per_container > 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _mock_deal(deal_id: str = "DEAL-SL-01", status: str = "PO_ISSUED") -> object:
    class FakeDealStatus:
        def __init__(self, v: str) -> None:
            self.value = v

        def __str__(self) -> str:
            return self.value

    class FakeDeal:
        def __init__(self) -> None:
            self.deal_id = deal_id
            self.status = FakeDealStatus(status)
            self.hs_code = "7208.51"

    return FakeDeal()


@pytest.fixture(autouse=True)
def use_in_memory_repo():
    """Force in-memory repository for every test."""
    import services.shield_link.repository as repo_mod
    repo_mod._repository = InMemoryShieldLinkRepository()
    repo_mod._motor_client = None
    yield
    repo_mod._repository = None


# ---------------------------------------------------------------------------
# QC booking
# ---------------------------------------------------------------------------

class TestBookQc:
    @pytest.mark.asyncio
    async def test_book_qc_success(self):
        deal = _mock_deal(status="PO_ISSUED")
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()),
            patch("services.shield_link.service._emit", new=AsyncMock()),
        ):
            req = QcBookingRequest(deal_id="DEAL-SL-01", provider="sgs")
            result = await book_qc(req)
        assert result.order_type == "QC"
        assert result.status == "BOOKED"
        assert result.ext_ref is not None

    @pytest.mark.asyncio
    async def test_book_qc_wrong_state(self):
        deal = _mock_deal(status="DRAFT")
        with patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)):
            with pytest.raises(InvalidDealState):
                await book_qc(QcBookingRequest(deal_id="DEAL-SL-01"))

    @pytest.mark.asyncio
    async def test_book_qc_deal_not_found(self):
        with patch(
            "services.shield_link.service._fetch_deal",
            new=AsyncMock(side_effect=DealNotFoundError("DEAL-MISSING")),
        ):
            with pytest.raises(DealNotFoundError):
                await book_qc(QcBookingRequest(deal_id="DEAL-MISSING"))

    @pytest.mark.asyncio
    async def test_book_qc_idempotent(self):
        deal = _mock_deal(status="PO_ISSUED")
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()),
            patch("services.shield_link.service._emit", new=AsyncMock()),
        ):
            req = QcBookingRequest(deal_id="DEAL-SL-01", idempotency_key="idem-qc-1")
            r1 = await book_qc(req)
            r2 = await book_qc(req)
        assert r1.tpo_id == r2.tpo_id

    @pytest.mark.asyncio
    async def test_book_qc_connector_error(self):
        from shared.connectors.shields import ConnectorError as ShieldConnectorError
        deal = _mock_deal(status="PO_ISSUED")
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch(
                "shared.connectors.shields.SgsConnector.book",
                new=AsyncMock(side_effect=ShieldConnectorError("SGS down")),
            ),
        ):
            with pytest.raises(ConnectorCallError):
                await book_qc(QcBookingRequest(deal_id="DEAL-SL-01", provider="sgs"))


# ---------------------------------------------------------------------------
# QC webhook
# ---------------------------------------------------------------------------

class TestQcWebhook:
    @pytest.mark.asyncio
    async def test_qc_pass_transitions_deal(self):
        deal = _mock_deal(status="PO_ISSUED")
        # First book the QC so we have a TPO
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()),
            patch("services.shield_link.service._emit", new=AsyncMock()),
        ):
            tpo = await book_qc(QcBookingRequest(deal_id="DEAL-SL-01", provider="sgs"))

        ext_ref = tpo.ext_ref
        payload = QcWebhookPayload(ext_ref=ext_ref, result="PASS", report_url="https://sgs.example.com/report.pdf")
        with (
            patch("shared.connectors.shields.SgsConnector.webhook_verify", new=AsyncMock(return_value=True)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()) as mock_trans,
            patch("services.shield_link.service._emit", new=AsyncMock()),
        ):
            result = await handle_qc_result(ext_ref, payload, {}, b"{}")
        assert result["result"] == "PASS"
        mock_trans.assert_awaited_once_with("DEAL-SL-01", "QC_PASSED", pytest.approx("QC passed — ref " + ext_ref, rel=0))

    @pytest.mark.asyncio
    async def test_qc_fail_transitions_deal(self):
        deal = _mock_deal(status="PO_ISSUED")
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()),
            patch("services.shield_link.service._emit", new=AsyncMock()),
        ):
            tpo = await book_qc(QcBookingRequest(deal_id="DEAL-SL-01", provider="sgs"))

        payload = QcWebhookPayload(ext_ref=tpo.ext_ref, result="FAIL", notes="Moisture content too high")
        with (
            patch("shared.connectors.shields.SgsConnector.webhook_verify", new=AsyncMock(return_value=True)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()) as mock_trans,
            patch("services.shield_link.service._emit", new=AsyncMock()),
        ):
            result = await handle_qc_result(tpo.ext_ref, payload, {}, b"{}")
        assert result["result"] == "FAIL"
        mock_trans.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_qc_webhook_bad_signature(self):
        deal = _mock_deal(status="PO_ISSUED")
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()),
            patch("services.shield_link.service._emit", new=AsyncMock()),
        ):
            tpo = await book_qc(QcBookingRequest(deal_id="DEAL-SL-01", provider="sgs"))

        payload = QcWebhookPayload(ext_ref=tpo.ext_ref, result="PASS")
        with patch("shared.connectors.shields.SgsConnector.webhook_verify", new=AsyncMock(return_value=False)):
            with pytest.raises(WebhookVerifyError):
                await handle_qc_result(tpo.ext_ref, payload, {}, b"{}")

    @pytest.mark.asyncio
    async def test_qc_webhook_unknown_ext_ref(self):
        payload = QcWebhookPayload(ext_ref="UNKNOWN-REF", result="PASS")
        with patch("shared.connectors.shields.SgsConnector.webhook_verify", new=AsyncMock(return_value=True)):
            with pytest.raises(TpoNotFoundError):
                await handle_qc_result("UNKNOWN-REF", payload, {}, b"{}")


# ---------------------------------------------------------------------------
# Freight booking
# ---------------------------------------------------------------------------

class TestBookFreight:
    @pytest.mark.asyncio
    async def test_book_freight_success(self):
        deal = _mock_deal(status="QC_PASSED")
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()),
            patch("services.shield_link.service._emit", new=AsyncMock()),
        ):
            req = FreightBookingRequest(
                deal_id="DEAL-SL-01",
                provider="cogoport",
                origin_port="INBOM",
                destination_port="DEHAM",
            )
            result = await book_freight(req)
        assert result.order_type == "FREIGHT"
        assert result.status == "BOOKED"

    @pytest.mark.asyncio
    async def test_book_freight_wrong_state(self):
        deal = _mock_deal(status="PO_ISSUED")
        with patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)):
            with pytest.raises(InvalidDealState):
                await book_freight(FreightBookingRequest(deal_id="DEAL-SL-01"))

    @pytest.mark.asyncio
    async def test_book_freight_idempotent(self):
        deal = _mock_deal(status="QC_PASSED")
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()),
            patch("services.shield_link.service._emit", new=AsyncMock()),
        ):
            req = FreightBookingRequest(deal_id="DEAL-SL-01", idempotency_key="idem-frt-1")
            r1 = await book_freight(req)
            r2 = await book_freight(req)
        assert r1.tpo_id == r2.tpo_id


# ---------------------------------------------------------------------------
# Freight milestone
# ---------------------------------------------------------------------------

class TestFreightMilestone:
    @pytest.mark.asyncio
    async def test_arrived_transitions_to_cleared(self):
        deal = _mock_deal(status="QC_PASSED")
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()),
            patch("services.shield_link.service._emit", new=AsyncMock()),
        ):
            tpo = await book_freight(FreightBookingRequest(deal_id="DEAL-SL-01"))

        payload = FreightMilestonePayload(ext_ref=tpo.ext_ref, milestone="ARRIVED")
        with (
            patch("shared.connectors.shields.CogoportConnector.webhook_verify", new=AsyncMock(return_value=True)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()) as mock_trans,
            patch("services.shield_link.service._emit", new=AsyncMock()),
        ):
            result = await handle_freight_milestone(tpo.ext_ref, payload, {}, b"{}")
        assert result["milestone"] == "ARRIVED"
        mock_trans.assert_awaited_once_with("DEAL-SL-01", "CLEARED", pytest.approx("Cargo ARRIVED — ref " + tpo.ext_ref, rel=0))

    @pytest.mark.asyncio
    async def test_departed_no_deal_transition(self):
        deal = _mock_deal(status="QC_PASSED")
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()),
            patch("services.shield_link.service._emit", new=AsyncMock()),
        ):
            tpo = await book_freight(FreightBookingRequest(deal_id="DEAL-SL-01"))

        payload = FreightMilestonePayload(ext_ref=tpo.ext_ref, milestone="DEPARTED")
        with (
            patch("shared.connectors.shields.CogoportConnector.webhook_verify", new=AsyncMock(return_value=True)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()) as mock_trans,
            patch("services.shield_link.service._emit", new=AsyncMock()),
        ):
            result = await handle_freight_milestone(tpo.ext_ref, payload, {}, b"{}")
        assert result["milestone"] == "DEPARTED"
        mock_trans.assert_not_awaited()


# ---------------------------------------------------------------------------
# CHA filing
# ---------------------------------------------------------------------------

class TestFileCha:
    @pytest.mark.asyncio
    async def test_file_cha_success(self):
        deal = _mock_deal(status="IN_TRANSIT")
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.shield_link.service._emit", new=AsyncMock()),
        ):
            req = ChaFilingRequest(deal_id="DEAL-SL-01", documents=["doc-1", "doc-2"])
            result = await file_cha(req)
        assert result.order_type == "CHA"
        assert result.status == "COMPLETED"

    @pytest.mark.asyncio
    async def test_file_cha_idempotent(self):
        deal = _mock_deal(status="IN_TRANSIT")
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.shield_link.service._emit", new=AsyncMock()),
        ):
            req = ChaFilingRequest(deal_id="DEAL-SL-01", idempotency_key="idem-cha-1")
            r1 = await file_cha(req)
            r2 = await file_cha(req)
        assert r1.tpo_id == r2.tpo_id


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

class TestReadHelpers:
    @pytest.mark.asyncio
    async def test_get_tpo_not_found(self):
        with pytest.raises(TpoNotFoundError):
            await get_tpo("TPO-NOPE")

    @pytest.mark.asyncio
    async def test_list_deal_orders_empty(self):
        result = await list_deal_orders("DEAL-SL-EMPTY")
        assert result.orders == []
