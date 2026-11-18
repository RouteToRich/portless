"""Buyer search in the market's language (shared/search_terms.py)."""
from __future__ import annotations

from shared.connectors.apify import ApifyCompanySearchConnector
from shared.search_terms import language_of, local_buyer_line


def test_local_line_per_market(monkeypatch):
    assert local_buyer_line("dried onions", "DE") == "dried onions Importeur Großhändler, DE"
    assert local_buyer_line("rice", "es") == "rice importador distribuidor, ES"
    assert local_buyer_line("rice", "GB") is None and local_buyer_line("rice", "US") is None
    assert language_of("AE") == "ar"
    monkeypatch.setenv("SEARCH_LOCAL_LANGUAGE", "false")
    assert local_buyer_line("rice", "DE") is None


def test_catalogue_local_name_used(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr("shared.catalogue.line_for", lambda hs: SimpleNamespace(local_names={"de": "Zwiebeln"}))
    assert local_buyer_line("dried onions", "DE", "07122000") == "Zwiebeln Importeur Großhändler, DE"
    assert local_buyer_line("dried onions", "FR", "07122000") == "dried onions importateur grossiste, FR"


def test_build_queries_adds_it_for_buyers_only():
    buyer = ApifyCompanySearchConnector.build_queries("chilli", ["FR", "GB"], "buyer")
    assert buyer == ["chilli importers, FR", "chilli wholesale distributors, FR", "chilli importateur grossiste, FR",
                     "chilli importers, GB", "chilli wholesale distributors, GB"]
    supplier = ApifyCompanySearchConnector.build_queries("chilli", ["IN"], "supplier")
    assert supplier == ["chilli exporter, IN", "chilli manufacturer, IN"]
