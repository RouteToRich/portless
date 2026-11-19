"""More buyers and suppliers from list actors — the PortLess side of A.2, A.5, A.9, A.10, A.11, A.12, A.13,
A.14, A.15 and A.18 (todo 21.3 / 15.4).

``run(connector, hs_code, market)`` calls one list actor (each runs only once it is built —
``APIFY_ACTOR_<KEY>_BUILT``) and ``ingest`` puts its companies into the directory with the source as the
actor names it (``trade_fair:<fair>``, ``registry:<board>``, ``b2b:<site>``, ``apmc``,
``association:<name>``, ``article``, ``directory:<name>``, ``places``, ``competitor_client``,
``us_customs``), unverified until the normal check runs:

- **suppliers** keep their registration number (A.9 — the registry check verifies it), GSTIN, listed
  price (A.10: into the price book as ``supplier_listed``, half the weight of a quote), MOQ;
- **buyers** keep association membership, ``buys_from_competitor`` (A.18), and — from US import records
  (A.2) — ``annual_volume_mt`` / ``shipments_12m`` / ``last_import_date`` (``volume_source=us_customs``),
  which drive volume fit, reorder timing and the yearly potential; a forwarder consignee is skipped;
- names extracted from "top importers" articles (A.13) with no website or e-mail are looked up with the
  contact finder (at most ``DIRECTORY_SOURCES_CONTACT_LOOKUPS`` per run, 10).

``weekly`` (deal watcher, ``DIRECTORY_SOURCES_ENABLED`` — pinned off in tests) runs every list actor for
the catalogue's products and markets.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

LIST_CONNECTORS = ("apify_trade_fairs", "apify_exporter_registries", "apify_india_b2b", "apify_apmc_traders",
                   "apify_association_members", "apify_importer_lists", "apify_business_directories",
                   "apify_local_business", "apify_competitor_clients", "apify_us_import_records")
_last: dict[str, datetime] = {}


def enabled() -> bool:
    return os.getenv("DIRECTORY_SOURCES_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def record_for(c: dict[str, Any], hs_code: str, market: str | None, now: datetime) -> Any:
    """Pure-ish: one actor company → a directory record (None for a forwarder or a nameless row)."""
    from .directory import record_key
    from .schemas import CounterpartyRecord
    name = (c.get("name") or "").strip()
    if not name or "forwarder" in (c.get("flags") or []):
        return None
    role = "supplier" if c.get("role") == "supplier" else "buyer"
    country = (c.get("country") or ("IN" if role == "supplier" else market) or "").upper() or None
    volume = c.get("annual_volume_mt") or c.get("shipments_12m")
    return CounterpartyRecord(
        id=record_key(role, name, c.get("website")), role=role, name=name, country=country, email=c.get("email"),
        phone=c.get("phone"), website=c.get("website"), address=c.get("address"), contact_url=c.get("evidence_url"),
        source=c.get("source") or "list", sources=[c.get("source") or "list"], hs_codes=[hs_code],
        markets=[market.upper()] if market else [], gstin=c.get("gstin"),
        estimated_fob_price_usd=c.get("price_usd_per_mt") if role == "supplier" else None,
        annual_volume_mt=c.get("annual_volume_mt"), shipments_12m=c.get("shipments_12m"),
        last_import_date=c.get("last_import_date"), typical_shipment_mt=c.get("typical_shipment_mt"),
        frequency_days=c.get("frequency_days"),
        volume_source=c.get("source") if volume else None,
        flags=list(dict.fromkeys([*(c.get("flags") or []),
                                  *([f"registration:{c['registration_number']}"] if c.get("registration_number") else [])])),
        last_outcome=f"found by {c.get('source')}", first_seen_at=now, last_seen_at=now,
    )


async def _lookup_contacts(companies: list[dict[str, Any]], market: str | None) -> None:
    """Article names without a website / e-mail → the contact finder (bounded)."""
    todo = [c for c in companies if c.get("source") == "article" and not (c.get("website") or c.get("email"))]
    todo = todo[: int(os.getenv("DIRECTORY_SOURCES_CONTACT_LOOKUPS", "10") or 10)]
    if not todo:
        return
    try:
        from shared.connectors.registry import get_connector
        res = await get_connector("apify_contact_finder").quote(
            {"companies": [{"name": c["name"], "country": c.get("country") or market} for c in todo]})
    except Exception as exc:  # noqa: BLE001
        logger.warning("DirectorySources: contact lookup failed: %s", exc)
        return
    found = {(r.get("input_name") or r.get("name") or "").lower(): r for r in res.get("contacts", [])}
    for c in todo:
        r = found.get(c["name"].lower())
        if r and not r.get("not_found"):
            c.update(website=r.get("website"), email=r.get("email"), phone=r.get("phone"))


async def ingest(companies: list[dict[str, Any]], hs_code: str, market: str | None) -> dict[str, Any]:
    """Companies → directory records; listed supplier prices → the price book. Never raises."""
    from shared import pricebook
    from .directory import get_directory
    await _lookup_contacts(companies, market)
    store = await get_directory()
    now = datetime.now(timezone.utc)
    out = {"stored": 0, "skipped": 0, "prices": 0}
    for c in companies:
        try:
            rec = record_for(c, hs_code, market, now)
            if rec is None:
                out["skipped"] += 1
                continue
            await store.upsert(rec)
            out["stored"] += 1
            if rec.role == "supplier" and c.get("price_usd_per_mt"):
                if await pricebook.record(pricebook.PriceObservation(
                        hs_code=hs_code, side="supplier", kind="supplier_listed", price_usd_per_mt=float(c["price_usd_per_mt"]),
                        counterparty_id=rec.id, note=f"listed on {c.get('source')}")):
                    out["prices"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("DirectorySources: %s not stored: %s", c.get("name"), exc)
            out["skipped"] += 1
    return out


async def run(connector: str, hs_code: str, market: str | None, *, products: list[str] | None = None,
              queries: list[str] | None = None) -> dict[str, Any]:
    from shared.connectors.registry import get_connector
    res = await get_connector(connector).quote({"products": products or [], "hs_codes": [hs_code],
                                                "countries": [market] if market else [], "queries": queries or []})
    if res.get("stub"):
        return {"connector": connector, "stub": True}
    return {"connector": connector, **(await ingest(res.get("companies", []), hs_code, market))}


async def weekly(now: datetime | None = None) -> dict[str, Any] | None:
    if not enabled():
        return None
    now = now or datetime.now(timezone.utc)
    if _last.get("run") and now - _last["run"] < timedelta(days=7):
        return None
    _last["run"] = now
    from shared import catalogue
    out: list[dict[str, Any]] = []
    for line in catalogue.active_lines():
        name = getattr(line, "name", "") or ""
        for market in (getattr(line, "markets", None) or [None]):
            queries = [f"top {name} importers in {market}"] if market and name else []
            for conn in LIST_CONNECTORS:
                try:
                    out.append(await run(conn, str(line.hs_code), market, products=[name] if name else [], queries=queries))
                except Exception as exc:  # noqa: BLE001
                    out.append({"connector": conn, "error": str(exc)[:200]})
    return {"runs": out}


def potential(rec: Any, today: Any = None) -> dict[str, Any] | None:
    """Pure: a buyer's yearly potential and reorder timing from its own import pattern (todo 16.3 / 15.4)
    — annual MT (stated, else typical lot × 365 / frequency, else shipments × typical lot), the next
    order it is due, and ``ranking.reorder_probability`` from the days since the last import."""
    from datetime import date
    from .ranking import reorder_probability
    freq = getattr(rec, "frequency_days", None)
    lot = getattr(rec, "typical_shipment_mt", None)
    annual = getattr(rec, "annual_volume_mt", None)
    if not annual and lot and freq:
        annual = round(lot * 365.0 / freq, 1)
    if not annual and lot and getattr(rec, "shipments_12m", None):
        annual = round(lot * rec.shipments_12m, 1)
    if not annual and not freq:
        return None
    out: dict[str, Any] = {"annual_mt": annual, "basis": getattr(rec, "volume_source", None) or "import records",
                           "next_order_due": None, "reorder_probability": None}
    last = getattr(rec, "last_import_date", None)
    if last and freq:
        today = today or datetime.now(timezone.utc).date()
        last_d = date.fromisoformat(str(last)[:10])
        out["next_order_due"] = (last_d + timedelta(days=freq)).isoformat()
        out["reorder_probability"] = reorder_probability((today - last_d).days, freq)
    return out


def reset_for_tests() -> None:
    _last.clear()
