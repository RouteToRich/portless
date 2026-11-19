"""How much a buyer buys (todo 15.4) — shown, and used before the one-container default.

- **Recorded / stated**: import records (``annual_volume_mt`` / ``shipments_12m`` on the buyer
  signal), a quantity the buyer stated (``quantity`` in MT) or a reply's quantity (product facts).
  ``volume_line`` → "Buys ~480 MT/yr · ships every ~6 weeks (apify_us_importers)".
- **Order-size band** when nothing is stated: ``small`` / ``medium`` / ``large`` from company
  signals (product lines it lists, words on its site such as branches / warehouses / chain, the
  market's import value ÷ the importers found). Labelled an estimate and never used as an
  offer quantity.
- **Lot**: ``stated_order_mt`` — the median order of the opportunity's buyers that state or
  record one; the radar's lot and a deal's default quantity use it before one container.
"""
from __future__ import annotations

import re
import statistics
from typing import Any

_BIG = re.compile(r"\b(branches|warehouses?|distribution cent(?:er|re)s?|nationwide|chain|largest|leading|"
                  r"supermarkets?|hypermarkets?|group of companies|subsidiar(?:y|ies)|tonnes? per (?:month|year))\b",
                  re.IGNORECASE)
_SMALL = re.compile(r"\b(family[- ]run|shop|corner store|boutique|small business|home[- ]made|online store)\b",
                    re.IGNORECASE)


def order_mt(annual_volume_mt: float | None, shipments_12m: int | None) -> float | None:
    if annual_volume_mt and shipments_12m:
        return round(float(annual_volume_mt) / max(1, int(shipments_12m)), 1)
    return None


def volume_line(annual_volume_mt: float | None, shipments_12m: int | None, source: str | None = None) -> str | None:
    if not annual_volume_mt and not shipments_12m:
        return None
    parts = []
    if annual_volume_mt:
        parts.append(f"Buys ~{annual_volume_mt:,.0f} MT/yr")
    if shipments_12m:
        weeks = 52.0 / max(1, int(shipments_12m))
        parts.append(f"ships every ~{weeks:.0f} week{'s' if round(weeks) != 1 else ''}")
    return " · ".join(parts) + (f" ({source})" if source else "")


def order_size_band(*, text: str | None = None, product_lines: int = 0, market_import_value_usd: float | None = None,
                    importers_found: int = 0, annual_volume_mt: float | None = None) -> str | None:
    """small / medium / large — an estimate from company signals (None when there is nothing)."""
    if annual_volume_mt:
        return "small" if annual_volume_mt < 200 else "medium" if annual_volume_mt < 2000 else "large"
    points = 0
    seen = False
    if text:
        if _BIG.search(text):
            points += 1
            seen = True
        if _SMALL.search(text):
            points -= 1
            seen = True
    if product_lines:
        seen = True
        points += 1 if product_lines >= 5 else 0
    if market_import_value_usd and importers_found:
        seen = True
        per_importer = market_import_value_usd / importers_found
        points += 1 if per_importer >= 20_000_000 else -1 if per_importer < 1_000_000 else 0
    if not seen:
        return None
    return "large" if points >= 2 else "medium" if points >= 0 else "small"


def _stated(b: Any) -> float | None:
    q = getattr(b, "quantity", None)
    unit = (getattr(b, "quantity_unit", None) or "MT").upper()
    if q and unit in ("MT", "T", "TON", "TONS", "TONNE", "TONNES"):
        return float(q)
    if q and unit in ("KG", "KGS"):
        return float(q) / 1000.0
    return order_mt(getattr(b, "annual_volume_mt", None), getattr(b, "shipments_12m", None))


def stated_order_mt(buyers: list[Any]) -> tuple[float | None, str | None]:
    """Median order size of the buyers that state or record one, and what it rests on."""
    sizes = [x for x in (_stated(b) for b in buyers or []) if x and 0.5 <= x <= 20000]
    if not sizes:
        return None, None
    return round(statistics.median(sizes), 1), f"order size stated / recorded by {len(sizes)} buyer(s)"
