"""HTTP surface of every service: status codes for each failure, and the REST clients' calls."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.negotiation_agent import router as negotiation_router
from agents.negotiation_agent import schemas as negotiation_schemas
from agents.negotiation_agent.client import NegotiationAgentClient
from agents.negotiation_agent.email_history import EmailHistoryRecord
from services.api_gateway import router as gateway_router
from services.api_gateway.client import ApiGatewayClient
from services.dealmaker import router as dealmaker_router
from services.dealmaker import schemas as dealmaker_schemas
from services.dealmaker.client import DealMakerClient
from services.deals.client import DealsClient
from services.docugen import router as docugen_router
from services.docugen import schemas as docugen_schemas
from services.docugen.client import DocuGenClient
from services.email_gateway.client import EmailGatewayClient
from services.email_gateway.schemas import SendEmailRequest, SendEmailResult
from services.email_processor import router as email_router
from services.email_processor import schemas as email_schemas
from services.email_processor.client import EmailProcessorClient
from services.escrow_guard.client import EscrowGuardClient
from services.exception_resolver import router as exception_router
from services.exception_resolver import schemas as exception_schemas
from services.exception_resolver.client import ExceptionResolverClient
from services.hitl_approval import router as hitl_router
from services.hitl_approval import schemas as hitl_schemas
from services.hitl_approval.client import HitlApprovalClient
from services.match_engine.client import MatchEngineClient
from services.shield_link import router as shield_router
from services.shield_link import schemas as shield_schemas
from services.shield_link.client import ShieldLinkClient
from services.sourcing import router as sourcing_router
from services.sourcing import schemas as sourcing_schemas
from services.sourcing.client import SourcingClient
from services.trade_min import router as trade_router
from services.trade_min import schemas as trade_schemas
from services.trade_min.client import TradeMinClient
from shared.connectors.google_drive import GoogleDriveError
from tests.factories import make


def _api(module) -> TestClient:
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app, raise_server_exceptions=False)


def _error(cls):
    """An instance of *cls* whatever its constructor wants; routes only read str(exc)."""
    return cls.__new__(cls)


def _body(model):
    return model if isinstance(model, dict) else make(model).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Shield-Link
# ---------------------------------------------------------------------------

S = shield_schemas
_S = shield_router
TPO = S.ThirdPartyOrderResponse
SHIELD_ROUTES = [
    ("POST", "/shield-link/qc/quote", "quote_qc", S.QcQuoteRequest, S.QcQuoteResponse, 200,
     [(_S.UnknownQcProviderError, 422), (_S.ConnectorCallError, 502)]),
    ("POST", "/shield-link/qc", "book_qc", S.QcBookingRequest, TPO, 201,
     [(_S.DealNotFoundError, 404), (_S.UnknownQcProviderError, 422), (_S.InvalidDealState, 422), (_S.ConnectorCallError, 502)]),
    ("POST", "/shield-link/freight/quote", "quote_freight", S.FreightQuoteRequest, S.FreightQuoteResponse, 200,
     [(_S.ConnectorCallError, 502)]),
    ("POST", "/shield-link/freight", "book_freight", S.FreightBookingRequest, TPO, 201,
     [(_S.DealNotFoundError, 404), (_S.InvalidDealState, 422), (_S.ConnectorCallError, 502)]),
    ("POST", "/shield-link/freight/R1/milestone", "record_milestone", S.RecordMilestoneRequest, {"ok": True}, 200,
     [(_S.TpoNotFoundError, 404)]),
    ("POST", "/shield-link/deals/D1/loading", "record_loading", {"container_numbers": ["C1"], "seal_numbers": ["S1"]}, {"ok": True}, 200,
     [(_S.DealNotFoundError, 404)]),
    ("POST", "/shield-link/cha", "file_cha", S.ChaFilingRequest, TPO, 201,
     [(_S.DealNotFoundError, 404), (_S.ConnectorCallError, 502)]),
    ("GET", "/shield-link/tpo/T1", "get_tpo", None, TPO, 200, [(_S.TpoNotFoundError, 404)]),
    ("POST", "/shield-link/deals/D1/track", "poll_tracking", None, {"polled": True}, 200, []),
]


def _result(spec):
    return spec if isinstance(spec, dict) else make(spec)


def _flatten(routes):
    for method, url, target, body, result, ok, errors in routes:
        yield pytest.param(method, url, target, body, result, ok, None, id=f"{target}-ok")
        for error, code in errors:
            yield pytest.param(method, url, target, body, result, code, error, id=f"{target}-{error.__name__}")


def _check(module, method, url, target_owner, target, body, result, expected, error):
    payload = _body(body) if body else None
    mock = AsyncMock(side_effect=_error(error)) if error else AsyncMock(return_value=_result(result))
    with patch.object(target_owner, target, new=mock):
        response = _api(module).request(method, url, json=payload)
    assert response.status_code == expected, response.text
    mock.assert_awaited_once()


@pytest.mark.parametrize(("method", "url", "target", "body", "result", "expected", "error"), list(_flatten(SHIELD_ROUTES)))
def test_shield_link_routes(method, url, target, body, result, expected, error):
    _check(shield_router, method, url, shield_router, target, body, result, expected, error)


class TestShieldLinkWebhooks:
    CASES = (("/shield-link/qc/webhook", "handle_qc_result", S.QcWebhookPayload),
             ("/shield-link/freight/webhook", "handle_freight_milestone", S.FreightMilestonePayload))

    @pytest.mark.parametrize(("url", "target", "model"), CASES)
    def test_ok_passes_reference_headers_and_raw_body(self, url, target, model):
        body = {**_body(model), "ext_ref": "E1"}
        mock = AsyncMock(return_value={"status": "ok"})
        with patch.object(shield_router, target, new=mock):
            response = _api(shield_router).post(url, json=body, headers={"x-sgs-signature": "sig"})
        assert response.status_code == 200
        call = mock.await_args.kwargs
        assert call["ext_ref"] == "E1" and call["raw_headers"]["x-sgs-signature"] == "sig"
        assert json.loads(call["raw_body"])["ext_ref"] == "E1"

    @pytest.mark.parametrize(("url", "target", "model"), CASES)
    @pytest.mark.parametrize(("error", "code"), [(_S.WebhookVerifyError, 401), (_S.TpoNotFoundError, 404)])
    def test_error_mapping(self, url, target, model, error, code):
        with patch.object(shield_router, target, new=AsyncMock(side_effect=_error(error))):
            assert _api(shield_router).post(url, json=_body(model)).status_code == code

    @pytest.mark.parametrize(("url", "target", "model"), CASES)
    def test_internal_errors_do_not_leak(self, url, target, model):
        with patch.object(shield_router, target, new=AsyncMock(side_effect=RuntimeError("secret"))):
            response = _api(shield_router).post(url, json=_body(model))
        assert response.status_code == 500 and response.json()["detail"] == "Webhook processing failed"

    @pytest.mark.parametrize("url", ["/shield-link/qc/webhook", "/shield-link/freight/webhook"])
    def test_malformed_body_is_a_client_error(self, url):
        response = _api(shield_router).post(url, content=b"{not json", headers={"content-type": "application/json"})
        assert response.status_code == 400

    @pytest.mark.parametrize("url", ["/shield-link/qc/webhook", "/shield-link/freight/webhook"])
    @pytest.mark.parametrize("body", [{"nonsense": 1}, [1, 2]])
    def test_invalid_shape_is_a_client_error(self, url, body):
        response = _api(shield_router).post(url, json=body)
        assert response.status_code == 422 and response.json()["detail"] == "Invalid webhook payload"

    def test_providers_and_lists(self):
        api = _api(shield_router)
        assert api.get("/shield-link/health").json()["status"] == "ok"
        assert api.get("/shield-link/qc/providers").status_code == 200
        with patch.object(shield_router, "list_deal_orders", new=AsyncMock(return_value=make(S.DealOrdersResponse))):
            assert api.get("/shield-link/deals/D1/orders").status_code == 200


# ---------------------------------------------------------------------------
# DealMaker + DocuGen
# ---------------------------------------------------------------------------

M = dealmaker_schemas
_M = dealmaker_router
DEALMAKER_ROUTES = [
    ("POST", "/dealmaker/pi", "issue_pi", M.IssuePiRequest, M.PiResponse, 201, [(_M.DealNotFoundError, 404), (_M.InvalidDealState, 422)]),
    ("POST", "/dealmaker/pi/P1/accept", "accept_pi", M.AcceptPiRequest, M.PiResponse, 200,
     [(_M.PiNotFoundError, 404), (_M.PiStateError, 422), (_M.DealNotFoundError, 422), (_M.InvalidDealState, 422)]),
    ("GET", "/dealmaker/pi/P1", "get_pi", None, M.PiResponse, 200, [(_M.PiNotFoundError, 404)]),
    ("POST", "/dealmaker/po", "issue_po", M.IssuePoRequest, M.PoResponse, 201,
     [(_M.DealNotFoundError, 404), (_M.InvalidDealState, 422), (_M.MarginGuardError, 422)]),
    ("GET", "/dealmaker/po/P1", "get_po", None, M.PoResponse, 200, [(_M.PoNotFoundError, 404)]),
    ("GET", "/dealmaker/deals/D1/documents", "get_deal_documents", None, M.DealDocumentsResponse, 200, []),
]


@pytest.mark.parametrize(("method", "url", "target", "body", "result", "expected", "error"), list(_flatten(DEALMAKER_ROUTES)))
def test_dealmaker_routes(method, url, target, body, result, expected, error):
    _check(dealmaker_router, method, url, dealmaker_router.service, target, body, result, expected, error)


D = docugen_schemas
_D = docugen_router
DOCUGEN_ROUTES = [
    ("POST", "/docugen/documents", "generate_document", D.GenerateDocumentRequest, D.DocumentResponse, 201, [(_D.DealNotFoundError, 404), (_D.DocuGenError, 422)]),
    ("GET", "/docugen/documents/X1", "get_document", None, D.DocumentResponse, 200, [(_D.DocumentNotFoundError, 404)]),
    ("GET", "/docugen/deals/D1/documents", "list_deal_documents", None, D.DealDocumentsListResponse, 200, []),
    ("POST", "/docugen/customs/file", "file_customs", D.CustomsFilingRequest, D.CustomsFilingResponse, 201, [(_D.DealNotFoundError, 404), (_D.DocuGenError, 422)]),
]


@pytest.mark.parametrize(("method", "url", "target", "body", "result", "expected", "error"), list(_flatten(DOCUGEN_ROUTES)))
def test_docugen_routes(method, url, target, body, result, expected, error):
    _check(docugen_router, method, url, docugen_router.service, target, body, result, expected, error)


FILE_ROUTES = [
    (dealmaker_router, "/dealmaker/pi/P1/file", "get_pi_file", dealmaker_router.PiNotFoundError),
    (dealmaker_router, "/dealmaker/po/P1/file", "get_po_file", dealmaker_router.PoNotFoundError),
    (docugen_router, "/docugen/documents/X1/file", "get_document_file", docugen_router.DocumentNotFoundError),
]


@pytest.mark.parametrize(("module", "url", "target", "missing"), FILE_ROUTES)
class TestFileRoutes:
    def test_inline_by_default(self, module, url, target, missing):
        with patch.object(module.service, target, new=AsyncMock(return_value=(b"<html>", "text/html", "pi.html"))):
            response = _api(module).get(url)
        assert response.content == b"<html>" and response.headers["content-disposition"] == 'inline; filename="pi.html"'

    def test_download_switches_disposition(self, module, url, target, missing):
        with patch.object(module.service, target, new=AsyncMock(return_value=(b"x", "text/html", "pi.html"))):
            response = _api(module).get(url, params={"download": "true"})
        assert response.headers["content-disposition"].startswith("attachment")

    @pytest.mark.parametrize(("kind", "code"), [("missing", 404), ("file", 404), ("drive", 502)])
    def test_failures(self, module, url, target, missing, kind, code):
        error = {"missing": _error(missing), "file": FileNotFoundError("gone"), "drive": GoogleDriveError("down")}[kind]
        with patch.object(module.service, target, new=AsyncMock(side_effect=error)):
            assert _api(module).get(url).status_code == code


@pytest.mark.parametrize(("module", "url"), [
    (dealmaker_router, "/dealmaker/health"), (docugen_router, "/docugen/health"), (sourcing_router, "/sourcing/health"),
    (exception_router, "/exception-resolver/health"), (negotiation_router, "/negotiation-agent/health"),
    (email_router, "/email-processor/health"), (trade_router, "/trade-min/health"), (hitl_router, "/hitl-approval/health"),
    (gateway_router, "/health"),
])
def test_health_routes(module, url):
    response = _api(module).get(url)
    assert response.status_code == 200 and response.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Sourcing, Exception Resolver, Negotiation
# ---------------------------------------------------------------------------

Q = sourcing_schemas
SOURCING_ROUTES = [
    ("POST", "/sourcing/rfq", "dispatch_rfq", Q.RfqRequest, Q.RfqResponse, 201, []),
    ("GET", "/sourcing/rfqs/R1", "get_rfq", None, Q.RfqRecord, 200, [(sourcing_router.RfqNotFoundError, 404)]),
    ("POST", "/sourcing/quotes", "receive_quote", Q.QuoteIntakeRequest, Q.QuoteRecord, 201, []),
    ("GET", "/sourcing/quotes/Q1", "get_quote", None, Q.QuoteRecord, 200, [(sourcing_router.QuoteNotFoundError, 404)]),
    ("POST", "/sourcing/verify/S1?gstin=G&iec=I&cin=C&udyam_number=U", "verify_supplier", None, Q.VerificationResult, 200, []),
]


@pytest.mark.parametrize(("method", "url", "target", "body", "result", "expected", "error"), list(_flatten(SOURCING_ROUTES)))
def test_sourcing_routes(method, url, target, body, result, expected, error):
    _check(sourcing_router, method, url, sourcing_router.service, target, body, result, expected, error)


def test_sourcing_lists_pass_filters():
    quotes = AsyncMock(return_value=[])
    with patch.object(sourcing_router.service, "list_rfqs", new=AsyncMock(return_value=[])), \
         patch.object(sourcing_router.service, "list_quotes", new=quotes):
        api = _api(sourcing_router)
        assert api.get("/sourcing/deals/D1/rfqs").json() == []
        assert api.get("/sourcing/deals/D1/quotes", params={"rfq_id": "R1"}).json() == []
    quotes.assert_awaited_once_with(deal_id="D1", rfq_id="R1")


X = exception_schemas


class TestExceptionRoutes:
    def test_raise(self):
        with patch.object(exception_router.service, "raise_exception", new=AsyncMock(return_value=make(X.ExceptionResponse))):
            assert _api(exception_router).post("/exception-resolver/exceptions", json=_body(X.RaiseExceptionRequest)).status_code == 201

    def test_raise_failure(self):
        with patch.object(exception_router.service, "raise_exception", new=AsyncMock(side_effect=RuntimeError("no"))):
            assert _api(exception_router).post("/exception-resolver/exceptions", json=_body(X.RaiseExceptionRequest)).status_code == 500

    def test_list_counts_items(self):
        with patch.object(exception_router.service, "list_exceptions", new=AsyncMock(return_value=[make(X.ExceptionResponse)])):
            assert _api(exception_router).get("/exception-resolver/exceptions").json()["total"] == 1

    def test_list_failure(self):
        with patch.object(exception_router.service, "list_exceptions", new=AsyncMock(side_effect=RuntimeError("no"))):
            assert _api(exception_router).get("/exception-resolver/exceptions").status_code == 500

    @pytest.mark.parametrize(("error", "code"), [
        (None, 200), (exception_router.ExceptionNotFoundError, 404), (RuntimeError, 500),
    ])
    def test_get(self, error, code):
        mock = AsyncMock(side_effect=_error(error)) if error else AsyncMock(return_value=make(X.ExceptionResponse))
        with patch.object(exception_router.service, "get_exception", new=mock):
            assert _api(exception_router).get("/exception-resolver/exceptions/E1").status_code == code


N = negotiation_schemas


class TestNegotiationRoutes:
    def test_start(self):
        with patch.object(negotiation_router.service, "start_negotiation", new=AsyncMock(return_value=make(N.NegotiationResponse))):
            assert _api(negotiation_router).post("/negotiation-agent/negotiations", json=_body(N.StartNegotiationRequest)).status_code == 201

    @pytest.mark.parametrize(("error", "code"), [(None, 200), ("Negotiation x not found", 404), ("session is closed", 409)])
    def test_round(self, error, code):
        mock = AsyncMock(side_effect=ValueError(error)) if error else AsyncMock(return_value=make(N.NegotiationResponse))
        with patch.object(negotiation_router.service, "run_round", new=mock):
            assert _api(negotiation_router).post("/negotiation-agent/negotiations/N1/round", json=_body(N.RoundRequest)).status_code == code

    @pytest.mark.parametrize(("error", "code"), [(None, 200), ("not found", 404)])
    def test_get(self, error, code):
        mock = AsyncMock(side_effect=ValueError(error)) if error else AsyncMock(return_value=make(N.NegotiationSession))
        with patch.object(negotiation_router.service, "get_negotiation", new=mock):
            assert _api(negotiation_router).get("/negotiation-agent/negotiations/N1").status_code == code

    @pytest.mark.parametrize(("url", "target", "item"), [
        ("/negotiation-agent/negotiations/N1/rounds", "list_rounds", N.NegotiationRound),
        ("/negotiation-agent/negotiations/N1/emails", "list_email_history", EmailHistoryRecord),
        ("/negotiation-agent/negotiations", "list_negotiations", N.NegotiationSession),
        ("/negotiation-agent/email-threads", "get_email_threads", N.NegotiationEmailThread),
    ])
    def test_lists(self, url, target, item):
        with patch.object(negotiation_router.service, target, new=AsyncMock(return_value=[make(item)])):
            assert len(_api(negotiation_router).get(url).json()) == 1


# ---------------------------------------------------------------------------
# HITL
# ---------------------------------------------------------------------------

H = hitl_schemas
_H = hitl_router


class TestHitlRoutes:
    def test_create(self):
        with patch.object(_H, "create_approval", new=AsyncMock(return_value=make(H.ApprovalResponse))):
            assert _api(_H).post("/hitl-approval/approvals", json=_body(H.CreateApprovalRequest)).status_code == 201

    def test_create_invalid_action(self):
        with patch.object(_H, "create_approval", new=AsyncMock(side_effect=_error(_H.InvalidAction))):
            assert _api(_H).post("/hitl-approval/approvals", json=_body(H.CreateApprovalRequest)).status_code == 422

    def test_list_passes_filters(self):
        mock = AsyncMock(return_value=[])
        with patch.object(_H, "list_approvals", new=mock):
            assert _api(_H).get("/hitl-approval/approvals", params={"status": "PENDING", "deal_id": "D1"}).json() == []
        mock.assert_awaited_once_with(status="PENDING", deal_id="D1")

    @pytest.mark.parametrize(("error", "code"), [(None, 200), (_H.ApprovalNotFoundError, 404)])
    def test_get(self, error, code):
        mock = AsyncMock(side_effect=_error(error)) if error else AsyncMock(return_value=make(H.ApprovalResponse))
        with patch.object(_H, "get_approval", new=mock):
            assert _api(_H).get("/hitl-approval/approvals/A1").status_code == code

    @pytest.mark.parametrize(("error", "code"), [
        (None, 200), (_H.ApprovalNotFoundError, 404), (_H.ApprovalAlreadyResolved, 409),
        (_H.SameApproverError, 409), (_H.ComplianceBlockedError, 409),
    ])
    def test_resolve(self, error, code):
        mock = AsyncMock(side_effect=_error(error)) if error else AsyncMock(return_value=make(H.ApprovalResponse))
        with patch.object(_H, "resolve_approval", new=mock):
            response = _api(_H).post("/hitl-approval/approvals/A1/resolve", json={"decision": "APPROVE", "resolved_by": "ops"})
        assert response.status_code == code

    def test_policy_view(self):
        response = _api(_H).get("/hitl-approval/autonomy-policy")
        assert response.status_code == 200 and response.json()["actions"]

    def test_policy_update_leaves_threshold_unless_asked(self):
        mock = AsyncMock()
        with patch("shared.orchestration.autonomy_policy.set_policy", new=mock):
            response = _api(_H).put("/hitl-approval/autonomy-policy", json={"levels": {"ISSUE_PO": "L1"}, "gates": {"ISSUE_PO": {"timeout_seconds": 60}, "BOOK_FREIGHT": None}})
        assert response.status_code == 200
        assert mock.await_args.kwargs["money_auto_approve_below_usd"] == "__unchanged__"
        assert mock.await_args.kwargs["gates"] == {"ISSUE_PO": {"timeout_seconds": 60}, "BOOK_FREIGHT": None}

    def test_policy_update_sets_threshold_when_asked(self):
        mock = AsyncMock()
        with patch("shared.orchestration.autonomy_policy.set_policy", new=mock):
            _api(_H).put("/hitl-approval/autonomy-policy", json={"set_money_threshold": True, "money_auto_approve_below_usd": 500})
        assert mock.await_args.kwargs["money_auto_approve_below_usd"] == 500

    def test_invalid_policy_is_422(self):
        with patch("shared.orchestration.autonomy_policy.set_policy", new=AsyncMock(side_effect=ValueError("bad level"))):
            assert _api(_H).put("/hitl-approval/autonomy-policy", json={}).status_code == 422

    def test_learning(self):
        rows = [MagicMock(model_dump=lambda mode: {"id": 1})]
        with patch("shared.orchestration.outcomes.all_outcomes", new=AsyncMock(return_value=rows)), \
             patch("shared.orchestration.outcomes.autonomy_evidence", return_value={"e": 1}), \
             patch("shared.orchestration.outcomes.track_records", return_value={"a": {"deals": 1}, "b": {"deals": 3}}), \
             patch("shared.orchestration.outcomes.offer_pricing", return_value={"p": 1}):
            body = _api(_H).get("/hitl-approval/learning").json()
        assert body["evidence"] == {"e": 1} and list(body["track_records"]) == ["b", "a"] and body["recent"] == [{"id": 1}]

    def test_digest_json_and_text(self):
        with patch("shared.orchestration.digest.build_digest", new=AsyncMock(return_value={"n": 1})), \
             patch("shared.orchestration.digest.render_digest_text", return_value="hello"):
            assert _api(_H).get("/hitl-approval/digest").json() == {"n": 1, "text": "hello"}
            assert _api(_H).get("/hitl-approval/digest", params={"format": "text"}).text == "hello"

    def test_digest_send(self):
        with patch("shared.orchestration.digest.send_daily_digest", new=AsyncMock(return_value={"sent": True})):
            assert _api(_H).post("/hitl-approval/digest/send").json() == {"sent": True}

    async def test_event_stream_replays_then_stops_on_disconnect(self):
        request = MagicMock(is_disconnected=AsyncMock(return_value=True))
        with patch.object(_H, "register_sse_client", return_value=("C1", MagicMock())), \
             patch.object(_H, "get_recent_events", return_value=[{"event_type": "x"}]), \
             patch.object(_H, "deregister_sse_client") as gone:
            events = [e async for e in _H._event_generator(request)]
        assert events == ['data: {"event_type": "x"}\n\n']
        gone.assert_called_once_with("C1")

    async def test_event_stream_delivers_live_events(self):
        queue = MagicMock(get=AsyncMock(return_value={"event_type": "live"}))
        request = MagicMock(is_disconnected=AsyncMock(side_effect=[False, True]))
        with patch.object(_H, "register_sse_client", return_value=("C1", queue)), \
             patch.object(_H, "get_recent_events", return_value=[]), patch.object(_H, "deregister_sse_client"):
            events = [e async for e in _H._event_generator(request)]
        assert events == ['data: {"event_type": "live"}\n\n']

    async def test_event_stream_sends_heartbeats(self):
        request = MagicMock(is_disconnected=AsyncMock(side_effect=[False, True]))
        with patch.object(_H, "register_sse_client", return_value=("C1", MagicMock())), \
             patch.object(_H, "get_recent_events", return_value=[]), patch.object(_H, "deregister_sse_client"), \
             patch.object(_H.asyncio, "wait_for", new=AsyncMock(side_effect=TimeoutError)):
            events = [e async for e in _H._event_generator(request)]
        assert events == [": heartbeat\n\n"]

    async def test_stream_response_headers(self):
        response = await _H.api_sse_stream(MagicMock())
        assert response.media_type == "text/event-stream" and response.headers["x-accel-buffering"] == "no"


# ---------------------------------------------------------------------------
# Trade-Min
# ---------------------------------------------------------------------------

T = trade_schemas
_T = trade_router


class TestTradeMinRoutes:
    def test_catalogue_view(self):
        body = _api(_T).get("/trade-min/catalogue").json()
        assert "corridors" in body and "defaults" in body and body["active"] is False

    def test_put_catalogue_remembers_each_line(self):
        line = {"product_id": "P1", "hs_code": "1520", "name": "glycerine", "markets": ["ES"]}
        with patch("shared.catalogue.set_catalogue", new=AsyncMock()) as saved, \
             patch("shared.orchestration.memory.remember_product") as remembered:
            assert _api(_T).put("/trade-min/catalogue", json={"products": [line], "updated_by": "ops"}).status_code == 200
        assert saved.await_args.kwargs["updated_by"] == "ops"
        remembered.assert_called_once()

    def test_invalid_catalogue_is_422(self):
        with patch("shared.catalogue.set_catalogue", new=AsyncMock(side_effect=ValueError("bad"))):
            assert _api(_T).put("/trade-min/catalogue", json={}).status_code == 422

    def test_sources(self):
        with patch.object(_T.service, "describe_sources", return_value={"a": "live"}):
            assert _api(_T).get("/trade-min/sources").json() == {"a": "live"}

    def test_scan(self):
        with patch.object(_T.service, "scan_opportunities", new=AsyncMock(return_value=[make(T.EnrichedOpportunity)])):
            assert len(_api(_T).post("/trade-min/scan", json={}).json()) == 1

    NOTE: ClassVar[dict] = {"subject_type": "buyer", "subject_id": "acme.com", "text": "pays late"}

    def test_note_needs_the_knowledge_service(self):
        with patch("shared.knowledge.enabled", return_value=False):
            assert _api(_T).post("/trade-min/knowledge/notes", json=self.NOTE).status_code == 503

    def test_note_is_queued(self):
        with patch("shared.knowledge.enabled", return_value=True), \
             patch("shared.orchestration.memory.remember_note", return_value=True):
            assert _api(_T).post("/trade-min/knowledge/notes", json=self.NOTE).json()["queued"] is True

    def test_search_when_disabled(self):
        with patch("shared.knowledge.enabled", return_value=False):
            assert _api(_T).get("/trade-min/knowledge/search", params={"q": "abc"}).json() == {"enabled": False, "results": []}

    def test_search_results_are_trimmed(self):
        hit = {"text": "x" * 2000, "metadata": {"a": 1}, "score": 0.5}
        with patch("shared.knowledge.enabled", return_value=True), \
             patch("shared.knowledge.recall", new=AsyncMock(return_value=[hit, {"content": "y"}])), \
             patch("shared.knowledge.is_graph_fact", return_value=False):
            results = _api(_T).get("/trade-min/knowledge/search", params={"q": "abc"}).json()["results"]
        assert len(results[0]["text"]) == 1200 and results[1]["text"] == "y" and results[1]["metadata"] == {}

    def test_price_book_upper_cases_the_market(self):
        estimate = MagicMock(model_dump=lambda mode: {"mid": 1})
        priors = AsyncMock(return_value={})
        with patch("shared.pricebook.public_priors", new=priors), \
             patch("shared.pricebook.estimate", new=AsyncMock(return_value=estimate)), \
             patch("shared.pricebook.observations", new=AsyncMock(return_value=[])):
            body = _api(_T).get("/trade-min/price-book/1520", params={"market": "es"}).json()
        priors.assert_awaited_once_with("1520", "ES")
        assert body["buyer"] == {"mid": 1} and body["observations"] == []

    def test_price_book_without_market(self):
        priors = AsyncMock(return_value={})
        with patch("shared.pricebook.public_priors", new=priors), \
             patch("shared.pricebook.estimate", new=AsyncMock(return_value=MagicMock(model_dump=lambda mode: {}))), \
             patch("shared.pricebook.observations", new=AsyncMock(return_value=[])):
            _api(_T).get("/trade-min/price-book/1520")
        priors.assert_awaited_once_with("1520", None)

    @pytest.mark.parametrize(("url", "role"), [("/trade-min/buyers", "buyer"), ("/trade-min/suppliers", "supplier")])
    def test_directory(self, url, role):
        mock = AsyncMock(return_value=[])
        with patch.object(_T.directory, "list_counterparties", new=mock):
            assert _api(_T).get(url, params={"country": "IN", "q": "oil"}).json() == []
        assert mock.await_args.args == (role,) and mock.await_args.kwargs["country"] == "IN"

    def test_opportunity_list(self):
        with patch.object(_T.service, "list_opportunities", new=AsyncMock(return_value=[])):
            assert _api(_T).get("/trade-min/opportunities").json() == []

    @pytest.mark.parametrize(("url", "target", "result", "method"), [
        ("/trade-min/opportunities/O1", "get_opportunity", T.EnrichedOpportunity, "GET"),
        ("/trade-min/opportunities/O1/match-seed", "get_match_seed", T.MatchEngineSeed, "GET"),
        ("/trade-min/opportunities/O1/convert", "mark_opportunity_converted", True, "POST"),
    ])
    def test_found_and_missing(self, url, target, result, method):
        found = result if result is True else make(result)
        with patch.object(_T.service, target, new=AsyncMock(return_value=found)):
            assert _api(_T).request(method, url).status_code == 200
        with patch.object(_T.service, target, new=AsyncMock(return_value=None)):
            assert _api(_T).request(method, url).status_code == 404


# ---------------------------------------------------------------------------
# Email Processor
# ---------------------------------------------------------------------------

E = email_schemas
_E = email_router


class TestEmailProcessorRoutes:
    @pytest.fixture
    def process(self):
        mock = AsyncMock(return_value=make(E.ProcessingResult))
        with patch.object(_E.service, "process_inbound", new=mock):
            yield mock

    def test_receiving_api_passes_overrides(self, process):
        body = {"email": _body(E.InboundRawEmail), "negotiation_id_override": "N1", "party_override": "buyer", "counter_price_override": 9.5}
        assert _api(_E).post("/email-processor/inbound", json=body).status_code == 201
        assert process.await_args.kwargs == {"negotiation_id_override": "N1", "party_override": "buyer", "counter_price_override": 9.5}

    def test_manual_submission_is_marked_manual(self, process):
        body = {**_body(E.ManualInboundRequest), "negotiation_id": "N1", "party": "buyer", "counter_price": 5.0}
        assert _api(_E).post("/email-processor/inbound/manual", json=body).status_code == 201
        assert process.await_args.args[0].source == E.InboundEmailSource.MANUAL
        assert process.await_args.kwargs["negotiation_id_override"] == "N1"

    def test_list_passes_filters(self):
        mock = AsyncMock(return_value=[])
        with patch.object(_E.service, "list_inbound", new=mock):
            assert _api(_E).get("/email-processor/inbound", params={"negotiation_id": "N1", "limit": 5}).json() == []
        assert mock.await_args.kwargs == {"status": None, "negotiation_id": "N1", "limit": 5}

    def test_get(self):
        with patch.object(_E.service, "get_inbound", new=AsyncMock(return_value=make(E.InboundEmailRecord))):
            assert _api(_E).get("/email-processor/inbound/I1").status_code == 200

    def test_get_missing(self):
        with patch.object(_E.service, "get_inbound", new=AsyncMock(side_effect=ValueError("no"))):
            assert _api(_E).get("/email-processor/inbound/I1").status_code == 404

    def test_sendgrid_form(self, process):
        headers = "Message-ID: <m1>\nIn-Reply-To: <p1>\nReferences: <a> junk <b>\nX-Other: y"
        data = {"from": "Ann <ann@acme.com>", "to": "t@p.io", "subject": "Hi", "text": "body", "html": "<p>b</p>", "headers": headers}
        files = {"attachment1": ("po.pdf", b"PDF", "application/pdf")}
        assert _api(_E).post("/email-processor/inbound/sendgrid", data=data, files=files).status_code == 200
        raw = process.await_args.args[0]
        assert raw.from_address == "ann@acme.com" and raw.subject == "Hi" and raw.source == E.InboundEmailSource.SENDGRID_WEBHOOK
        assert (raw.message_id, raw.in_reply_to, raw.references) == ("<m1>", "<p1>", ["<a>", "<b>"])
        attachment = raw.attachments[0]
        assert (attachment.filename, attachment.size, base64.b64decode(attachment.content_b64)) == ("po.pdf", 3, b"PDF")

    def test_sendgrid_verifier_runs_when_a_key_is_set(self, process, monkeypatch):
        monkeypatch.setattr(_E.settings, "sendgrid_webhook_public_key", "KEY")
        with patch.object(_E, "_verify_sendgrid_signature") as verify:
            assert _api(_E).post("/email-processor/inbound/sendgrid", data={"from": "a@b.c"}).status_code == 200
        verify.assert_called_once()

    @staticmethod
    def _mailgun(**extra):
        headers = [["Message-Id", "<m1>"], ["In-Reply-To", "<p1>"], ["References", "<a> <b> junk"], "bad"]
        return {"From": "Ann <ann@acme.com>", "To": "t@p.io", "Subject": "Hi", "body-plain": "body", "body-html": "<p>", "message-headers": json.dumps(headers), **extra}

    def test_mailgun_form(self, process, monkeypatch):
        monkeypatch.setattr(_E.settings, "mailgun_signing_key", "")
        assert _api(_E).post("/email-processor/inbound/mailgun", data=self._mailgun()).status_code == 200
        raw = process.await_args.args[0]
        assert raw.from_address == "ann@acme.com" and raw.source == E.InboundEmailSource.MAILGUN_WEBHOOK
        assert (raw.message_id, raw.in_reply_to, raw.references) == ("<m1>", "<p1>", ["<a>", "<b>"])

    def test_mailgun_bad_header_json_is_tolerated(self, process, monkeypatch):
        monkeypatch.setattr(_E.settings, "mailgun_signing_key", "")
        data = {**self._mailgun(), "message-headers": "{not json"}
        assert _api(_E).post("/email-processor/inbound/mailgun", data=data).status_code == 200
        assert process.await_args.args[0].message_id == ""

    def test_mailgun_signature_accepted(self, process, monkeypatch):
        monkeypatch.setattr(_E.settings, "mailgun_signing_key", "secret")
        signature = hmac.new(b"secret", b"1700tok", hashlib.sha256).hexdigest()
        data = self._mailgun(timestamp="1700", token="tok", signature=signature)
        assert _api(_E).post("/email-processor/inbound/mailgun", data=data).status_code == 200

    @pytest.mark.parametrize(("extra", "code"), [
        ({}, 400),
        ({"timestamp": "1700", "token": "tok"}, 400),
        ({"timestamp": "1700", "token": "tok", "signature": "0" * 64}, 403),
    ])
    def test_mailgun_signature_refused(self, process, monkeypatch, extra, code):
        monkeypatch.setattr(_E.settings, "mailgun_signing_key", "secret")
        assert _api(_E).post("/email-processor/inbound/mailgun", data=self._mailgun(**extra)).status_code == code
        process.assert_not_awaited()

    def test_empty_headers(self):
        assert _E._parse_raw_headers("") == ("", "", [])


class TestSendGridSignature:
    BODY = b"from=ann%40acme.com&subject=Hi"
    TS = "1700000000"

    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.public = self._b64(self.private.public_key())
        monkeypatch.setattr(_E.settings, "sendgrid_webhook_public_key", self.public)

    @staticmethod
    def _b64(public_key):
        der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        return base64.b64encode(der).decode()

    def _headers(self, body=None, ts=None, private=None):
        message = (ts or self.TS).encode() + (self.BODY if body is None else body)
        signature = (private or self.private).sign(message, ec.ECDSA(hashes.SHA256()))
        return {"x-twilio-email-event-webhook-signature": base64.b64encode(signature).decode(), "x-twilio-email-event-webhook-timestamp": self.TS}

    def _post(self, headers, process):
        with patch.object(_E.service, "process_inbound", new=process):
            return _api(_E).post("/email-processor/inbound/sendgrid", content=self.BODY, headers={"content-type": "application/x-www-form-urlencoded", **headers})

    def test_real_signature_is_accepted(self):
        process = AsyncMock(return_value=make(E.ProcessingResult))
        assert self._post(self._headers(), process).status_code == 200
        assert process.await_args.args[0].from_address == "ann@acme.com"

    @pytest.mark.parametrize("missing", ["x-twilio-email-event-webhook-signature", "x-twilio-email-event-webhook-timestamp"])
    def test_missing_header(self, missing):
        process = AsyncMock()
        headers = {k: v for k, v in self._headers().items() if k != missing}
        assert self._post(headers, process).status_code == 400
        process.assert_not_awaited()

    def test_changed_body_is_refused(self):
        process = AsyncMock()
        assert self._post(self._headers(body=b"from=attacker%40evil.com"), process).status_code == 403
        process.assert_not_awaited()

    def test_changed_timestamp_is_refused(self):
        process = AsyncMock()
        assert self._post(self._headers(ts="1"), process).status_code == 403
        process.assert_not_awaited()

    def test_someone_elses_key_is_refused(self):
        process = AsyncMock()
        assert self._post(self._headers(private=ec.generate_private_key(ec.SECP256R1())), process).status_code == 403
        process.assert_not_awaited()

    def test_garbage_signature_is_refused(self):
        process = AsyncMock()
        headers = {**self._headers(), "x-twilio-email-event-webhook-signature": "%%%"}
        assert self._post(headers, process).status_code == 403
        process.assert_not_awaited()

    def test_misconfigured_key_refuses_rather_than_accepts(self, monkeypatch):
        monkeypatch.setattr(_E.settings, "sendgrid_webhook_public_key", "not-a-key")
        process = AsyncMock()
        assert self._post(self._headers(), process).status_code == 403
        process.assert_not_awaited()


# ---------------------------------------------------------------------------
# API gateway
# ---------------------------------------------------------------------------

class TestGatewayRoutes:
    @pytest.mark.parametrize(("method", "url", "target", "body"), [
        ("GET", "/health/downstream", "downstream_health", None),
        ("GET", "/deals", "list_deals", None),
        ("GET", "/deals/D1", "get_deal", None),
        ("PATCH", "/deals/D1/status", "update_deal_status", {"target_status": "PAID"}),
        ("GET", "/opportunities", "list_opportunities", None),
        ("POST", "/opportunities/scan", "scan_opportunities", {}),
        ("GET", "/approvals", "list_approvals", None),
        ("POST", "/approvals/A1/resolve", "resolve_approval", {"decision": "APPROVE"}),
        ("GET", "/exceptions/E1", "get_exception", None),
        ("POST", "/webhooks/stripe", "ingest_webhook", None),
    ])
    def test_delegates(self, method, url, target, body):
        mock = AsyncMock(return_value={"status": "ok"})
        with patch.object(gateway_router.service, target, new=mock):
            response = _api(gateway_router).request(method, url, json=body)
        assert response.json() == {"status": "ok"}
        mock.assert_awaited_once()

    def test_readiness(self):
        with patch("shared.readiness.evaluate", return_value={"ready": True}):
            assert _api(gateway_router).get("/health/readiness").json() == {"ready": True}


# ---------------------------------------------------------------------------
# REST clients
# ---------------------------------------------------------------------------

class _Recorder:
    def __init__(self, reply=None):
        self.requests: list[httpx.Request] = []
        self.reply = {} if reply is None else reply

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self.reply)


@pytest.fixture
def wire():
    recorder = _Recorder()
    real = httpx.AsyncClient

    def client(**kwargs):
        return real(transport=httpx.MockTransport(recorder), **kwargs)

    with patch("shared.clients.base.httpx.AsyncClient", side_effect=client):
        yield recorder


P = {"a": 1}
CLIENT_CALLS = [
    (SourcingClient, "health", (), {}, "GET", "/sourcing/health", None),
    (SourcingClient, "dispatch_rfq", (P,), {}, "POST", "/sourcing/rfq", P),
    (SourcingClient, "get_rfq", ("R",), {}, "GET", "/sourcing/rfqs/R", None),
    (SourcingClient, "list_rfqs", ("D",), {}, "GET", "/sourcing/deals/D/rfqs", None),
    (SourcingClient, "receive_quote", (P,), {}, "POST", "/sourcing/quotes", P),
    (SourcingClient, "get_quote", ("Q",), {}, "GET", "/sourcing/quotes/Q", None),
    (SourcingClient, "list_quotes", ("D",), {}, "GET", "/sourcing/deals/D/quotes", None),
    (SourcingClient, "list_quotes", ("D",), {"rfq_id": "R"}, "GET", "/sourcing/deals/D/quotes?rfq_id=R", None),
    (SourcingClient, "verify_supplier", ("S",), {}, "POST", "/sourcing/verify/S", {}),
    (SourcingClient, "verify_supplier", ("S",), {"gstin": "G", "iec": "I", "cin": "C", "udyam_number": "U"}, "POST", "/sourcing/verify/S?gstin=G&iec=I&cin=C&udyam_number=U", {}),
    (DealsClient, "health", (), {}, "GET", "/deals/health", None),
    (DealsClient, "list_deals", (), {}, "GET", "/deals?limit=100", None),
    (DealsClient, "list_deals", (), {"status": "PAID", "operator_id": "O"}, "GET", "/deals?limit=100&status=PAID&operator_id=O", None),
    (DealsClient, "create_deal", (P,), {}, "POST", "/deals", P),
    (DealsClient, "get_deal", ("D",), {}, "GET", "/deals/D", None),
    (DealsClient, "transition_status", ("D", "PAID", "why"), {}, "PATCH", "/deals/D/status", {"target_status": "PAID", "reason": "why"}),
    (DealsClient, "update_status", ("D", P), {}, "PATCH", "/deals/D/status", P),
    (DealsClient, "update_financials", ("D", P), {}, "PATCH", "/deals/D/financials", P),
    (DealsClient, "get_match_scores", ("D",), {}, "GET", "/deals/D/match-scores", None),
    (TradeMinClient, "health", (), {}, "GET", "/trade-min/health", None),
    (TradeMinClient, "scan", (["1520"], ["ES"], 2.0, True), {}, "POST", "/trade-min/scan", {"hs_codes": ["1520"], "target_markets": ["ES"], "min_arbitrage_rank": 2.0, "force_refresh": True}),
    (TradeMinClient, "list_opportunities", (), {}, "GET", "/trade-min/opportunities?limit=100&min_rank=0.0", None),
    (TradeMinClient, "list_opportunities", (), {"urgency_tier": 0}, "GET", "/trade-min/opportunities?limit=100&min_rank=0.0&urgency_tier=0", None),
    (TradeMinClient, "get_opportunity", ("O",), {}, "GET", "/trade-min/opportunities/O", None),
    (TradeMinClient, "get_match_seed", ("O",), {}, "GET", "/trade-min/opportunities/O/match-seed", None),
    (TradeMinClient, "mark_converted", ("O",), {}, "POST", "/trade-min/opportunities/O/convert", None),
    (HitlApprovalClient, "health", (), {}, "GET", "/hitl-approval/health", None),
    (HitlApprovalClient, "create_approval", (P,), {}, "POST", "/hitl-approval/approvals", P),
    (HitlApprovalClient, "get_approval", ("A",), {}, "GET", "/hitl-approval/approvals/A", None),
    (HitlApprovalClient, "list_approvals", (), {}, "GET", "/hitl-approval/approvals", None),
    (HitlApprovalClient, "list_approvals", (), {"status": "PENDING", "deal_id": "D"}, "GET", "/hitl-approval/approvals?status=PENDING&deal_id=D", None),
    (HitlApprovalClient, "resolve_approval", ("A", P), {}, "POST", "/hitl-approval/approvals/A/resolve", P),
    (ExceptionResolverClient, "health", (), {}, "GET", "/exception-resolver/health", None),
    (ExceptionResolverClient, "raise_exception", (P,), {}, "POST", "/exception-resolver/exceptions", P),
    (ExceptionResolverClient, "get_exception", ("E",), {}, "GET", "/exception-resolver/exceptions/E", None),
    (ExceptionResolverClient, "list_exceptions", (), {}, "GET", "/exception-resolver/exceptions?limit=100", None),
    (ExceptionResolverClient, "list_exceptions", (), {"deal_id": "D", "status": "OPEN"}, "GET", "/exception-resolver/exceptions?limit=100&deal_id=D&status=OPEN", None),
    (ShieldLinkClient, "health", (), {}, "GET", "/shield-link/health", None),
    (ShieldLinkClient, "list_qc_providers", (), {}, "GET", "/shield-link/qc/providers", None),
    (ShieldLinkClient, "quote_qc", (P,), {}, "POST", "/shield-link/qc/quote", P),
    (ShieldLinkClient, "book_qc", (P,), {}, "POST", "/shield-link/qc", P),
    (ShieldLinkClient, "quote_freight", (P,), {}, "POST", "/shield-link/freight/quote", P),
    (ShieldLinkClient, "book_freight", (P,), {}, "POST", "/shield-link/freight", P),
    (ShieldLinkClient, "file_cha", (P,), {}, "POST", "/shield-link/cha", P),
    (ShieldLinkClient, "get_tpo", ("T",), {}, "GET", "/shield-link/tpo/T", None),
    (ShieldLinkClient, "get_deal_orders", ("D",), {}, "GET", "/shield-link/deals/D/orders", None),
    (DealMakerClient, "health", (), {}, "GET", "/dealmaker/health", None),
    (DealMakerClient, "issue_pi", (P,), {}, "POST", "/dealmaker/pi", P),
    (DealMakerClient, "accept_pi", ("P", P), {}, "POST", "/dealmaker/pi/P/accept", P),
    (DealMakerClient, "get_pi", ("P",), {}, "GET", "/dealmaker/pi/P", None),
    (DealMakerClient, "issue_po", (P,), {}, "POST", "/dealmaker/po", P),
    (DealMakerClient, "get_po", ("P",), {}, "GET", "/dealmaker/po/P", None),
    (DealMakerClient, "get_deal_documents", ("D",), {}, "GET", "/dealmaker/deals/D/documents", None),
    (EscrowGuardClient, "health", (), {}, "GET", "/escrow-guard/health", None),
    (EscrowGuardClient, "create_hold", (P,), {}, "POST", "/escrow-guard/holds", P),
    (EscrowGuardClient, "get_hold", ("E",), {}, "GET", "/escrow-guard/holds/E", None),
    (EscrowGuardClient, "get_hold_by_deal", ("D",), {}, "GET", "/escrow-guard/deals/D/hold", None),
    (EscrowGuardClient, "request_payout", (P,), {}, "POST", "/escrow-guard/payouts", P),
    (EscrowGuardClient, "get_payout", ("P",), {}, "GET", "/escrow-guard/payouts/P", None),
    (EscrowGuardClient, "get_deal_summary", ("D",), {}, "GET", "/escrow-guard/deals/D/summary", None),
    (DocuGenClient, "health", (), {}, "GET", "/docugen/health", None),
    (DocuGenClient, "generate_document", (P,), {}, "POST", "/docugen/documents", P),
    (DocuGenClient, "get_document", ("X",), {}, "GET", "/docugen/documents/X", None),
    (DocuGenClient, "list_deal_documents", ("D",), {}, "GET", "/docugen/deals/D/documents", None),
    (DocuGenClient, "file_customs", (P,), {}, "POST", "/docugen/customs/file", P),
    (MatchEngineClient, "health", (), {}, "GET", "/match-engine/health", None),
    (MatchEngineClient, "score", (P,), {}, "POST", "/match-engine/score", P),
    (MatchEngineClient, "list_scores", (), {}, "GET", "/match-engine/scores", None),
    (MatchEngineClient, "get_score", ("D",), {}, "GET", "/match-engine/scores/D", None),
    (ApiGatewayClient, "health", (), {}, "GET", "/health", None),
    (ApiGatewayClient, "create_deal", (P,), {}, "POST", "/deals", P),
    (NegotiationAgentClient, "health", (), {}, "GET", "/negotiation-agent/health", None),
    (NegotiationAgentClient, "start", (P,), {}, "POST", "/negotiation-agent/negotiations", P),
    (NegotiationAgentClient, "run_round", ("N", P), {}, "POST", "/negotiation-agent/negotiations/N/round", P),
    (NegotiationAgentClient, "get_negotiation", ("N",), {}, "GET", "/negotiation-agent/negotiations/N", None),
    (NegotiationAgentClient, "list_rounds", ("N",), {}, "GET", "/negotiation-agent/negotiations/N/rounds", None),
    (NegotiationAgentClient, "list_negotiations", (), {}, "GET", "/negotiation-agent/negotiations", None),
    (NegotiationAgentClient, "list_negotiations", ("D",), {}, "GET", "/negotiation-agent/negotiations?deal_id=D", None),
    (EmailProcessorClient, "health", (), {}, "GET", "/email-processor/health", None),
    (EmailProcessorClient, "get_inbound", ("I",), {}, "GET", "/email-processor/inbound/I", None),
    (EmailProcessorClient, "list_inbound", (), {}, "GET", "/email-processor/inbound?limit=100", None),
    (EmailProcessorClient, "list_inbound", (), {"status": "NEW", "negotiation_id": "N"}, "GET", "/email-processor/inbound?limit=100&status=NEW&negotiation_id=N", None),
    (EmailGatewayClient, "health", (), {}, "GET", "/email-gateway/health", None),
]


@pytest.mark.parametrize(("cls", "name", "args", "kwargs", "verb", "target", "sent"), CLIENT_CALLS)
async def test_client_call(wire, cls, name, args, kwargs, verb, target, sent):
    await getattr(cls(), name)(*args, **kwargs)
    request = wire.requests[0]
    assert request.method == verb
    assert request.url.raw_path.decode() == target
    if sent is not None:
        assert json.loads(request.content) == sent


async def test_submit_inbound_returns_a_typed_result(wire):
    wire.reply = make(E.ProcessingResult).model_dump(mode="json")
    result = await EmailProcessorClient("http://x").submit_inbound(make(E.InboundRawEmail), party_override="buyer", negotiation_id_override="N", counter_price_override=2.0)
    assert isinstance(result, E.ProcessingResult)
    body = json.loads(wire.requests[0].content)
    assert (body["party_override"], body["negotiation_id_override"], body["counter_price_override"]) == ("buyer", "N", 2.0)


async def test_send_email_returns_a_typed_result(wire):
    wire.reply = make(SendEmailResult).model_dump(mode="json")
    assert isinstance(await EmailGatewayClient("http://x").send_email(make(SendEmailRequest)), SendEmailResult)
    assert wire.requests[0].url.path == "/email-gateway/send"
