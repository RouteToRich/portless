"""Search in the market's language (todo 20.x: today every query is English — "dried onions
importers, DE").

Per buyer market, the words an importer's own website uses (``LOCAL_TERMS``), and the product's
name in that language from the catalogue (``ProductLine.local_names``: ``{"de": "Zwiebeln",
"ar": "بصل"}`` — entered by staff, never machine-guessed without a check). One extra search line
per market: ``"<local product name> <local terms>, CC"``. Without a local product name the
English keyword is kept with the local terms (a German page often names "basmati" in English but
calls itself "Importeur"). ``SEARCH_LOCAL_LANGUAGE`` (default on) switches it off. Pure.
"""
from __future__ import annotations

import os

LANGUAGE: dict[str, str] = {
    "DE": "de", "AT": "de", "CH": "de", "FR": "fr", "BE": "fr", "LU": "fr", "ES": "es", "MX": "es",
    "CO": "es", "CL": "es", "PE": "es", "AR": "es", "IT": "it", "NL": "nl", "PT": "pt", "BR": "pt",
    "AE": "ar", "SA": "ar", "QA": "ar", "OM": "ar", "KW": "ar", "BH": "ar", "EG": "ar", "JO": "ar",
    "MA": "fr", "DZ": "fr", "TN": "fr", "TR": "tr", "PL": "pl", "SE": "sv", "DK": "da", "RU": "ru",
    "ID": "id", "VN": "vi", "TH": "th", "JP": "ja", "CN": "zh", "KR": "ko",
}

LOCAL_TERMS: dict[str, str] = {
    "de": "Importeur Großhändler",
    "fr": "importateur grossiste",
    "es": "importador distribuidor",
    "it": "importatore grossista",
    "nl": "importeur groothandel",
    "pt": "importador distribuidor",
    "ar": "مستورد تاجر جملة",
    "tr": "ithalatçı toptancı",
    "pl": "importer hurtownia",
    "sv": "importör grossist",
    "da": "importør grossist",
    "ru": "импортер оптовик",
    "id": "importir distributor",
    "vi": "nhà nhập khẩu",
    "th": "ผู้นำเข้า",
    "ja": "輸入業者 卸売",
    "zh": "进口商 批发",
    "ko": "수입업체 도매",
}


def enabled() -> bool:
    return os.getenv("SEARCH_LOCAL_LANGUAGE", "true").strip().lower() in ("1", "true", "yes", "on")


def language_of(country: str | None) -> str | None:
    return LANGUAGE.get((country or "").strip().upper()[:2])


def local_name(hs_code: str | None, lang: str) -> str | None:
    if not hs_code:
        return None
    try:
        from shared import catalogue
        line = catalogue.line_for(hs_code)
        names = getattr(line, "local_names", None) or {}
        return (names.get(lang) or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


def local_buyer_line(keyword: str, country: str, hs_code: str | None = None) -> str | None:
    """The extra search line in the market's language, or None (English market / switched off)."""
    if not enabled():
        return None
    lang = language_of(country)
    terms = LOCAL_TERMS.get(lang or "")
    if not terms:
        return None
    product = local_name(hs_code, lang) or keyword
    return f"{product} {terms}, {country.strip().upper()[:2]}"
