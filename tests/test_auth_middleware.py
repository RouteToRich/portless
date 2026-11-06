"""Tests for PortLess authentication (shared/auth) and its access model.

Sign-in is auth-service's, through the shared RouteToRich API gateway; PortLess
only checks a caller's token by asking the gateway's ``GET /auth/me``
(shared/auth/identity.py). Here the gateway is a fake: ``identity.ask_gateway``
is swapped for a dict lookup, token -> the ``/auth/me`` body auth-service
would answer, so nothing reaches the network.

Access model: Portless platform staff (AUTH_PORTLESS_EMAILS) reach every
endpoint; everyone else reaches only GET /deals and GET /identity/me and 403s
on everything else, regardless of their organization.

Isolation: this drives the *combined* app (``app.py``) through ``TestClient``.
To never touch the real shared MongoDB cluster we patch the identity/deals
repository getters to fresh in-memory instances and patch the auth settings
singleton by hand (module scope — pytest's ``monkeypatch`` is function-scoped).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import app
from services.auth.config import settings as auth_settings
from services.auth.repository import InMemoryOrganizationRepository, InMemoryUserRepository
from services.auth.schemas import UserRecord
from services.deals.repository import InMemoryFallbackRepository
from shared.auth import identity
from shared.cache import InMemoryCacheBackend, reset_for_tests

PORTLESS_ACCOUNT = "acc-portless"
OTHER_ACCOUNT = "acc-makemerich"


def _me(user_id: str, email: str, name: str, account_id: str = PORTLESS_ACCOUNT) -> dict:
    """auth-service's GET /auth/me body."""
    return {
        "user_id": user_id,
        "email": email,
        "name": name,
        "accounts": [
            {"account_id": "acc-unrelated", "selected": False},
            {"account_id": account_id, "selected": True},
        ],
    }


# token -> /auth/me body (None = auth-service says the token is invalid)
TOKENS: dict[str, dict | None] = {
    "tok-staff": _me("USR-STAFF", "admin@portless.io", "Portless Admin"),
    "tok-org": _me("USR-ORG", "org-user@example.com", "Org User"),
    "tok-other-app": _me("USR-MMR", "someone@example.com", "MakeMeRich User", OTHER_ACCOUNT),
    "tok-legacy": _me("USR-NEWID", "legacy@example.com", "Legacy Person"),
    "tok-revoked": None,
}
CALLS: list[str] = []


async def _fake_gateway(token: str) -> dict | None:
    CALLS.append(token)
    return TOKENS.get(token)


@pytest.fixture(scope="module")
def repos():
    return {"users": InMemoryUserRepository(), "orgs": InMemoryOrganizationRepository()}


@pytest.fixture(scope="module")
def client(repos):
    import services.auth.service as auth_service
    import services.deals.service as deals_service

    saved = {
        "get_repository": auth_service.get_repository,
        "get_org_repository": auth_service.get_org_repository,
        "deals_repo": deals_service.get_repository,
        "ask_gateway": identity.ask_gateway,
        "portless": auth_settings.portless_emails_raw,
        "gateway_url": auth_settings.gateway_url,
        "account_id": auth_settings.account_id,
        "trust": auth_settings.trust_gateway_headers,
    }
    deals_repo = InMemoryFallbackRepository()
    auth_service.get_repository = lambda: repos["users"]
    auth_service.get_org_repository = lambda: repos["orgs"]
    deals_service.get_repository = lambda: deals_repo
    identity.ask_gateway = _fake_gateway
    auth_settings.portless_emails_raw = "admin@portless.io"
    auth_settings.gateway_url = "https://gateway.test"
    auth_settings.account_id = PORTLESS_ACCOUNT
    auth_settings.trust_gateway_headers = False
    identity.clear_cache()
    reset_for_tests(InMemoryCacheBackend())

    try:
        with TestClient(app) as c:
            yield c
    finally:
        auth_service.get_repository = saved["get_repository"]
        auth_service.get_org_repository = saved["get_org_repository"]
        deals_service.get_repository = saved["deals_repo"]
        identity.ask_gateway = saved["ask_gateway"]
        auth_settings.portless_emails_raw = saved["portless"]
        auth_settings.gateway_url = saved["gateway_url"]
        auth_settings.account_id = saved["account_id"]
        auth_settings.trust_gateway_headers = saved["trust"]
        identity.clear_cache()
        reset_for_tests(InMemoryCacheBackend())


@pytest.fixture(autouse=True)
def _fresh_cache():
    identity.clear_cache()
    CALLS.clear()
    yield
    auth_settings.trust_gateway_headers = False


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Who gets in
# ---------------------------------------------------------------------------

def test_no_token_is_401_regardless(client):
    assert client.get("/deals").status_code == 401
    assert client.get("/hitl-approval/approvals").status_code == 401
    assert client.get("/identity/me").status_code == 401


def test_invalid_or_revoked_token_is_401(client):
    assert client.get("/deals", headers=_auth("tok-revoked")).status_code == 401
    assert client.get("/deals", headers=_auth("tok-unknown")).status_code == 401


def test_token_of_another_application_is_403(client):
    r = client.get("/identity/me", headers=_auth("tok-other-app"))
    assert r.status_code == 403
    assert "another application" in r.json()["detail"]


def test_portless_no_longer_signs_anyone_in(client):
    """Sign-in moved to auth-service: there is no local /auth surface left."""
    assert client.post("/auth/login", json={"email": "a@b.c", "password": "x"}).status_code == 401
    r = client.post("/auth/login", json={}, headers=_auth("tok-staff"))
    assert r.status_code in (404, 405)


def test_health_and_docs_stay_public(client):
    assert client.get("/health").status_code == 200
    assert client.get("/identity/health").status_code == 200


def test_first_request_creates_the_local_record(client, repos):
    r = client.get("/identity/me", headers=_auth("tok-staff"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_id"] == "USR-STAFF" and body["email"] == "admin@portless.io"
    assert body["is_portless"] is True and body["org_id"] == "portless" and body["legacy"] is False


def test_token_checks_are_cached(client):
    for _ in range(3):
        assert client.get("/identity/me", headers=_auth("tok-staff")).status_code == 200
    assert CALLS == ["tok-staff"]


# ---------------------------------------------------------------------------
# Access model
# ---------------------------------------------------------------------------

def test_portless_reaches_everything(client):
    assert client.get("/deals", headers=_auth("tok-staff")).status_code == 200
    assert client.get("/hitl-approval/approvals", headers=_auth("tok-staff")).status_code == 200
    assert client.get("/identity/organizations", headers=_auth("tok-staff")).status_code == 200


def test_org_user_sees_deals_once_assigned_without_signing_in_again(client):
    # Unassigned: the middleware lets /deals through, the router's
    # require_org_scope 403s until a Portless admin assigns an organization.
    assert client.get("/deals", headers=_auth("tok-org")).status_code == 403

    r = client.post("/identity/organizations", json={"name": "Middleware Test Org"}, headers=_auth("tok-staff"))
    assert r.status_code == 201, r.text
    org_id = r.json()["org_id"]

    me = client.get("/identity/me", headers=_auth("tok-org")).json()
    assert me["org_id"] is None and me["is_portless"] is False
    r = client.patch(
        f"/identity/users/{me['user_id']}/organization",
        json={"org_id": org_id},
        headers=_auth("tok-staff"),
    )
    assert r.status_code == 200, r.text

    # The organization is read from PortLess's record on every request, not
    # stamped into the token — the same token now sees the org's deals.
    assert client.get("/deals", headers=_auth("tok-org")).status_code == 200
    assert client.get("/identity/me", headers=_auth("tok-org")).json()["org_id"] == org_id


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/deals/DEAL-GHOST"),
        ("POST", "/deals"),
        ("PATCH", "/deals/DEAL-GHOST/status"),
        ("GET", "/deals/DEAL-GHOST/match-scores"),
        ("GET", "/hitl-approval/approvals"),
        ("GET", "/exception-resolver/exceptions/EXC-GHOST"),
        ("GET", "/trade-min/sources"),
        ("GET", "/identity/organizations"),
        ("GET", "/identity/users"),
    ],
)
def test_org_user_blocked_everywhere_else(client, method, path):
    r = client.request(method, path, headers=_auth("tok-org"), json={} if method in ("POST", "PATCH") else None)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code} (expected 403)"


def test_query_token_only_counts_on_the_sse_path(client):
    assert client.get("/hitl-approval/approvals?token=tok-staff").status_code == 401
    assert client.get("/hitl-approval/stream?token=tok-revoked").status_code == 401


# ---------------------------------------------------------------------------
# Gateway identity headers
# ---------------------------------------------------------------------------

_FORGED = {
    "X-User-ID": "USR-FORGED",
    "X-User-Email": "admin@portless.io",
    "X-User-Name": "Forged",
    "X-Account-ID": PORTLESS_ACCOUNT,
}


def test_identity_headers_are_ignored_by_default(client):
    """A caller that reaches the service directly cannot claim to be staff."""
    assert client.get("/hitl-approval/approvals", headers=_FORGED).status_code == 401


def test_identity_headers_are_believed_when_trust_is_on(client):
    auth_settings.trust_gateway_headers = True
    headers = {**_FORGED, "X-User-ID": "USR-VIA-GATEWAY", "X-User-Email": "admin@portless.io"}
    assert client.get("/hitl-approval/approvals", headers=headers).status_code == 200
    assert CALLS == []  # no call back to the gateway

    other_app = {**headers, "X-Account-ID": OTHER_ACCOUNT}
    assert client.get("/hitl-approval/approvals", headers=other_app).status_code == 403


# ---------------------------------------------------------------------------
# Legacy (pre-auth-service) records
# ---------------------------------------------------------------------------

def test_legacy_record_is_not_adopted_by_email(client, repos):
    # Seed the in-memory store directly: asyncio.run() here would clear the
    # current event loop for every test that runs after this module.
    legacy = UserRecord(
        user_id="USR-OLDLOCAL",
        email="legacy@example.com",
        name="Legacy Person",
        password_hash="pbkdf2_sha256$1$salt$hash",
        org_id="ORG-SECRET",
        created_at=datetime.now(timezone.utc),
    )
    repos["users"]._by_id[legacy.user_id] = legacy
    me = client.get("/identity/me", headers=_auth("tok-legacy")).json()
    assert me["user_id"] == "USR-NEWID" and me["org_id"] is None and me["legacy"] is False

    users = {u["user_id"]: u for u in client.get("/identity/users", headers=_auth("tok-staff")).json()}
    assert users["USR-OLDLOCAL"]["legacy"] is True and users["USR-OLDLOCAL"]["org_id"] == "ORG-SECRET"


# ---------------------------------------------------------------------------
# identity.py units
# ---------------------------------------------------------------------------

def test_selected_account_is_read_from_auth_me():
    ident = identity._identity_from_me(_me("USR-1", "A@B.COM", "A"))
    assert ident.user_id == "USR-1" and ident.email == "a@b.com" and ident.account_id == PORTLESS_ACCOUNT
    assert identity._identity_from_me({"email": "x@y.z"}) is None


@pytest.mark.asyncio
async def test_gateway_unconfigured_or_down_is_503(monkeypatch):
    import httpx

    monkeypatch.setattr(auth_settings, "gateway_url", "")
    with pytest.raises(identity.IdentityError) as exc:
        await identity._ask_gateway("t")
    assert exc.value.status == 503

    monkeypatch.setattr(auth_settings, "gateway_url", "https://gateway.test")

    class _Down:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): raise httpx.ConnectError("down")

    monkeypatch.setattr(identity.httpx, "AsyncClient", _Down)
    with pytest.raises(identity.IdentityError) as exc:
        await identity._ask_gateway("t")
    assert exc.value.status == 503


@pytest.mark.asyncio
async def test_gateway_401_means_invalid_token(monkeypatch):
    import httpx

    monkeypatch.setattr(auth_settings, "gateway_url", "https://gateway.test")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(401, json={"error": {"code": "invalid_token"}})

    real = httpx.AsyncClient
    monkeypatch.setattr(identity.httpx, "AsyncClient",
                        lambda **k: real(transport=httpx.MockTransport(handler), **k))
    assert await identity._ask_gateway("tok-x") is None
    assert seen == {"auth": "Bearer tok-x", "url": "https://gateway.test/auth/me"}


@pytest.mark.asyncio
async def test_staff_first_seen_before_the_allowlist_is_moved_to_the_portless_org():
    """A staff address added to AUTH_PORTLESS_EMAILS after its first sign-in has a
    record without an organization; creating a deal then had no org (500)."""
    import services.auth.service as auth_service
    from services.auth.organization import PORTLESS_ORG_ID
    from shared.auth.identity import Identity

    users = InMemoryUserRepository()
    await users.create(UserRecord(user_id="USR-LATE", email="late@portless.io", name="Late Staff",
                                  account_id=PORTLESS_ACCOUNT, org_id=None,
                                  created_at=datetime.now(timezone.utc)))
    saved_repo, saved_emails = auth_service.get_repository, auth_settings.portless_emails_raw
    auth_service.get_repository = lambda: users
    auth_settings.portless_emails_raw = "late@portless.io"
    try:
        rec = await auth_service.user_for_identity(
            Identity(user_id="USR-LATE", email="late@portless.io", name="Late Staff", account_id=PORTLESS_ACCOUNT))
        assert rec.org_id == PORTLESS_ORG_ID
        assert (await users.get_by_id("USR-LATE")).org_id == PORTLESS_ORG_ID
    finally:
        auth_service.get_repository, auth_settings.portless_emails_raw = saved_repo, saved_emails
