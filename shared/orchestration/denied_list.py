"""DGFT denied-entity list (DEF-17): the real list, not a stub that always says "not denied".

DGFT publishes the list as a document (dgft.gov.in → Denied Entity List), not an API. Staff
upload a CSV copy (``PUT /deals/compliance/dgft-denied``: columns ``name, iec, address,
order_ref, notified_on``; a header row is required) — it replaces the stored list and records
who loaded it and when. ``check(name, iec)`` matches:

* the **IEC** exactly (10 characters, case-insensitive) — a hit is certain;
* the **name** after removing legal suffixes and punctuation ("M/s Shree Rice Mills Pvt. Ltd."
  ≈ "shree rice mills") — a hit is reported as a name match for a person to confirm.

The answer always says whether a list is loaded and how old it is: with no list the check is
``unchecked`` (never "clear"), and a list older than ``DGFT_LIST_MAX_AGE_DAYS`` (90) is flagged.
Mongo collection ``dgft_denied_entities`` (memory without Mongo). Reads never raise.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_memory: dict[str, Any] = {"entries": [], "meta": None}
# Only legal forms are dropped: words like "Exports" or "Traders" are part of the name.
_SUFFIX = re.compile(r"\b(m/s|messrs|private|pvt|limited|ltd|llp|llc|co|company|corp|corporation|inc)\b", re.I)


def _norm_name(name: str | None) -> str:
    text = re.sub(r"[^a-z0-9 &/]", " ", (name or "").lower())
    text = _SUFFIX.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _norm_iec(iec: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", iec or "").upper()


def _collection() -> Any:
    try:
        from shared.mongo import collection
        return collection("dgft_denied_entities")
    except Exception:  # noqa: BLE001
        return None


def parse_csv(text: str) -> list[dict[str, Any]]:
    """Rows from the uploaded CSV; a row needs a name or an IEC."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    rows = []
    for raw in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        name, iec = row.get("name") or row.get("entity") or row.get("firm"), row.get("iec")
        if not name and not iec:
            continue
        rows.append({"name": name or "", "iec": _norm_iec(iec), "address": row.get("address") or None,
                     "order_ref": row.get("order_ref") or row.get("order") or None,
                     "notified_on": row.get("notified_on") or row.get("date") or None,
                     "name_key": _norm_name(name)})
    return rows


async def load(text: str, *, by: str, source: str = "upload") -> dict[str, Any]:
    rows = parse_csv(text)
    if not rows:
        raise ValueError("no rows found — the CSV needs a header with at least a 'name' or 'iec' column")
    meta = {"_id": "_meta", "loaded_at": datetime.now(timezone.utc).isoformat(), "loaded_by": by,
            "count": len(rows), "source": source}
    col = _collection()
    if col is None:
        _memory["entries"], _memory["meta"] = rows, meta
    else:
        await col.delete_many({})
        await col.insert_many([dict(r) for r in rows] + [meta])
    logger.warning("DGFT denied list loaded: %d entries by %s", len(rows), by)
    return {k: v for k, v in meta.items() if k != "_id"}


async def _state() -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    col = _collection()
    if col is None:
        return _memory["entries"], _memory["meta"]
    try:
        entries, meta = [], None
        async for d in col.find({}):
            if d.get("_id") == "_meta":
                meta = d
            else:
                entries.append(d)
        return entries, meta
    except Exception as exc:  # noqa: BLE001
        logger.warning("DGFT denied list unreadable: %s", exc)
        return [], None


def _age_days(meta: dict[str, Any] | None) -> float | None:
    try:
        loaded = datetime.fromisoformat(str((meta or {}).get("loaded_at")))
        return (datetime.now(timezone.utc) - loaded).total_seconds() / 86400
    except (TypeError, ValueError):
        return None


async def status() -> dict[str, Any]:
    entries, meta = await _state()
    age = _age_days(meta)
    try:
        max_age = float(os.getenv("DGFT_LIST_MAX_AGE_DAYS", "90"))
    except ValueError:
        max_age = 90.0
    return {"loaded": bool(meta), "count": len(entries), "loaded_at": (meta or {}).get("loaded_at"),
            "loaded_by": (meta or {}).get("loaded_by"), "age_days": round(age, 1) if age is not None else None,
            "stale": age is None or age > max_age}


async def check(name: str | None = None, iec: str | None = None) -> dict[str, Any]:
    entries, meta = await _state()
    st = await status()
    out: dict[str, Any] = {"on_denied_list": False, "match": None, "list_loaded": st["loaded"],
                           "list_age_days": st["age_days"], "list_stale": st["stale"],
                           "checked_at": datetime.now(timezone.utc).isoformat()}
    if not meta:
        out.update(unchecked=True, note="no DGFT denied list loaded — PUT /deals/compliance/dgft-denied")
        return out
    want_iec, want_name = _norm_iec(iec), _norm_name(name)
    for e in entries:
        if want_iec and e.get("iec") and e["iec"] == want_iec:
            out.update(on_denied_list=True, match={"by": "iec", "name": e.get("name"), "order_ref": e.get("order_ref")})
            return out
    if want_name and len(want_name) >= 4:
        for e in entries:
            if e.get("name_key") and e["name_key"] == want_name:
                out.update(on_denied_list=True, match={"by": "name", "name": e.get("name"),
                                                       "order_ref": e.get("order_ref"), "confirm": True})
                return out
    return out


def reset_for_tests() -> None:
    _memory["entries"], _memory["meta"] = [], None
