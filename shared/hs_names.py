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


def search(query: str, limit: int = 20) -> list[dict[str, str | None]]:
    """HS codes for the console's picker: by code prefix ("1006") or by words ("basmati",
    "chilli powder") over the curated names and the official descriptions. Curated
    8-digit lines and 6-digit subheadings first, then headings."""
    from shared import product_label as pl
    q = (query or "").strip().lower()
    if not q:
        return []
    table = _nomenclature()
    codes: list[str] = sorted({*table.keys(), *(k for k in pl._DISPLAY if len(k) == 8)})
    digits = _digits(q)
    words = [w for w in q.replace(",", " ").split() if not w.isdigit()]
    hits: list[tuple[int, str]] = []
    for code in codes:
        if len(code) < 4:
            continue
        if digits and not words:
            if code.startswith(digits) or digits.startswith(code):
                hits.append((0 if len(code) >= 6 else 1, code))
            continue
        text = f"{pl._DISPLAY.get(code, '')} {table.get(code, '')}".lower()
        if words and all(w in text for w in words) and (not digits or code.startswith(digits)):
            curated = 0 if code in pl._DISPLAY else 1
            hits.append((curated * 2 + (0 if len(code) >= 6 else 1), code))
    hits.sort(key=lambda h: (h[0], h[1]))
    return [{"code": c, "name": display_name(c), "description": official(c)} for _, c in hits[:limit]]

