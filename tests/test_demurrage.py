"""Demurrage / detention clocks against the free days, one warning per stage, buyer notice,
staff empty-return relay, Maersk empty gate-in (shared/orchestration/demurrage.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from shared.connectors.maersk import normalize_events
from shared.orchestration import demurrage

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset():
    demurrage.reset_for_tests()
    yield
    demurrage.reset_for_tests()


def _tpo(**kw):
    base = dict(tpo_id="T1", ext_ref="BK1", order_type="FREIGHT", tracking_events=[], last_milestone=None,
                last_milestone_at=None, delivered_at=None, empty_returned_at=None, containers=[{}, {}])
    base.update(kw)
    return SimpleNamespace(**base)


def test_demurrage_warning_then_running():
    arrived = (NOW - timedelta(days=4)).isoformat()
    tpo = _tpo(tracking_events=[{"milestone": "ARRIVED", "at": arrived}])
    row = demurrage.assess(tpo, "AE", NOW)[0]
    assert row["kind"] == "demurrage" and row["stage"] == "warning" and row["days_left"] == 1.0
    late = demurrage.assess(tpo, "AE", NOW + timedelta(days=3))[0]
    assert late["stage"] == "running" and late["cost_so_far_usd"] == 2 * 75 * 2  # 2 days over × 2 boxes


def test_country_free_days_and_stopped_clock(monkeypatch):
    monkeypatch.setenv("DEMURRAGE_FREE_DAYS_AE", "10")
    tpo = _tpo(last_milestone="CLEARED", last_milestone_at=NOW - timedelta(days=4))
    assert demurrage.assess(tpo, "AE", NOW)[0]["stage"] == "ok"
    done = _tpo(last_milestone="DELIVERED", tracking_events=[{"milestone": "DISCHARGED", "at": (NOW - timedelta(days=9)).isoformat()}],
                delivered_at=NOW - timedelta(days=2))
    rows = {r["kind"]: r for r in demurrage.assess(done, "IN", NOW)}
    assert rows["demurrage"]["stage"] == "stopped" and rows["demurrage"]["cost_so_far_usd"] == 2 * 75 * 2
    assert rows["detention"]["stage"] == "ok" and rows["detention"]["days_used"] == 2.0


def test_detention_stops_on_empty_return():
    tpo = _tpo(delivered_at=NOW - timedelta(days=9), empty_returned_at=NOW - timedelta(days=1))
    det = [r for r in demurrage.assess(tpo, None, NOW) if r["kind"] == "detention"][0]
    assert det["stage"] == "stopped" and det["days_used"] == 8.0 and det["cost_so_far_usd"] == 1 * 40 * 2


def test_maersk_empty_gate_in_is_empty_returned():
    ev = normalize_events([{"eventClassifierCode": "ACT", "equipmentEventTypeCode": "GTIN", "eventType": "EQUIPMENT",
                            "emptyIndicatorCode": "EMPTY", "eventDateTime": "2026-11-18T10:00:00Z",
                            "eventLocation": {"UNLocationCode": "AEJEA"}}], origin="INNSA", destination="AEJEA")
    assert ev[0]["milestone"] == "EMPTY_RETURNED" and ev[0]["empty"] is True


async def test_sweep_warns_once_per_stage(monkeypatch):
    tpo = _tpo(tracking_events=[{"milestone": "ARRIVED", "at": (NOW - timedelta(days=4)).isoformat()}])

    async def orders(deal_id):
        return [tpo]

    monkeypatch.setattr(demurrage, "_freight_orders", orders)
    pages, notices = [], []

    async def send(m):
        pages.append(m)

    async def notify(deal, row):
        notices.append((deal.deal_id, row["kind"], row["stage"]))

    deal = SimpleNamespace(deal_id="D1", status="CLEARED", market="AE")
    other = SimpleNamespace(deal_id="D2", status="PAID", market="AE")
    assert await demurrage.sweep([deal, other], NOW, send=send, notify=notify) == 1
    assert await demurrage.sweep([deal], NOW + timedelta(hours=1), send=send, notify=notify) == 0
    assert await demurrage.sweep([deal], NOW + timedelta(days=2), send=send, notify=notify) == 1
    assert notices == [("D1", "demurrage", "warning"), ("D1", "demurrage", "running")]
    assert "DEMURRAGE SOON: D1" in pages[0] and "DEMURRAGE RUNNING" in pages[1]


def test_free_days_template():
    from shared.orchestration.notifications import _TEMPLATES
    subject, body = _TEMPLATES["free_days_warning"]({
        "deal_id": "D1", "buyer_name": "Gulf Foods",
        "clock": {"kind": "detention", "stage": "warning", "days_used": 6, "free_days": 7, "days_left": 1}})
    assert "Free days ending soon" in subject and "empty container" in body and "detention" in body


def test_empty_returned_route(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from services.shield_link import router as sl_router
    from shared.auth.dependencies import get_current_user

    saved = []

    class Repo:
        async def list_by_deal(self, deal_id):
            return [SimpleNamespace(tpo_id="T1", order_type="FREIGHT",
                                    model_copy=lambda update: SimpleNamespace(tpo_id="T1", **update))]

        async def save(self, rec):
            saved.append(rec)

    async def relay(*a, **k):
        return None

    monkeypatch.setattr("services.shield_link.repository.get_repository", lambda: Repo())
    monkeypatch.setattr("shared.audit_log.staff_relay", relay)
    app = FastAPI()
    app.include_router(sl_router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(email="ops", user_id="u", is_portless=True, org_id="o")
    r = TestClient(app).post("/shield-link/deals/D1/empty-returned", json={"returned_at": "2026-11-18T09:00:00"})
    assert r.status_code == 200 and saved[0].empty_returned_at.isoformat().startswith("2026-11-18T09:00:00")
    assert TestClient(app).post("/shield-link/deals/D1/empty-returned", json={"returned_at": "soon"}).status_code == 422
