"""Buyer / supplier profile and directory summary: engagements and timeline across
prospects and deals, open prospects to join, next actions, add-to-prospect (buyer →
outreach funnel, supplier → match board with quote pending, refused when blocked)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import (
    BuyerCandidate, DealRecord, DealStatus, IndicativeOffer, Party, SupplierOffer,
)
from services.trade_min import counterparty_profile as cp
from services.trade_min import directory as dr
from services.trade_min import relationships as rl
from services.trade_min.schemas import CounterpartyRecord, CounterpartyValidation

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    dr.reset_directory()
    monkeypatch.setattr(rl, "_tracks", AsyncMock(return_value={}))
    yield
    dr.reset_directory()


@pytest.fixture
def deals(monkeypatch):
    repo = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.service.get_repository", lambda: repo)
    store: list = []

    async def _list():
        return [d for d in repo._store.values()] if hasattr(repo, "_store") else store

    monkeypatch.setattr(rl, "_deals", _list)
    return repo


def _rec(role="buyer", name="Gulf Foods", domain="gulf.ae", **kw):
    base = dict(id=f"{role}:{domain}", role=role, name=name, website=f"https://{domain}", email=f"buy@{domain}",
                country="AE", source="apify_company_search", hs_codes=["090422"], markets=["AE"],
                first_seen_at=NOW - timedelta(days=3), last_seen_at=NOW)
    base.update(kw)
    return CounterpartyRecord(**base)


def _deal(deal_id, status=DealStatus.MATCHING, hs="090422", market="AE", cands=(), offers=(), parties=()):
    return DealRecord(deal_id=deal_id, operator_id="op", org_id="o", hs_code=hs, market=market, status=status,
                      created_at=NOW, updated_at=NOW, buyer_candidates=list(cands), supplier_offers=list(offers),
                      parties=list(parties))


async def _store(*recs):
    s = await dr.get_directory()
    for r in recs:
        await s.upsert(r)
    return s


class TestProfile:
    async def test_engagements_timeline_and_open_prospects(self, deals):
        await _store(_rec())
        cand = BuyerCandidate(candidate_id="gulf.ae", legal_name="Gulf Foods", country="AE", email="buy@gulf.ae",
                              website="https://gulf.ae", status="interested", contacted_at=NOW - timedelta(days=2),
                              replied_at=NOW - timedelta(days=1), reply_preview="Yes, send prices",
                              indicative_offer=IndicativeOffer(offer_id="O1", price_low_usd_per_mt=3000,
                                                               price_high_usd_per_mt=3100, sent_at=NOW))
        await deals.save(_deal("D1", cands=[cand]))
        await deals.save(_deal("D2"))                       # same product, not in it → can join
        await deals.save(_deal("D3", hs="100630"))          # other product → not offered
        await deals.save(_deal("D4", market="DE"))          # other market → not offered to an AE buyer
        p = await cp.profile("buyer", "buyer:gulf.ae")
        assert p["company"]["relationship"]["stage"] == "interested"
        assert [e["deal_id"] for e in p["engagements"]] == ["D1"] and p["engagements"][0]["reply"] == "Yes, send prices"
        assert [o["deal_id"] for o in p["open_prospects"]] == ["D2"]
        kinds = [e["kind"] for e in p["timeline"]]
        assert {"contacted", "replied", "offer", "found"} <= set(kinds) and kinds[-1] == "found"
        labels = [a["kind"] for a in p["next_actions"]]
        assert labels[:2] == ["open_deal", "add_to_prospect"]

    async def test_supplier_quotes_and_party(self, deals):
        await _store(_rec("supplier", "Kerala Spices", "ks.in", country="IN", markets=[]))
        offer = SupplierOffer(offer_id="O1", supplier_id="ks.in", company_name="Kerala Spices", email="s@ks.in",
                              fob_price_usd_per_mt=2000.0, received_at=NOW)
        party = Party(role="supplier", legal_name="Kerala Spices", country="IN", email="s@ks.in")
        await deals.save(_deal("D1", offers=[offer]))
        await deals.save(_deal("D2", status=DealStatus.PAID, parties=[party]))
        p = await cp.profile("supplier", "supplier:ks.in")
        assert {(e["deal_id"], e["as"]) for e in p["engagements"]} == {("D1", "quote"), ("D2", "party")}
        assert any(e["kind"] == "traded" for e in p["timeline"]) and p["company"]["relationship"]["stage"] == "traded"

    async def test_blocked_company_says_why(self, deals):
        await _store(_rec(do_not_contact=True, dnc_reason="asked"))
        p = await cp.profile("buyer", "buyer:gulf.ae")
        assert p["blocked_reason"] == "on the do-not-contact list" and p["next_actions"][0]["kind"] == "blocked"

    async def test_unknown_company(self, deals):
        with pytest.raises(cp.ProfileError):
            await cp.profile("buyer", "buyer:nope.ae")


class TestAddToProspect:
    async def test_buyer_joins_the_funnel(self, deals):
        await _store(_rec(validation=CounterpartyValidation(status="verified")))
        await deals.save(_deal("D2"))
        out = await cp.add_to_prospect("buyer", "buyer:gulf.ae", "D2")
        deal = await deals.get("D2")
        assert out["added"] == "buyer_candidate" and deal.buyer_candidates[0].email == "buy@gulf.ae"
        assert deal.buyer_candidates[0].validation_status == "verified"
        with pytest.raises(ValueError):  # twice → duplicate refused by the deal
            await cp.add_to_prospect("buyer", "buyer:gulf.ae", "D2")

    async def test_supplier_joins_the_board_with_quote_pending(self, deals):
        await _store(_rec("supplier", "Kerala Spices", "ks.in", country="IN"))
        await deals.save(_deal("D2"))
        out = await cp.add_to_prospect("supplier", "supplier:ks.in", "D2")
        offer = (await deals.get("D2")).supplier_offers[0]
        assert out["added"] == "supplier_offer" and offer.fob_price_usd_per_mt is None and offer.company_name == "Kerala Spices"

    @pytest.mark.parametrize("kw", [{"do_not_contact": True}, {"validation": CounterpartyValidation(status="failed")},
                                    {"flags": ["role_suspect"]}, {"email": None}])
    async def test_refused_when_blocked_or_unreachable(self, deals, kw):
        await _store(_rec(**kw))
        await deals.save(_deal("D2"))
        with pytest.raises(cp.ProfileError):
            await cp.add_to_prospect("buyer", "buyer:gulf.ae", "D2")

    async def test_only_open_prospects(self, deals):
        await _store(_rec())
        await deals.save(_deal("D9", status=DealStatus.PI_SENT))
        with pytest.raises(cp.ProfileError, match="no longer a prospect"):
            await cp.add_to_prospect("buyer", "buyer:gulf.ae", "D9")


class TestSummaryAndRoutes:
    async def test_summary(self, deals):
        await _store(_rec(), _rec(name="Old Lead", domain="old.ae", first_seen_at=NOW - timedelta(days=30)),
                     _rec(name="Opted", domain="out.ae", do_not_contact=True))
        cand = BuyerCandidate(candidate_id="gulf.ae", legal_name="Gulf Foods", country="AE", email="buy@gulf.ae",
                              website="https://gulf.ae", status="interested", replied_at=NOW)
        await deals.save(_deal("D1", cands=[cand]))
        s = (await cp.summary())["buyer"]
        assert s["total"] == 3 and s["stages"]["interested"] == 1 and s["do_not_contact"] == 1
        assert s["new_this_week"] == 2 and s["contactable"] == 2 and s["hot"][0]["name"] == "Gulf Foods"
        assert s["new_by_product"][0] == ("090422", 2)

    async def test_routes(self, deals):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from services.trade_min.router import router
        await _store(_rec())
        await deals.save(_deal("D2"))
        app = FastAPI()
        app.include_router(router)
        c = TestClient(app)
        assert c.get("/trade-min/buyers/buyer:gulf.ae/profile").json()["company"]["name"] == "Gulf Foods"
        assert c.get("/trade-min/buyers/buyer:none/profile").status_code == 404
        assert c.post("/trade-min/buyers/buyer:gulf.ae/add-to-prospect", json={"deal_id": "D2"}).status_code == 200
        assert c.post("/trade-min/buyers/buyer:gulf.ae/add-to-prospect", json={"deal_id": "D2"}).status_code == 422
        assert c.post("/trade-min/buyers/buyer:gulf.ae/add-to-prospect", json={"deal_id": "NOPE"}).status_code == 404
        assert c.get("/trade-min/directory/summary").json()["buyer"]["total"] == 1


# --- contact refresh on entering a prospect --------------------------------------------------

class TestContactRefresh:
    def _rec(self, **kw):
        from datetime import UTC, datetime, timedelta
        from services.trade_min.schemas import CounterpartyRecord, CounterpartyValidation
        now = datetime.now(UTC)
        base = dict(id="buyer:gulf.ae", role="buyer", name="Gulf Foods", website="https://gulf.ae", country="AE",
                    email="old@gulf.ae", source="apify_company_search", hs_codes=["090422"], markets=["AE"],
                    first_seen_at=now - timedelta(days=200), last_seen_at=now - timedelta(days=120),
                    validation=CounterpartyValidation(status="verified", sanctions="clear", email="ok"))
        base.update(kw)
        return CounterpartyRecord(**base)

    def test_when_contacts_are_stale(self):
        from datetime import UTC, datetime, timedelta
        from services.trade_min import contact_refresh as cr
        from services.trade_min.schemas import CounterpartyValidation
        assert "older than 90 days" in cr.needs_refresh(self._rec())
        assert cr.needs_refresh(self._rec(last_seen_at=datetime.now(UTC))) is None
        bounced = self._rec(last_seen_at=datetime.now(UTC),
                            validation=CounterpartyValidation(status="unverified", sanctions="clear", email="invalid"))
        assert cr.needs_refresh(bounced) == "e-mail undeliverable"
        assert cr.needs_refresh(self._rec(contacts_refreshed_at=datetime.now(UTC) - timedelta(days=5))) is None

    async def test_a_new_email_replaces_the_old_and_is_rechecked(self, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from services.trade_min import contact_refresh as cr
        from services.trade_min import directory as dr
        dr.reset_directory()
        store = await dr.get_directory()
        rec = await store.upsert(self._rec())
        conn = SimpleNamespace(quote=AsyncMock(return_value={"contacts": [
            {"email": "import@gulf.ae", "email_status": "unverified", "phone": "+971 4 000", "address": "Deira, Dubai"}]}))
        monkeypatch.setattr("shared.connectors.registry.get_connector", lambda name: conn)
        out = await cr.ensure_fresh(rec)
        assert out.email == "import@gulf.ae" and out.phone == "+971 4 000" and out.contacts_refreshed_at
        assert out.validation.email == "unverified" and out.validation.status == "unverified"
        stored = await store.get(rec.id)
        assert stored.email == "import@gulf.ae"
        conn.quote.reset_mock()
        assert (await cr.ensure_fresh(stored)).email == "import@gulf.ae"
        conn.quote.assert_not_awaited()  # fresh now
        dr.reset_directory()

    async def test_stub_or_failure_keeps_the_record(self, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from services.trade_min import contact_refresh as cr
        rec = self._rec()
        monkeypatch.setattr("shared.connectors.registry.get_connector",
                            lambda name: SimpleNamespace(quote=AsyncMock(return_value={"stub": True, "contacts": []})))
        assert (await cr.ensure_fresh(rec)) is rec
        monkeypatch.setattr("shared.connectors.registry.get_connector",
                            lambda name: SimpleNamespace(quote=AsyncMock(side_effect=RuntimeError("apify down"))))
        assert (await cr.ensure_fresh(rec)) is rec
