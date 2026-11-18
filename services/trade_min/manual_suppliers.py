"""Local suppliers added by hand (staff), and their CSV import.

Many good Indian suppliers never show up in a web search: a mill PortLess knows, a trader met
at a mandi. Staff add them to the supplier directory:

- ``POST /trade-min/suppliers`` — legal name, contact person, e-mail, phone / WhatsApp, full
  facility address with PIN code, GSTIN, IEC, Udyam, FSSAI / APEDA, products (HS codes,
  grades), capacity and MOQ (MT), usual price (INR or USD, FOB or ex-works), port of loading
  and its distance, bank (bank name, IFSC and the last 4 digits only — the full account number
  is entered on the payout rail, never stored here). ``source="manual"``.
- ``PUT /trade-min/suppliers/{id}`` edits, ``DELETE /trade-min/suppliers/{id}`` removes one that
  is in no live deal.
- On add and edit the same checks as a scanned supplier run (sanctions screening, e-mail
  check, GSTN / DGFT IEC registries — ``validation.validate_record``); a failed check marks it
  ``failed`` (never contacted, never a deal party).
- Duplicates (same GSTIN, website domain or phone) are refused with the existing id
  (``force=true`` adds anyway).
- A later scan never overwrites what staff entered (``directory._merge_raw``).
- ``POST /trade-min/suppliers/import`` reads CSV; ``dry_run=true`` reports rows accepted,
  duplicates and invalid rows without saving or running paid checks.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from services.trade_min import directory
from services.trade_min.schemas import CounterpartyRecord, ManualSupplierFacts

logger = logging.getLogger(__name__)

_GSTIN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


class ManualSupplierIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    contact_person: str | None = None
    email: str | None = None
    phone: str | None = None
    whatsapp: str | None = None
    website: str | None = None
    address: str | None = Field(default=None, max_length=400)
    """Full facility address with the PIN code (the inspector drives there)."""
    country: str = "IN"
    gstin: str | None = None
    iec: str | None = None
    udyam: str | None = None
    fssai: str | None = None
    apeda: str | None = None
    hs_codes: list[str] = Field(default_factory=list)
    grades: list[str] = Field(default_factory=list)
    capacity_mt_per_month: float | None = Field(default=None, ge=0)
    moq_mt: float | None = Field(default=None, ge=0)
    usual_price: float | None = Field(default=None, gt=0)
    price_currency: Literal["INR", "USD"] = "USD"
    price_basis: Literal["FOB", "EXW"] = "FOB"
    port_of_loading: str | None = None
    port_km: float | None = Field(default=None, ge=0)
    bank_name: str | None = None
    bank_ifsc: str | None = None
    bank_account_last4: str | None = Field(default=None, pattern=r"^\d{4}$")

    @field_validator("gstin", "iec", "udyam", "fssai", "apeda", "bank_ifsc", mode="before")
    @classmethod
    def _upper(cls, v: Any) -> Any:
        return v.strip().upper() if isinstance(v, str) and v.strip() else None

    @field_validator("gstin")
    @classmethod
    def _gstin(cls, v: str | None) -> str | None:
        if v and not _GSTIN.match(v):
            raise ValueError("GSTIN must be 15 characters like 32ABCDE1234F1Z5")
        return v

    @field_validator("hs_codes", mode="before")
    @classmethod
    def _codes(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = [c for c in re.split(r"[\s,;|]+", v) if c]
        return [re.sub(r"\D", "", str(c)) for c in v if re.sub(r"\D", "", str(c))]

    @field_validator("grades", mode="before")
    @classmethod
    def _grades(cls, v: Any) -> Any:
        return [g.strip() for g in re.split(r"[;|]", v) if g.strip()] if isinstance(v, str) else v


class DuplicateSupplier(ValueError):
    def __init__(self, existing_id: str, why: str):
        super().__init__(f"already in the directory as {existing_id} (same {why})")
        self.existing_id, self.why = existing_id, why


def _usd_price(data: ManualSupplierIn, usd_inr: float | None) -> float | None:
    if data.usual_price is None:
        return None
    if data.price_currency == "USD":
        return round(data.usual_price, 2)
    return round(data.usual_price / usd_inr, 2) if usd_inr else None


def _phone_key(phone: str | None) -> str | None:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else None


async def find_duplicate(data: ManualSupplierIn, *, exclude: str | None = None) -> tuple[str, str] | None:
    store = await directory.get_directory()
    domain = directory._domain(data.website)
    phone = _phone_key(data.phone or data.whatsapp)
    for r in await store.list("supplier", limit=100_000):
        if r.id == exclude:
            continue
        if data.gstin and r.gstin and r.gstin.upper() == data.gstin:
            return r.id, "GSTIN"
        if domain and directory._domain(r.website) == domain:
            return r.id, "website"
        if phone and _phone_key(r.phone) == phone:
            return r.id, "phone"
    return None


async def _record(data: ManualSupplierIn, record_id: str, now: datetime, old: CounterpartyRecord | None) -> CounterpartyRecord:
    from shared.currency import usd_rate
    rate = await usd_rate("INR") if data.price_currency == "INR" else None
    facts = ManualSupplierFacts(**{k: getattr(data, k) for k in ManualSupplierFacts.model_fields if hasattr(data, k)})
    base = old.model_dump() if old else {"first_seen_at": now, "times_seen": 1, "sources": [], "markets": [],
                                         "opportunity_ids": [], "flags": []}
    base.update(
        id=record_id, role="supplier", name=data.name.strip(), country=data.country.upper(), email=data.email,
        phone=data.phone or data.whatsapp, website=data.website, address=data.address, gstin=data.gstin, iec=data.iec,
        source="manual", hs_codes=data.hs_codes, estimated_fob_price_usd=_usd_price(data, rate),
        last_seen_at=now, manual=facts.model_dump(),
    )
    base["sources"] = list(dict.fromkeys([*(base.get("sources") or []), "manual"]))
    return CounterpartyRecord.model_validate(base)


async def _check(rec: CounterpartyRecord) -> CounterpartyRecord:
    from services.trade_min import validation
    store = await directory.get_directory()
    try:
        checked = await validation.validate_record(rec)
        await store.set_validation(checked)
        return checked
    except Exception as exc:  # noqa: BLE001 - saved unverified; the check can be run again
        logger.warning("ManualSupplier: check of %s failed: %s", rec.name, exc)
        return rec


async def add(data: ManualSupplierIn, by: str, *, force: bool = False, check: bool = True) -> CounterpartyRecord:
    if not force:
        dup = await find_duplicate(data)
        if dup:
            raise DuplicateSupplier(*dup)
    store = await directory.get_directory()
    now = datetime.now(timezone.utc)
    record_id = directory.record_key("supplier", data.name, data.website)
    if await store.get(record_id) is not None:
        record_id = f"{record_id}-{now.strftime('%H%M%S')}"
    rec = await _record(data, record_id, now, None)
    rec = rec.model_copy(update={"manual": {**(rec.manual or {}), "added_by": by}})
    await store.replace(rec)
    logger.info("ManualSupplier: %s added by %s (%s)", rec.name, by, rec.id)
    return await _check(rec) if check else rec


async def edit(record_id: str, data: ManualSupplierIn, by: str) -> CounterpartyRecord | None:
    store = await directory.get_directory()
    old = await store.get(record_id)
    if old is None or old.role != "supplier":
        return None
    dup = await find_duplicate(data, exclude=record_id)
    if dup:
        raise DuplicateSupplier(*dup)
    rec = await _record(data, record_id, datetime.now(timezone.utc), old)
    rec = rec.model_copy(update={"manual": {**(rec.manual or {}), "edited_by": by}})
    await store.replace(rec)
    return await _check(rec)


async def delete(record_id: str) -> bool:
    from services.trade_min import relationships
    store = await directory.get_directory()
    rec = await store.get(record_id)
    if rec is None or rec.role != "supplier":
        return False
    rel = (await relationships.annotate([rec]))[0].relationship or {}
    if rel.get("deals_active"):
        raise ValueError(f"{rec.name} is in {rel['deals_active']} live deal(s) — it cannot be deleted")
    await store.delete(record_id)
    return True


_ALIASES = {"legal_name": "name", "company": "name", "company_name": "name", "mobile": "phone",
            "hs": "hs_codes", "hs_code": "hs_codes", "products": "hs_codes", "capacity": "capacity_mt_per_month",
            "moq": "moq_mt", "price": "usual_price", "currency": "price_currency", "basis": "price_basis",
            "port": "port_of_loading", "ifsc": "bank_ifsc", "account_last4": "bank_account_last4"}


async def import_csv(text: str, by: str, *, dry_run: bool = True, check: bool = True) -> dict[str, Any]:
    """Rows → suppliers. Dry run: what would be accepted / is a duplicate / is invalid."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    accepted, duplicates, invalid, created = [], [], [], []
    seen: dict[str, int] = {}
    for n, raw in enumerate(reader, start=2):  # row 1 is the header
        row = {_ALIASES.get(k.strip().lower().replace(" ", "_"), k.strip().lower().replace(" ", "_")): (v or "").strip()
               for k, v in raw.items() if k}
        row = {k: v for k, v in row.items() if v != ""}
        try:
            data = ManualSupplierIn(**row)
        except ValidationError as exc:
            invalid.append({"row": n, "name": row.get("name"), "errors": [e["msg"] for e in exc.errors()][:3]})
            continue
        key = (data.gstin or directory._domain(data.website) or _phone_key(data.phone) or data.name.lower())
        if key in seen:
            duplicates.append({"row": n, "name": data.name, "existing": f"row {seen[key]} of this file"})
            continue
        seen[key] = n
        dup = await find_duplicate(data)
        if dup:
            duplicates.append({"row": n, "name": data.name, "existing": dup[0], "why": dup[1]})
            continue
        accepted.append({"row": n, "name": data.name})
        if not dry_run:
            rec = await add(data, by, force=True, check=check)
            created.append({"row": n, "id": rec.id, "check": rec.validation.status if rec.validation else "unchecked"})
    return {"dry_run": dry_run, "accepted": accepted, "duplicates": duplicates, "invalid": invalid,
            "created": created, "checks_note": ("each accepted supplier is checked on import (paid Apify runs)"
                                                if dry_run and check else None)}
