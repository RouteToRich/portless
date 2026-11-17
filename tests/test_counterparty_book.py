"""Buyers & Suppliers as a trading book: relationship stage from every prospect and deal,
counterparty score, supplier quote vs the median, do-not-contact list (set by staff or by a
"not interested" reply, honoured by outreach in every prospect), exporter "buyers" skipped,
and the stored directory cleaned (names, roles)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.deals.schemas import BuyerCandidate, BuyerRequirement, DealRecord, DealStatus, Party, SupplierOffer
from services.trade_min import directory as dr
from services.trade_min import relationships as rl
from services.trade_min.schemas import CounterpartyRecord, CounterpartyValidation

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    dr.reset_directory()
    monkeypatch.setattr(rl, "_deals", AsyncMock(return_value=[]))
    monkeypatch.setattr(rl, "_tracks", AsyncMock(return_value={}))
    yield
    dr.reset_directory()


def _rec(role="buyer", name="Gulf Foods", website="https://gulf.ae", **kw) -> CounterpartyRecord:
    base = dict(id=dr.record_key(role, name, website), role=role, name=name, website=website, country="AE",
                email=f"buy@{website.split('//')[-1]}" if website else None, source="apify_company_search",
                hs_codes=["090422"], markets=["AE"], first_seen_at=NOW, last_seen_at=NOW)
    base.update(kw)
    return CounterpartyRecord(**base)


def _deal(deal_id="D1", status=DealStatus.MATCHING, cands=(), offers=(), parties=()):
    return DealRecord(deal_id=deal_id, operator_id="op", org_id="o", hs_code="090422", market="AE", status=status,
                      created_at=NOW, updated_at=NOW, buyer_candidates=list(cands), supplier_offers=list(offers),
                      parties=list(parties))


def _cand(cid="gulf.ae", status="contacted", email="buy@gulf.ae", **kw):
    kw.setdefault("website", f"https://{cid}")
    return BuyerCandidate(candidate_id=cid, legal_name="Gulf Foods" if cid == "gulf.ae" else cid, country="AE",
                          email=email, status=status, contacted_at=NOW - timedelta(days=2), **kw)


# ── relationship stages ──────────────────────────────────────────────────────

class TestRelationships:
    def test_stage_ladder_and_activity(self):
        deals = [
            _deal("D1", cands=[_cand(status="contacted")]),
            _deal("D2", cands=[_cand(status="interested", replied_at=NOW,
                                     requirement=BuyerRequirement(grade="ASTA 120", quantity_mt=20, target_price_usd_per_mt=3100))]),
        ]
        rel = rl.relationship_of(_rec(), rl.build_index(deals), {})
        assert rel["stage"] == "interested" and rel["prospects"] == 2 and rel["contacted"] == 2
        assert rel["replied"] == 1 and rel["reply_rate"] == 0.5 and rel["target_price_usd_per_mt"] == 3100
        assert rel["requirement"] == "ASTA 120 · 20 MT"

    def test_party_of_live_and_paid_deals(self):
        party = Party(role="buyer", legal_name="Gulf Foods", country="AE", email="buy@gulf.ae")
        idx = rl.build_index([_deal("D1", DealStatus.PI_SENT, parties=[party]), _deal("D2", DealStatus.PAID, parties=[party])])
        rel = rl.relationship_of(_rec(), idx, {})
        assert rel["stage"] == "traded" and rel["deals_won"] == 1 and rel["deals_active"] == 1

    def test_supplier_quotes_and_median(self):
        offers = [SupplierOffer(offer_id="O1", supplier_id="ks.in", company_name="Kerala Spices", email="s@ks.in",
                                fob_price_usd_per_mt=2000.0, received_at=NOW)]
        idx = rl.build_index([_deal(offers=offers)])
        rel = rl.relationship_of(_rec("supplier", "Kerala Spices", "https://ks.in", country="IN"), idx, {"090422": 2500.0})
        assert rel["stage"] == "quoted" and rel["last_quote_usd_per_mt"] == 2000.0 and rel["quote_vs_median_pct"] == -20.0

    def test_free_mail_never_joins_companies(self):
        assert rl.keys_for("a@gmail.com", name="Acme") == {"n:acme"}

    def test_never_contacted_is_new(self):
        assert rl.relationship_of(_rec(), {}, {})["stage"] == "new"


# ── score ────────────────────────────────────────────────────────────────────

class TestScore:
    def test_verified_engaged_company_scores_high(self):
        r = _rec(validation=CounterpartyValidation(status="verified", email="ok"), fit_score=90)
        score, basis = rl.score_of(r, {"stage": "interested"}, {"signed": 2, "failed": 0})
        assert score == 30 + 10 + 20 + 10 + 9 and any("past deals" in b for b in basis)

    def test_failed_or_dnc_scores_zero(self):
        assert rl.score_of(_rec(validation=CounterpartyValidation(status="failed")), {"stage": "new"}, None)[0] == 0
        assert rl.score_of(_rec(do_not_contact=True, dnc_reason="asked"), {"stage": "new"}, None) == (0.0, ["do not contact: asked"])

    def test_flags_and_cheap_quote(self):
        flagged = _rec(flags=["role_suspect"])
        assert rl.score_of(flagged, {"stage": "new"}, None)[0] == 0.0 + max(0, 10 + 10 - 30)
        cheap = _rec("supplier", "KS", "https://ks.in")
        assert any("below the median" in b for b in rl.score_of(cheap, {"stage": "quoted", "quote_vs_median_pct": -8}, None)[1])

    async def test_annotate_and_listing_filters(self, monkeypatch):
        store = await dr.get_directory()
        await store.upsert(_rec(validation=CounterpartyValidation(status="verified", email="ok")))
        await store.upsert(_rec(name="Cold Lead", website="https://cold.ae"))
        await store.upsert(_rec(name="Opted Out", website="https://out.ae", do_not_contact=True))
        monkeypatch.setattr(rl, "_deals", AsyncMock(return_value=[_deal(cands=[_cand(status="interested")])]))
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from services.trade_min.router import router
        app = FastAPI()
        app.include_router(router)
        c = TestClient(app)
        rows = c.get("/trade-min/buyers", params={"sort": "score"}).json()
        assert rows[0]["name"] == "Gulf Foods" and rows[0]["relationship"]["stage"] == "interested"
        assert [r["name"] for r in c.get("/trade-min/buyers", params={"stage": "do_not_contact"}).json()] == ["Opted Out"]
        names = {r["name"] for r in c.get("/trade-min/buyers", params={"contactable": True}).json()}
        assert names == {"Gulf Foods", "Cold Lead"}


# ── do not contact ───────────────────────────────────────────────────────────

class TestDoNotContact:
    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from services.trade_min.router import _staff, router
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[_staff()] = lambda: SimpleNamespace(email="ops@portless.in", is_portless=True)
        return TestClient(app)

    async def test_staff_set_and_clear_with_reason(self):
        store = await dr.get_directory()
        rec = await store.upsert(_rec())
        c = self._client()
        assert c.put(f"/trade-min/buyers/{rec.id}/contact-preference", json={"do_not_contact": True}).status_code == 422
        r = c.put(f"/trade-min/buyers/{rec.id}/contact-preference", json={"do_not_contact": True, "reason": "asked by phone"})
        assert r.status_code == 200 and r.json()["do_not_contact"] and "ops@portless.in" in r.json()["dnc_reason"]
        assert r.json()["relationship"]["stage"] == "do_not_contact"
        r = c.put(f"/trade-min/buyers/{rec.id}/contact-preference", json={"do_not_contact": False})
        assert r.json()["do_not_contact"] is False and r.json()["dnc_reason"] is None
        assert c.put("/trade-min/buyers/buyer:nope/contact-preference", json={"do_not_contact": False}).status_code == 404

    async def test_a_scan_never_clears_do_not_contact(self):
        store = await dr.get_directory()
        rec = await store.upsert(_rec())
        await store.set_contact_preference(rec.id, {"do_not_contact": True, "dnc_reason": "x"})
        again = await store.upsert(_rec(flags=["name_from_domain"]))
        assert again.do_not_contact and again.flags == ["name_from_domain"] and again.times_seen == 2

    async def test_blocked_contacts_from_directory_and_declines(self, monkeypatch):
        store = await dr.get_directory()
        rec = await store.upsert(_rec())
        await store.set_contact_preference(rec.id, {"do_not_contact": True})
        declined = _cand("other.ae", status="declined", email="ceo@other.ae", reply_preview="no thanks")
        skipped = _cand("x.ae", status="declined", email="a@x.ae", reply_preview="declined by operator")
        monkeypatch.setattr(rl, "_deals", AsyncMock(return_value=[_deal(cands=[declined, skipped])]))
        blocked = await rl.blocked_contacts()
        assert {"gulf.ae", "buy@gulf.ae", "ceo@other.ae"} <= blocked and "a@x.ae" not in blocked

    async def test_outreach_skips_blocked_and_exporter_buyers(self, monkeypatch):
        from shared.orchestration import outreach
        deal = _deal(cands=[_cand("gulf.ae", status="pending"), _cand("exp.in", status="pending", email="x@exp.in"),
                            _cand("ok.ae", status="pending", email="buy@ok.ae")])
        update = AsyncMock()
        gate = AsyncMock(return_value="APPR-1")
        monkeypatch.setattr(outreach, "enabled", lambda: True)
        monkeypatch.setattr(outreach, "_blocked", AsyncMock(return_value={"gulf.ae"}))
        monkeypatch.setattr(outreach, "_suspect", AsyncMock(return_value={"exp.in"}))
        monkeypatch.setattr(outreach, "_gate", gate)
        with patch("services.deals.service.update_buyer_candidate", update):
            sent = await outreach.start_outreach(deal, limit=3)
        reasons = {call.args[1]: call.args[2]["reply_preview"] for call in update.await_args_list}
        assert "do-not-contact" in reasons["gulf.ae"] and "Indian exporter" in reasons["exp.in"]
        assert sent == 1 and gate.await_args.args[1].candidate_id == "ok.ae"

    async def test_not_interested_reply_puts_the_company_on_the_list(self):
        from shared.orchestration import outreach
        store = await dr.get_directory()
        rec = await store.upsert(_rec())
        await outreach._mark_do_not_contact(_cand(), "D9", "Not interested, thanks")
        got = await store.get(rec.id)
        assert got.do_not_contact and got.dnc_reason.startswith("replied not interested on D9")


# ── stored directory clean-up ────────────────────────────────────────────────

class TestClean:
    def test_names_and_roles(self):
        rec = _rec(name="Jimmyandsons", website="https://jimmyandsons.in", country="IN", markets=["DE"])
        cleaned = dr.clean_record(rec)
        assert cleaned.name == "Jimmy & Sons" and "role_suspect" in cleaned.flags  # .in domain pitched as a DE buyer
        sentence = dr.clean_record(_rec("supplier", "As Cumin Exporters in India, our motto at VORA SPICE MILLS LLP",
                                        "https://voraspices.com", country="IN"))
        assert sentence.name == "VORA SPICE MILLS LLP" and sentence.flags == []

    async def test_listing_cleans_on_read_and_endpoint_stores_it(self):
        store = await dr.get_directory()
        raw = _rec(name="Naturalspicesllp", website="https://naturalspicesllp.in", markets=["DE"])
        store._store[raw.id] = raw  # stored before the current clean-up existed
        rows = await dr.list_counterparties("buyer")
        assert rows[0].name == "Natural Spices LLP" and "role_suspect" in rows[0].flags
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from services.trade_min.router import _staff, router
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[_staff()] = lambda: SimpleNamespace(email="ops@x", is_portless=True)
        assert TestClient(app).post("/trade-min/directory/clean").json()["changed"] == 1
        assert (await store.list("buyer"))[0].name == "Natural Spices LLP"
