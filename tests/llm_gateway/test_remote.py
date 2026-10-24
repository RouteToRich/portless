"""LLMClient → shared LLM Gateway (shared/llm_gateway/remote.py).

Every request goes to an httpx.MockTransport — no network, no gateway budget.
"""
from __future__ import annotations

import json

import httpx
import pytest

from shared.llm_gateway import remote
from shared.llm_gateway.client import LLMClient, LLMConfigurationError, LLMExhaustedException
from shared.llm_gateway.config import LLMGatewaySettings
from shared.llm_gateway.tracker import RequestTracker

GATEWAY = "https://gateway.test"


def _settings(**overrides) -> LLMGatewaySettings:
    defaults = {
        "gateway_url": GATEWAY,
        "gateway_email": "svc@portless.test",
        "gateway_password": "pw",
        "gateway_account_id": "acct-1",
        "gateway_token": "",
        "tracking_enabled": False,
        "mongo_tracker_enabled": False,
        "cache_enabled": False,
    }
    defaults.update(overrides)
    return LLMGatewaySettings(**defaults)


def _llm_reply(content: str = "hello", model: str = "groq/qwen") -> httpx.Response:
    return httpx.Response(200, json={
        "content": content, "model": model, "finish_reason": "stop", "category": "reasoning",
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    })


class _Gateway:
    """Records requests; answers sign-in with a fresh token each time."""

    def __init__(self, llm_responses: list[httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self.sign_ins = 0
        self._llm = list(llm_responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/auth/login":
            self.sign_ins += 1
            return httpx.Response(200, json={"access_token": f"tok-{self.sign_ins}"})
        assert request.url.path == "/api/llm/v1/llm"
        return self._llm.pop(0)

    def llm_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/v1/llm")]


@pytest.fixture(autouse=True)
def _fresh_singleton():
    remote.reset_remote_client()
    yield
    remote.reset_remote_client()


def _client(settings: LLMGatewaySettings, gateway: _Gateway) -> LLMClient:
    remote.get_remote_client(settings, transport=httpx.MockTransport(gateway))
    return LLMClient(settings=settings, tracker=RequestTracker(enabled=False))


async def test_chat_signs_in_and_posts_to_gateway():
    gw = _Gateway([_llm_reply("draft")])
    client = _client(_settings(), gw)

    resp = await client.chat(
        [{"role": "user", "content": "hi"}], caller="negotiation_agent",
        temperature=0.2, max_tokens=50, category="reasoning",
    )

    assert resp.content == "draft"
    assert resp.model == "groq/qwen"
    assert resp.total_tokens == 10
    login = gw.requests[0]
    assert json.loads(login.content) == {
        "email": "svc@portless.test", "password": "pw", "account_id": "acct-1",
    }
    (call,) = gw.llm_requests()
    assert call.headers["Authorization"] == "Bearer tok-1"
    body = json.loads(call.content)
    assert body["caller"] == "portless.negotiation_agent"
    assert body["category"] == "reasoning"
    assert body["temperature"] == 0.2 and body["max_tokens"] == 50


async def test_token_is_reused_across_calls():
    gw = _Gateway([_llm_reply(), _llm_reply()])
    client = _client(_settings(), gw)
    await client.chat([{"role": "user", "content": "a"}])
    await client.chat([{"role": "user", "content": "b"}])
    assert gw.sign_ins == 1


async def test_401_signs_in_again_and_retries_once():
    gw = _Gateway([httpx.Response(401, json={"detail": "expired"}), _llm_reply("ok")])
    client = _client(_settings(), gw)
    resp = await client.chat([{"role": "user", "content": "a"}])
    assert resp.content == "ok"
    assert gw.sign_ins == 2
    assert gw.llm_requests()[-1].headers["Authorization"] == "Bearer tok-2"


async def test_static_token_skips_sign_in():
    gw = _Gateway([_llm_reply()])
    client = _client(_settings(gateway_email="", gateway_password="", gateway_token="minted"), gw)
    await client.chat([{"role": "user", "content": "a"}])
    assert gw.sign_ins == 0
    assert gw.llm_requests()[0].headers["Authorization"] == "Bearer minted"


async def test_gateway_failure_raises_exhausted():
    gw = _Gateway([httpx.Response(503, json={"error": {"code": "all_models_failed", "message": "x"}})])
    client = _client(_settings(), gw)
    with pytest.raises(LLMExhaustedException, match="503"):
        await client.chat([{"role": "user", "content": "a"}])


async def test_missing_credential_is_a_configuration_error():
    client = LLMClient(
        settings=_settings(gateway_email="", gateway_password=""),
        tracker=RequestTracker(enabled=False),
    )
    with pytest.raises(LLMConfigurationError, match="credential"):
        await client.chat([{"role": "user", "content": "a"}])
