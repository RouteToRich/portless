"""Escrow money layer from the outside in.

  HTTP       → the whole escrow journey through real routes, every route's error
               mapping, webhook signatures and malformed webhooks
  providers  → Stripe, Payoneer, Tazapay, Escrow.com: stub mode, real request
               shape (mock transport), provider failure, signature checks
  service    → branches the journey does not reach (provider down, wrong state,
               idempotency, watcher, reconciliation)
  repository → the same contract against the in-memory and the Mongo backend
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from services.escrow_guard import repository as repo_mod
from services.escrow_guard import service as svc
from services.escrow_guard.repository import (
    InMemoryEscrowGuardRepository,
    MongoEscrowGuardRepository,
    get_repository,
)
from services.escrow_guard.router import router
from services.escrow_guard.schemas import (
    AcceptEscrowRequest,
    CreateEscrowRequest,
    DisputeEscrowRequest,
    EscrowFundedWebhookPayload,
    EscrowHoldRecord,
    PayoutRecord,
    PayoutRequest,
    PayoutWebhookPayload,
    RefundRequest,
    ReleaseTrancheRequest,
    TrancheSpec,
)
from shared.connectors.escrow import EscrowComConnector, TazapayEscrowConnector
from shared.connectors.payments import ConnectorError, PayoneerConnector, StripeTreasuryConnector
from shared.domain.events import EventTopic

NOW = datetime.now(UTC)
PROVIDER_ENV = (
    "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_WEBHOOK_TOLERANCE_SECONDS",
    "PAYONEER_CLIENT_ID", "PAYONEER_CLIENT_SECRET", "PAYONEER_WEBHOOK_SECRET", "PAYONEER_BASE_URL",
    "TAZAPAY_API_KEY", "TAZAPAY_API_SECRET", "TAZAPAY_WEBHOOK_SECRET", "TAZAPAY_BASE_URL",
    "ESCROW_COM_API_KEY", "ESCROW_COM_EMAIL", "ESCROW_COM_WEBHOOK_SECRET", "ESCROW_COM_BASE_URL",
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    repo_mod._repository = InMemoryEscrowGuardRepository()
    repo_mod._motor_client = None
    yield
    repo_mod._repository = None
    repo_mod._motor_client = None


class _World:
    """The deal as the escrow service sees it, plus every event it emitted."""

    def __init__(self, status="PI_ACCEPTED"):
        self.status = status
        self.events: list[tuple[EventTopic, str, dict]] = []
        self.transitions: list[str] = []

    async def fetch(self, deal_id):
        return SimpleNamespace(deal_id=deal_id, status=SimpleNamespace(value=self.status))

    async def transition(self, _deal_id, target, _reason):
        self.transitions.append(target)
        self.status = target

    async def emit(self, topic, deal_id, payload):
        self.events.append((topic, deal_id, payload))

    def topics(self):
        return [t for t, _d, _p in self.events]


@pytest.fixture
def world(monkeypatch):
    w = _World()
    monkeypatch.setattr(svc, "_fetch_deal", w.fetch)
    monkeypatch.setattr(svc, "_transition_deal", w.transition)
    monkeypatch.setattr(svc, "_emit", w.emit)
    monkeypatch.setattr(svc, "_patch_deal_realized_margin", AsyncMock())
    return w


@pytest.fixture
def api():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def _hold(**kw):
    base = {
        "escrow_id": "ESC-1", "deal_id": "D1", "provider": "tazapay", "amount": 1000.0, "currency": "USD", "status": "FUNDED",
        "ext_ref": "ref-1", "created_at": NOW, "updated_at": NOW, "idempotency_key": "idem-1",
    }
    base.update(kw)
    return EscrowHoldRecord(**base)


def _payout(**kw):
    base = {
        "payout_id": "PAY-1", "deal_id": "D1", "payee_id": "SUP", "amount": 100.0, "currency": "USD", "status": "PROCESSING",
        "ext_ref": "p-ref-1", "created_at": NOW, "updated_at": NOW, "idempotency_key": "p-idem-1",
    }
    base.update(kw)
    return PayoutRecord(**base)


def _tranches(released=()):
    specs = [("T1", "PO_ISSUED", 0.3), ("T2", "QC_PASSED", 0.4), ("T3", "CLEARED", 0.2), ("T4", "ACCEPTED", 0.1)]
    return [
        TrancheSpec(tranche_id=t, trigger=g, pct=p, amount=1000.0 * p, status="RELEASED" if t in released else "PENDING")
        for t, g, p in specs
    ]


async def _saved(**kw):
    hold = _hold(**kw)
    await get_repository().save_hold(hold)
    return hold


async def _open_funded(world):
    response = await svc.create_escrow(CreateEscrowRequest(deal_id="D1", provider="tazapay", amount=1000.0))
    await svc.handle_escrow_funded_webhook(
        EscrowFundedWebhookPayload(ext_ref=response.ext_ref, status="FUNDED", amount=1000.0), {}, b"{}")
    return response


def _provider_down(*_a, **_k):
    raise ConnectorError("provider down")


# ---------------------------------------------------------------------------
# HTTP journeys
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _gate_already_approved():
    """These tests cover the routes once their approval gate exists;
    tests/test_approval_gate_enforcement.py covers the gate check."""
    with patch("services.escrow_guard.router._require_approved_gate", new=AsyncMock()):
        yield


class TestHttpJourney:
    def test_escrow_from_open_to_paid(self, api, world):
        opened = api.post("/escrow-guard/escrows", json={"deal_id": "D1", "provider": "tazapay", "amount": 50000})
        assert opened.status_code == 201
        escrow = opened.json()
        escrow_id, ext_ref = escrow["escrow_id"], escrow["ext_ref"]
        assert [t["pct"] for t in escrow["tranches"]] == [0.3, 0.4, 0.2, 0.1]
        assert world.status == "ESCROW_PENDING"

        short = api.post("/escrow-guard/webhooks/escrow-funded", json={"ext_ref": ext_ref, "status": "FUNDED", "amount": 30000})
        assert short.json()["status"] == "shortfall" and world.status == "ESCROW_PENDING"
        refused = api.post(f"/escrow-guard/escrows/{escrow_id}/release", json={"tranche_id": "T1"})
        assert refused.status_code == 422

        full = api.post("/escrow-guard/webhooks/escrow-funded", json={"ext_ref": ext_ref, "status": "FUNDED", "amount": 50000})
        assert full.json()["status"] == "ok" and world.status == "ESCROW_FUNDED"

        for tranche in ("T1", "T2", "T3"):
            assert api.post(f"/escrow-guard/escrows/{escrow_id}/release", json={"tranche_id": tranche, "proof_refs": ["P"]}).status_code == 200
        final_by_proof = api.post(f"/escrow-guard/escrows/{escrow_id}/release", json={"tranche_id": "T4"})
        assert final_by_proof.status_code == 422, "PortLess must never self-release the buyer's last slice"

        window = api.post(f"/escrow-guard/escrows/{escrow_id}/open-window")
        assert window.status_code == 200 and window.json()["inspection_deadline"]
        accepted = api.post(f"/escrow-guard/escrows/{escrow_id}/accept", json={"accepted_by": "buyer"})
        assert accepted.json()["status"] == "RELEASED" and accepted.json()["settled_at"]
        assert accepted.json()["fees_total"] == pytest.approx(500.0)

        world.status = "CLEARED"
        too_much = {"deal_id": "D1", "payee_id": "SUP", "amount": 49501, "idempotency_key": "over"}
        assert api.post("/escrow-guard/payouts", json=too_much).status_code == 422
        payout = api.post("/escrow-guard/payouts", json={
            "deal_id": "D1", "payee_id": "SUP", "amount": 45000, "idempotency_key": "k1", "total_owed": 45000})
        assert payout.status_code == 201
        assert api.get(f"/escrow-guard/payouts/{payout.json()['payout_id']}").status_code == 200

        done = api.post("/escrow-guard/webhooks/payout", json={"ext_ref": payout.json()["ext_ref"], "status": "COMPLETED"})
        assert done.status_code == 200 and world.status == "PAID"

        ledger = api.get("/escrow-guard/deals/D1/ledger").json()
        assert ledger["reconciled"] is True and ledger["realized_margin"] == pytest.approx(4500.0)
        assert ledger["supplier_balance_due"] == pytest.approx(0.0)
        assert api.get("/escrow-guard/deals/D1/summary").status_code == 200
        assert api.get("/escrow-guard/deals/D1/hold").json()["escrow_id"] == escrow_id
        assert api.get(f"/escrow-guard/holds/{escrow_id}").status_code == 200
        assert api.get(f"/escrow-guard/escrows/{escrow_id}").status_code == 200
        assert [row["deal_id"] for row in api.get("/escrow-guard/ledgers").json()] == ["D1"]

    def test_journal_balances_and_exports(self, api, world):
        escrow = api.post("/escrow-guard/escrows", json={"deal_id": "D1", "provider": "tazapay", "amount": 1000}).json()
        api.post("/escrow-guard/webhooks/escrow-funded", json={"ext_ref": escrow["ext_ref"], "status": "FUNDED", "amount": 1000})
        api.post(f"/escrow-guard/escrows/{escrow['escrow_id']}/release", json={"tranche_id": "T1"})

        journal = api.get("/escrow-guard/deals/D1/journal")
        assert journal.status_code == 200 and journal.json()["deal_id"] == "D1"
        as_csv = api.get("/escrow-guard/deals/D1/journal", params={"format": "csv"})
        assert as_csv.headers["content-type"].startswith("text/csv")
        assert 'filename="journal-D1.csv"' in as_csv.headers["content-disposition"]
        assert api.get("/escrow-guard/deals/NOPE/journal").status_code == 404
        assert len(api.get("/escrow-guard/journals").json()) == 1

    def test_all_journals_csv_keeps_one_header(self, api, world):
        for deal in ("D1", "D2"):
            world.status = "PI_ACCEPTED"
            escrow = api.post("/escrow-guard/escrows", json={"deal_id": deal, "provider": "tazapay", "amount": 1000}).json()
            api.post("/escrow-guard/webhooks/escrow-funded", json={"ext_ref": escrow["ext_ref"], "status": "FUNDED", "amount": 1000})
        lines = api.get("/escrow-guard/journals", params={"format": "csv"}).text.splitlines()
        header = lines[0]
        assert lines.count(header) == 1 and len(lines) > 2

    def test_dispute_blocks_acceptance_until_cleared(self, api, world):
        escrow = api.post("/escrow-guard/escrows", json={"deal_id": "D1", "provider": "tazapay", "amount": 1000}).json()
        api.post("/escrow-guard/webhooks/escrow-funded", json={"ext_ref": escrow["ext_ref"], "status": "FUNDED", "amount": 1000})
        path = f"/escrow-guard/escrows/{escrow['escrow_id']}"
        assert api.post(f"{path}/dispute", json={"reason": "damaged"}).json()["disputed"] is True
        assert api.post(f"{path}/accept", json={}).status_code == 422
        assert api.post(f"{path}/release", json={"tranche_id": "T1"}).status_code == 422
        assert api.post(f"{path}/clear-dispute", json={"reason": "agreed"}).json()["disputed"] is False
        assert api.post(f"{path}/release", json={"tranche_id": "T1"}).status_code == 200

    def test_refund_closes_a_disputed_deal(self, api, world):
        escrow = api.post("/escrow-guard/escrows", json={"deal_id": "D1", "provider": "tazapay", "amount": 1000}).json()
        api.post("/escrow-guard/webhooks/escrow-funded", json={"ext_ref": escrow["ext_ref"], "status": "FUNDED", "amount": 1000})
        world.status = "DISPUTE"
        refunded = api.post(f"/escrow-guard/escrows/{escrow['escrow_id']}/refund", json={"reason": "QC failed"})
        assert refunded.json()["status"] == "REFUNDED" and world.status == "REFUNDED"
        assert refunded.json()["refunded_total"] == pytest.approx(1000.0)

    def test_refund_leaves_other_deal_statuses_alone(self, api, world):
        escrow = api.post("/escrow-guard/escrows", json={"deal_id": "D1", "provider": "tazapay", "amount": 1000}).json()
        api.post("/escrow-guard/webhooks/escrow-funded", json={"ext_ref": escrow["ext_ref"], "status": "FUNDED", "amount": 1000})
        world.status = "CANCELLED"
        api.post(f"/escrow-guard/escrows/{escrow['escrow_id']}/refund", json={"reason": "x"})
        assert world.status == "CANCELLED"

    def test_legacy_hold_route(self, api, world):
        world.status = "ESCROW_PENDING"
        assert api.post("/escrow-guard/holds", json={"deal_id": "D1", "amount": 100}).status_code == 201

    def test_providers_and_health(self, api):
        providers = api.get("/escrow-guard/providers").json()["providers"]
        assert [p["provider"] for p in providers] == ["tazapay", "escrow_com"]
        assert all(p["payment_methods"] for p in providers)
        assert api.get("/escrow-guard/health").json() == {"service": "escrow-guard", "status": "ok"}

    def test_empty_ledger_for_unknown_deal(self, api):
        ledger = api.get("/escrow-guard/deals/NOPE/ledger").json()
        assert ledger["reconciled"] is True and ledger["payouts"] == []


NOT_FOUND = svc.DealNotFoundError("D")
HOLD_MISSING = svc.HoldNotFoundError("E")
PAYOUT_MISSING = svc.PayoutNotFoundError("P")
BAD_STATE = svc.InvalidDealState("D", "A", "B")
PROVIDER_FAILED = svc.ConnectorCallError("down")
BODY_ESCROW = {"deal_id": "D", "amount": 1}
BODY_PAYOUT = {"deal_id": "D", "payee_id": "S", "amount": 1, "idempotency_key": "k"}
BODY_RELEASE = {"tranche_id": "T1"}
BODY_REASON = {"reason": "r"}

ERROR_ROUTES = [
    ("post", "/escrow-guard/holds", BODY_ESCROW, "create_hold", NOT_FOUND, 404),
    ("post", "/escrow-guard/holds", BODY_ESCROW, "create_hold", BAD_STATE, 422),
    ("post", "/escrow-guard/holds", BODY_ESCROW, "create_hold", PROVIDER_FAILED, 502),
    ("get", "/escrow-guard/holds/E", None, "get_hold", HOLD_MISSING, 404),
    ("get", "/escrow-guard/deals/D/hold", None, "get_hold_by_deal", HOLD_MISSING, 404),
    ("post", "/escrow-guard/escrows", BODY_ESCROW, "create_escrow", NOT_FOUND, 404),
    ("post", "/escrow-guard/escrows", BODY_ESCROW, "create_escrow", BAD_STATE, 422),
    ("post", "/escrow-guard/escrows", BODY_ESCROW, "create_escrow", PROVIDER_FAILED, 502),
    ("get", "/escrow-guard/escrows/E", None, "get_hold", HOLD_MISSING, 404),
    ("post", "/escrow-guard/escrows/E/release", BODY_RELEASE, "release_tranche", HOLD_MISSING, 404),
    ("post", "/escrow-guard/escrows/E/release", BODY_RELEASE, "release_tranche", BAD_STATE, 422),
    ("post", "/escrow-guard/escrows/E/release", BODY_RELEASE, "release_tranche", PROVIDER_FAILED, 502),
    ("post", "/escrow-guard/escrows/E/accept", {}, "accept_escrow", HOLD_MISSING, 404),
    ("post", "/escrow-guard/escrows/E/accept", {}, "accept_escrow", BAD_STATE, 422),
    ("post", "/escrow-guard/escrows/E/accept", {}, "accept_escrow", PROVIDER_FAILED, 502),
    ("post", "/escrow-guard/escrows/E/dispute", BODY_REASON, "dispute_escrow", HOLD_MISSING, 404),
    ("post", "/escrow-guard/escrows/E/dispute", BODY_REASON, "dispute_escrow", BAD_STATE, 422),
    ("post", "/escrow-guard/escrows/E/open-window", BODY_REASON, "open_inspection_window", HOLD_MISSING, 404),
    ("post", "/escrow-guard/escrows/E/open-window", BODY_REASON, "open_inspection_window", BAD_STATE, 422),
    ("post", "/escrow-guard/escrows/E/clear-dispute", BODY_REASON, "clear_dispute", HOLD_MISSING, 404),
    ("post", "/escrow-guard/escrows/E/refund", BODY_REASON, "refund", HOLD_MISSING, 404),
    ("post", "/escrow-guard/escrows/E/refund", BODY_REASON, "refund", BAD_STATE, 422),
    ("post", "/escrow-guard/escrows/E/refund", BODY_REASON, "refund", PROVIDER_FAILED, 502),
    ("post", "/escrow-guard/payouts", BODY_PAYOUT, "request_payout", NOT_FOUND, 404),
    ("post", "/escrow-guard/payouts", BODY_PAYOUT, "request_payout", BAD_STATE, 422),
    ("post", "/escrow-guard/payouts", BODY_PAYOUT, "request_payout", PROVIDER_FAILED, 502),
    ("get", "/escrow-guard/payouts/P", None, "get_payout", PAYOUT_MISSING, 404),
]


class TestHttpErrors:
    @pytest.mark.parametrize(("method", "url", "body", "target", "error", "expected"), ERROR_ROUTES)
    def test_service_error_maps_to_status(self, api, method, url, body, target, error, expected):
        with patch(f"services.escrow_guard.router.{target}", new=AsyncMock(side_effect=error)):
            kwargs = {"json": body} if body is not None else {}
            response = getattr(api, method)(url, **kwargs)
        assert response.status_code == expected
        assert response.json()["detail"] == str(error)

    @pytest.mark.parametrize("url", ["/escrow-guard/webhooks/escrow-funded", "/escrow-guard/webhooks/payout"])
    def test_unknown_reference_is_404(self, api, url, world):
        response = api.post(url, json={"ext_ref": "nope", "status": "COMPLETED"})
        assert response.status_code == 404

    @pytest.mark.parametrize(("url", "target"), [
        ("/escrow-guard/webhooks/escrow-funded", "handle_escrow_funded_webhook"),
        ("/escrow-guard/webhooks/payout", "handle_payout_webhook"),
    ])
    def test_webhook_error_mapping(self, api, url, target):
        body = {"ext_ref": "r", "status": "COMPLETED"}
        with patch(f"services.escrow_guard.router.{target}", new=AsyncMock(side_effect=svc.WebhookVerifyError("bad"))):
            assert api.post(url, json=body).status_code == 401
        with patch(f"services.escrow_guard.router.{target}", new=AsyncMock(side_effect=RuntimeError("secret detail"))):
            failed = api.post(url, json=body)
        assert failed.status_code == 500
        assert failed.json()["detail"] == "Webhook processing failed", "internal errors must not leak to the provider"

    @pytest.mark.parametrize("url", ["/escrow-guard/webhooks/escrow-funded", "/escrow-guard/webhooks/payout"])
    @pytest.mark.parametrize("body", [b"{not json", b"", b"\xff\xfe\xfa"])
    def test_malformed_webhook_body_is_a_client_error(self, api, url, body):
        response = api.post(url, content=body, headers={"content-type": "application/json"})
        assert response.status_code == 400

    @pytest.mark.parametrize("url", ["/escrow-guard/webhooks/escrow-funded", "/escrow-guard/webhooks/payout"])
    @pytest.mark.parametrize("body", [{"status": "COMPLETED"}, [1, 2], "text", 3])
    def test_invalid_webhook_shape_is_a_client_error(self, api, url, body):
        response = api.post(url, json=body)
        assert response.status_code == 422
        assert response.json()["detail"] == "Invalid webhook payload"


class TestWebhookSignatures:
    def _sign(self, secret: str, body: bytes) -> str:
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def test_escrow_funded_signature_enforced(self, api, world, monkeypatch):
        monkeypatch.setenv("TAZAPAY_WEBHOOK_SECRET", "s3cret")
        escrow = api.post("/escrow-guard/escrows", json={"deal_id": "D1", "provider": "tazapay", "amount": 100}).json()
        body = json.dumps({"ext_ref": escrow["ext_ref"], "status": "FUNDED", "amount": 100}).encode()
        forged = api.post("/escrow-guard/webhooks/escrow-funded", content=body, headers={"x-tazapay-signature": "0" * 64})
        assert forged.status_code == 401 and world.status == "ESCROW_PENDING"
        real = api.post("/escrow-guard/webhooks/escrow-funded", content=body,
                        headers={"x-tazapay-signature": self._sign("s3cret", body)})
        assert real.status_code == 200 and world.status == "ESCROW_FUNDED"

    def test_tampered_body_is_refused(self, api, world, monkeypatch):
        monkeypatch.setenv("TAZAPAY_WEBHOOK_SECRET", "s3cret")
        escrow = api.post("/escrow-guard/escrows", json={"deal_id": "D1", "provider": "tazapay", "amount": 100}).json()
        signed = json.dumps({"ext_ref": escrow["ext_ref"], "status": "FUNDED", "amount": 1}).encode()
        sent = json.dumps({"ext_ref": escrow["ext_ref"], "status": "FUNDED", "amount": 100}).encode()
        response = api.post("/escrow-guard/webhooks/escrow-funded", content=sent,
                            headers={"x-tazapay-signature": self._sign("s3cret", signed)})
        assert response.status_code == 401

    def test_non_ascii_signature_is_refused_not_a_crash(self, api, world, monkeypatch):
        monkeypatch.setenv("TAZAPAY_WEBHOOK_SECRET", "s3cret")
        escrow = api.post("/escrow-guard/escrows", json={"deal_id": "D1", "provider": "tazapay", "amount": 100}).json()
        body = json.dumps({"ext_ref": escrow["ext_ref"], "status": "FUNDED", "amount": 100}).encode()
        response = api.post("/escrow-guard/webhooks/escrow-funded", content=body,
                            headers={"x-tazapay-signature": "é".encode("latin-1")})
        assert response.status_code == 401

    async def test_payout_signature_enforced(self, api, world, monkeypatch):
        monkeypatch.setenv("PAYONEER_WEBHOOK_SECRET", "p-secret")
        await get_repository().save_payout(_payout())
        body = json.dumps({"ext_ref": "p-ref-1", "status": "COMPLETED"}).encode()
        assert api.post("/escrow-guard/webhooks/payout", content=body, headers={"x-payoneer-signature": "bad"}).status_code == 401
        assert (await get_repository().get_payout("PAY-1")).status == "PROCESSING"


# ---------------------------------------------------------------------------
# Provider connectors
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_http(monkeypatch):
    """Route every httpx.AsyncClient through a handler; records each request."""
    real = httpx.AsyncClient

    def install(handler):
        seen: list[httpx.Request] = []

        def _record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(_record), **kw))
        return seen

    return install


def _json_response(payload, code=200):
    return lambda _request: httpx.Response(code, json=payload)


class TestStripe:
    @pytest.fixture
    def live(self, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test")
        return StripeTreasuryConnector()

    async def test_stub_mode(self):
        conn = StripeTreasuryConnector()
        booked = await conn.book({"amount": 500, "currency": "EUR"})
        assert booked["stub"] is True and booked["currency"] == "eur" and booked["ext_ref"].startswith("pi_stub_")
        assert (await conn.status("x"))["status"] == "requires_capture"
        assert (await conn.capture("x"))["status"] == "succeeded"
        assert await conn.quote({}) == {}

    async def test_book_sends_a_manual_capture_intent(self, live, mock_http):
        seen = mock_http(_json_response({"id": "pi_1", "status": "requires_capture", "amount": 500, "currency": "usd", "client_secret": "cs"}))
        booked = await live.book({"amount": 500.9, "currency": "USD", "metadata": {"deal_id": "D1"}})
        assert booked["ext_ref"] == "pi_1" and booked["client_secret"] == "cs"
        sent = seen[0].content.decode()
        assert "capture_method=manual" in sent and "amount=500" in sent and "metadata%5Bdeal_id%5D=D1" in sent
        assert seen[0].headers["authorization"] == "Bearer sk_test"

    async def test_status_and_capture(self, live, mock_http):
        mock_http(_json_response({"id": "pi_1", "status": "succeeded", "amount": 5, "currency": "usd"}))
        assert (await live.status("pi_1"))["status"] == "succeeded"
        assert await live.capture("pi_1") == {"ext_ref": "pi_1", "status": "succeeded"}

    @pytest.mark.parametrize("call", ["book", "status", "capture"])
    @pytest.mark.parametrize("code", [400, 402, 500, 503])
    async def test_provider_error_raises(self, live, mock_http, call, code):
        mock_http(_json_response({"error": "x"}, code))
        arg = {"amount": 1} if call == "book" else "pi_1"
        with pytest.raises(ConnectorError, match=str(code)):
            await getattr(live, call)(arg)

    def _sig(self, secret, body, ts):
        mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        return f"t={ts},v1={mac}"

    async def test_no_secret_accepts(self):
        assert await StripeTreasuryConnector().webhook_verify({}, b"x") is True

    async def test_signature_outcomes(self, monkeypatch):
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec")
        conn = StripeTreasuryConnector()
        now = int(time.time())
        assert await conn.webhook_verify({"stripe-signature": self._sig("whsec", b"b", now)}, b"b") is True
        assert await conn.webhook_verify({"stripe-signature": self._sig("other", b"b", now)}, b"b") is False
        assert await conn.webhook_verify({"stripe-signature": self._sig("whsec", b"b", now)}, b"tampered") is False

    @pytest.mark.parametrize("header", ["", "garbage", "t=abc,v1=00", "v1"])
    async def test_malformed_header_is_refused(self, monkeypatch, header):
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec")
        assert await StripeTreasuryConnector().webhook_verify({"stripe-signature": header}, b"b") is False

    async def test_zero_tolerance_turns_the_replay_window_off(self, monkeypatch):
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec")
        monkeypatch.setenv("STRIPE_WEBHOOK_TOLERANCE_SECONDS", "0")
        old = int(time.time()) - 86400
        assert await StripeTreasuryConnector().webhook_verify({"stripe-signature": self._sig("whsec", b"b", old)}, b"b") is True


class TestPayoneer:
    @pytest.fixture
    def live(self, monkeypatch):
        monkeypatch.setenv("PAYONEER_CLIENT_ID", "cid")
        monkeypatch.setenv("PAYONEER_CLIENT_SECRET", "sec")
        monkeypatch.setenv("PAYONEER_BASE_URL", "https://payoneer.test")
        return PayoneerConnector()

    @staticmethod
    def _api(payload=None, code=200):
        def handler(request):
            if request.url.path.endswith("/oauth2/token"):
                return httpx.Response(200, json={"access_token": "tok"})
            return httpx.Response(code, json=payload or {})

        return handler

    async def test_stub_mode(self):
        conn = PayoneerConnector()
        assert (await conn.quote({"amount": 100}))["fee_usd"] == pytest.approx(1.5)
        booked = await conn.book({"amount": 10, "payee_id": "P"})
        assert booked["stub"] is True and booked["status"] == "PENDING" and booked["ext_ref"].startswith("PAY-STUB-")
        assert (await conn.status("x"))["status"] == "COMPLETED"

    async def test_book_carries_the_idempotency_key(self, live, mock_http):
        seen = mock_http(self._api({"payment_id": "PM-1", "status": "QUEUED"}))
        booked = await live.book({"payee_id": "P", "amount": 10.5, "idempotency_key": "k-1"})
        assert booked["ext_ref"] == "PM-1" and booked["status"] == "QUEUED"
        payment = seen[-1]
        assert payment.headers["idempotency-key"] == "k-1" and payment.headers["authorization"] == "Bearer tok"
        assert json.loads(payment.content) == {"payee_id": "P", "amount": 10.5, "currency": "USD", "description": ""}

    async def test_token_is_fetched_once(self, live, mock_http):
        seen = mock_http(self._api({"status": "x"}))
        await live.status("a")
        await live.status("b")
        assert sum(1 for r in seen if r.url.path.endswith("/oauth2/token")) == 1

    async def test_quote(self, live, mock_http):
        mock_http(self._api({"rate": 83.1}))
        assert await live.quote({}) == {"rate": 83.1}

    async def test_token_failure(self, live, mock_http):
        mock_http(_json_response({}, 401))
        with pytest.raises(ConnectorError, match="token fetch failed"):
            await live.status("a")

    @pytest.mark.parametrize(("call", "arg"), [("quote", {}), ("book", {"payee_id": "P", "amount": 1}), ("status", "x")])
    async def test_provider_error_raises(self, live, mock_http, call, arg):
        mock_http(self._api({}, 500))
        with pytest.raises(ConnectorError, match="500"):
            await getattr(live, call)(arg)

    async def test_webhook(self, monkeypatch):
        assert await PayoneerConnector().webhook_verify({}, b"b") is True
        monkeypatch.setenv("PAYONEER_WEBHOOK_SECRET", "s")
        conn = PayoneerConnector()
        good = hmac.new(b"s", b"b", hashlib.sha256).hexdigest()
        assert await conn.webhook_verify({"x-payoneer-signature": good}, b"b") is True
        assert await conn.webhook_verify({"x-payoneer-signature": good}, b"c") is False
        assert await conn.webhook_verify({}, b"b") is False


ESCROW_CONNECTORS = [
    pytest.param(TazapayEscrowConnector, {"TAZAPAY_API_KEY": "k", "TAZAPAY_API_SECRET": "s"}, "x-tazapay-signature",
                 "TAZAPAY_WEBHOOK_SECRET", id="tazapay"),
    pytest.param(EscrowComConnector, {"ESCROW_COM_API_KEY": "k", "ESCROW_COM_EMAIL": "e@x"}, "x-escrow-signature",
                 "ESCROW_COM_WEBHOOK_SECRET", id="escrow_com"),
]


class TestEscrowConnectors:
    @pytest.mark.parametrize(("cls", "_env", "_h", "_s"), ESCROW_CONNECTORS)
    async def test_stub_mode(self, cls, _env, _h, _s):
        conn = cls()
        booked = await conn.book({"amount": 10, "currency": "USD"})
        assert booked["stub"] is True and booked["amount"] == 10 and booked["checkout_url"].endswith(booked["ext_ref"])
        released = await conn.release({"ext_ref": "r", "amount": 3, "tranche_id": "T1"})
        assert released["stub"] is True and released["tranche_id"] == "T1" and released["amount"] == 3
        assert (await conn.refund({"ext_ref": "r", "amount": 3}))["stub"] is True
        assert (await conn.status("r"))["stub"] is True
        assert await conn.list_payment_methods()
        assert "fee_pct" in await conn.quote({})

    @pytest.mark.parametrize(("cls", "env", "_h", "_s"), ESCROW_CONNECTORS)
    @pytest.mark.parametrize(("call", "arg"), [
        ("book", {"amount": 10}), ("release", {"ext_ref": "r", "amount": 3}),
        ("refund", {"ext_ref": "r", "amount": 3}), ("status", "r"),
    ])
    @pytest.mark.parametrize("code", [400, 500])
    async def test_provider_error_raises(self, monkeypatch, mock_http, cls, env, _h, _s, call, arg, code):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        mock_http(_json_response({}, code))
        with pytest.raises(ConnectorError, match=str(code)):
            await getattr(cls(), call)(arg)

    @pytest.mark.parametrize(("cls", "env", "_h", "_s"), ESCROW_CONNECTORS)
    @pytest.mark.parametrize(("call", "arg"), [("release", {"ext_ref": "r", "amount": 3}), ("refund", {"ext_ref": "r", "amount": 3}), ("status", "r")])
    async def test_success_returns_provider_body(self, monkeypatch, mock_http, cls, env, _h, _s, call, arg):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        mock_http(_json_response({"ok": 1}))
        assert await getattr(cls(), call)(arg) == {"ok": 1}

    @pytest.mark.parametrize(("cls", "_e", "header", "secret"), ESCROW_CONNECTORS)
    async def test_webhook(self, monkeypatch, cls, _e, header, secret):
        assert await cls().webhook_verify({}, b"b") is True
        monkeypatch.setenv(secret, "s")
        conn = cls()
        good = hmac.new(b"s", b"b", hashlib.sha256).hexdigest()
        assert await conn.webhook_verify({header: good}, b"b") is True
        assert await conn.webhook_verify({header: good}, b"c") is False
        assert await conn.webhook_verify({}, b"b") is False

    async def test_tazapay_book_uses_basic_auth(self, monkeypatch, mock_http):
        monkeypatch.setenv("TAZAPAY_API_KEY", "k")
        monkeypatch.setenv("TAZAPAY_API_SECRET", "s")
        seen = mock_http(_json_response({"escrow_id": "tz-1", "checkout_url": "https://c"}))
        booked = await TazapayEscrowConnector().book({"amount": 10, "reference": "D1", "description": "d"})
        assert booked["ext_ref"] == "tz-1" and booked["status"] == "AWAITING_FUNDING"
        assert seen[0].headers["authorization"].startswith("Basic ")
        assert json.loads(seen[0].content) == {"amount": 10, "currency": "USD", "reference": "D1", "transaction_description": "d"}

    async def test_tazapay_prefers_id_field(self, monkeypatch, mock_http):
        monkeypatch.setenv("TAZAPAY_API_KEY", "k")
        mock_http(_json_response({"id": "a", "escrow_id": "b"}))
        assert (await TazapayEscrowConnector().book({"amount": 1}))["ext_ref"] == "a"

    @pytest.mark.parametrize(("status_body", "expected"), [
        ({"in_dispute": True}, "in_dispute"), ({"in_dispute": False}, "awaiting_payment"), ({}, "awaiting_payment"),
    ])
    async def test_escrow_com_book(self, monkeypatch, mock_http, status_body, expected):
        monkeypatch.setenv("ESCROW_COM_API_KEY", "k")
        monkeypatch.setenv("ESCROW_COM_EMAIL", "e@x")
        seen = mock_http(_json_response({"id": 42, "status": status_body, "landing_page": "https://l"}))
        booked = await EscrowComConnector().book({"amount": 10, "currency": "USD", "reference": "D1"})
        assert booked["ext_ref"] == "42" and booked["status"] == expected and booked["checkout_url"] == "https://l"
        sent = json.loads(seen[0].content)
        assert sent["currency"] == "usd" and sent["items"][0]["schedule"][0]["amount"] == 10


# ---------------------------------------------------------------------------
# Service branches
# ---------------------------------------------------------------------------

class TestServiceHelpers:
    @pytest.mark.parametrize(("provider", "amount", "expected"), [
        ("stripe", 12345, 123.45), ("stripe_treasury", 100, 1.0), ("tazapay", 123.456, 123.46),
        ("escrow_com", 10, 10.0), ("tazapay", None, None), ("stripe", None, None), ("stripe", 0, 0.0),
    ])
    def test_amount_units(self, provider, amount, expected):
        assert svc._normalize_funded_amount(provider, amount) == expected

    async def test_fetch_deal_not_found(self):
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(side_effect=RuntimeError("Deal Not Found"))),
            pytest.raises(svc.DealNotFoundError),
        ):
            await svc._fetch_deal("D")

    async def test_fetch_deal_other_error_propagates(self):
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(side_effect=RuntimeError("mongo down"))),
            pytest.raises(RuntimeError, match="mongo down"),
        ):
            await svc._fetch_deal("D")

    async def test_transition_calls_the_deal_service(self):
        update = AsyncMock()
        with patch("services.deals.service.update_status", new=update):
            await svc._transition_deal("D", "PAID", "because")
        assert update.await_args.args[0] == "D"
        assert update.await_args.args[1].target_status.value == "PAID"

    async def test_transition_failure_propagates(self):
        with (
            patch("services.deals.service.update_status", new=AsyncMock(side_effect=RuntimeError("no"))),
            pytest.raises(RuntimeError),
        ):
            await svc._transition_deal("D", "PAID", "r")

    async def test_emit_publishes(self):
        publisher = SimpleNamespace(publish=AsyncMock())
        with patch.object(svc, "get_publisher", return_value=publisher):
            await svc._emit(EventTopic.ESCROW_FUNDED, "D", {"a": 1})
        event = publisher.publish.await_args.args[0]
        assert event.topic == EventTopic.ESCROW_FUNDED and event.deal_id == "D"

    async def test_emit_failure_is_swallowed(self):
        with patch.object(svc, "get_publisher", side_effect=RuntimeError("bus down")):
            await svc._emit(EventTopic.ESCROW_FUNDED, "D", {})

    @pytest.mark.parametrize(("name", "cls"), [
        ("stripe", StripeTreasuryConnector), ("stripe_treasury", StripeTreasuryConnector), ("payoneer", PayoneerConnector),
        ("tazapay", TazapayEscrowConnector), ("escrow_com", EscrowComConnector),
    ])
    def test_connector_lookup(self, name, cls):
        assert isinstance(svc._payment_connector(name), cls)

    def test_unknown_provider(self):
        with pytest.raises(ValueError, match="Unknown payment provider"):
            svc._payment_connector("wire-it-to-me")

    async def test_fee_quote_failure_uses_default(self):
        with patch.object(TazapayEscrowConnector, "quote", new=AsyncMock(side_effect=RuntimeError("x"))):
            assert await svc._provider_fee_pct("tazapay") == svc._DEFAULT_FEE_PCT.get("tazapay")

    async def test_supplier_owed_from_po(self):
        repo = SimpleNamespace(get_po_by_deal=AsyncMock(return_value=SimpleNamespace(total_value=123.456)))
        with patch("services.dealmaker.repository.get_repository", return_value=repo):
            assert await svc._supplier_owed("D") == 123.46

    @pytest.mark.parametrize("po", [None, SimpleNamespace(total_value=0)])
    async def test_supplier_owed_unknown(self, po):
        repo = SimpleNamespace(get_po_by_deal=AsyncMock(return_value=po))
        with patch("services.dealmaker.repository.get_repository", return_value=repo):
            assert await svc._supplier_owed("D") is None

    async def test_supplier_owed_lookup_failure(self):
        with patch("services.dealmaker.repository.get_repository", side_effect=RuntimeError("x")):
            assert await svc._supplier_owed("D") is None

    @pytest.mark.parametrize(("hold", "expected"), [
        ({"status": "PENDING"}, 0.0),
        ({"status": "FAILED"}, 0.0),
        ({"status": "FUNDED"}, 1000.0),
        ({"status": "RELEASED", "funded_amount": 900.0}, 900.0),
    ])
    def test_net_received_legacy(self, hold, expected):
        assert svc._net_received(_hold(**hold)) == expected

    def test_net_received_staged_is_net_of_fees(self):
        assert svc._net_received(_hold(tranches=_tranches(("T1",)), released_total=300.0, fees_total=3.0)) == 297.0

    def test_payout_totals_ignore_failed(self):
        payouts = [_payout(status="COMPLETED", amount=10), _payout(status="PROCESSING", amount=5),
                   _payout(status="REQUESTED", amount=2), _payout(status="FAILED", amount=99)]
        assert svc._payout_totals(payouts) == (10.0, 7.0)

    async def test_settle_is_idempotent(self, world):
        settled = _hold(tranches=_tranches(("T1", "T2", "T3", "T4")), settled_at=NOW)
        assert await svc._settle_if_complete(settled, NOW) is settled
        assert world.events == []

    async def test_settle_needs_tranches(self, world):
        legacy = _hold()
        assert await svc._settle_if_complete(legacy, NOW) is legacy

    async def test_shortfall_notice(self):
        deal = SimpleNamespace(deal_id="D")
        notify = AsyncMock()
        with patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)), \
                patch("shared.orchestration.notifications.notify_funding_shortfall", new=notify):
            await svc._notify_funding_shortfall(_hold())
        notify.assert_awaited_once()

    async def test_shortfall_notice_failure_is_swallowed(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(side_effect=RuntimeError("x"))):
            await svc._notify_funding_shortfall(_hold())

    async def test_realized_margin_patched(self):
        await _saved(tranches=_tranches(("T1",)), released_total=300.0, fees_total=3.0)
        await get_repository().save_payout(_payout(status="COMPLETED", amount=100.0))
        update = AsyncMock()
        with patch("services.deals.service.update_financials", new=update):
            await svc._patch_deal_realized_margin("D1")
        assert update.await_args.args[1].realized_margin_usd == pytest.approx(197.0)

    async def test_realized_margin_without_a_hold(self):
        update = AsyncMock()
        with patch("services.deals.service.update_financials", new=update):
            await svc._patch_deal_realized_margin("NOPE")
        update.assert_not_awaited()

    async def test_realized_margin_failure_is_swallowed(self):
        await _saved(tranches=_tranches(("T1",)), released_total=300.0, fees_total=3.0)
        with patch("services.deals.service.update_financials", new=AsyncMock(side_effect=RuntimeError("x"))):
            await svc._patch_deal_realized_margin("D1")


class TestFundedWebhookBranches:
    async def test_verifier_crash_is_a_rejection(self, world):
        await _saved(status="PENDING")
        with (
            patch.object(TazapayEscrowConnector, "webhook_verify", new=AsyncMock(side_effect=RuntimeError("x"))),
            pytest.raises(svc.WebhookVerifyError),
        ):
            await svc.handle_escrow_funded_webhook(
                EscrowFundedWebhookPayload(ext_ref="ref-1", status="FUNDED"), {}, b"{}")

    async def test_failed_transition_still_records_the_money(self, world, monkeypatch):
        await _saved(status="PENDING")
        monkeypatch.setattr(svc, "_transition_deal", AsyncMock(side_effect=RuntimeError("deals down")))
        out = await svc.handle_escrow_funded_webhook(
            EscrowFundedWebhookPayload(ext_ref="ref-1", status="FUNDED", amount=1000), {}, b"{}")
        assert out["status"] == "ok"
        assert (await get_repository().get_hold("ESC-1")).status == "FUNDED"


class TestCreateEscrowBranches:
    REQUEST: ClassVar[dict] = {"deal_id": "D1", "provider": "tazapay", "amount": 1000.0}

    async def test_idempotency_key_returns_existing(self, world):
        await _saved(idempotency_key="same")
        out = await svc.create_escrow(CreateEscrowRequest(**self.REQUEST, idempotency_key="same"))
        assert out.escrow_id == "ESC-1"

    async def test_open_escrow_is_reused_for_the_deal(self, world):
        await _saved(status="PENDING")
        assert (await svc.create_escrow(CreateEscrowRequest(**self.REQUEST))).escrow_id == "ESC-1"
        assert world.transitions == []

    async def test_failed_escrow_can_be_reopened(self, world):
        await _saved(status="FAILED")
        assert (await svc.create_escrow(CreateEscrowRequest(**self.REQUEST))).escrow_id != "ESC-1"

    @pytest.mark.parametrize("status", ["DRAFT", "ESCROW_FUNDED", "PAID", "CANCELLED", "DISPUTE"])
    async def test_refused_in_other_deal_states(self, world, status):
        world.status = status
        with pytest.raises(svc.InvalidDealState):
            await svc.create_escrow(CreateEscrowRequest(**self.REQUEST))

    async def test_already_pending_deal_is_not_transitioned(self, world):
        world.status = "ESCROW_PENDING"
        await svc.create_escrow(CreateEscrowRequest(**self.REQUEST))
        assert world.transitions == []

    async def test_provider_failure_records_failed_and_raises(self, world):
        with (
            patch.object(TazapayEscrowConnector, "book", new=AsyncMock(side_effect=_provider_down)),
            pytest.raises(svc.ConnectorCallError),
        ):
            await svc.create_escrow(CreateEscrowRequest(**self.REQUEST))
        assert (await get_repository().get_hold_by_deal("D1")).status == "FAILED"
        assert world.transitions == []

    async def test_failed_transition_keeps_the_escrow(self, world, monkeypatch):
        monkeypatch.setattr(svc, "_transition_deal", AsyncMock(side_effect=RuntimeError("x")))
        out = await svc.create_escrow(CreateEscrowRequest(**self.REQUEST))
        assert out.ext_ref


class TestPayoutBranches:
    REQUEST: ClassVar[dict] = {"deal_id": "D1", "payee_id": "S", "amount": 100.0, "idempotency_key": "k"}

    async def _ready(self, world, **hold):
        world.status = "CLEARED"
        await _saved(tranches=_tranches(("T1", "T2", "T3")), released_total=900.0, fees_total=9.0, **hold)

    async def test_disputed_escrow_refused(self, world):
        await self._ready(world, disputed=True)
        with pytest.raises(svc.InvalidDealState, match="disputed"):
            await svc.request_payout(PayoutRequest(**self.REQUEST))

    @pytest.mark.parametrize("amount", [0.0, -1.0])
    async def test_non_positive_amount_refused(self, world, amount):
        await self._ready(world)
        with pytest.raises(svc.InvalidDealState, match="amount"):
            await svc.request_payout(PayoutRequest(**{**self.REQUEST, "amount": amount}))

    @pytest.mark.parametrize("status", ["PI_SENT", "IN_TRANSIT", "PAID", "DISPUTE"])
    async def test_main_payout_needs_cleared_cargo(self, world, status):
        await self._ready(world)
        world.status = status
        with pytest.raises(svc.InvalidDealState):
            await svc.request_payout(PayoutRequest(**self.REQUEST))

    async def test_balance_payout_allowed_after_paid(self, world):
        await self._ready(world)
        world.status = "PAID"
        out = await svc.request_payout(PayoutRequest(**self.REQUEST, kind="supplier_balance"))
        assert out.status == "PROCESSING"

    @pytest.mark.parametrize("status", ["PENDING", "PARTIALLY_FUNDED", "REFUNDED", "FAILED"])
    async def test_unfunded_escrow_refused(self, world, status):
        world.status = "CLEARED"
        await _saved(status=status)
        with pytest.raises(svc.InvalidDealState):
            await svc.request_payout(PayoutRequest(**self.REQUEST))

    async def test_no_escrow_refused(self, world):
        world.status = "CLEARED"
        with pytest.raises(svc.InvalidDealState, match="none"):
            await svc.request_payout(PayoutRequest(**self.REQUEST))

    async def test_exact_available_amount_is_allowed(self, world):
        await self._ready(world)
        assert (await svc.request_payout(PayoutRequest(**{**self.REQUEST, "amount": 891.0}))).status == "PROCESSING"

    async def test_one_dollar_over_available_refused(self, world):
        await self._ready(world)
        with pytest.raises(svc.InvalidDealState, match="exceeds"):
            await svc.request_payout(PayoutRequest(**{**self.REQUEST, "amount": 892.0}))

    async def test_pending_payout_reduces_what_is_available(self, world):
        await self._ready(world)
        await svc.request_payout(PayoutRequest(**{**self.REQUEST, "amount": 800.0}))
        with pytest.raises(svc.InvalidDealState, match="exceeds"):
            await svc.request_payout(PayoutRequest(**{**self.REQUEST, "amount": 100.0, "idempotency_key": "k2"}))

    async def test_same_key_pays_once(self, world):
        await self._ready(world)
        book = AsyncMock(return_value={"ext_ref": "x", "status": "PENDING"})
        with patch.object(PayoneerConnector, "book", new=book):
            first = await svc.request_payout(PayoutRequest(**self.REQUEST))
            second = await svc.request_payout(PayoutRequest(**self.REQUEST))
        assert first.payout_id == second.payout_id
        book.assert_awaited_once()

    async def test_provider_failure_frees_the_money_for_a_retry(self, world):
        await self._ready(world)
        with (
            patch.object(PayoneerConnector, "book", new=AsyncMock(side_effect=_provider_down)),
            pytest.raises(svc.ConnectorCallError),
        ):
            await svc.request_payout(PayoutRequest(**{**self.REQUEST, "amount": 891.0}))
        payouts = await get_repository().list_payouts_by_deal("D1")
        assert [p.status for p in payouts] == ["FAILED"]
        retried = await svc.request_payout(PayoutRequest(**{**self.REQUEST, "amount": 891.0, "idempotency_key": "k2"}))
        assert retried.status == "PROCESSING"


class TestPayoutWebhookBranches:
    def _payload(self, status="COMPLETED", **kw):
        return PayoutWebhookPayload(ext_ref="p-ref-1", status=status, **kw)

    async def test_unknown_payout(self, world):
        with pytest.raises(svc.PayoutNotFoundError):
            await svc.handle_payout_webhook(self._payload(), {}, b"{}")

    async def test_verifier_crash_is_a_rejection(self, world):
        await get_repository().save_payout(_payout())
        with (
            patch.object(PayoneerConnector, "webhook_verify", new=AsyncMock(side_effect=RuntimeError("x"))),
            pytest.raises(svc.WebhookVerifyError),
        ):
            await svc.handle_payout_webhook(self._payload(), {}, b"{}")

    async def test_completed_twice_does_nothing_more(self, world):
        await _saved(tranches=_tranches(("T1",)), released_total=300.0)
        await get_repository().save_payout(_payout(status="COMPLETED"))
        out = await svc.handle_payout_webhook(self._payload(), {}, b"{}")
        assert out == {"status": "ok", "payout_id": "PAY-1", "deal_id": "D1"}
        assert world.events == []

    async def test_paid_transition_failure_is_tolerated(self, world, monkeypatch):
        await get_repository().save_payout(_payout(total_owed=100.0))
        world.status = "CLEARED"
        monkeypatch.setattr(svc, "_transition_deal", AsyncMock(side_effect=RuntimeError("x")))
        await svc.handle_payout_webhook(self._payload(), {}, b"{}")
        assert (await get_repository().get_payout("PAY-1")).status == "COMPLETED"

    async def test_already_paid_deal_is_not_transitioned_again(self, world):
        await get_repository().save_payout(_payout(total_owed=100.0))
        world.status = "PAID"
        await svc.handle_payout_webhook(self._payload(), {}, b"{}")
        assert world.transitions == []

    async def test_failed_payout_raises_an_exception_event(self, world):
        await get_repository().save_payout(_payout())
        await svc.handle_payout_webhook(self._payload("FAILED"), {}, b"{}")
        assert (await get_repository().get_payout("PAY-1")).status == "FAILED"
        assert EventTopic.DEAL_EXCEPTION in world.topics()
        assert "PAID" not in world.transitions


MISSING_HOLD_CALLS = [
    pytest.param(lambda: svc.release_tranche("X", ReleaseTrancheRequest(tranche_id="T1")), id="release"),
    pytest.param(lambda: svc.refund("X", RefundRequest()), id="refund"),
    pytest.param(lambda: svc.open_inspection_window("X", reason="r", trigger="operator"), id="open_window"),
    pytest.param(lambda: svc.clear_dispute("X"), id="clear_dispute"),
    pytest.param(lambda: svc.accept_escrow("X", AcceptEscrowRequest()), id="accept"),
    pytest.param(lambda: svc.dispute_escrow("X", DisputeEscrowRequest(reason="r")), id="dispute"),
]
WRONG_STATE_CALLS = [
    pytest.param(lambda: svc.release_tranche("ESC-1", ReleaseTrancheRequest(tranche_id="T1")), id="release"),
    pytest.param(lambda: svc.open_inspection_window("ESC-1", reason="r", trigger="operator"), id="open_window"),
    pytest.param(lambda: svc.accept_escrow("ESC-1", AcceptEscrowRequest()), id="accept"),
    pytest.param(lambda: svc.dispute_escrow("ESC-1", DisputeEscrowRequest(reason="r")), id="dispute"),
]


class TestEscrowStateGuards:
    @pytest.mark.parametrize("call", MISSING_HOLD_CALLS)
    async def test_missing_hold(self, world, call):
        with pytest.raises(svc.HoldNotFoundError):
            await call()

    @pytest.mark.parametrize("call", WRONG_STATE_CALLS)
    @pytest.mark.parametrize("status", ["PENDING", "PARTIALLY_FUNDED", "REFUNDED", "FAILED"])
    async def test_unfunded_escrow_refused(self, world, call, status):
        await _saved(status=status, tranches=_tranches())
        with pytest.raises(svc.InvalidDealState):
            await call()

    @pytest.mark.parametrize("status", ["PENDING", "FAILED"])
    async def test_refund_refused_before_any_money(self, world, status):
        await _saved(status=status)
        with pytest.raises(svc.InvalidDealState):
            await svc.refund("ESC-1", RefundRequest())

    async def test_refund_is_idempotent(self, world):
        await _saved(status="REFUNDED", refunded_total=1000.0)
        with patch.object(TazapayEscrowConnector, "refund", new=AsyncMock()) as call:
            out = await svc.refund("ESC-1", RefundRequest())
        assert out.status == "REFUNDED"
        call.assert_not_awaited()

    async def test_refund_only_returns_the_undisbursed_balance(self, world):
        await _saved(tranches=_tranches(("T1",)), released_total=300.0, fees_total=3.0)
        out = await svc.refund("ESC-1", RefundRequest(reason="r"))
        assert out.refunded_total == pytest.approx(700.0)

    async def test_refund_provider_failure(self, world):
        await _saved()
        with (
            patch.object(TazapayEscrowConnector, "refund", new=AsyncMock(side_effect=_provider_down)),
            pytest.raises(svc.ConnectorCallError),
        ):
            await svc.refund("ESC-1", RefundRequest())
        assert (await get_repository().get_hold("ESC-1")).status == "FUNDED"

    async def test_final_tranche_cannot_be_released_by_proof(self, world):
        await _saved(tranches=_tranches(("T1", "T2", "T3")), released_total=900.0)
        with pytest.raises(svc.InvalidDealState, match="ACCEPTED"):
            await svc.release_tranche("ESC-1", ReleaseTrancheRequest(tranche_id="T4"))

    async def test_unknown_tranche(self, world):
        await _saved(tranches=_tranches())
        with pytest.raises(svc.HoldNotFoundError):
            await svc.release_tranche("ESC-1", ReleaseTrancheRequest(tranche_id="T9"))

    async def test_released_tranche_is_not_paid_twice(self, world):
        await _saved(tranches=_tranches(("T1",)), released_total=300.0)
        with patch.object(TazapayEscrowConnector, "release", new=AsyncMock()) as call:
            out = await svc.release_tranche("ESC-1", ReleaseTrancheRequest(tranche_id="T1"))
        assert out.released_total == 300.0
        call.assert_not_awaited()

    @pytest.mark.parametrize("deal_status", sorted(svc._RELEASE_BLOCKING_DEAL_STATUSES))
    async def test_every_blocking_deal_status_stops_release(self, world, deal_status):
        await _saved(tranches=_tranches())
        world.status = deal_status
        with pytest.raises(svc.InvalidDealState):
            await svc.release_tranche("ESC-1", ReleaseTrancheRequest(tranche_id="T1"))

    async def test_release_provider_failure_keeps_the_tranche_pending(self, world):
        await _saved(tranches=_tranches())
        with (
            patch.object(TazapayEscrowConnector, "release", new=AsyncMock(side_effect=_provider_down)),
            pytest.raises(svc.ConnectorCallError),
        ):
            await svc.release_tranche("ESC-1", ReleaseTrancheRequest(tranche_id="T1"))
        hold = await get_repository().get_hold("ESC-1")
        assert hold.tranches[0].status == "PENDING" and hold.released_total == 0.0

    async def test_accept_provider_failure_keeps_the_tranche_pending(self, world):
        await _saved(tranches=_tranches(("T1", "T2", "T3")), released_total=900.0)
        with (
            patch.object(TazapayEscrowConnector, "release", new=AsyncMock(side_effect=_provider_down)),
            pytest.raises(svc.ConnectorCallError),
        ):
            await svc.accept_escrow("ESC-1", AcceptEscrowRequest())
        assert (await get_repository().get_hold("ESC-1")).tranches[3].status == "PENDING"

    async def test_accept_twice_is_idempotent(self, world):
        await _saved(tranches=_tranches(("T1", "T2", "T3", "T4")), released_total=1000.0, buyer_accepted_at=NOW)
        out = await svc.accept_escrow("ESC-1", AcceptEscrowRequest())
        assert out.released_total == 1000.0
        assert world.events == []

    async def test_window_stays_open_once(self, world):
        await _saved(tranches=_tranches(("T1", "T2", "T3")), inspection_deadline=NOW + timedelta(days=1))
        out = await svc.open_inspection_window("ESC-1", reason="r", trigger="operator")
        assert out.inspection_deadline is not None
        assert world.events == []

    async def test_window_needs_a_pending_final_tranche(self, world):
        await _saved(tranches=_tranches(("T1", "T2", "T3", "T4")))
        assert (await svc.open_inspection_window("ESC-1", reason="r", trigger="operator")).inspection_deadline is None

    async def test_clear_when_not_disputed_changes_nothing(self, world):
        await _saved()
        assert (await svc.clear_dispute("ESC-1")).disputed is False
        assert world.events == []


class TestInspectionWatcher:
    PAST = NOW - timedelta(hours=1)

    async def test_lapsed_window_releases_the_final_tranche(self, world):
        await _saved(tranches=_tranches(("T1", "T2", "T3")), released_total=900.0, inspection_deadline=self.PAST)
        assert await svc.check_inspection_windows() == 1
        assert (await get_repository().get_hold("ESC-1")).tranches[3].status == "RELEASED"

    async def test_naive_deadline_is_treated_as_utc(self, world):
        naive = (NOW - timedelta(days=2)).replace(tzinfo=None)
        await _saved(tranches=_tranches(("T1", "T2", "T3")), released_total=900.0, inspection_deadline=naive)
        assert await svc.check_inspection_windows() == 1

    @pytest.mark.parametrize("kw", [
        {"inspection_deadline": NOW + timedelta(hours=1)},
        {"inspection_deadline": None},
        {"inspection_deadline": PAST, "disputed": True},
        {"inspection_deadline": PAST, "status": "RELEASED"},
    ], ids=["not_yet", "no_window", "disputed", "not_funded"])
    async def test_nothing_is_released_early(self, world, kw):
        await _saved(tranches=_tranches(("T1", "T2", "T3")), released_total=900.0, **kw)
        assert await svc.check_inspection_windows() == 0
        assert (await get_repository().get_hold("ESC-1")).tranches[3].status == "PENDING"

    async def test_already_released_final_tranche_is_skipped(self, world):
        await _saved(tranches=_tranches(("T1", "T2", "T3", "T4")), inspection_deadline=self.PAST)
        assert await svc.check_inspection_windows() == 0

    async def test_provider_failure_leaves_it_for_the_next_sweep(self, world):
        await _saved(tranches=_tranches(("T1", "T2", "T3")), released_total=900.0, inspection_deadline=self.PAST)
        with patch.object(TazapayEscrowConnector, "release", new=AsyncMock(side_effect=_provider_down)):
            assert await svc.check_inspection_windows() == 0
        assert await svc.check_inspection_windows() == 1

    async def test_watcher_survives_a_failed_tick_and_stops_on_cancel(self, world):
        sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])
        check = AsyncMock(side_effect=[RuntimeError("tick failed"), 0])
        reconcile = AsyncMock(return_value=0)
        with patch.object(svc.asyncio, "sleep", new=sleep), \
                patch.object(svc, "check_inspection_windows", new=check), \
                patch.object(svc, "check_reconciliation", new=reconcile):
            await svc.run_inspection_watcher()
        assert check.await_count == 2
        reconcile.assert_awaited_once()


class TestReads:
    async def test_found(self, world):
        await _saved()
        await get_repository().save_payout(_payout())
        assert (await svc.get_hold("ESC-1")).escrow_id == "ESC-1"
        assert (await svc.get_hold_by_deal("D1")).escrow_id == "ESC-1"
        assert (await svc.get_payout("PAY-1")).payout_id == "PAY-1"

    @pytest.mark.parametrize(("call", "error"), [
        (lambda: svc.get_hold("X"), svc.HoldNotFoundError),
        (lambda: svc.get_hold_by_deal("X"), svc.HoldNotFoundError),
        (lambda: svc.get_payout("X"), svc.PayoutNotFoundError),
    ])
    async def test_missing(self, call, error):
        with pytest.raises(error):
            await call()


class TestReconciliation:
    async def test_clean_ledger_is_not_flagged(self, world):
        await _saved(funded_amount=1000.0, tranches=_tranches(("T1",)), released_total=300.0, fees_total=3.0)
        assert await svc.check_reconciliation() == 0

    async def test_fees_above_released_is_a_breach(self, world):
        await _saved(funded_amount=1000.0, tranches=_tranches(("T1",)), released_total=300.0, fees_total=301.0)
        assert any("fees_total" in issue for issue in svc._ledger_issues(await get_repository().get_hold("ESC-1"))[1])

    async def test_negative_fees_is_a_breach(self, world):
        hold = _hold(funded_amount=1000.0, tranches=_tranches(("T1",)), released_total=300.0, fees_total=-1.0)
        assert any("fees_total" in issue for issue in svc._ledger_issues(hold)[1])

    async def test_tranche_sum_mismatch_is_a_breach(self, world):
        hold = _hold(funded_amount=1000.0, tranches=_tranches(("T1",)), released_total=999.0)
        assert any("released tranche sum" in issue for issue in svc._ledger_issues(hold)[1])

    def test_payouts_above_net_received_is_a_breach(self):
        hold = _hold(tranches=_tranches(("T1",)), released_total=300.0, fees_total=3.0)
        assert svc._payout_issues(hold, [_payout(status="COMPLETED", amount=298.0)])
        assert svc._payout_issues(hold, [_payout(status="COMPLETED", amount=297.0)]) == []

    async def test_breach_is_alerted_once_then_resets(self, world):
        repo = get_repository()
        await _saved(funded_amount=1000.0, tranches=_tranches(("T1",)), released_total=300.0, fees_total=301.0)
        assert await svc.check_reconciliation() == 1
        assert await svc.check_reconciliation() == 0
        assert world.topics().count(EventTopic.DEAL_EXCEPTION) == 1
        assert world.events[0][2]["reason"] == "ledger_unreconciled"

        hold = await repo.get_hold("ESC-1")
        await repo.save_hold(hold.model_copy(update={"fees_total": 3.0}))
        assert await svc.check_reconciliation() == 0
        assert (await repo.get_hold("ESC-1")).reconciliation_flagged is False

    async def test_ledger_uses_po_value_when_payouts_do_not_say(self, world):
        await _saved(tranches=_tranches(("T1",)), released_total=300.0, fees_total=3.0, funded_amount=1000.0)
        with patch.object(svc, "_supplier_owed", new=AsyncMock(return_value=500.0)):
            ledger = await svc.get_deal_ledger("D1")
        assert ledger.supplier_owed == 500.0 and ledger.supplier_balance_due == 500.0

    async def test_ledger_without_a_hold_still_shows_payouts(self, world):
        await get_repository().save_payout(_payout(status="COMPLETED", amount=10.0))
        ledger = await svc.get_deal_ledger("D1")
        assert ledger.payouts_total == 10.0 and len(ledger.payouts) == 1


# ---------------------------------------------------------------------------
# Repository contract
# ---------------------------------------------------------------------------

@pytest.fixture(params=["memory", "mongo"])
def backend(request):
    if request.param == "memory":
        return InMemoryEscrowGuardRepository()
    db = AsyncMongoMockClient()["portless_test"]
    return MongoEscrowGuardRepository(db["holds"], db["payouts"])


class TestRepositoryContract:
    async def test_hold_lookups(self, backend):
        await backend.save_hold(_hold())
        assert (await backend.get_hold("ESC-1")).deal_id == "D1"
        assert (await backend.get_hold_by_deal("D1")).escrow_id == "ESC-1"
        assert (await backend.get_hold_by_ext_ref("ref-1")).escrow_id == "ESC-1"
        assert (await backend.get_hold_by_idempotency_key("idem-1")).escrow_id == "ESC-1"
        assert [h.escrow_id for h in await backend.list_holds()] == ["ESC-1"]

    async def test_missing_holds(self, backend):
        assert await backend.get_hold("X") is None
        assert await backend.get_hold_by_deal("X") is None
        assert await backend.get_hold_by_ext_ref("X") is None
        assert await backend.get_hold_by_idempotency_key("X") is None
        assert await backend.list_holds() == []

    async def test_save_replaces_the_same_hold(self, backend):
        await backend.save_hold(_hold())
        await backend.save_hold(_hold(status="RELEASED"))
        assert (await backend.get_hold("ESC-1")).status == "RELEASED"
        assert len(await backend.list_holds()) == 1

    async def test_hold_round_trips_tranches_and_events(self, backend):
        await backend.save_hold(_hold(tranches=_tranches(("T1",)), processed_events=["e1"], disputed=True))
        hold = await backend.get_hold("ESC-1")
        assert hold.tranches[0].status == "RELEASED" and hold.processed_events == ["e1"] and hold.disputed is True

    async def test_payout_lookups(self, backend):
        await backend.save_payout(_payout())
        assert (await backend.get_payout("PAY-1")).deal_id == "D1"
        assert (await backend.get_payout_by_idempotency_key("p-idem-1")).payout_id == "PAY-1"
        assert (await backend.get_payout_by_ext_ref("p-ref-1")).payout_id == "PAY-1"

    async def test_missing_payouts(self, backend):
        assert await backend.get_payout("X") is None
        assert await backend.get_payout_by_idempotency_key("X") is None
        assert await backend.get_payout_by_ext_ref("X") is None
        assert await backend.list_payouts_by_deal("X") == []

    async def test_payouts_are_scoped_to_the_deal(self, backend):
        await backend.save_payout(_payout())
        await backend.save_payout(_payout(payout_id="PAY-2", deal_id="D2", ext_ref="p2", idempotency_key="p-2"))
        assert [p.payout_id for p in await backend.list_payouts_by_deal("D1")] == ["PAY-1"]

    async def test_save_replaces_the_same_payout(self, backend):
        await backend.save_payout(_payout())
        await backend.save_payout(_payout(status="COMPLETED"))
        assert (await backend.get_payout("PAY-1")).status == "COMPLETED"
        assert len(await backend.list_payouts_by_deal("D1")) == 1


@pytest.mark.parametrize("kind", ["memory", "mongo"])
async def test_latest_hold_wins_after_a_failed_escrow_is_reopened(kind):
    if kind == "memory":
        repo = InMemoryEscrowGuardRepository()
    else:
        db = AsyncMongoMockClient()["portless_test"]
        repo = MongoEscrowGuardRepository(db["holds"], db["payouts"])
    await repo.save_hold(_hold(status="FAILED", created_at=NOW - timedelta(days=1)))
    await repo.save_hold(_hold(escrow_id="ESC-2", ext_ref="ref-2", idempotency_key="idem-2", created_at=NOW))
    assert (await repo.get_hold_by_deal("D1")).escrow_id == "ESC-2"


class TestRepositoryLifecycle:
    @staticmethod
    def _motor(ping=None):
        client = MagicMock()
        client.admin.command = AsyncMock(side_effect=ping)
        db = AsyncMongoMockClient()["portless_test"]
        client.__getitem__.return_value = db
        return client

    async def test_no_uri_uses_memory(self, monkeypatch):
        monkeypatch.setattr(repo_mod.settings, "escrow_guard_mongo_uri", "")
        await repo_mod.init_repository()
        assert isinstance(get_repository(), InMemoryEscrowGuardRepository)

    async def test_uri_uses_mongo_and_closes(self, monkeypatch):
        monkeypatch.setattr(repo_mod.settings, "escrow_guard_mongo_uri", "mongodb://user:pw@host/db")
        client = self._motor()
        with patch("motor.motor_asyncio.AsyncIOMotorClient", return_value=client):
            await repo_mod.init_repository()
        assert isinstance(get_repository(), MongoEscrowGuardRepository)
        await repo_mod.close_repository()
        client.close.assert_called_once()
        assert repo_mod._motor_client is None
        await repo_mod.close_repository()
        client.close.assert_called_once()

    async def test_unreachable_mongo_falls_back_to_memory(self, monkeypatch):
        monkeypatch.setattr(repo_mod.settings, "escrow_guard_mongo_uri", "mongodb://host/db")
        with patch("motor.motor_asyncio.AsyncIOMotorClient", return_value=self._motor(ping=RuntimeError("no route"))):
            await repo_mod.init_repository()
        assert isinstance(get_repository(), InMemoryEscrowGuardRepository)

    def test_uninitialised_repository_is_an_error(self):
        repo_mod._repository = None
        with pytest.raises(RuntimeError, match="not initialised"):
            get_repository()
