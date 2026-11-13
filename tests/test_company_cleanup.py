"""Clean-up of company-search results and stored buyers / suppliers: names, e-mails,
countries (shared/connectors/apify.py, services/trade_min/directory.py)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.trade_min import directory as dr
from services.trade_min.schemas import CounterpartyRecord
from shared.connectors import apify as a

NOW = datetime.now(timezone.utc)


@pytest.mark.parametrize("raw,clean", [
    ("For over a decade, Punjab Foodstuff Trading LLC", "Punjab Foodstuff Trading LLC"),
    ("Since 1985, Acme Rice Mills", "Acme Rice Mills"),
    ("Established in 2001, Vasu Exports Pvt Ltd", "Vasu Exports Pvt Ltd"),
    ("21, Warsan 1, Dubai, UAE, PO BOX 455012 Arham Foodstuff Trading L.L.C", "Arham Foodstuff Trading L.L.C"),
    ("At Pavit Group, we deliver quality", "At Pavit Group, we deliver quality"),   # never "we deliver quality"
    ("Since 1985, we export rice", "Since 1985, we export rice"),
    ("Assam Tea Co, Kolkata", "Assam Tea Co, Kolkata"),
])
def test_marketing_clauses_are_dropped_from_names(raw, clean):
    assert a._clean_name(raw) == clean


@pytest.mark.parametrize("raw,clean", [
    ("38844info@ricemasterglobal.com", "info@ricemasterglobal.com"),
    ("mailto:Sales@Acme.AE", "sales@acme.ae"),
    ("viji_krishnan2003@yahoo.co.in", "viji_krishnan2003@yahoo.co.in"),
    ("logo@2x.png", None),
    ("user@example.com", None),
    ("info@sentry.wixpress.com", None),
    ("not-an-email", None),
    ("", None),
])
def test_emails_are_cleaned_or_refused(raw, clean):
    assert a.clean_email(raw) == clean


@pytest.mark.parametrize("row,searched,country", [
    ({"domain": "nutriboostsuperfoods.com", "email": "info@nutriboost.nl"}, "AE", "NL"),   # e-mail ccTLD
    ({"domain": "acme.co.in", "email": "x@gmail.com"}, "AE", "IN"),                       # website ccTLD
    ({"domain": "acme.com", "email": "x@acme.com", "phone": "+971 4 123 4567"}, None, "AE"),
    ({"domain": "acme.com", "email": "x@acme.com", "phone": "0091 98400 12345"}, None, "IN"),
    ({"domain": "acme.com", "email": "x@gmail.com"}, "AE", "AE"),                          # nothing better: searched
    ({"domain": "acme.com", "email": "x@gmail.com"}, None, None),
])
def test_country_comes_from_the_company_before_the_searched_market(row, searched, country):
    assert a.company_country(row, searched) == country


def test_refine_companies_cleans_and_drops_unreachable_rows():
    rows = a.refine_companies([
        {"name": "For over a decade, Punjab Foodstuff Trading LLC", "domain": "punjabfoodstuff.com",
         "email": "38844info@punjabfoodstuff.com", "country": "AE"},
        {"name": "Nutriboost", "domain": "nutriboostsuperfoods.com", "email": "info@nutriboost.nl", "country": "AE"},
        {"name": "Ghost Co", "domain": "ghost.com", "email": "logo@2x.png", "emails": ["x@example.com"]},
        {"name": "Backup Mail Co", "domain": "backup.com", "email": "bad", "emails": ["sales@backup.com"]},
    ])
    by = {r["domain"]: r for r in rows}
    assert by["punjabfoodstuff.com"]["email"] == "info@punjabfoodstuff.com"
    assert by["nutriboostsuperfoods.com"]["country"] == "NL" and by["nutriboostsuperfoods.com"]["searched_country"] == "AE"
    assert "ghost.com" not in by                                   # no usable e-mail and no phone
    assert by["backup.com"]["email"] == "sales@backup.com"         # falls back to the next good address


def _rec(**kw):
    base = dict(id="buyer:x", role="buyer", name="Acme", country="AE", source="apify_company_search",
                first_seen_at=NOW, last_seen_at=NOW)
    return CounterpartyRecord(**{**base, **kw})


def test_a_stored_record_is_cleaned_on_save():
    rec = dr.clean_record(_rec(name="For over a decade, Punjab Foodstuff Trading LLC",
                               email="38844info@punjabfoodstuff.com", website="https://punjabfoodstuff.com/"))
    assert (rec.name, rec.email, rec.country) == ("Punjab Foodstuff Trading LLC", "info@punjabfoodstuff.com", "AE")
    nl = dr.clean_record(_rec(email="info@nutriboost.nl", website="https://nutriboostsuperfoods.com/"))
    assert nl.country == "NL"
    same = _rec(email="info@acme.com", website="https://acme.com/")
    assert dr.clean_record(same) is same                           # nothing to change → same object


async def test_existing_records_are_cleaned_in_place(monkeypatch):
    store = dr.InMemoryDirectory()
    monkeypatch.setattr(dr, "get_directory", lambda: _store(store))
    store._store["buyer:p"] = _rec(id="buyer:p", name="For over a decade, Punjab Foodstuff Trading LLC",
                                   email="38844info@punjabfoodstuff.com")
    store._store["buyer:ok"] = _rec(id="buyer:ok", email="info@acme.com")
    assert await dr.clean_existing() == 1
    assert store._store["buyer:p"].name == "Punjab Foodstuff Trading LLC" and store._store["buyer:p"].id == "buyer:p"


async def _store(s):
    return s
