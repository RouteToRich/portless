"""Tests for shared.auth.middleware.AuthMiddleware's access model.

Portless platform staff reach every endpoint; everyone else is restricted to
a small allowlist (GET /deals, GET /auth/me) and 403s on everything else,
regardless of their organization.

Isolation: this test drives the *combined* app (``app.py``) through
``TestClient``, which is the only test module in this suite that does so —
every other test calls service-layer functions directly against an
in-memory repository (see test_deals.py's ``_repo`` fixture pattern). To
avoid ever touching the real shared MongoDB cluster (whose connection string
lands in ``os.environ`` via ``shared/config.py``'s ``load_dotenv()`` the
moment *anything* in the backend is imported — env-var blanking here would
be a no-op if another test module already triggered that import first), we
monkeypatch the auth/deals repository getters directly to fresh in-memory
instances, monkeypatch the Portless allowlist onto the already-constructed
settings singleton, and swap the shared app-wide cache (used by the logout
token blacklist — shared/auth/blacklist.py) to its documented in-memory test
backend. All three bypass Mongo and env-var timing entirely, regardless of
test collection order.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import app
from services.auth.config import settings as auth_settings
from services.auth.repository import InMemoryOrganizationRepository, InMemoryUserRepository
from services.deals.repository import InMemoryFallbackRepository
from shared.cache import InMemoryCacheBackend, reset_for_tests


@pytest.fixture(scope="module")
def client():
    """One full app lifespan for the whole module (starting it per-test is
    slow), with the auth/deals repository getters, the Portless allowlist,
    and the shared cache patched by hand (not pytest's ``monkeypatch``, which
    is function-scoped) so nothing here ever reaches the real shared Mongo
    cluster."""
    import services.auth.service as auth_service
    import services.deals.service as deals_service

    orig_get_user_repo = auth_service.get_repository
    orig_get_org_repo = auth_service.get_org_repository
    orig_get_deals_repo = deals_service.get_repository
    orig_portless_raw = auth_settings.portless_emails_raw

    user_repo = InMemoryUserRepository()
    org_repo = InMemoryOrganizationRepository()
    deals_repo = InMemoryFallbackRepository()

    auth_service.get_repository = lambda: user_repo
    auth_service.get_org_repository = lambda: org_repo
    deals_service.get_repository = lambda: deals_repo
    auth_settings.portless_emails_raw = "admin@portless.io"
    reset_for_tests(InMemoryCacheBackend())

    try:
        with TestClient(app) as c:
            yield c
    finally:
        auth_service.get_repository = orig_get_user_repo
        auth_service.get_org_repository = orig_get_org_repo
        deals_service.get_repository = orig_get_deals_repo
        auth_settings.portless_emails_raw = orig_portless_raw
        # Leave a harmless fresh in-memory cache behind rather than resetting
        # to None, which would make the next caller anywhere in the suite
        # lazily reconnect to the real Mongo cache collection.
        reset_for_tests(InMemoryCacheBackend())


def _register(client, email: str, name: str) -> tuple[str, dict]:
    r = client.post("/auth/register", json={"email": email, "name": name, "password": "hunter22"})
    assert r.status_code == 201, r.text
    body = r.json()
    return body["access_token"], body["user"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def portless_token(client) -> str:
    token, user = _register(client, "admin@portless.io", "Portless Admin")
    assert user["is_portless"] is True
    return token


@pytest.fixture(scope="module")
def org_user_token(client) -> str:
    token, user = _register(client, "org-user@example.com", "Org User")
    assert user["is_portless"] is False
    return token


def test_no_token_is_401_regardless(client):
    assert client.get("/deals").status_code == 401
    assert client.get("/hitl-approval/approvals").status_code == 401


def test_portless_reaches_deals_list(client, portless_token):
    r = client.get("/deals", headers=_auth(portless_token))
    assert r.status_code == 200


def test_portless_reaches_non_deals_endpoint(client, portless_token):
    """Portless staff aren't limited to the non-Portless allowlist."""
    r = client.get("/hitl-approval/approvals", headers=_auth(portless_token))
    assert r.status_code == 200


def test_portless_reaches_org_admin_endpoint(client, portless_token):
    r = client.get("/auth/organizations", headers=_auth(portless_token))
    assert r.status_code == 200


def test_org_user_reaches_deals_list_once_assigned_to_an_org(client, portless_token, org_user_token):
    # An unassigned org user still reaches the /deals route (middleware allows
    # it) but the router's own require_org_scope 403s until a Portless admin
    # assigns them an organization — assign one, then re-login for a fresh
    # token carrying the new org_id (claims are stamped at login time).
    r = client.get("/deals", headers=_auth(org_user_token))
    assert r.status_code == 403  # not yet assigned to an org

    r = client.post("/auth/organizations", json={"name": "Middleware Test Org"}, headers=_auth(portless_token))
    assert r.status_code == 201, r.text
    org_id = r.json()["org_id"]

    me = client.get("/auth/me", headers=_auth(org_user_token)).json()
    r = client.patch(
        f"/auth/users/{me['user_id']}/organization",
        json={"org_id": org_id},
        headers=_auth(portless_token),
    )
    assert r.status_code == 200, r.text

    r = client.post("/auth/login", json={"email": "org-user@example.com", "password": "hunter22"})
    assert r.status_code == 200
    refreshed_token = r.json()["access_token"]

    r = client.get("/deals", headers=_auth(refreshed_token))
    assert r.status_code == 200


def test_org_user_reaches_auth_me(client, org_user_token):
    r = client.get("/auth/me", headers=_auth(org_user_token))
    assert r.status_code == 200


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/deals/DEAL-GHOST"),
        ("POST", "/deals"),
        ("PATCH", "/deals/DEAL-GHOST/status"),
        ("GET", "/deals/DEAL-GHOST/match-scores"),
        ("GET", "/hitl-approval/approvals"),
        ("GET", "/exception-resolver/exceptions/EXC-GHOST"),
        ("GET", "/rag/stats"),
        ("GET", "/auth/organizations"),
        ("GET", "/auth/users"),
    ],
)
def test_org_user_blocked_everywhere_else(client, org_user_token, method, path):
    r = client.request(method, path, headers=_auth(org_user_token), json={} if method in ("POST", "PATCH") else None)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code} (expected 403)"


# ---------------------------------------------------------------------------
# Logout / token revocation
# ---------------------------------------------------------------------------

def test_logout_revokes_the_token_used(client):
    token, _ = _register(client, "logout-a@example.com", "Logout A")

    r = client.get("/auth/me", headers=_auth(token))
    assert r.status_code == 200

    r = client.post("/auth/logout", headers=_auth(token))
    assert r.status_code == 204

    r = client.get("/auth/me", headers=_auth(token))
    assert r.status_code == 401
    assert "revoked" in r.json()["detail"].lower()


def test_logout_only_revokes_that_specific_token(client):
    """Logging out one session token must not invalidate a second, separately
    issued token for the same user (e.g. logged in on two devices)."""
    r = client.post("/auth/register", json={
        "email": "logout-b@example.com", "name": "Logout B", "password": "hunter22",
    })
    assert r.status_code == 201
    first_token = r.json()["access_token"]

    r = client.post("/auth/login", json={"email": "logout-b@example.com", "password": "hunter22"})
    assert r.status_code == 200
    second_token = r.json()["access_token"]
    assert second_token != first_token  # distinct jti per issuance

    r = client.post("/auth/logout", headers=_auth(first_token))
    assert r.status_code == 204

    assert client.get("/auth/me", headers=_auth(first_token)).status_code == 401
    assert client.get("/auth/me", headers=_auth(second_token)).status_code == 200


def test_org_user_can_log_out_too(client, org_user_token):
    """Logout isn't a Portless-only feature — it's in the non-Portless allowlist."""
    r = client.post("/auth/logout", headers=_auth(org_user_token))
    assert r.status_code == 204


def test_logout_requires_a_token(client):
    assert client.post("/auth/logout").status_code == 401
