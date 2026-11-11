"""Tests for the buyer-selectable QC provider ecosystem.

Coverage:
  • shared/connectors/qc_providers — registry, aliases, catalog, stub
    quote/book economics, deterministic mention detection, webhook HMAC.
  • shield_link — provider catalog + quote endpoints, provider resolution in
    book_qc (explicit → buyer-opted → default), unknown-provider rejection.
  • negotiation_agent — QC cost seeded into the envelope at start, buyer
    opt-in captured (deterministic + classifier paths) and envelope re-priced.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from shared.connectors.qc_providers import (
    QC_CONNECTOR_CLASSES,
    QC_PROVIDER_CATALOG,
    BureauVeritasConnector,
    IntertekConnector,
    QimaConnector,
    SgsConnector,
    TuvSudConnector,
    cheapest_qc_provider,
    detect_qc_provider_mention,
    get_qc_connector,
    resolve_qc_provider,
)


# ---------------------------------------------------------------------------
# Connector registry + catalog
# ---------------------------------------------------------------------------

class TestQcProviderRegistry:
    def test_top_five_registered(self):
        assert set(QC_CONNECTOR_CLASSES) == {"sgs", "qima", "bv", "intertek", "tuv_sud"}
        assert set(QC_PROVIDER_CATALOG) == set(QC_CONNECTOR_CLASSES)

    def test_catalog_all_available_in_india_with_photo_evidence(self):
        for meta in QC_PROVIDER_CATALOG.values():
            assert meta["available_in_india"] is True
            assert meta["evidence"]["photos"] is True
            assert meta["indicative_fee_usd"] > 0

    def test_get_connector_per_provider(self):
        assert isinstance(get_qc_connector("sgs"), SgsConnector)
        assert isinstance(get_qc_connector("qima"), QimaConnector)
        assert isinstance(get_qc_connector("bv"), BureauVeritasConnector)
        assert isinstance(get_qc_connector("intertek"), IntertekConnector)
        assert isinstance(get_qc_connector("tuv_sud"), TuvSudConnector)

    def test_get_connector_alias_and_unknown_fallback(self):
        assert isinstance(get_qc_connector("Bureau Veritas"), BureauVeritasConnector)
        assert isinstance(get_qc_connector("TÜV SÜD"), TuvSudConnector)
        assert isinstance(get_qc_connector("no-such-agency"), SgsConnector)

    def test_resolve_aliases(self):
        assert resolve_qc_provider("SGS") == "sgs"
        assert resolve_qc_provider("bureau veritas") == "bv"
        assert resolve_qc_provider("BureauVeritas") == "bv"
        assert resolve_qc_provider("tuv sud") == "tuv_sud"
        assert resolve_qc_provider("AsiaInspection") == "qima"
        assert resolve_qc_provider("acme") is None
        assert resolve_qc_provider(None) is None

    def test_cheapest_provider_overall_is_qima(self):
        assert cheapest_qc_provider() == "qima"  # lowest indicative fee (289)

    def test_cheapest_provider_among_candidates(self):
        assert cheapest_qc_provider(["bv", "tuv_sud"]) == "bv"  # 340 < 380
        assert cheapest_qc_provider(["sgs", "intertek"]) == "sgs"  # 320 < 330

    def test_cheapest_provider_ignores_unknown_candidates(self):
        assert cheapest_qc_provider(["acme", "tuv_sud"]) == "tuv_sud"
        assert cheapest_qc_provider(["acme"]) == "qima"  # falls back to catalog


class TestQcMentionDetection:
    def test_single_mention(self):
        assert detect_qc_provider_mention("We would prefer QIMA for inspection.") == "qima"
        assert detect_qc_provider_mention("Please use Bureau Veritas.") == "bv"

    def test_multiple_mentions_ambiguous(self):
        assert detect_qc_provider_mention("Either SGS or Intertek works for us.") is None

    def test_no_mention(self):
        assert detect_qc_provider_mention("We accept the price of USD 470.") is None
        assert detect_qc_provider_mention(None) is None
        assert detect_qc_provider_mention("") is None


# ---------------------------------------------------------------------------
# Connector stubs (no API keys in test env)
# ---------------------------------------------------------------------------

class TestQcConnectorStubs:
    @pytest.mark.asyncio
    async def test_stub_quote_returns_fee_and_per_unit(self):
        quote = await QimaConnector().quote({"quantity_mt": 25.0})
        assert quote["stub"] is True
        assert quote["provider"] == "qima"
        assert quote["fee_usd"] > 0
        assert quote["qc_cost_per_unit_usd"] == round(quote["fee_usd"] / 25.0, 4)
        assert quote["evidence"] == {"photos": True, "video": True}

    @pytest.mark.asyncio
    async def test_stub_quote_without_quantity_omits_per_unit(self):
        quote = await SgsConnector().quote({})
        assert quote["qc_cost_per_unit_usd"] is None

    @pytest.mark.asyncio
    async def test_stub_fees_differ_per_provider(self):
        fees = {}
        for key in QC_CONNECTOR_CLASSES:
            q = await get_qc_connector(key).quote({"quantity_mt": 10})
            fees[key] = q["fee_usd"]
        assert len(set(fees.values())) == len(fees)

    @pytest.mark.asyncio
    async def test_stub_book_carries_provider_ref_prefix(self):
        result = await TuvSudConnector().book({"deal_id": "DEAL-1"})
        assert result["status"] == "BOOKED"
        assert result["ext_ref"].startswith("TUV-STUB-")
        assert result["provider"] == "tuv_sud"

    @pytest.mark.asyncio
    async def test_webhook_verify_accepts_all_without_secret(self):
        assert await IntertekConnector().webhook_verify({}, b"{}") is True

    @pytest.mark.asyncio
    async def test_webhook_verify_hmac(self, monkeypatch):
        monkeypatch.setenv("QIMA_WEBHOOK_SECRET", "s3cret")
        connector = QimaConnector()
        body = b'{"result": "PASS"}'
        good = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        assert await connector.webhook_verify({"x-qima-signature": good}, body) is True
        assert await connector.webhook_verify({"x-qima-signature": "bad"}, body) is False


# ---------------------------------------------------------------------------
# Shield-Link — catalog, quote, provider resolution
# ---------------------------------------------------------------------------

from services.shield_link.repository import InMemoryShieldLinkRepository  # noqa: E402
from services.shield_link.schemas import QcBookingRequest, QcQuoteRequest  # noqa: E402
from services.shield_link.service import (  # noqa: E402
    UnknownQcProviderError,
    book_qc,
    list_qc_providers,
    quote_qc,
)


def _mock_deal(deal_id: str = "DEAL-QC-01", status: str = "PO_ISSUED") -> object:
    class FakeDealStatus:
        def __init__(self, v: str) -> None:
            self.value = v

    class FakeDeal:
        def __init__(self) -> None:
            self.deal_id = deal_id
            self.status = FakeDealStatus(status)
            self.hs_code = "7208.51"

    return FakeDeal()


@pytest.fixture(autouse=True)
def use_in_memory_repo():
    import services.shield_link.repository as repo_mod
    repo_mod._repository = InMemoryShieldLinkRepository()
    repo_mod._motor_client = None
    yield
    repo_mod._repository = None


class TestShieldLinkQcProviders:
    def test_catalog_endpoint_lists_top_five(self):
        resp = list_qc_providers()
        assert resp.default_provider == "qima"
        assert {p.provider for p in resp.providers} == set(QC_PROVIDER_CATALOG)
        assert all(p.available_in_india for p in resp.providers)

    @pytest.mark.asyncio
    async def test_quote_qc_default_provider(self):
        resp = await quote_qc(QcQuoteRequest(quantity_mt=20.0))
        assert resp.provider == "qima"
        assert resp.fee_usd > 0
        assert resp.qc_cost_per_unit_usd == round(resp.fee_usd / 20.0, 4)
        assert resp.stub is True

    @pytest.mark.asyncio
    async def test_quote_qc_alias_resolution(self):
        resp = await quote_qc(QcQuoteRequest(provider="Bureau Veritas", quantity_mt=10.0))
        assert resp.provider == "bv"
        assert resp.display_name == "Bureau Veritas"

    @pytest.mark.asyncio
    async def test_quote_qc_unknown_provider_rejected(self):
        with pytest.raises(UnknownQcProviderError):
            await quote_qc(QcQuoteRequest(provider="acme-inspections"))

    @pytest.mark.asyncio
    async def test_book_qc_explicit_provider(self):
        deal = _mock_deal()
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()),
            patch("services.shield_link.service._emit", new=AsyncMock()),
        ):
            tpo = await book_qc(QcBookingRequest(deal_id="DEAL-QC-01", provider="qima"))
        assert tpo.provider == "qima"
        assert tpo.ext_ref.startswith("QIMA-STUB-")

    @pytest.mark.asyncio
    async def test_book_qc_unknown_provider_rejected(self):
        deal = _mock_deal()
        with patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)):
            with pytest.raises(UnknownQcProviderError):
                await book_qc(QcBookingRequest(deal_id="DEAL-QC-01", provider="acme"))

    @pytest.mark.asyncio
    async def test_book_qc_uses_buyer_opted_provider(self):
        """No provider in the request → the negotiation's buyer-opted provider wins."""
        deal = _mock_deal()
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()),
            patch("services.shield_link.service._emit", new=AsyncMock()),
            patch(
                "services.shield_link.service._buyer_opted_qc_provider",
                new=AsyncMock(return_value="intertek"),
            ),
        ):
            tpo = await book_qc(QcBookingRequest(deal_id="DEAL-QC-01"))
        assert tpo.provider == "intertek"

    @pytest.mark.asyncio
    async def test_book_qc_without_opt_in_books_cheapest(self):
        """Buyer never opted → the cheapest catalog provider is booked."""
        deal = _mock_deal()
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()),
            patch("services.shield_link.service._emit", new=AsyncMock()),
            patch(
                "services.shield_link.service._buyer_opted_qc_provider",
                new=AsyncMock(return_value=None),
            ),
        ):
            tpo = await book_qc(QcBookingRequest(deal_id="DEAL-QC-01"))
        assert tpo.provider == "qima"

    @pytest.mark.asyncio
    async def test_book_qc_fallback_strategy_default(self, monkeypatch):
        from services.shield_link import service as sl_service
        monkeypatch.setattr(sl_service.settings, "qc_fallback_strategy", "default")
        deal = _mock_deal()
        with (
            patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=deal)),
            patch("services.shield_link.service._transition_deal", new=AsyncMock()),
            patch("services.shield_link.service._emit", new=AsyncMock()),
            patch(
                "services.shield_link.service._buyer_opted_qc_provider",
                new=AsyncMock(return_value=None),
            ),
        ):
            tpo = await book_qc(QcBookingRequest(deal_id="DEAL-QC-01"))
        assert tpo.provider == "qima"


# ---------------------------------------------------------------------------
# Negotiation — QC cost seeding + buyer opt-in
# ---------------------------------------------------------------------------

from agents.negotiation_agent.schemas import (  # noqa: E402
    CostEnvelope,
    NegotiationPolicy,
    NegotiationSession,
    StartNegotiationRequest,
)
from agents.negotiation_agent.service import (  # noqa: E402
    _apply_buyer_qc_choice,
    _seed_logistics_cost,
    _seed_qc_costing,
)


def _start_payload(**overrides) -> StartNegotiationRequest:
    defaults = dict(
        deal_id="DEAL-1",
        trade_match_id="TM-1",
        buyer_id="BYR-1",
        seller_id="SUP-1",
        product_code="1006",
        buyer_country_code="DE",
        buyer_target_price=500.0,
        supplier_quote_price=460.0,
        policy=NegotiationPolicy(),
    )
    defaults.update(overrides)
    return StartNegotiationRequest(**defaults)


def _session(**overrides) -> NegotiationSession:
    now = datetime.now(timezone.utc)
    defaults = dict(
        negotiation_id="neg-1",
        deal_id="DEAL-1",
        trade_match_id="TM-1",
        buyer_id="BYR-1",
        seller_id="SUP-1",
        product_code="1006",
        buyer_country_code="DE",
        buyer_target_price=500.0,
        supplier_quote_price=460.0,
        policy=NegotiationPolicy(),
        status="ROUND",
        round_no=1,
        current_bid=500.0,
        current_ask=460.0,
        created_at=now,
        updated_at=now,
    )
    defaults.update(overrides)
    return NegotiationSession(**defaults)


class TestNegotiationQcCosting:
    @pytest.mark.asyncio
    async def test_seed_qc_costing_fills_empty_envelope(self):
        provider, envelope = await _seed_qc_costing(_start_payload(quantity_mt=25.0))
        assert provider is None  # buyer hasn't opted yet
        assert envelope is not None
        assert float(envelope.qc_cost) > 0

    @pytest.mark.asyncio
    async def test_seed_qc_costing_keeps_caller_supplied_cost(self):
        supplied = CostEnvelope(qc_cost=Decimal("2.5"))
        provider, envelope = await _seed_qc_costing(
            _start_payload(cost_envelope=supplied, qc_provider="QIMA")
        )
        assert provider == "qima"
        assert envelope.qc_cost == Decimal("2.5")

    @pytest.mark.asyncio
    async def test_seed_qc_costing_uses_opted_provider_fee(self):
        provider, envelope = await _seed_qc_costing(
            _start_payload(qc_provider="tuv sud", quantity_mt=10.0)
        )
        assert provider == "tuv_sud"
        # TÜV SÜD stub: 380 base + 1.5/MT × 10 = 395 → 39.5/unit at 10 MT
        assert float(envelope.qc_cost) == pytest.approx(39.5, abs=0.01)

    @pytest.mark.asyncio
    async def test_apply_buyer_qc_choice_reprices_envelope(self):
        session = _session(cost_envelope=CostEnvelope(qc_cost=Decimal("1.0")), quantity_mt=25.0)
        await _apply_buyer_qc_choice(session, "qima")
        assert session.qc_provider == "qima"
        # QIMA stub: 289 + 1.5×25 = 326.5 → 13.06/unit at 25 MT
        assert float(session.cost_envelope.qc_cost) == pytest.approx(13.06, abs=0.01)

    @pytest.mark.asyncio
    async def test_apply_buyer_qc_choice_creates_envelope_when_missing(self):
        session = _session(cost_envelope=None)
        await _apply_buyer_qc_choice(session, "bv")
        assert session.qc_provider == "bv"
        assert session.cost_envelope is not None
        assert float(session.cost_envelope.qc_cost) > 0


class TestNegotiationFreightCosting:
    """Ocean freight is quoted into the margin floor at negotiation start —
    buyer prices are CIF, supplier Ex-Works."""

    @pytest.mark.asyncio
    async def test_seed_logistics_cost_from_stub_quote(self):
        cost = await _seed_logistics_cost(_start_payload(quantity_mt=25.0))
        # Cogoport stub: $2,850 per 40HC ÷ 25 MT = $114/MT
        assert cost == pytest.approx(114.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_seed_logistics_cost_caller_override_wins(self):
        cost = await _seed_logistics_cost(
            _start_payload(logistics_cost_usd=42.0, quantity_mt=25.0)
        )
        assert cost == 42.0

    @pytest.mark.asyncio
    async def test_seed_logistics_cost_disabled_flag(self, monkeypatch):
        from agents.negotiation_agent import service as neg_service
        monkeypatch.setattr(neg_service.settings, "freight_cost_enabled", False)
        assert await _seed_logistics_cost(_start_payload(quantity_mt=25.0)) is None

    @pytest.mark.asyncio
    async def test_seed_logistics_cost_never_raises(self):
        with patch(
            "services.shield_link.service.quote_freight",
            new=AsyncMock(side_effect=RuntimeError("provider down")),
        ):
            assert await _seed_logistics_cost(_start_payload(quantity_mt=25.0)) is None


# ---------------------------------------------------------------------------
# Agreement completeness checklist
# ---------------------------------------------------------------------------

from agents.negotiation_agent.guards import missing_agreement_info  # noqa: E402


class TestAgreementChecklist:
    def test_missing_info_listed(self):
        session = _session()  # no qc_provider, no quantity_mt
        missing = missing_agreement_info(session)
        assert len(missing) == 2
        assert any("QC inspection agency" in m for m in missing)
        assert any("quantity" in m for m in missing)

    def test_complete_session_has_no_missing_info(self):
        session = _session(qc_provider="sgs", quantity_mt=25.0)
        assert missing_agreement_info(session) == []

    @pytest.mark.asyncio
    async def test_agreement_withheld_when_info_missing(self):
        """Explicit acceptance at healthy margin but missing checklist items →
        no NEGOTIATION_AGREED; buyer agent drafts an info-request instead."""
        from agents.negotiation_agent.graph import orchestrator_node as node
        session = _session()  # missing qc_provider + quantity_mt
        state = _graph_state(session, inbound_message="We accept your offer.")
        with patch(
            "agents.negotiation_agent.graph._chat",
            new=_classifier_chat(intent="ACCEPT"),
        ):
            out = await node(state)
        assert out["routing"] == "CALL_BUYER_AGENT"
        assert out["pending_agreement_info"]
        assert out["hitl_required"] is True
        assert "Agreement withheld" in out["reasoning"]

    @pytest.mark.asyncio
    async def test_agreement_declared_when_checklist_complete(self):
        from agents.negotiation_agent.graph import orchestrator_node as node
        session = _session(qc_provider="sgs", quantity_mt=25.0)
        state = _graph_state(session, inbound_message="We accept your offer.")
        with patch(
            "agents.negotiation_agent.graph._chat",
            new=_classifier_chat(intent="ACCEPT"),
        ):
            out = await node(state)
        assert out["routing"] == "NEGOTIATION_AGREED"

    @pytest.mark.asyncio
    async def test_qc_choice_in_accepting_reply_satisfies_checklist(self):
        """A QC opt-in arriving in the same reply as the acceptance counts."""
        from agents.negotiation_agent.graph import orchestrator_node as node
        session = _session(quantity_mt=25.0)  # qc_provider still unset
        state = _graph_state(
            session, inbound_message="We accept, and QIMA works for inspection.",
        )
        with patch(
            "agents.negotiation_agent.graph._chat",
            new=_classifier_chat(intent="ACCEPT", qc_providers=["qima"]),
        ):
            out = await node(state)
        assert out["routing"] == "NEGOTIATION_AGREED"
        assert out["mentioned_qc_providers"] == ["qima"]

    @pytest.mark.asyncio
    async def test_operator_override_bypasses_checklist(self):
        from agents.negotiation_agent.graph import orchestrator_node as node
        session = _session()  # missing info
        state = _graph_state(
            session,
            inbound_message="We accept your offer.",
            operator_override_price=500.0,
        )
        with patch(
            "agents.negotiation_agent.graph._chat",
            new=_classifier_chat(intent="ACCEPT"),
        ):
            out = await node(state)
        assert out["routing"] == "NEGOTIATION_AGREED"

    @pytest.mark.asyncio
    async def test_checklist_disabled_flag(self, monkeypatch):
        from agents.negotiation_agent import graph as graph_mod
        from agents.negotiation_agent.graph import orchestrator_node as node
        monkeypatch.setattr(graph_mod.settings, "agreement_checklist_enabled", False)
        session = _session()  # missing info
        state = _graph_state(session, inbound_message="We accept your offer.")
        with patch(
            "agents.negotiation_agent.graph._chat",
            new=_classifier_chat(intent="ACCEPT"),
        ):
            out = await node(state)
        assert out["routing"] == "NEGOTIATION_AGREED"


# ---------------------------------------------------------------------------
# Negotiation graph — classifier extraction + buyer-agent payload
# ---------------------------------------------------------------------------

from agents.negotiation_agent.graph import (  # noqa: E402
    buyer_agent_node,
    classify_inbound_intent,
    orchestrator_node,
)


def _graph_state(session: NegotiationSession, **overrides) -> dict:
    s = {
        "session": session,
        "logistics_cost": 0.0,
        "is_first_round": False,
        "party": "buyer",
        "inbound_message": None,
        "inbound_price": None,
        "operator_override_price": None,
        "current_bid": session.current_bid,
        "current_ask": session.current_ask,
        "current_margin": 0.0,
        "orchestrator_decision": None,
        "buyer_outbound_message": "",
        "supplier_outbound_message": "",
        "buyer_terms": {},
        "supplier_terms": {},
        "routing": "CALL_BUYER_AGENT",
        "hitl_required": False,
        "agreed_price": None,
        "reasoning": "",
        "error": None,
    }
    s.update(overrides)
    return s


def _classifier_chat(intent: str = "COUNTER_OFFER", qc_providers: list[str] | None = None):
    async def _chat(system_prompt: str, payload: dict, caller: str) -> str:
        if "intent_classifier" in caller:
            return json.dumps({
                "intent": intent,
                "mentioned_price_usd": None,
                "mentioned_qc_providers": qc_providers or [],
                "reasoning": "test",
            })
        return json.dumps({
            "trade_match_id": "TM-1",
            "current_estimated_margin_pct": 10.0,
            "action_routing": "CALL_SUPPLIER_AGENT",
            "margin_at_current": 10.0,
            "hitl_required": False,
            "reasoning": "route",
            "buyer_round": 0,
            "supplier_round": 0,
        })
    return _chat


class TestGraphQcCapture:
    @pytest.mark.asyncio
    async def test_classifier_returns_resolved_qc_providers(self):
        with patch(
            "agents.negotiation_agent.graph._chat",
            new=AsyncMock(return_value=json.dumps({
                "intent": "COUNTER_OFFER",
                "mentioned_price_usd": 480.0,
                "mentioned_qc_providers": ["bureau veritas", "TUV SUD"],
                "reasoning": "buyer accepts either",
            })),
        ):
            result = await classify_inbound_intent("buyer", "BV or TUV both fine", 500.0)
        assert result["mentioned_qc_providers"] == ["bv", "tuv_sud"]

    @pytest.mark.asyncio
    async def test_classifier_tolerates_legacy_singular_field(self):
        with patch(
            "agents.negotiation_agent.graph._chat",
            new=AsyncMock(return_value=json.dumps({
                "intent": "COUNTER_OFFER",
                "mentioned_price_usd": None,
                "mentioned_qc_provider": "qima",
                "reasoning": "singular form",
            })),
        ):
            result = await classify_inbound_intent("buyer", "We choose QIMA", 500.0)
        assert result["mentioned_qc_providers"] == ["qima"]

    @pytest.mark.asyncio
    async def test_orchestrator_propagates_buyer_qc_mention(self):
        session = _session()
        state = _graph_state(session, inbound_message="We choose QIMA. Counter at 480.")
        with patch(
            "agents.negotiation_agent.graph._chat",
            new=_classifier_chat(qc_providers=["qima"]),
        ):
            out = await orchestrator_node(state)
        assert out.get("mentioned_qc_providers") == ["qima"]

    @pytest.mark.asyncio
    async def test_orchestrator_ignores_supplier_qc_mention(self):
        session = _session()
        state = _graph_state(
            session, party="supplier", inbound_message="We can arrange SGS ourselves.",
        )
        with patch(
            "agents.negotiation_agent.graph._chat",
            new=_classifier_chat(qc_providers=["sgs"]),
        ):
            out = await orchestrator_node(state)
        assert out.get("mentioned_qc_providers") is None

    @pytest.mark.asyncio
    async def test_buyer_agent_payload_includes_qc_options(self):
        session = _session()
        captured: dict = {}

        async def _capture_chat(system_prompt: str, payload: dict, caller: str) -> str:
            captured.update(payload)
            return json.dumps({
                "counter_price_usd": 495.0,
                "outbound_message": "offer",
                "negotiated_terms": {"unit_price": 495.0, "currency": "USD"},
                "reasoning": "r",
            })

        with patch("agents.negotiation_agent.graph._chat", new=_capture_chat):
            await buyer_agent_node(_graph_state(session))
        assert "QIMA" in captured["qc_provider_options"]
        assert "SGS" in captured["qc_provider_options"]
        assert captured["buyer_selected_qc_provider"] == "NOT_YET_SELECTED"

    @pytest.mark.asyncio
    async def test_buyer_agent_payload_shows_selected_provider(self):
        session = _session(qc_provider="tuv_sud")
        captured: dict = {}

        async def _capture_chat(system_prompt: str, payload: dict, caller: str) -> str:
            captured.update(payload)
            return json.dumps({
                "counter_price_usd": 495.0,
                "outbound_message": "offer",
                "negotiated_terms": {"unit_price": 495.0, "currency": "USD"},
                "reasoning": "r",
            })

        with patch("agents.negotiation_agent.graph._chat", new=_capture_chat):
            await buyer_agent_node(_graph_state(session))
        assert captured["buyer_selected_qc_provider"] == "TÜV SÜD"

    @pytest.mark.asyncio
    async def test_buyer_agent_payload_includes_pending_info(self):
        session = _session()
        captured: dict = {}

        async def _capture_chat(system_prompt: str, payload: dict, caller: str) -> str:
            captured.update(payload)
            return json.dumps({
                "counter_price_usd": 495.0,
                "outbound_message": "offer",
                "negotiated_terms": {"unit_price": 495.0, "currency": "USD"},
                "reasoning": "r",
            })

        state = _graph_state(session)
        state["pending_agreement_info"] = ["confirmed order quantity (MT)"]
        with patch("agents.negotiation_agent.graph._chat", new=_capture_chat):
            await buyer_agent_node(state)
        assert captured["pending_agreement_info"] == ["confirmed order quantity (MT)"]
