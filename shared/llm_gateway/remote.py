"""The shared LLM Gateway, reached over HTTP — PortLess's copy of
makemerich-backend's ``app/agents/llm_gateway.py``.

``LLMClient.chat`` sends every call here: ``POST /v1/llm`` on the LLM Gateway, through the API
gateway's ``/api/llm`` route (``LLM_GATEWAY_URL`` + ``LLM_GATEWAY_LLM_PATH``),
using the vendored client in ``remote_sdk.py``. PortLess holds no provider key
and imports no provider SDK: model choice, fallbacks, retries, provider keys
and cost tracking are the LLM Gateway's business.

**Which credential goes out.** makemerich forwards the requesting user's
auth-service token. PortLess cannot: its LLM calls run inside event-bus
continuations, the deal watcher and the exception resolver, none of which has
a request — and PortLess users hold PortLess's own JWT, which the shared API
gateway does not recognise. So PortLess signs in to auth-service as its own
service account (``POST /auth/login`` through the API gateway, the same call
makemerich's SMS forwarder makes) and every call is metered against that
account. ``_ServiceAccountAuth`` caches the bearer, re-signs-in before the
1-hour expiry and once more when the gateway answers 401. A pre-minted
``LLM_GATEWAY_TOKEN`` is sent as-is instead, with no sign-in.

The credential is never logged.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncGenerator, Generator

import httpx

from .config import LLMGatewaySettings, llm_gateway_settings
from .remote_sdk import GatewayError, RemoteLLMClient

logger = logging.getLogger(__name__)

_client: RemoteLLMClient | None = None
_auth: _ServiceAccountAuth | None = None


class GatewayNotConfiguredError(RuntimeError):
    """URL or credential is missing — the one way a gateway call can fail
    before leaving the process."""


class _ServiceAccountAuth(httpx.Auth):
    """Attach PortLess's service-account bearer to each outbound request.

    Chat requests use the async httpx client.
    """

    def __init__(
        self,
        settings: LLMGatewaySettings,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._static_token = settings.gateway_token.strip()
        self._token = ""
        self._issued_at = 0.0
        self._async_lock = asyncio.Lock()
        self._sync_lock = threading.Lock()

    @property
    def _can_sign_in(self) -> bool:
        return bool(self._settings.gateway_email and self._settings.gateway_password)

    def _sign_in_request(self) -> tuple[str, dict[str, str]]:
        s = self._settings
        payload = {"email": s.gateway_email, "password": s.gateway_password}
        if s.gateway_account_id:
            payload["account_id"] = s.gateway_account_id
        return f"{s.gateway_url.rstrip('/')}{s.gateway_auth_path}", payload

    def _accept_sign_in(self, response: httpx.Response) -> str:
        if not response.is_success:
            raise GatewayError(
                response.status_code, "service_sign_in_failed",
                f"auth-service refused the PortLess service account ({self._settings.gateway_email})",
            )
        token = response.json().get("access_token", "")
        if not token:
            raise GatewayError(502, "service_sign_in_failed", "sign-in returned no access_token")
        logger.info("LLM Gateway: signed in as service account %s", self._settings.gateway_email)
        self._token, self._issued_at = token, time.monotonic()
        return token

    def _fresh(self) -> bool:
        age = time.monotonic() - self._issued_at
        return bool(self._token) and age <= self._settings.gateway_token_ttl_seconds

    async def _abearer(self, *, force: bool = False) -> str:
        if self._static_token:
            return self._static_token
        async with self._async_lock:
            if force or not self._fresh():
                url, payload = self._sign_in_request()
                async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as http:
                    self._accept_sign_in(await http.post(url, json=payload))
            return self._token

    def _bearer(self, *, force: bool = False) -> str:
        if self._static_token:
            return self._static_token
        with self._sync_lock:
            if force or not self._fresh():
                url, payload = self._sign_in_request()
                with httpx.Client(timeout=30.0, transport=self._transport) as http:
                    self._accept_sign_in(http.post(url, json=payload))
            return self._token

    def _retry_on_401(self, response: httpx.Response) -> bool:
        # Revoked, or expired earlier than the TTL assumed — sign in once more.
        return response.status_code == 401 and self._can_sign_in and not self._static_token

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = f"Bearer {await self._abearer()}"
        response = yield request
        if self._retry_on_401(response):
            request.headers["Authorization"] = f"Bearer {await self._abearer(force=True)}"
            yield request

    def sync_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._bearer()}"
        response = yield request
        if self._retry_on_401(response):
            request.headers["Authorization"] = f"Bearer {self._bearer(force=True)}"
            yield request


def _base_url(s: LLMGatewaySettings) -> str:
    return f"{s.gateway_url.rstrip('/')}{s.gateway_llm_path}"


def _service_auth(
    s: LLMGatewaySettings, transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None
) -> _ServiceAccountAuth:
    """The process-wide credential for gateway chat calls."""
    global _auth
    if _auth is None:
        if not s.gateway_url.strip():
            raise GatewayNotConfiguredError(
                "LLM_GATEWAY_URL is not set; every LLM call goes through the shared LLM "
                "Gateway and there is nowhere to send this one."
            )
        if not s.gateway_configured:
            raise GatewayNotConfiguredError(
                "LLM_GATEWAY_URL is set but there is no credential: set LLM_GATEWAY_EMAIL + "
                "LLM_GATEWAY_PASSWORD (+ LLM_GATEWAY_ACCOUNT_ID), or LLM_GATEWAY_TOKEN."
            )
        _auth = _ServiceAccountAuth(s, transport=transport)
    return _auth


def get_remote_client(
    settings: LLMGatewaySettings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RemoteLLMClient:
    """The process-wide gateway client, built on first use — one connection pool.

    ``transport`` replaces the network for both the sign-in and the LLM calls (tests).
    """
    global _client
    if _client is None:
        s = settings if settings is not None else llm_gateway_settings
        auth = _service_auth(s, transport)
        http = None
        if transport is not None:
            http = httpx.AsyncClient(
                base_url=_base_url(s), timeout=s.gateway_timeout_seconds,
                auth=auth, transport=transport,
            )
        _client = RemoteLLMClient(
            base_url=_base_url(s), timeout=s.gateway_timeout_seconds, client=http, auth=auth
        )
    return _client


def reset_remote_client() -> None:
    """Drop the singletons so the next call rebuilds them (tests)."""
    global _client, _auth
    _client = None
    _auth = None


async def aclose_remote_client() -> None:
    global _client, _auth
    if _client is not None:
        await _client.aclose()
    _client = None
    _auth = None


__all__ = [
    "GatewayError",
    "GatewayNotConfiguredError",
    "aclose_remote_client",
    "get_remote_client",
    "reset_remote_client",
]
