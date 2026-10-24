"""Product catalogue + corridors — what PortLess actually sells, and where.

Until now the scanner's HS codes and markets were constants in
``services/trade_min/config.py`` and the actors' product keywords came from a
static map in ``shared/connectors/trade_intel.py``. The catalogue is the
operator-editable version of both (Catalogue page, ``GET/PUT
/trade-min/catalogue``), persisted in Mongo (``catalogue`` collection) with an
in-memory fallback:

  ProductLine
    hs_code            6-digit HS code (the scan key)
    name               plain product name used as the search keyword
    products           bill-of-lading words the buyer actors search for
    markets            ISO-2 destination markets for this product
    seller_profile     what the Alibaba RFQ actor tells buyers about PortLess
    default_quantity_mt / min_order_mt
    active             inactive lines are kept but not scanned

With at least one active line, ``POST /trade-min/scan`` without explicit
codes/markets scans the catalogue, Phase 2/4 keywords come from it, and the
Apify inputs (``products``, ``sellerProfile``, ``goalDescription``) are built
from it. Empty catalogue → the config defaults keep working.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_COLLECTION = "catalogue"
_DOC_ID = "global"


class ProductLine(BaseModel):
    product_id: str
    hs_code: str = Field(min_length=4, max_length=10)
    name: str
    products: list[str] = Field(default_factory=list)
    markets: list[str] = Field(default_factory=list)
    seller_profile: str | None = None
    default_quantity_mt: float | None = None
    min_order_mt: float | None = None
    active: bool = True
    notes: str | None = None


class Catalogue(BaseModel):
    products: list[ProductLine] = Field(default_factory=list)
    updated_by: str | None = None
    updated_at: datetime | None = None


_state = Catalogue()
_loaded = False


def _mongo_collection():
    try:
        from shared.config import settings
        if not settings.mongo_uri:
            return None
        import motor.motor_asyncio as motor  # type: ignore[import]
        client: Any = motor.AsyncIOMotorClient(settings.mongo_uri)
        return client[settings.mongo_db_name][_COLLECTION]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Catalogue: Mongo unavailable (%s) — in-memory only", exc)
        return None


async def load_catalogue() -> Catalogue:
    global _state, _loaded
    col = _mongo_collection()
    if col is not None:
        try:
            doc = await col.find_one({"_id": _DOC_ID})
            if doc:
                doc.pop("_id", None)
                _state = Catalogue(**doc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Catalogue: could not load: %s", exc)
    _loaded = True
    if _state.products:
        logger.info("Catalogue: %d product line(s) loaded", len(_state.products))
    return _state


def get_catalogue() -> Catalogue:
    return _state


async def set_catalogue(products: list[ProductLine] | list[dict[str, Any]], updated_by: str = "operator") -> Catalogue:
    global _state
    lines: list[ProductLine] = []
    seen: set[str] = set()
    for raw in products:
        line = raw if isinstance(raw, ProductLine) else ProductLine(**raw)
        line.hs_code = "".join(ch for ch in line.hs_code if ch.isdigit())
        if len(line.hs_code) < 4:
            raise ValueError(f"HS code {raw!r} must have at least 4 digits")
        line.markets = sorted({m.strip().upper()[:2] for m in line.markets if m and m.strip()})
        line.products = [p.strip() for p in line.products if p and p.strip()]
        if not line.name.strip():
            raise ValueError(f"product line {line.product_id} needs a name")
        if line.product_id in seen:
            raise ValueError(f"duplicate product_id {line.product_id}")
        seen.add(line.product_id)
        lines.append(line)
    _state = Catalogue(products=lines, updated_by=updated_by, updated_at=datetime.now(timezone.utc))
    col = _mongo_collection()
    if col is not None:
        try:
            await col.replace_one({"_id": _DOC_ID}, {"_id": _DOC_ID, **_state.model_dump(mode="json")}, upsert=True)
        except Exception as exc:  # noqa: BLE001
            logger.error("Catalogue: persisting failed (kept in memory): %s", exc)
    logger.info("Catalogue: %d product line(s) saved by %s", len(lines), updated_by)
    return _state


# ---------------------------------------------------------------------------
# Lookups used by the scanner, the actors and the outreach e-mails
# ---------------------------------------------------------------------------

def active_lines() -> list[ProductLine]:
    return [p for p in _state.products if p.active]


def line_for(hs_code: str) -> ProductLine | None:
    hs = (hs_code or "").strip()
    best: ProductLine | None = None
    for p in _state.products:
        if hs == p.hs_code or hs.startswith(p.hs_code) or p.hs_code.startswith(hs[:4]):
            if best is None or len(p.hs_code) > len(best.hs_code):
                best = p
    return best


def product_name_for(hs_code: str) -> str | None:
    line = line_for(hs_code)
    return line.name if line else None


def products_for(hs_code: str) -> list[str]:
    line = line_for(hs_code)
    if not line:
        return []
    return list(line.products) or [line.name]


def seller_profile_for(hs_code: str) -> str | None:
    line = line_for(hs_code)
    return line.seller_profile if line else None


def default_quantity_for(hs_code: str) -> float | None:
    line = line_for(hs_code)
    return line.default_quantity_mt if line else None


def scan_defaults() -> tuple[list[str], list[str]] | None:
    """(hs_codes, markets) from the active catalogue, or None when empty."""
    lines = active_lines()
    if not lines:
        return None
    codes = sorted({p.hs_code for p in lines})
    markets = sorted({m for p in lines for m in p.markets})
    return codes, markets


def corridors() -> list[dict[str, Any]]:
    return [
        {"hs_code": p.hs_code, "name": p.name, "market": m}
        for p in active_lines() for m in (p.markets or [])
    ]


def reset_for_tests() -> None:
    global _state, _loaded
    _state = Catalogue()
    _loaded = False
