"""Data-quality checks on the buyers and suppliers a scan found.

A web search returns whatever ranks for "<product> importers, DE": sometimes an
Indian exporter's German landing page, or a spice mill that makes a different
spice. These checks never drop a company (the directory keeps everyone); they
put a flag on it that pair matching, the match seed and the radar respect:

* ``role_suspect`` (buyer) — the company looks like an Indian exporter, not an
  importer in the market: Indian legal form (LLP / Pvt Ltd / Private Limited),
  an Indian domain or phone, or a site about exporting from India. A buyer pair
  with it is blocked and it never enters the match seed.
* ``product_mismatch`` (supplier) — its own site names another product of the
  same family (cumin on a chilli corridor) and never this one. Product fit drops
  and the pair says why.

The supplier's town / state is read from its address (PIN code, town, state) so
the port distance and logistics score work.
"""
from __future__ import annotations

import re
from typing import Iterable

from .schemas import BuyerSignal, SellerCandidate

ROLE_SUSPECT = "role_suspect"
WRONG_MARKET = "wrong_market"
PRODUCT_MISMATCH = "product_mismatch"
NAME_FROM_DOMAIN = "name_from_domain"

# Products PortLess trades that a search confuses with each other. Each entry:
# the words that name it on a company's site.
PRODUCT_WORDS: dict[str, tuple[str, ...]] = {
    "pepper": ("pepper", "peppercorn", "piper nigrum"),
    "chilli": ("chilli", "chili", "chillies", "capsicum", "paprika", "red pepper", "mirchi"),
    "cumin": ("cumin", "jeera"),
    "turmeric": ("turmeric", "haldi", "curcuma"),
    "coriander": ("coriander", "dhania"),
    "cardamom": ("cardamom", "elaichi"),
    "fennel": ("fennel", "saunf"),
    "fenugreek": ("fenugreek", "methi"),
    "mustard": ("mustard",),
    "clove": ("clove",),
    "cinnamon": ("cinnamon", "cassia"),
    "saffron": ("saffron", "kesar"),
    "ginger": ("ginger",),
    "garlic": ("garlic",),
    "onion": ("onion",),
    "rice": ("rice", "basmati", "paddy"),
    "wheat": ("wheat",),
    "sesame": ("sesame", "til seed"),
    "cashew": ("cashew",),
    "tea": (" tea ", "tea leaves", "black tea", "green tea"),
    "coffee": ("coffee",),
    "sugar": ("sugar",),
    "glycerine": ("glycerine", "glycerin", "glycerol"),
}

# Words of a "red pepper" / "black pepper" overlap that must not count as pepper.
_RED_PEPPER = re.compile(r"\bred\s+peppers?\b", re.IGNORECASE)

_INDIAN_FORM = re.compile(r"\b(?:llp|pvt\.?|private\s+limited|\(opc\))\b", re.IGNORECASE)
_EXPORTER_NAME = re.compile(r"\bexporters?\b", re.IGNORECASE)
_EXPORTER_TEXT = re.compile(
    r"\b(?:exporters?|manufacturers?)\b[^.]{0,40}\b(?:india|indian)\b|\b(?:india|indian)\b[^.]{0,40}\bexporters?\b",
    re.IGNORECASE)


def _contains(text: str, word: str) -> bool:
    w = word.strip()
    if not w:
        return False
    return re.search(rf"(?<![a-z]){re.escape(w)}", text) is not None


def products_named(text: str) -> set[str]:
    """Which known products a piece of text names."""
    low = f" {(text or '').lower()} "
    found: set[str] = set()
    for product, words in PRODUCT_WORDS.items():
        if product == "pepper":
            stripped = _RED_PEPPER.sub(" ", low)
            if any(_contains(stripped, w) for w in words):
                found.add(product)
            continue
        if any(_contains(low, w) if w.strip() == w else w in low for w in words):
            found.add(product)
    return found


def corridor_products(keyword: str, extra: Iterable[str] = ()) -> set[str]:
    """The known products the corridor's search keyword (and catalogue words) name."""
    return products_named(" ".join([keyword or "", *extra]))


def is_product_mismatch(text: str, corridor: set[str]) -> bool:
    """The company's text names other known products and none of the corridor's."""
    if not corridor or not (text or "").strip():
        return False
    named = products_named(text)
    return bool(named) and not (named & corridor)


def looks_like_indian_exporter(b: BuyerSignal, market: str) -> bool:
    """A "buyer" in a foreign market that is really an Indian exporter."""
    if (market or "").upper() == "IN":
        return False
    domain = (b.company_id or b.website or "").lower()
    domain = re.sub(r"^https?://", "", domain).split("/")[0]
    if domain.endswith((".in", ".co.in")):
        return True
    phone = re.sub(r"[^\d+]", "", b.phone or "")
    if phone.startswith("+91"):
        return True
    if _INDIAN_FORM.search(b.buyer_name or ""):
        return True
    if _EXPORTER_NAME.search(b.buyer_name or ""):
        return True  # "Hexa Global Exporters" pitched as a German importer
    return bool(_EXPORTER_TEXT.search(" ".join(x for x in (b.buyer_name, b.fit_reason) if x)))


def _add_flag(flags: list[str], flag: str) -> None:
    if flag not in flags:
        flags.append(flag)


def check_buyers(buyers: list[BuyerSignal], market: str) -> int:
    """Flag buyers that look like exporters, and buyers based outside the market they
    were searched for; returns how many were flagged as exporters."""
    n = 0
    for b in buyers:
        if looks_like_indian_exporter(b, market):
            _add_flag(b.flags, ROLE_SUSPECT)
            n += 1
        country = (b.buyer_country or "").upper()
        if market and country and country != market.upper() and market.upper() != "IN":
            _add_flag(b.flags, WRONG_MARKET)
        if b.name_source == "domain":
            _add_flag(b.flags, NAME_FROM_DOMAIN)
    return n


def check_sellers(sellers: list[SellerCandidate], keyword: str, extra: Iterable[str] = ()) -> int:
    """Flag sellers about another product, fill town / state / port km from the address."""
    corridor = corridor_products(keyword, extra)
    n = 0
    for s in sellers:
        text = " ".join(x for x in (s.company_name, s.fit_reason, s.website) if x)
        if is_product_mismatch(text, corridor):
            _add_flag(s.flags, PRODUCT_MISMATCH)
            n += 1
        if s.name_source == "domain":
            _add_flag(s.flags, NAME_FROM_DOMAIN)
        locate(s)
    return n


def locate(s: SellerCandidate) -> None:
    """Town / state and port distance from the supplier's address, when it has one."""
    if not s.address or (s.location_state and s.supplier_to_port_km is not None):
        return
    try:
        from shared.orchestration.trade_costs import locate_supplier
        route = locate_supplier(s.address)
    except Exception:  # noqa: BLE001 - a lookup never breaks a scan
        return
    if route.state and not s.location_state:
        s.location_state = route.state
    if not s.location_city:
        m = re.search(r"([A-Z][A-Za-z .]+?)\s*[-,]?\s*[1-8]\d{5}\b", s.address)
        if m:
            s.location_city = m.group(1).strip(" ,.-").split(",")[-1].strip()
    if route.km is not None and s.supplier_to_port_km is None:
        s.supplier_to_port_km = route.km
