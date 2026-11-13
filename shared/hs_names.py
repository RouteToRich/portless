"""HS code names for the console: every code gets a readable name.

Order: the catalogue's name for the product line → the curated short name
(``shared/product_label.py``: 8-, 6-, then 4-digit) → the official HS description of
the 6-digit subheading → the official heading / chapter. A curated 4-digit name is
used only for headings whose subheadings all fit it (0904 mixes pepper and chillies,
so its subheadings are curated one by one).
The official text (``data/hs_nomenclature.json``, from UN Comtrade's public HS
reference) is used only for display; e-mails keep the curated names, because the
official wording ("Alcohols; polyhydric, mannitol") reads badly in a letter.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA = Path(__file__).resolve().parents[1] / "data" / "hs_nomenclature.json"
_SHORT_MAX = 70


@lru_cache(maxsize=1)
def _nomenclature() -> dict[str, str]:
    try:
        data = json.loads(_DATA.read_text(encoding="utf-8"))
        data.pop("_source", None)
        return data
    except Exception as exc:  # noqa: BLE001 — names are a nicety; the code is still shown
        logger.warning("HS nomenclature not loaded from %s: %s", _DATA, exc)
        return {}


def _digits(code: str | None) -> str:
    return "".join(ch for ch in str(code or "") if ch.isdigit())


def official(code: str | None) -> str | None:
    """The official description of the most specific known level (6, then 4, then 2 digits)."""
    hs = _digits(code)
    table = _nomenclature()
    for size in (6, 4, 2):
        if len(hs) >= size and hs[:size] in table:
            return table[hs[:size]]
    return None


def _short(text: str) -> str:
    """The official text, category kept ("Cotton; not carded or combed"), at most 70
    characters — the full text goes in the console's tooltip."""
    part = text[:1].upper() + text[1:]
    if len(part) > _SHORT_MAX:
        part = part[:_SHORT_MAX].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return part


def display_name(code: str | None) -> str | None:
    """What the console shows next to an HS code; None only for a code no source knows."""
    hs = _digits(code)
    if not hs:
        return None
    from shared import product_label as pl
    try:
        from shared.catalogue import product_name_for
        name = product_name_for(hs)
        if name and name.strip() and not name.strip().isdigit():
            return name.strip()
    except Exception:  # noqa: BLE001
        pass
    curated = pl._DISPLAY.get(hs[:8]) or pl._DISPLAY.get(hs[:6]) or pl._DISPLAY.get(hs[:4])
    if curated:
        return curated
    text = official(hs)
    return _short(text) if text else None
