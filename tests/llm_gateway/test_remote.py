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


# ---------------------------------------------------------------------------
# Contract: every way the gateway can misbehave becomes LLMExhaustedException,
# which the callers turn into a template draft / an escalation.
# ---------------------------------------------------------------------------

def _raising(exc: Exception):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"access_token": "tok"})
        raise exc
    return handler


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.ConnectError("down")])
async def test_timeout_or_network_error_is_exhausted(exc):
    s = _settings()
    remote.get_remote_client(s, transport=httpx.MockTransport(_raising(exc)))
    client = LLMClient(settings=s, tracker=RequestTracker(enabled=False))
    with pytest.raises(LLMExhaustedException):
        await client.chat([{"role": "user", "content": "a"}])


@pytest.mark.parametrize("reply", [
    httpx.Response(200, text="<html>bad gateway</html>"),          # not JSON
    httpx.Response(200, json={"model": "m"}),                       # no content
    _llm_reply("   \n"),                                            # blank
    _llm_reply(""),
])
async def test_malformed_or_blank_reply_is_exhausted(reply):
    gw = _Gateway([reply])
    client = _client(_settings(), gw)
    with pytest.raises(LLMExhaustedException):
        await client.chat([{"role": "user", "content": "a"}])


async def test_a_blank_reply_is_never_cached():
    from shared.llm_gateway.cache import ResponseCache
    gw = _Gateway([_llm_reply(""), _llm_reply("real")])
    s = _settings()
    remote.get_remote_client(s, transport=httpx.MockTransport(gw))
    client = LLMClient(settings=s, tracker=RequestTracker(enabled=False))
    try:
        client._cache = ResponseCache(ttl_seconds=60)
    except TypeError:
        pytest.skip("cache signature differs")
    with pytest.raises(LLMExhaustedException):
        await client.chat([{"role": "user", "content": "same"}], temperature=0.0)
    assert (await client.chat([{"role": "user", "content": "same"}], temperature=0.0)).content == "real"


async def test_refused_sign_in_is_exhausted_and_names_the_account():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad password"})
    s = _settings()
    remote.get_remote_client(s, transport=httpx.MockTransport(handler))
    client = LLMClient(settings=s, tracker=RequestTracker(enabled=False))
    with pytest.raises(LLMExhaustedException, match="svc@portless.test"):
        await client.chat([{"role": "user", "content": "a"}])


async def test_an_expired_token_is_renewed_before_the_call(monkeypatch):
    gw = _Gateway([_llm_reply(), _llm_reply()])
    client = _client(_settings(gateway_token_ttl_seconds=60), gw)
    await client.chat([{"role": "user", "content": "a"}])
    auth = remote._auth
    auth._issued_at -= 61  # the token has aged past its TTL
    await client.chat([{"role": "user", "content": "b"}])
    assert gw.sign_ins == 2
    assert gw.llm_requests()[-1].headers["Authorization"] == "Bearer tok-2"


async def test_a_second_401_is_not_retried_forever():
    gw = _Gateway([httpx.Response(401, json={}), httpx.Response(401, json={})])
    client = _client(_settings(), gw)
    with pytest.raises(LLMExhaustedException, match="401"):
        await client.chat([{"role": "user", "content": "a"}])
    assert len(gw.llm_requests()) == 2


async def test_negotiation_draft_falls_back_to_the_template_when_the_gateway_is_down(monkeypatch):
    from agents.negotiation_agent import graph
    calls = []

    async def down(*_a, **_k):
        calls.append(1)
        raise LLMExhaustedException("LLM Gateway call failed: blank reply")
    monkeypatch.setattr(graph, "_chat", down)
    out = await graph._chat_json("sys", {"x": 1}, "buyer_agent", {"email_body": "template body"})
    assert out["email_body"] == "template body" and out["_degraded"] is True
    assert "blank reply" in out["reasoning"] and len(calls) == 2
