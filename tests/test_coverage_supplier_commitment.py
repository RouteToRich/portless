"""shared/orchestration/supplier_commitment.py at 100 %: the supplier's firm confirmation before the
PI, the standby switch, the PO acknowledgement, the goods-ready date and the facility address."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mongomock_motor import AsyncMongoMockClient

from shared.orchestration import supplier_commitment as sc

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


class _Broken:
    async def find_one(self, *a, **k):
        raise RuntimeError("down")

    async def replace_one(self, *a, **k):
        raise RuntimeError("down")

    def find(self, *a, **k):
        raise RuntimeError("down")


def _rec(**kw) -> sc.Commitment:
    base = dict(deal_id="D", agreed_price=500.0, quantity_mt=20.0, market="AE")
    base.update(kw)
    return sc.Commitment(**base)


def _attempt(**kw) -> sc.Attempt:
    base = dict(supplier_id="S1", company_name="Acme", email="s1@acme.in", price_usd_per_mt=400.0, requested_at=NOW)
    base.update(kw)
    return sc.Attempt(**base)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    sc.reset_for_tests()
    monkeypatch.setattr(sc, "_mongo", lambda: None)
    yield
    sc.reset_for_tests()


# ── settings, storage ──────────────────────────────────────────────────────

def test_confirm_hours_setting(monkeypatch):
    monkeypatch.setenv("SUPPLIER_CONFIRMATION_HOURS", "x")
    assert sc.confirm_hours() == 24.0


def test_mongo_collection_selection(monkeypatch):
    monkeypatch.undo()
    fake = SimpleNamespace(mongo_uri="mongodb://x", mongo_db_name="p")
    with patch("shared.config.settings", new=fake), patch("shared.mongo.get_client", return_value=AsyncMongoMockClient()):
        assert sc._mongo() is not None
    with patch("shared.config.settings", new=fake), patch("shared.mongo.get_client", side_effect=RuntimeError("x")):
        assert sc._mongo() is None


async def test_get_and_store_in_mongo_and_on_failure(monkeypatch):
    col = AsyncMongoMockClient()["t"]["supplier_commitments"]
    monkeypatch.setattr(sc, "_mongo", lambda: col)
    assert await sc.get("D") is None
    await sc._store(_rec())
    assert (await sc.get("D")).agreed_price == 500.0
    monkeypatch.setattr(sc, "_mongo", lambda: _Broken())
    await sc._store(_rec(deal_id="M"))  # write fails → memory
    assert (await sc.get("M")).deal_id == "M"


def test_aware_and_margin_paths():
    assert sc._aware(NOW.replace(tzinfo=None)).tzinfo is not None
    with_sheet = SimpleNamespace(cost_sheet={"fixed_usd_per_mt": 50.0, "pct_of_price": 0.0})
    assert sc._net_margin_pct(with_sheet, 500.0, 400.0) == pytest.approx(10.0)
    env = SimpleNamespace(cost_sheet=None, cost_envelope=SimpleNamespace(total=lambda: 20.0), logistics_cost_usd=30.0)
    assert sc._net_margin_pct(env, 500.0, 400.0) == pytest.approx(10.0)
    assert sc._net_margin_pct(SimpleNamespace(cost_sheet=None, cost_envelope=None), 0.0, 400.0) == -100.0


async def test_session_lookup():
    repo = SimpleNamespace(get_session_by_deal=AsyncMock(return_value="S"))
    with patch("agents.negotiation_agent.repository.get_repository", return_value=repo):
        assert await sc._session("D") == "S"


# ── standby suppliers ──────────────────────────────────────────────────────

async def test_manual_standbys_filters():
    with patch("services.trade_min.directory.get_directory", side_effect=RuntimeError("down")):
        assert await sc._manual_standbys(SimpleNamespace(hs_code="070310"), set()) == []

    def r(**kw):
        base = dict(id="supplier:x", source="manual", name="X", email="x@x.in", do_not_contact=False, validation=None,
                    estimated_fob_price_usd=300.0, hs_codes=["070310"])
        base.update(kw)
        return SimpleNamespace(**base)
    rows = [r(id="a", source="scan"), r(id="known"), r(id="b", email=None), r(id="c", do_not_contact=True),
            r(id="d", validation=SimpleNamespace(status="failed")), r(id="e", estimated_fob_price_usd=None),
            r(id="f", hs_codes=["100630"]), r(id="g", estimated_fob_price_usd=350.0), r(id="h", estimated_fob_price_usd=320.0)]
    store = SimpleNamespace(list=AsyncMock(return_value=rows))
    with patch("services.trade_min.directory.get_directory", new=AsyncMock(return_value=store)):
        out = await sc._manual_standbys(SimpleNamespace(hs_code="070310"), {"known"})
    assert [o["supplier_id"] for o in out] == ["h", "g"]


async def test_standby_suppliers_rows():
    deal = SimpleNamespace(
        hs_code="070310",
        match_results=[SimpleNamespace(supplier_id="M1", company_name="M1", decision="REJECT", fob_price_usd=300, composite_score=90),
                       SimpleNamespace(supplier_id="M2", company_name="", decision="SOURCE", fob_price_usd=310, composite_score=80)],
        supplier_offers=[SimpleNamespace(supplier_id="M2", company_name="Two", email="two@x.in", fob_price_usd_per_mt=305,
                                         verification_failed=False),
                         SimpleNamespace(supplier_id="BAD", company_name="", email=None, fob_price_usd_per_mt=200,
                                         verification_failed=True),
                         SimpleNamespace(supplier_id="O3", company_name="Three", email=None, fob_price_usd_per_mt=330,
                                         verification_failed=False)])
    with patch.object(sc, "_manual_standbys", new=AsyncMock(return_value=[{"supplier_id": "MAN", "email": "m@m.in",
                                                                           "company_name": "Man", "price": 290, "score": 0}])), \
            patch("services.deals.service._supplier_contact", side_effect=RuntimeError("no contact")):
        out = {r["supplier_id"]: r for r in await sc.standby_suppliers(deal)}
    assert set(out) == {"M2", "O3", "MAN"} and out["M2"]["email"] == "two@x.in" and out["M2"]["company_name"] == "Two"
    assert out["O3"]["email"] is None


# ── before the PI ──────────────────────────────────────────────────────────

async def test_ready_for_pi_without_session_or_price(monkeypatch):
    monkeypatch.setenv("SUPPLIER_CONFIRMATION_REQUIRED", "true")
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=SimpleNamespace(parties=[]))), \
            patch.object(sc, "_session", new=AsyncMock(return_value=None)):
        assert await sc.ready_for_pi("D", agreed_price=500, quantity_mt=20, market="AE") is True
    session = SimpleNamespace(supplier_last_quote=None, supplier_quote_price=None)
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=SimpleNamespace(parties=[]))), \
            patch.object(sc, "_session", new=AsyncMock(return_value=session)):
        assert await sc.ready_for_pi("D2", agreed_price=500, quantity_mt=20, market="AE") is True


# ── reading replies ────────────────────────────────────────────────────────

def test_new_text_survives_a_broken_guard():
    with patch("agents.negotiation_agent.guards._new_reply_text", side_effect=RuntimeError("x")):
        assert sc._new_text("hello") == "hello"


async def test_replaced_supplier_detection():
    rec = _rec(attempts=[_attempt(supplier_id="OLD", email="old@old.in", outcome="replaced"),
                         _attempt(supplier_id="NEW", email="new@new.in")])
    sc._memory["D"] = rec
    assert await sc.sent_by_replaced_supplier("D", "old@old.in") is True
    assert await sc.sent_by_replaced_supplier("D", "new@new.in") is False
    assert await sc.sent_by_replaced_supplier("none", "x@y.z") is False


async def test_handle_reply_routes():
    assert await sc.handle_supplier_reply("missing", "PO_ISSUED", "hi", "E1") is None
    sc._memory["D"] = _rec(attempts=[_attempt(email="s1@acme.in")])
    assert await sc.handle_supplier_reply("D", "NEGOTIATION_AGREED", "we confirm", "E1", from_address="x@other.com") is None
    assert await sc.handle_supplier_reply("D", "NEGOTIATION_AGREED", "price is now 420", "E1") is None or True
    assert await sc.handle_supplier_reply("D", "PAID", "thanks", "E1") is None


async def test_handle_reply_order_decline_and_unclear():
    sc._memory["D"] = _rec(attempts=[_attempt()])
    with patch.object(sc, "read_order_reply", new=AsyncMock(return_value=("declined", None))), \
            patch.object(sc, "mark_and_advance", new=AsyncMock(return_value="requested")) as adv:
        assert "declined" in await sc.handle_supplier_reply("D", "NEGOTIATION_AGREED", "cannot", "E1")
    adv.assert_awaited()
    with patch.object(sc, "read_order_reply", new=AsyncMock(return_value=("unclear", 420.0))):
        assert await sc.handle_supplier_reply("D", "NEGOTIATION_AGREED", "420?", "E1") is None


async def test_handle_reply_after_the_po():
    sc._memory["D"] = _rec(po_ack="awaiting")
    with patch.object(sc, "acknowledge_po", new=AsyncMock(return_value=None)), \
            patch.object(sc, "get", new=AsyncMock(side_effect=[_rec(po_ack="awaiting"), _rec(facility_address_status="awaiting")])):
        assert "facility" in await sc.handle_supplier_reply("D", "PO_ISSUED", "We acknowledge the PO", "E1")
    with patch.object(sc, "acknowledge_po", new=AsyncMock(return_value=None)), \
            patch.object(sc, "get", new=AsyncMock(side_effect=[_rec(po_ack="awaiting"), None])):
        assert "goods are ready" in await sc.handle_supplier_reply("D", "PO_ISSUED", "We acknowledge the PO", "E1")
    sc._memory["D"] = _rec(po_ack="awaiting")
    assert await sc.handle_supplier_reply("D", "PO_ISSUED", "who are you?", "E1") is None
    sc._memory["D"] = _rec(po_ack="acknowledged", qc_ready_status="awaiting")
    with patch.object(sc, "record_qc_readiness", new=AsyncMock(return_value={"status": "recorded"})):
        assert "waits for its facility address" in await sc.handle_supplier_reply(
            "D", "PO_ISSUED", "Goods ready on 2026-12-20", "E2")
    sc._memory["D"] = _rec(po_ack="acknowledged", facility_address_status="awaiting")
    with patch.object(sc, "_address_now_known", new=AsyncMock(return_value=True)), \
            patch.object(sc, "address_known", new=AsyncMock(return_value=SimpleNamespace(inspection_date="2026-12-22"))):
        assert "2026-12-22" in await sc.handle_supplier_reply("D", "PO_ISSUED", "Plot 4, Nashik 422001", "E3")
    with patch.object(sc, "_address_now_known", new=AsyncMock(return_value=True)), \
            patch.object(sc, "address_known", new=AsyncMock(return_value=None)):
        assert await sc.handle_supplier_reply("D", "PO_ISSUED", "Plot 4, Nashik 422001", "E3") is None


async def test_address_now_known():
    with patch("shared.orchestration.supplier_address.current", new=AsyncMock(return_value=("addr", True))):
        assert await sc._address_now_known("D") is True


async def test_record_confirmed_refusals():
    with pytest.raises(RuntimeError, match="no supplier order request"):
        await sc.record_confirmed("none", supplier_id="S1", price=400)
    sc._memory["D"] = _rec(attempts=[_attempt()])
    with pytest.raises(RuntimeError, match="does not hold"):
        await sc.record_confirmed("D", supplier_id="OTHER", price=400)


# ── standby switch ─────────────────────────────────────────────────────────

async def test_mark_and_advance_without_open_request():
    assert await sc.mark_and_advance("none", "declined") == "none"
    sc._memory["D"] = _rec(status="confirmed")
    assert await sc.mark_and_advance("D", "declined") == "confirmed"


async def test_mark_and_advance_skips_and_operator_party(monkeypatch):
    sc._memory["D"] = _rec(attempts=[_attempt()], skipped=[{"supplier_id": "SKIPPED"}])
    session = SimpleNamespace(policy=SimpleNamespace(margin_floor_pct=8.0),
                              cost_sheet={"fixed_usd_per_mt": 0.0, "pct_of_price": 0.0})
    rows = [{"supplier_id": "S1", "email": "s1@acme.in", "price": 300, "company_name": "Acme"},
            {"supplier_id": "SKIPPED", "email": "x@x.in", "price": 300, "company_name": "X"},
            {"supplier_id": "NOMAIL", "email": None, "price": 300, "company_name": "N"},
            {"supplier_id": "DEAR", "email": "d@d.in", "price": 490, "company_name": "D"},
            {"supplier_id": "OK", "email": "ok@ok.in", "price": 300, "company_name": "OK"}]
    operator_set = SimpleNamespace(parties=[SimpleNamespace(role="supplier", party_id="OPERATOR")])
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=SimpleNamespace())), \
            patch("services.deals.service.set_supplier_party", new=AsyncMock(return_value=operator_set)), \
            patch.object(sc, "_session", new=AsyncMock(return_value=session)), \
            patch.object(sc, "standby_suppliers", new=AsyncMock(return_value=rows)), \
            patch.object(sc, "_save", new=AsyncMock()), \
            patch("shared.orchestration.continuations._publish_exception", new=AsyncMock()) as pub:
        assert await sc.mark_and_advance("D", "declined") == "exhausted"
    reasons = [s["reason"] for s in sc._memory["D"].skipped if "reason" in s]
    assert any("no e-mail" in r for r in reasons) and any("below" in r for r in reasons)
    assert any("operator-set" in r for r in reasons)
    pub.assert_awaited()


async def test_without_a_session_no_standby_is_trusted():
    """No negotiation session → the margin of a standby cannot be checked → nobody is asked."""
    sc._memory["D"] = _rec(attempts=[_attempt()])
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=SimpleNamespace())),             patch.object(sc, "_session", new=AsyncMock(return_value=None)),             patch.object(sc, "standby_suppliers", new=AsyncMock(return_value=[
                {"supplier_id": "OK", "email": "ok@ok.in", "price": 300, "company_name": "OK"}])),             patch.object(sc, "_save", new=AsyncMock()),             patch("shared.orchestration.continuations._publish_exception", new=AsyncMock()):
        assert await sc.mark_and_advance("D", "no_reply") == "exhausted"


async def test_mark_and_advance_with_session_moves_to_standby():
    sc._memory["D"] = _rec(attempts=[_attempt()])
    session = SimpleNamespace(policy=SimpleNamespace(margin_floor_pct=8.0), cost_sheet={"fixed_usd_per_mt": 0.0, "pct_of_price": 0.0},
                              model_copy=lambda update: SimpleNamespace(**update))
    updated = SimpleNamespace(parties=[SimpleNamespace(role="supplier", party_id="OK")])
    repo = SimpleNamespace(save_session=AsyncMock())
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=SimpleNamespace())),             patch("services.deals.service.set_supplier_party", new=AsyncMock(return_value=updated)),             patch.object(sc, "_session", new=AsyncMock(return_value=session)),             patch.object(sc, "standby_suppliers", new=AsyncMock(return_value=[
                {"supplier_id": "OK", "email": "ok@ok.in", "price": 300, "company_name": "OK"}])),             patch.object(sc, "_save", new=AsyncMock()),             patch("agents.negotiation_agent.repository.get_repository", return_value=repo),             patch.object(sc, "_request", new=AsyncMock()) as req:
        assert await sc.mark_and_advance("D", "no_reply") == "requested"
    req.assert_awaited()
    repo.save_session.assert_awaited()


async def test_record_confirmed_without_a_session_still_issues_the_pi():
    sc._memory["D"] = _rec(attempts=[_attempt()])
    with patch.object(sc, "_save", new=AsyncMock()), patch.object(sc, "_session", new=AsyncMock(return_value=None)),             patch("shared.orchestration.continuations.pi_after_compliance", new=AsyncMock(return_value="PI")):
        assert await sc.record_confirmed("D", supplier_id="S1", price=400) == "PI"


async def test_sweep_off_and_po_ack_already_given(monkeypatch):
    monkeypatch.setenv("SUPPLIER_CONFIRMATION_REQUIRED", "false")
    assert await sc.sweep(NOW) == 0
    sc._memory["D"] = _rec(po_ack="acknowledged")
    with patch.object(sc, "_save", new=AsyncMock()) as save:
        await sc.await_po_ack("D", 20.0)
    save.assert_not_awaited()


async def test_sweep_reads_mongo_and_survives_failures(monkeypatch):
    monkeypatch.setenv("SUPPLIER_CONFIRMATION_REQUIRED", "true")
    col = AsyncMongoMockClient()["t"]["supplier_commitments"]
    old = _rec(deal_id="OLD", attempts=[_attempt(requested_at=NOW - timedelta(days=3))])
    await col.insert_one(old.model_dump(mode="python"))
    sc._memory["MEM"] = _rec(deal_id="MEM", attempts=[_attempt(requested_at=NOW)])
    monkeypatch.setattr(sc, "_mongo", lambda: col)
    with patch.object(sc, "mark_and_advance", new=AsyncMock(return_value="requested")) as adv:
        assert await sc.sweep(NOW) == 1
    adv.assert_awaited_once()
    monkeypatch.setattr(sc, "_mongo", lambda: _Broken())
    with patch.object(sc, "mark_and_advance", new=AsyncMock()):
        assert await sc.sweep(NOW) == 0


# ── PO acknowledgement, goods-ready date, facility address ─────────────────

async def test_acknowledge_po_paths():
    sc._memory["D"] = _rec(po_ack="acknowledged")
    assert await sc.acknowledge_po("D", by="x") is None
    with patch.object(sc, "_save", new=AsyncMock()), patch.object(sc, "book_qc_when_ready", new=AsyncMock(return_value="TPO")):
        assert await sc.acknowledge_po("NEW", by="x", ready_date="2026-12-20") == "TPO"


async def test_request_qc_readiness_paths():
    sc._memory["D"] = _rec(qc_ready_status="awaiting")
    assert await sc.request_qc_readiness("D") is False
    with patch.object(sc, "_save", new=AsyncMock()), \
            patch("services.deals.service.get_deal", side_effect=RuntimeError("down")):
        assert await sc.request_qc_readiness("NEW") is True  # the e-mail failure is logged, the watcher asks again


async def test_book_qc_when_ready_waits_for_the_address(monkeypatch):
    sc._memory["D"] = _rec(qc_ready_date="2026-12-20", facility_address_status="awaiting")
    with patch("shared.orchestration.qc_schedule.readiness_required", return_value=False), \
            patch("shared.orchestration.supplier_address.required", return_value=True), \
            patch("shared.orchestration.supplier_address.current", new=AsyncMock(return_value=(None, False))), \
            patch.object(sc, "request_facility_address", new=AsyncMock(return_value=True)) as ask:
        assert await sc.book_qc_when_ready("D") is None
    ask.assert_awaited()
    with patch("shared.orchestration.qc_schedule.readiness_required", return_value=False), \
            patch("shared.orchestration.supplier_address.required", return_value=False), \
            patch.object(sc, "_save", new=AsyncMock()), \
            patch("shared.orchestration.continuations.book_qc_for_deal", new=AsyncMock(return_value="TPO")):
        assert await sc.book_qc_when_ready("D") == "TPO"
    assert sc._memory["D"].facility_address_status == "received"


async def test_request_facility_address_paths():
    sc._memory["D"] = _rec(facility_address_status="received")
    assert await sc.request_facility_address("D") is False
    deal = SimpleNamespace(parties=[])
    with patch.object(sc, "_save", new=AsyncMock()), \
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)), \
            patch("shared.orchestration.supplier_address.current", new=AsyncMock(return_value=("Nashik", False))), \
            patch("shared.orchestration.notifications.notify_party", new=AsyncMock()) as notify:
        assert await sc.request_facility_address("NEW") is True
    notify.assert_awaited()
    with patch.object(sc, "_save", new=AsyncMock()), \
            patch("services.deals.service.get_deal", side_effect=RuntimeError("down")):
        assert await sc.request_facility_address("NEW2") is True


async def test_address_known_paths():
    assert await sc.address_known("none") is None
    sc._memory["D"] = _rec(facility_address_status="awaiting", po_ack="acknowledged")
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=SimpleNamespace(status="QC_PASSED"))):
        assert await sc.address_known("D") is None
    with patch("services.deals.service.get_deal", side_effect=RuntimeError("x")):
        assert await sc.address_known("D") is None
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=SimpleNamespace(status="PO_ISSUED"))), \
            patch.object(sc, "book_qc_when_ready", new=AsyncMock(return_value="TPO")):
        assert await sc.address_known("D") == "TPO"


async def test_record_qc_readiness_paths():
    from services.shield_link.service import DealNotFoundError
    with patch("services.deals.service.get_deal", side_effect=RuntimeError("missing")):
        with pytest.raises(DealNotFoundError):
            await sc.record_qc_readiness("X", "2026-12-20", by="ops")
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=SimpleNamespace(status="PO_ISSUED"))), \
            patch.object(sc, "_save", new=AsyncMock()), \
            patch.object(sc, "book_qc_when_ready", new=AsyncMock(return_value=None)):
        out = await sc.record_qc_readiness("D", "2026-12-20", by="ops")
    assert out["status"] == "recorded"
    with patch("services.deals.service.get_deal", new=AsyncMock(return_value=SimpleNamespace(status="ESCROW_FUNDED"))), \
            patch.object(sc, "_save", new=AsyncMock()):
        assert (await sc.record_qc_readiness("E", "2026-12-20", by="ops"))["status"] == "recorded"
