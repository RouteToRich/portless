"""Refusal history — import refusals and alerts against Indian goods (A.33 ``import-refusals-ai`` + the
US FDA download) — todo 23.3 / 13.6 "Rejection history".

Rows (Mongo ``import_refusals``): country, authority, date, product, HS, exporter name when published,
hazard (pesticide residue / ethylene oxide / aflatoxin / salmonella …), action, reference, link.

- **What to test for, per HS × market** (``tests_for``): the hazards found in the last
  ``REFUSAL_LOOKBACK_MONTHS`` (24) for the product (4-digit HS) in that market (an EU market counts
  every EU country's RASFF notice) — added to the QC booking note so the inspector tests for them.
- **Per supplier** (``supplier_refusals``): a refusal naming the supplier in the window forces its
  ``CONFIRM_SUPPLIER_SELECTION`` gate to L0 with ``supplier_import_refusal``.

Sources: the actor daily (``daily``; only once it is built) and the FDA import-refusal download
(``FDA_REFUSALS_URL``, a CSV — ``parse_fda_csv`` reads it by its column names). ``REFUSALS_ENABLED``
pins it off in tests.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

EU = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU",
      "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}
_rows: dict[str, dict[str, Any]] = {}
_last: dict[str, str] = {}
_LEGAL = re.compile(r"\b(pvt|private|ltd|limited|llp|inc|co|company|exports?|industries|traders?|enterprises?)\b\.?")


def enabled() -> bool:
    return os.getenv("REFUSALS_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def lookback_days() -> int:
    return int(float(os.getenv("REFUSAL_LOOKBACK_MONTHS", "24") or 24) * 30.5)


def _norm_name(name: str | None) -> str:
    t = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    return " ".join(_LEGAL.sub(" ", t).split())


def _collection() -> Any:
    try:
        from shared.mongo import collection
        return collection("import_refusals")
    except Exception:  # noqa: BLE001
        return None


def _id(r: dict[str, Any]) -> str:
    return hashlib.sha1("|".join(str(r.get(k) or "") for k in ("authority", "reference", "date", "product", "exporter"))
                        .encode()).hexdigest()[:16]


async def store(rows: list[dict[str, Any]]) -> int:
    new = 0
    col = _collection()
    for r in rows:
        rid = _id(r)
        if rid in _rows:
            continue
        _rows[rid] = {**r, "refusal_id": rid}
        new += 1
        if col is not None:
            try:
                await col.replace_one({"refusal_id": rid}, dict(_rows[rid]), upsert=True)
            except Exception:  # noqa: BLE001
                pass
    return new


async def load() -> None:
    col = _collection()
    if col is None:
        return
    try:
        for d in [d async for d in col.find({}, {"_id": 0})]:
            _rows[d["refusal_id"]] = d
    except Exception:  # noqa: BLE001
        pass


def _recent(r: dict[str, Any], today: date) -> bool:
    try:
        return date.fromisoformat(str(r.get("date"))[:10]) >= today - timedelta(days=lookback_days())
    except ValueError:
        return False


def _market_match(country: str | None, market: str | None) -> bool:
    c, m = (country or "").upper(), (market or "").upper()
    return bool(m) and (c == m or (m in EU and c in EU | {"EU"}))


def tests_for(hs: str | None, market: str | None, today: date | None = None) -> list[str]:
    """Hazards to test for before shipping this product to this market (most frequent first)."""
    today = today or datetime.now(timezone.utc).date()
    h4 = "".join(ch for ch in str(hs or "") if ch.isdigit())[:4]
    counts: dict[str, int] = {}
    for r in _rows.values():
        if r.get("hazard") and str(r.get("hs_code") or "")[:4] == h4 and _market_match(r.get("country"), market) and _recent(r, today):
            counts[r["hazard"]] = counts.get(r["hazard"], 0) + 1
    return [k for k, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def supplier_refusals(name: str | None, today: date | None = None) -> list[dict[str, Any]]:
    """Refusals naming this supplier in the look-back window."""
    key = _norm_name(name)
    if len(key) < 4:
        return []
    today = today or datetime.now(timezone.utc).date()
    return [r for r in _rows.values() if _norm_name(r.get("exporter")) == key and _recent(r, today)]


_HAZARDS = (("ethylene oxide", "ethylene oxide"), ("salmonella", "salmonella"), ("aflatoxin", "aflatoxin"),
            ("pesticide", "pesticide residue"), ("filth", "filth"), ("unsafe color", "unsafe colour"),
            ("listeria", "listeria"), ("mould", "mould"), ("mold", "mould"))


def parse_fda_csv(text: str) -> list[dict[str, Any]]:
    """Pure: the FDA import-refusal download (CSV) → refusal rows for Indian firms. Columns are found by
    name (firm, country, date, product, charge / reason) so a reordered export still reads."""
    reader = csv.DictReader(io.StringIO(text or ""))
    cols = {k.lower(): k for k in reader.fieldnames or []}

    def col(*words: str) -> str | None:
        return next((orig for low, orig in cols.items() if all(w in low for w in words)), None)

    firm, country, when = col("firm"), col("country"), col("date")
    product, charge, code = col("product", "desc") or col("product"), col("charge") or col("reason"), col("product", "code")
    out = []
    for row in reader:
        if country and str(row.get(country) or "").strip().upper() not in ("IN", "INDIA", "IND"):
            continue
        reason = str(row.get(charge) or "") if charge else ""
        hazard = next((label for word, label in _HAZARDS if word in reason.lower()), None)
        raw_date = str(row.get(when) or "").strip() if when else ""
        iso = None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y"):
            try:
                iso = datetime.strptime(raw_date, fmt).date().isoformat()
                break
            except ValueError:
                continue
        out.append({"country": "US", "authority": "FDA", "date": iso, "product": row.get(product) if product else None,
                    "hs_code": "", "exporter": row.get(firm) if firm else None, "hazard": hazard,
                    "hazard_detail": reason[:300] or None, "action": "import refusal",
                    "reference": row.get(code) if code else None, "url": None})
    return out


async def load_fda(*, transport: Any = None) -> dict[str, Any]:
    url = os.getenv("FDA_REFUSALS_URL", "").strip()
    if not url:
        return {"ok": False, "reason": "FDA_REFUSALS_URL is not set"}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=60.0, transport=transport, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"download failed: {exc}"[:200]}
    return {"ok": True, "new": await store(parse_fda_csv(resp.text))}


async def daily(now: datetime | None = None) -> dict[str, Any] | None:
    if not enabled():
        return None
    now = now or datetime.now(timezone.utc)
    day = now.date().isoformat()
    if _last.get("day") == day:
        return None
    since = _last.get("day")
    _last["day"] = day
    from shared import catalogue
    from shared.connectors.registry import get_connector
    lines = catalogue.active_lines()
    out: dict[str, Any] = {}
    try:
        res = await get_connector("apify_import_refusals").quote({
            "hs_codes": sorted({str(getattr(line, "hs_code", ""))[:6] for line in lines}),
            "products": sorted({getattr(line, "name", "") for line in lines if getattr(line, "name", "")}), "since": since})
        out["actor_new"] = 0 if res.get("stub") else await store(res.get("refusals", []))
    except Exception as exc:  # noqa: BLE001
        out["actor_error"] = str(exc)[:200]
    out["fda"] = await load_fda()
    return out


def reset_for_tests() -> None:
    _rows.clear()
    _last.clear()
