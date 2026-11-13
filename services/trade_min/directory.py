"""Buyer / supplier directory — every counterparty a scan found, kept whether or
not that scan produced an opportunity or a deal.

The opportunity pipeline drops a corridor at the tariff gate or below the
arbitrage-rank floor, and with it the named buyers / suppliers the Apify actors
already paid to find. ``record_scan()`` stores them first, merged per company
(website domain, else name), with the products, markets, opportunities and
the latest scan outcome they appeared in. Served by ``GET /trade-min/buyers``
and ``GET /trade-min/suppliers`` (the console's Buyers & Suppliers page).

Storage: the shared Mongo database, collection ``trade_counterparties``
(``TRADE_COUNTERPARTIES_COLLECTION``); in-memory without ``MONGO_URI``.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from .config import settings
from .schemas import BuyerSignal, CounterpartyRecord, SellerCandidate

logger = logging.getLogger(__name__)

# Placeholder buyers some sources emit without a name or a contact.
_PLACEHOLDER = re.compile(r"^(alibaba buyer|eu institution|unknown)\b", re.IGNORECASE)
_MAX_LIST = 50  # cap on hs_codes / markets / opportunity_ids kept per record
# Outcome of the worldwide search (not a corridor) — never replaces a corridor's outcome.
WORLDWIDE_OUTCOME = "found by the worldwide search (not tied to a scanned market)"


def _domain(url: str | None) -> str:
    if not url:
        return ""
    host = urlsplit(url if "//" in url else f"//{url}").hostname or ""
    return host.lower().removeprefix("www.")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def record_key(role: str, name: str, website: str | None) -> str:
    return f"{role}:{_domain(website) or _slug(name)}"


def _union(old: list[str], new: list[str]) -> list[str]:
    out = list(old)
    for v in new:
        if v and v not in out:
            out.append(v)
    return out[-_MAX_LIST:]


def clean_record(rec: CounterpartyRecord) -> CounterpartyRecord:
    """The same clean-up as the company search (shared/connectors/apify.py), applied to a
    stored record: a marketing clause or address dropped from the name, a usable e-mail
    only (phone digits glued to the front removed), and the country the company's own
    website / e-mail / phone points to rather than the market that was searched."""
    from shared.connectors.apify import _clean_name, clean_email, company_country
    name = _clean_name(rec.name) or rec.name
    email = clean_email(rec.email)
    domain = _domain(rec.website)
    evidence = company_country({"domain": domain, "email": email or "", "phone": rec.phone})
    country = (evidence or rec.country or "").upper() or None
    if (name, email, country) == (rec.name, rec.email, rec.country):
        return rec
    return rec.model_copy(update={"name": name, "email": email, "country": country})


def _merge(old: CounterpartyRecord | None, new: CounterpartyRecord) -> CounterpartyRecord:
    return clean_record(_merge_raw(old, new))


def _merge_raw(old: CounterpartyRecord | None, new: CounterpartyRecord) -> CounterpartyRecord:
    if old is None:
        return new
    data = old.model_dump()
    for field in ("name", "country", "email", "phone", "website", "address", "contact_url",
                  "fit_score", "fit_reason", "gstin", "iec", "estimated_fob_price_usd"):
        value = getattr(new, field)
        if value not in (None, ""):
            data[field] = value
    data.update(
        source=new.source,
        sources=_union(old.sources, new.sources),
        hs_codes=_union(old.hs_codes, new.hs_codes),
        markets=_union(old.markets, new.markets),
        opportunity_ids=_union(old.opportunity_ids, new.opportunity_ids),
        last_outcome=(old.last_outcome if new.last_outcome == WORLDWIDE_OUTCOME and old.last_outcome
                      else new.last_outcome or old.last_outcome),
        times_seen=old.times_seen + 1,
        last_seen_at=new.last_seen_at,
        # A newer check replaces the stored one; a scan that checked nothing keeps it.
        validation=(new.validation.model_dump() if new.validation is not None
                    else (old.validation.model_dump() if old.validation is not None else None)),
    )
    return CounterpartyRecord.model_validate(data)


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------

class InMemoryDirectory:
    def __init__(self) -> None:
        self._store: dict[str, CounterpartyRecord] = {}

    async def upsert(self, rec: CounterpartyRecord) -> CounterpartyRecord:
        merged = _merge(self._store.get(rec.id), rec)
        self._store[rec.id] = merged
        return merged

    async def get(self, record_id: str) -> CounterpartyRecord | None:
        return self._store.get(record_id)

    async def set_validation(self, rec: CounterpartyRecord) -> None:
        if rec.id in self._store:
            self._store[rec.id] = self._store[rec.id].model_copy(update={"validation": rec.validation})

    async def list(self, role: str, *, hs_code: str | None = None, market: str | None = None,
                   q: str | None = None, country: str | None = None,
                   limit: int = 200, validation: str | None = None) -> list[CounterpartyRecord]:
        rows = [r for r in self._store.values()
                if r.role == role and _matches(r, hs_code, market, q, country, validation)]
        rows.sort(key=lambda r: r.last_seen_at, reverse=True)
        return rows[:limit]

    async def clear(self) -> None:
        self._store.clear()


class MongoDirectory:
    def __init__(self, collection: Any) -> None:
        self._col = collection

    async def upsert(self, rec: CounterpartyRecord) -> CounterpartyRecord:
        doc = await self._col.find_one({"_id": rec.id})
        old = None
        if doc:
            doc.pop("_id", None)
            try:
                old = CounterpartyRecord.model_validate(doc)
            except Exception:  # noqa: BLE001 - a malformed old row is replaced
                old = None
        merged = _merge(old, rec)
        out = merged.model_dump(mode="json")
        out["_id"] = merged.id
        await self._col.replace_one({"_id": merged.id}, out, upsert=True)
        return merged

    async def get(self, record_id: str) -> CounterpartyRecord | None:
        doc = await self._col.find_one({"_id": record_id})
        if not doc:
            return None
        doc.pop("_id", None)
        try:
            return CounterpartyRecord.model_validate(doc)
        except Exception:  # noqa: BLE001 - a malformed row counts as unknown
            return None

    async def set_validation(self, rec: CounterpartyRecord) -> None:
        await self._col.update_one(
            {"_id": rec.id},
            {"$set": {"validation": rec.validation.model_dump(mode="json") if rec.validation else None}},
        )

    async def list(self, role: str, *, hs_code: str | None = None, market: str | None = None,
                   q: str | None = None, country: str | None = None,
                   limit: int = 200, validation: str | None = None) -> list[CounterpartyRecord]:
        query: dict[str, Any] = {"role": role}
        if validation == "unverified":
            query["$and"] = [{"$or": [{"validation": None}, {"validation.status": "unverified"}]}]
        elif validation:
            query["validation.status"] = validation
        if country:
            query["country"] = country.upper()
        if hs_code:
            query["hs_codes"] = hs_code
        if market:
            query["markets"] = market.upper()
        if q:
            rx = {"$regex": re.escape(q), "$options": "i"}
            query["$or"] = [{"name": rx}, {"email": rx}, {"website": rx}, {"country": rx}]
        rows: list[CounterpartyRecord] = []
        async for doc in self._col.find(query).sort("last_seen_at", -1).limit(limit):
            doc.pop("_id", None)
            try:
                rows.append(CounterpartyRecord.model_validate(doc))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Directory: skipping malformed row: %s", exc)
        return rows

    async def clear(self) -> None:
        await self._col.delete_many({})


def _matches(r: CounterpartyRecord, hs_code: str | None, market: str | None, q: str | None,
             country: str | None = None, validation: str | None = None) -> bool:
    if validation and (r.validation.status if r.validation else "unverified") != validation:
        return False
    if country and (r.country or "").upper() != country.upper():
        return False
    if hs_code and hs_code not in r.hs_codes:
        return False
    if market and market.upper() not in r.markets:
        return False
    if q:
        hay = " ".join(filter(None, [r.name, r.email, r.website, r.country])).lower()
        if q.lower() not in hay:
            return False
    return True


_directory: InMemoryDirectory | MongoDirectory | None = None


async def get_directory() -> InMemoryDirectory | MongoDirectory:
    global _directory
    if _directory is not None:
        return _directory
    uri = settings.trade_mongo_uri
    if not uri:
        _directory = InMemoryDirectory()
        return _directory
    try:
        from motor.motor_asyncio import AsyncIOMotorClient  # type: ignore[import]
        client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5_000, connectTimeoutMS=5_000)
        col = client[settings.trade_mongo_db_name][settings.trade_counterparties_collection]
        await col.create_index([("role", 1), ("last_seen_at", -1)], name="role_last_seen", background=True)
        await col.create_index("hs_codes", name="hs_codes", background=True)
        _directory = MongoDirectory(col)
    except Exception as exc:  # noqa: BLE001
        logger.error("Directory: MongoDB unavailable (%s) — buyers/suppliers kept in memory", exc)
        _directory = InMemoryDirectory()
    return _directory


def reset_directory() -> None:
    """Tests: back to a fresh in-memory store."""
    global _directory
    _directory = InMemoryDirectory()


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def _buyer_record(b: BuyerSignal, hs_code: str, market: str, outcome: str,
                  opportunity_id: str | None, now: datetime) -> CounterpartyRecord | None:
    name = (b.buyer_name or "").strip()
    has_contact = bool(b.email or b.phone or b.website or b.contact_url)
    if not name or (_PLACEHOLDER.match(name) and not has_contact):
        return None
    return CounterpartyRecord(
        id=record_key("buyer", name, b.website),
        role="buyer", name=name, country=(b.buyer_country or market or "").upper() or None,
        email=b.email, phone=b.phone, website=b.website, address=b.address, contact_url=b.contact_url,
        source=b.source, sources=[b.source], fit_score=b.fit_score, fit_reason=b.fit_reason,
        hs_codes=[hs_code], markets=[market.upper()] if market else [],  # the corridor scanned
        opportunity_ids=[opportunity_id] if opportunity_id else [],
        last_outcome=outcome, first_seen_at=now, last_seen_at=now, validation=b.validation,
    )


def _supplier_record(s: SellerCandidate, hs_code: str, market: str, outcome: str,
                     opportunity_id: str | None, now: datetime) -> CounterpartyRecord | None:
    name = (s.company_name or "").strip()
    if not name:
        return None
    return CounterpartyRecord(
        id=record_key("supplier", name, s.website),
        role="supplier", name=name, country=(s.country or "").upper() or None,
        email=s.email, phone=s.phone, website=s.website, contact_url=s.listing_url,
        source=s.source, sources=[s.source], fit_score=s.fit_score,
        gstin=s.gstin, iec=s.iec, estimated_fob_price_usd=s.estimated_fob_price_usd,
        hs_codes=[hs_code], markets=[market.upper()] if market else [],
        opportunity_ids=[opportunity_id] if opportunity_id else [],
        last_outcome=outcome, first_seen_at=now, last_seen_at=now, validation=s.validation,
    )


async def record_scan(hs_code: str, market: str, buyers: list[BuyerSignal], sellers: list[SellerCandidate],
                      *, outcome: str, opportunity_id: str | None = None) -> tuple[int, int]:
    """Store the buyers and suppliers one corridor scan found. Never raises."""
    now = datetime.now(timezone.utc)
    stored = [0, 0]
    try:
        directory = await get_directory()
        for i, (items, build) in enumerate(((buyers, _buyer_record), (sellers, _supplier_record))):
            for item in items:
                rec = build(item, hs_code, market, outcome, opportunity_id, now)  # type: ignore[arg-type]
                if rec is not None:
                    await directory.upsert(rec)
                    stored[i] += 1
    except Exception as exc:  # noqa: BLE001 - the directory must never break a scan
        logger.error("Directory: could not record %s×%s: %s", hs_code, market, exc)
    return stored[0], stored[1]


async def clean_existing() -> int:
    """Apply ``clean_record`` to every stored buyer and supplier (ids kept, so links to
    opportunities stay). Returns how many records changed."""
    directory = await get_directory()
    changed = 0
    if isinstance(directory, MongoDirectory):
        async for doc in directory._col.find({}):
            doc_id = doc.pop("_id", None)
            try:
                rec = CounterpartyRecord.model_validate(doc)
            except Exception:  # noqa: BLE001 - leave a malformed row alone
                continue
            cleaned = clean_record(rec)
            if cleaned is not rec:
                out = cleaned.model_dump(mode="json")
                out["_id"] = doc_id
                await directory._col.replace_one({"_id": doc_id}, out)
                changed += 1
    else:
        for key, rec in list(directory._store.items()):
            cleaned = clean_record(rec)
            if cleaned is not rec:
                directory._store[key] = cleaned
                changed += 1
    return changed


async def list_counterparties(role: str, **filters: Any) -> list[CounterpartyRecord]:
    return await (await get_directory()).list(role, **filters)
