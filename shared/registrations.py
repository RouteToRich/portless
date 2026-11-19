"""PortLess's own registrations — which ones a product needs, and when each one lapses (todo 3 / 14).

**Needed per product.** Exporting some goods needs the exporter's registration with a commodity
board, not only a shipment certificate: spices → Spices Board CRES, basmati / other APEDA
products → APEDA RCMC, seafood (HS 03) → MPEDA, tea / coffee / rubber / tobacco / cashew → their
boards, food → an FSSAI licence. ``REQUIRED_BY_HS`` (longest prefix) maps each to its kinds.

**Held, with expiry.** Each registration PortLess holds: kind, number, expiry — from
``PORTLESS_REGISTRATIONS`` (``"iec:0123456789:2027-06-30,lut:AD330424000123X:2027-03-31"``) and a
staff store (Mongo ``portless_registrations``, ``GET / PUT /deals/compliance/registrations``,
audited). The IEC must be updated every April – June, the LUT is per financial year, RCMCs last
five years, FSSAI licences have their own term.

Used by: the trade check (``registration_missing`` / ``registration_expired`` — block — for the
product's own registrations; a lapsed IEC / LUT blocks a new PI), readiness (fails once one has
lapsed) and the digest (warns ``REGISTRATION_WARN_DAYS`` (30) before).
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

KINDS: dict[str, str] = {
    "iec": "Importer-Exporter Code (DGFT)",
    "gstin": "GST registration",
    "lut": "Letter of Undertaking for zero-rated exports (current financial year)",
    "ad_code": "AD code registered with customs",
    "apeda_rcmc": "APEDA RCMC",
    "spices_board_cres": "Spices Board registration (CRES)",
    "mpeda": "MPEDA registration",
    "tea_board": "Tea Board registration",
    "coffee_board": "Coffee Board registration",
    "rubber_board": "Rubber Board registration",
    "tobacco_board": "Tobacco Board registration",
    "cepc": "Cashew Export Promotion Council RCMC",
    "fssai": "FSSAI licence",
    "fieo_rcmc": "FIEO RCMC",
}
ALWAYS = ("iec", "lut")
"""Needed for every export (a lapsed one blocks a new PI)."""

REQUIRED_BY_HS: dict[str, tuple[str, ...]] = {
    "03": ("mpeda",),
    "0801": ("cepc",),
    "0901": ("coffee_board",),
    "0902": ("tea_board",),
    "0904": ("spices_board_cres",), "0905": ("spices_board_cres",), "0906": ("spices_board_cres",),
    "0907": ("spices_board_cres",), "0908": ("spices_board_cres",), "0909": ("spices_board_cres",),
    "0910": ("spices_board_cres",),
    "1006": ("apeda_rcmc",),
    "0703": ("apeda_rcmc",), "0701": ("apeda_rcmc",), "0803": ("apeda_rcmc",), "0804": ("apeda_rcmc",),
    "2401": ("tobacco_board",),
    "4001": ("rubber_board",),
}
FOOD_CHAPTERS = range(2, 22)
"""HS chapters 02–21 are food: an FSSAI licence too."""

_memory: dict[str, dict[str, Any]] = {}
_cache: dict[str, dict[str, Any]] = {}
"""What the last store read saw — readiness (sync) reads it with the setting."""


def required_for(hs_code: str | None) -> list[str]:
    hs = "".join(ch for ch in str(hs_code or "") if ch.isdigit())
    out: list[str] = []
    for n in range(len(hs), 1, -1):
        hit = REQUIRED_BY_HS.get(hs[:n])
        if hit:
            out.extend(hit)
            break
    if hs[:2].isdigit() and int(hs[:2]) in FOOD_CHAPTERS and "fssai" not in out:
        out.append("fssai")
    return out


def _parse_setting() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for part in os.getenv("PORTLESS_REGISTRATIONS", "").split(","):
        bits = [b.strip() for b in part.split(":")]
        if len(bits) >= 2 and bits[0] in KINDS:
            out[bits[0]] = {"kind": bits[0], "number": bits[1], "expires_on": bits[2] if len(bits) > 2 and bits[2] else None,
                            "source": "setting PORTLESS_REGISTRATIONS"}
    return out


def _collection() -> Any:
    try:
        from shared.mongo import collection
        return collection("portless_registrations")
    except Exception:  # noqa: BLE001
        return None


async def held() -> dict[str, dict[str, Any]]:
    """Every registration PortLess holds: the staff store over the setting."""
    out = _parse_setting()
    col = _collection()
    if col is not None:
        try:
            async for d in col.find({}):
                d.pop("_id", None)
                out[d["kind"]] = d
        except Exception as exc:  # noqa: BLE001
            logger.warning("Registrations: store unreadable: %s", exc)
    else:
        out.update(_memory)
    _cache.clear()
    _cache.update(out)
    return out


def cached() -> dict[str, dict[str, Any]]:
    """Setting + the last store read (no I/O): for the sync readiness report."""
    return {**_parse_setting(), **_memory, **_cache}


async def record(kind: str, number: str, expires_on: str | None, *, by: str, source: str = "staff") -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if not (number or "").strip():
        raise ValueError("the registration number is required")
    if expires_on:
        try:
            date.fromisoformat(expires_on)
        except ValueError as exc:
            raise ValueError("expires_on must be an ISO date (YYYY-MM-DD)") from exc
    row = {"kind": kind, "number": number.strip(), "expires_on": expires_on, "recorded_by": by,
           "recorded_at": datetime.now(timezone.utc).isoformat(), "source": source}
    col = _collection()
    if col is None:
        _memory[kind] = row
    else:
        await col.replace_one({"kind": kind}, dict(row), upsert=True)
    _cache[kind] = row
    try:
        from shared import audit_log
        await audit_log.record("registration_recorded", by, kind, {"number": row["number"], "expires_on": expires_on})
    except Exception:  # noqa: BLE001
        pass
    return row


def _days_left(row: dict[str, Any], today: date) -> int | None:
    raw = row.get("expires_on")
    if not raw:
        return None
    try:
        return (date.fromisoformat(str(raw)[:10]) - today).days
    except ValueError:
        return None


def warn_days() -> int:
    try:
        return max(1, int(os.getenv("REGISTRATION_WARN_DAYS", "30")))
    except ValueError:
        return 30


def assess(kinds: list[str], have: dict[str, dict[str, Any]], today: date | None = None) -> list[dict[str, Any]]:
    """Pure: per needed kind — ``ok`` / ``missing`` / ``expired`` / ``expiring`` (days left)."""
    today = today or datetime.now(timezone.utc).date()
    out = []
    for k in kinds:
        row = have.get(k)
        if row is None:
            out.append({"kind": k, "label": KINDS.get(k, k), "status": "missing"})
            continue
        left = _days_left(row, today)
        status = "expired" if left is not None and left < 0 else "expiring" if left is not None and left <= warn_days() else "ok"
        out.append({"kind": k, "label": KINDS.get(k, k), "status": status, "number": row.get("number"),
                    "expires_on": row.get("expires_on"), "days_left": left})
    return out


async def check_for(hs_code: str | None, today: date | None = None) -> list[dict[str, Any]]:
    """What a deal of this product needs from PortLess's registrations, assessed."""
    return assess([*ALWAYS, *required_for(hs_code)], await held(), today)


async def expiring(today: date | None = None) -> list[dict[str, Any]]:
    """Every held registration that lapsed or lapses within the warning window (digest, readiness)."""
    have = await held()
    return [r for r in assess(list(have), have, today) if r["status"] in ("expired", "expiring")]


def reset_for_tests() -> None:
    _memory.clear()
    _cache.clear()
