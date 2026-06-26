"""Tests for the LLM ResponseCache and its LLMClient.chat() wiring."""
from __future__ import annotations

import pytest

from shared import cache as sc
from shared.llm_gateway import LLMClient, RequestTracker, ResponseCache
from shared.llm_gateway.client import LLMResponse


@pytest.fixture(autouse=True)
def _fresh_cache():
    sc.reset_for_tests()
    yield
    sc.reset_for_tests()


def _disabled_tracker() -> RequestTracker:
    # No-op tracker so the test never touches MongoDB / the filesystem tracker.
    return RequestTracker(enabled=False, tracking_file="data/test_llm_cache.txt")


# --------------------------------------------------------------------------- #
# make_key
# --------------------------------------------------------------------------- #
def test_make_key_normalises_temperature() -> None:
    c = ResponseCache()
    msgs = [{"role": "user", "content": "hi"}]
    assert c.make_key(msgs, "m", 0) == c.make_key(msgs, "m", 0.0)


def test_make_key_differs_by_model() -> None:
    c = ResponseCache()
    msgs = [{"role": "user", "content": "hi"}]
    assert c.make_key(msgs, "m1", 0.0) != c.make_key(msgs, "m2", 0.0)


# --------------------------------------------------------------------------- #
# aget / aset round-trip
# --------------------------------------------------------------------------- #
async def test_aset_then_aget_reconstructs_response() -> None:
    c = ResponseCache(ttl_seconds=60)
    resp = LLMResponse(
        content="hello", model="m", prompt_tokens=3,
        completion_tokens=5, finish_reason="stop", raw={"id": "x"},
    )
    key = c.make_key([{"role": "user", "content": "hi"}], "m", 0.0)
    await c.aset(key, resp)
    got = await c.aget(key)
    assert got is not None
    assert got.content == "hello"
    assert got.total_tokens == 8


async def test_aget_miss_returns_none() -> None:
    assert await ResponseCache().aget("missing") is None


# --------------------------------------------------------------------------- #
# End-to-end: chat() serves the second identical call from cache
# --------------------------------------------------------------------------- #
async def test_chat_second_identical_call_is_cached(monkeypatch) -> None:
    dispatches: list[str] = []

    async def fake_dispatch(self, endpoint=None, messages=None, temperature=None,
                            max_tokens=None, **kwargs) -> LLMResponse:
        dispatches.append(endpoint.model)
        return LLMResponse(
            content="cached-me", model=endpoint.model, prompt_tokens=1,
            completion_tokens=1, finish_reason="stop", raw={},
        )

    monkeypatch.setattr(LLMClient, "_dispatch", fake_dispatch)

    client = LLMClient(cache=ResponseCache(ttl_seconds=60), tracker=_disabled_tracker())
    msgs = [{"role": "user", "content": "hello"}]

    r1 = await client.chat(msgs, caller="test", temperature=0.0)
    r2 = await client.chat(msgs, caller="test", temperature=0.0)

    assert r1.content == r2.content == "cached-me"
    assert len(dispatches) == 1, "second identical chat should be served from cache"


async def test_chat_without_cache_always_dispatches(monkeypatch) -> None:
    dispatches: list[str] = []

    async def fake_dispatch(self, endpoint=None, messages=None, temperature=None,
                            max_tokens=None, **kwargs) -> LLMResponse:
        dispatches.append(endpoint.model)
        return LLMResponse(
            content="x", model=endpoint.model, prompt_tokens=1,
            completion_tokens=1, finish_reason="stop", raw={},
        )

    monkeypatch.setattr(LLMClient, "_dispatch", fake_dispatch)

    client = LLMClient(cache=None, tracker=_disabled_tracker())  # caching off
    msgs = [{"role": "user", "content": "hello"}]
    await client.chat(msgs, caller="test", temperature=0.0)
    await client.chat(msgs, caller="test", temperature=0.0)
    assert len(dispatches) == 2
