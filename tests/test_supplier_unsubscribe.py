"""Unsubscribe link in supplier e-mails (review board B-12): RFQ, reconfirmation and best and
final carry it; the POST puts the supplier on the do-not-contact list every supplier path checks."""
from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from services.trade_min import directory as dr
from services.trade_min.schemas import CounterpartyRecord
from shared.orchestration import contact_guard as cg
from shared.orchestration import unsubscribe


@pytest.fixture(autouse=True)
def linked(monkeypatch):
    monkeypatch.setenv("PORTLESS_PUBLIC_BASE_URL", "https://api.portless.example")
    monkeypatch.setenv("UNSUBSCRIBE_LINK_SECRET", "s3cret-for-tests")
    dr.reset_directory()
    yield
    dr.reset_directory()


def _qs(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_supplier_link_is_signed_for_the_address():
    url = unsubscribe.supplier_link("Sales@Mill.in")
    q = _qs(url)
    assert q["d"] == unsubscribe.SUPPLIER and q["c"] == "sales@mill.in"
    assert unsubscribe.verify(q["d"], q["c"], q["sig"])
    assert unsubscribe.supplier_link(None) is None and unsubscribe.supplier_link("no-address") is None


def test_every_supplier_email_carries_it():
    from datetime import timedelta

    from services.sourcing.service import rfq_email_body
    from shared.orchestration import best_and_final, quote_reconfirm
    texts = [
        rfq_email_body("Mill", "Please quote.", "sales@mill.in"),
        quote_reconfirm.body("Mill", 900.0, datetime.now(UTC) + timedelta(days=3), "sales@mill.in"),
        best_and_final.body("Mill", 880.0, 20, "turmeric", "21 November", "sales@mill.in"),
    ]
    for text in texts:
        assert "https://api.portless.example/trade-min/unsubscribe?" in text
        assert "c=sales%40mill.in" in text


def test_without_a_link_the_reply_opt_out_stays(monkeypatch):
    monkeypatch.delenv("PORTLESS_PUBLIC_BASE_URL")
    line = unsubscribe.supplier_opt_out_line("sales@mill.in")
    assert "http" not in line and "not interested" in line


async def test_known_supplier_company_goes_on_the_list():
    now = datetime.now(UTC)
    store = await dr.get_directory()
    await store.upsert(CounterpartyRecord(id=dr.record_key("supplier", "Mill", "https://mill.in"), role="supplier",
                                          name="Mill", website="https://mill.in", email="info@mill.in",
                                          source="manual", first_seen_at=now, last_seen_at=now))
    assert not await cg.supplier_blocked("sales@mill.in")
    out = await unsubscribe.apply(unsubscribe.SUPPLIER, "sales@mill.in")
    assert out["do_not_contact"] and len(out["records"]) == 1
    assert await cg.supplier_blocked("sales@mill.in")
    assert await cg.supplier_blocked("info@mill.in")  # the whole company


async def test_unknown_free_mail_supplier_gets_a_record_for_its_address_only():
    out = await unsubscribe.apply(unsubscribe.SUPPLIER, "mill99@gmail.com")
    assert out["records"] == ["supplier:mill99@gmail.com"]
    assert await cg.supplier_blocked("mill99@gmail.com")
    assert not await cg.supplier_blocked("someone@gmail.com")


async def test_not_an_address_is_refused():
    with pytest.raises(LookupError):
        await unsubscribe.apply(unsubscribe.SUPPLIER, "nobody")


def test_post_through_the_public_door():
    from app import app
    url = unsubscribe.supplier_link("sales@newmill.in")
    path = urlparse(url).path + "?" + urlparse(url).query
    with TestClient(app) as client:
        page = client.get(path)
        assert page.status_code == 200 and "Unsubscribe" in page.text
        done = client.post(path)
        assert done.status_code == 200
        bad = client.post(path.replace("sig=", "sig=0"))
        assert bad.status_code == 403
