"""Unsubscribe link in outreach e-mails (shared/orchestration/unsubscribe.py): signed, no
sign-in, GET only confirms, POST records do-not-contact and moves the funnel on."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

from shared.orchestration import outreach, unsubscribe

NOW = datetime.now(UTC)


@pytest.fixture
def linked(monkeypatch):
    monkeypatch.setenv("PORTLESS_PUBLIC_BASE_URL", "https://api.portless.example")
    monkeypatch.setenv("UNSUBSCRIBE_LINK_SECRET", "s3cret-for-tests")


def _qs(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_link_is_signed_and_verifies(linked):
    url = unsubscribe.link("DEAL-1", "gulf.ae")
    assert url.startswith("https://api.portless.example/trade-min/unsubscribe?")
    q = _qs(url)
    assert q["d"] == "DEAL-1" and q["c"] == "gulf.ae"
    assert unsubscribe.verify("DEAL-1", "gulf.ae", q["sig"])
    assert not unsubscribe.verify("DEAL-1", "other.ae", q["sig"])
    assert not unsubscribe.verify("DEAL-2", "gulf.ae", q["sig"])
    assert not unsubscribe.verify("DEAL-1", "gulf.ae", "0" * 64)


def test_falls_back_to_the_proof_link_secret(monkeypatch):
    monkeypatch.setenv("PORTLESS_PUBLIC_BASE_URL", "https://api.portless.example")
    monkeypatch.delenv("UNSUBSCRIBE_LINK_SECRET", raising=False)
    monkeypatch.setenv("ESCROW_PROOF_LINK_SECRET", "proof-secret")
    assert unsubscribe.link("D", "c") is not None


def test_no_link_without_base_or_secret(monkeypatch):
    monkeypatch.delenv("PORTLESS_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("UNSUBSCRIBE_LINK_SECRET", "x")
    assert unsubscribe.link("D", "c") is None
    assert "simply reply \"not interested\"" in unsubscribe.opt_out_line("D", "c")
    monkeypatch.setenv("PORTLESS_PUBLIC_BASE_URL", "https://api.portless.example")
    monkeypatch.delenv("UNSUBSCRIBE_LINK_SECRET")
    monkeypatch.delenv("ESCROW_PROOF_LINK_SECRET", raising=False)
    assert unsubscribe.link("D", "c") is None and not unsubscribe.verify("D", "c", "anything")


def _deal_and_cand(status="contacted"):
    cand = SimpleNamespace(candidate_id="gulf.ae", legal_name="Gulf Foods", email="buy@gulf.ae",
                           website="https://gulf.ae", status=status, pitch_angle=None, quantity_mt=None,
                           contact_name=None)
    deal = SimpleNamespace(deal_id="DEAL-1", hs_code="090422", quantity_mt=None, buyer_candidates=[cand], market="AE")
    return deal, cand


@pytest.mark.parametrize("followup", [False, True])
def test_every_outreach_email_carries_the_link(linked, followup):
    deal, cand = _deal_and_cand()
    _, body = outreach.draft(deal, cand, followup=followup)
    assert "unsubscribe here: https://api.portless.example/trade-min/unsubscribe?" in body
    assert "reply \"not interested\"" in body


def test_without_a_link_the_reply_opt_out_stays(monkeypatch):
    monkeypatch.delenv("PORTLESS_PUBLIC_BASE_URL", raising=False)
    deal, cand = _deal_and_cand()
    _, body = outreach.draft(deal, cand)
    assert "unsubscribe" not in body.lower() and "simply reply \"not interested\"" in body


@pytest.fixture
def wired(monkeypatch, linked):
    deal, cand = _deal_and_cand()
    state = SimpleNamespace(deal=deal, cand=cand)
    monkeypatch.setattr("services.deals.service.get_deal", AsyncMock(return_value=deal))
    state.update = AsyncMock()
    monkeypatch.setattr("services.deals.service.update_buyer_candidate", state.update)
    state.dnc = AsyncMock()
    monkeypatch.setattr(outreach, "_mark_do_not_contact", state.dnc)
    state.advance = AsyncMock(return_value={"contacted": ["next.ae"]})
    monkeypatch.setattr(outreach, "advance", state.advance)
    return state


async def test_apply_declines_a_courted_buyer_and_moves_on(wired):
    result = await unsubscribe.apply("DEAL-1", "gulf.ae")
    assert result["declined"] is True and result["next"] == {"contacted": ["next.ae"]}
    wired.dnc.assert_awaited_once()
    updates = wired.update.await_args.args[2]
    assert updates["status"] == "declined" and "unsubscribed" in updates["reply_preview"]


async def test_apply_keeps_a_buyer_already_in_the_deal(wired):
    wired.cand.status = "selected"
    result = await unsubscribe.apply("DEAL-1", "gulf.ae")
    assert result["declined"] is False and result["do_not_contact"] is True
    wired.dnc.assert_awaited_once()
    wired.update.assert_not_awaited()
    wired.advance.assert_not_awaited()


async def test_apply_unknown_candidate_raises(wired):
    with pytest.raises(LookupError):
        await unsubscribe.apply("DEAL-1", "nobody.ae")


class TestRoutes:
    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from services.trade_min.router import router
        from shared.auth.middleware import AuthMiddleware
        app = FastAPI()
        app.add_middleware(AuthMiddleware)
        app.include_router(router)
        return TestClient(app)

    async def test_get_confirms_and_post_records_without_sign_in(self, wired):
        url = unsubscribe.link("DEAL-1", "gulf.ae")
        path = url.replace("https://api.portless.example", "")
        c = self._client()
        page = c.get(path)
        assert page.status_code == 200 and "<form method='post'" in page.text
        wired.dnc.assert_not_awaited()  # a mail scanner opening the link changes nothing
        done = c.post(path)
        assert done.status_code == 200 and "unsubscribed" in done.text
        wired.dnc.assert_awaited_once()

    async def test_a_bad_signature_is_refused(self, wired):
        c = self._client()
        assert c.get("/trade-min/unsubscribe?d=DEAL-1&c=gulf.ae&sig=bad").status_code == 403
        assert c.post("/trade-min/unsubscribe?d=DEAL-1&c=gulf.ae&sig=bad").status_code == 403
        wired.dnc.assert_not_awaited()

    def test_other_trade_min_routes_still_need_sign_in(self, linked):
        assert self._client().get("/trade-min/screening/review").status_code == 401
