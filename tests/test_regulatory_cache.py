"""Tests that read-only regulatory verifications are cached (no duplicate calls)."""
from __future__ import annotations

import pytest

from shared import cache as sc
from shared.connectors import regulatory


@pytest.fixture(autouse=True)
def _fresh_cache():
    sc.reset_for_tests()
    yield
    sc.reset_for_tests()


async def test_cached_lookup_fetches_once() -> None:
    calls: list[int] = []

    async def _fetch() -> dict:
        calls.append(1)
        return {"status": "ACTIVE"}

    a = await regulatory._cached_lookup("regulatory:test", ("test", "X"), _fetch)
    b = await regulatory._cached_lookup("regulatory:test", ("test", "X"), _fetch)
    assert a == b == {"status": "ACTIVE"}
    assert len(calls) == 1, "second lookup should be served from cache"


async def test_cached_lookup_keys_on_inputs() -> None:
    calls: list[str] = []

    async def make_fetch(val: str):
        async def _fetch() -> dict:
            calls.append(val)
            return {"v": val}
        return _fetch

    await regulatory._cached_lookup("regulatory:test", ("test", "A"), await make_fetch("A"))
    await regulatory._cached_lookup("regulatory:test", ("test", "B"), await make_fetch("B"))
    assert calls == ["A", "B"]  # distinct keys → both fetched


async def test_vies_quote_caches_network_call(monkeypatch) -> None:
    """VIES needs no API key — verify the quote() wiring caches across calls."""
    hits: list[str] = []

    class _FakeResp:
        def raise_for_status(self) -> None: ...
        def json(self) -> dict:
            return {"isValid": True, "traderName": "ACME"}

    class _FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, *a, **k):
            hits.append(url)
            return _FakeResp()

    monkeypatch.setattr(regulatory.httpx, "AsyncClient", _FakeClient)

    conn = regulatory.ViesConnector()
    req = {"country_code": "DE", "vat_number": "123456789"}
    r1 = await conn.quote(req)
    r2 = await conn.quote(req)
    assert r1 == r2
    assert r1["valid"] is True
    assert len(hits) == 1, "second VIES check should be cached"
