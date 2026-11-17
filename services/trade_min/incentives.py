"""Export incentives per HS code — RoDTEP and duty drawback as income in the radar's profit.

India refunds part of the taxes embedded in exported goods: RoDTEP (DGFT, Appendix 4R,
a % of FOB with a cap per unit) and the all-industry duty drawback (CBIC schedule, a % of
FOB). For a merchant exporter they are often 1–3 % of FOB — enough to move a deal over
the margin floor — but eligibility depends on the product, the exporter and how the
goods are procured, so a rate counts as income only once staff have confirmed it
(``eligible_confirmed``, with who confirmed it). Rates are entered from the official
schedules (``PUT /trade-min/incentives/{hs}``); nothing is guessed.

Stored in Mongo ``export_incentives`` next to the opportunities, else in memory.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_COLLECTION = "export_incentives"
_memory: dict[str, "ExportIncentive"] = {}


class ExportIncentive(BaseModel):
    hs_code: str = Field(min_length=4, max_length=10)
    """6- or 8-digit ITC-HS code (a shorter code covers the longer ones)."""
    rodtep_pct: float = Field(default=0.0, ge=0.0, le=20.0)
    rodtep_cap_usd_per_mt: float | None = Field(default=None, ge=0.0)
    """RoDTEP cap per unit, converted to USD per MT (Appendix 4R caps are per kg / unit)."""
    drawback_pct: float = Field(default=0.0, ge=0.0, le=20.0)
    drawback_cap_usd_per_mt: float | None = Field(default=None, ge=0.0)
    eligible_confirmed: bool = False
    """Only a confirmed entry is counted as income in the profit."""
    confirmed_by: str | None = None
    source: str | None = None
    """Where the rate comes from: e.g. "DGFT Appendix 4R, notification 12/2026"."""
    note: str | None = None
    updated_at: datetime | None = None


def reset_for_tests() -> None:
    _memory.clear()


def _digits(hs: str) -> str:
    return "".join(ch for ch in str(hs or "") if ch.isdigit())


async def _collection():
    try:
        from .repository import MongoOpportunityRepository, get_repository
        repo = await get_repository()
        if isinstance(repo, MongoOpportunityRepository):
            return repo._col.database[_COLLECTION]  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Incentives: no Mongo collection: %s", exc)
    return None


async def set_entry(entry: ExportIncentive) -> ExportIncentive:
    entry.hs_code = _digits(entry.hs_code)
    entry.updated_at = datetime.now(timezone.utc)
    _memory[entry.hs_code] = entry
    col = await _collection()
    if col is not None:
        doc = entry.model_dump(mode="json")
        doc["_id"] = entry.hs_code
        await col.replace_one({"_id": entry.hs_code}, doc, upsert=True)
    return entry


async def list_entries() -> list[ExportIncentive]:
    col = await _collection()
    if col is not None:
        try:
            docs = await col.find({}).to_list(length=2000)
            out = []
            for d in docs:
                d.pop("_id", None)
                out.append(ExportIncentive.model_validate(d))
            return sorted(out, key=lambda e: e.hs_code)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Incentives: read failed: %s", exc)
    return sorted(_memory.values(), key=lambda e: e.hs_code)


async def entry_for(hs_code: str) -> ExportIncentive | None:
    """The most specific entry covering the code (8 → 6 → 4 digits)."""
    hs = _digits(hs_code)
    entries = {e.hs_code: e for e in await list_entries()}
    for n in (8, 6, 4):
        if len(hs) >= n and hs[:n] in entries:
            return entries[hs[:n]]
    return None


def income_per_mt(entry: ExportIncentive | None, fob_usd_per_mt: float | None) -> tuple[float | None, str | None]:
    """(USD per MT, basis) — None unless the entry is confirmed and a FOB price exists."""
    if entry is None or not fob_usd_per_mt or fob_usd_per_mt <= 0:
        return None, None
    if not entry.eligible_confirmed:
        return None, f"HS {entry.hs_code} rates on file but eligibility not confirmed — not counted"
    rodtep = fob_usd_per_mt * entry.rodtep_pct / 100.0
    if entry.rodtep_cap_usd_per_mt is not None:
        rodtep = min(rodtep, entry.rodtep_cap_usd_per_mt)
    drawback = fob_usd_per_mt * entry.drawback_pct / 100.0
    if entry.drawback_cap_usd_per_mt is not None:
        drawback = min(drawback, entry.drawback_cap_usd_per_mt)
    total = round(rodtep + drawback, 2)
    if total <= 0:
        return None, None
    parts = []
    if rodtep:
        parts.append(f"RoDTEP {entry.rodtep_pct:g}%")
    if drawback:
        parts.append(f"drawback {entry.drawback_pct:g}%")
    return total, f"{' + '.join(parts)} of FOB (HS {entry.hs_code}; {entry.source or 'official schedule'})"


def income_per_mt_value(income_at_fob: float | None, fob: float, base_fob: float | None) -> float:
    """The income per MT scaled to another FOB price (it is a % of FOB), 0 when none."""
    if not income_at_fob or not base_fob:
        return 0.0
    return income_at_fob * fob / base_fob
