"""Human product name for an HS code — what a counterparty reads in an e-mail.

The catalogue's ``name`` is the operator's word for the product line and wins.
Otherwise a curated display map (deliberately NOT ``trade_intel._HS_TO_KEYWORD``:
those are search keywords like "glycerol alcohol" or "ground pepper spices",
which read badly in a letter). ``None`` when nothing is known — callers then
fall back to ``"the HS <code> product"`` instead of inventing a name.
"""
from __future__ import annotations

# 8-digit (Indian ITC-HS tariff line) entries are looked up first, then 6-digit, then the 4-digit heading.
_DISPLAY: dict[str, str] = {
    "10063010": "parboiled rice",
    "10063020": "basmati rice",
    "10063090": "non-basmati rice",
    "100610": "paddy rice",
    "100620": "brown rice",
    "100640": "broken rice",
    "090411": "black pepper",
    "090412": "ground black pepper",
    "090421": "dried chillies",
    "090422": "chilli powder",
    "0904": "pepper",
    "0902": "tea",
    "0901": "coffee",
    "0908": "nutmeg, mace and cardamom",
    "0909": "cumin seeds",
    "0910": "spices",
    "1001": "wheat",
    "1005": "maize",
    "1006": "rice",
    "100630": "milled rice",
    "1207": "sesame seeds",
    "0801": "cashew nuts",
    "0703": "onions",
    "1701": "sugar",
    "1513": "coconut oil",
    "1515": "vegetable oil",
    "1520": "crude glycerine",
    "290543": "mannitol",
    "290544": "sorbitol",
    "290545": "refined glycerine",
    "0306": "shrimp",
    "5201": "raw cotton",
    "5205": "cotton yarn",
    "6109": "cotton T-shirts",
    "3004": "pharmaceutical formulations",
}


def product_label(hs_code: str | None) -> str | None:
    hs = str(hs_code or "").strip()
    if not hs:
        return None
    try:
        from shared.catalogue import product_name_for
        name = product_name_for(hs)
        if name and name.strip() and not name.strip().isdigit():
            return name.strip()
    except Exception:  # noqa: BLE001 — catalogue not loaded / unavailable
        pass
    return _DISPLAY.get(hs[:8]) or _DISPLAY.get(hs[:6]) or _DISPLAY.get(hs[:4])


def product_phrase(hs_code: str | None) -> str:
    """``"crude glycerine"`` or, when unknown, ``"the HS 1520 product"``."""
    return product_label(hs_code) or f"the HS {hs_code} product"


def product_title(hs_code: str | None) -> str:
    """Subject-line form: ``"Crude glycerine"`` / ``"HS 1520"`` (never ``"Hs 1520"``)."""
    label = product_label(hs_code)
    if not label:
        return f"HS {hs_code}"
    return label[:1].upper() + label[1:]
