"""What each buyer wants / each supplier supplies, per product, on its directory record (todo 15).

Filled from (15.2):
- **replies** — a buyer's reply read into ``BuyerRequirement`` and a supplier's quote read into
  ``SupplierOffer`` are copied onto the company's record (source ``email``), so they outlive
  the prospect;
- **completed deals** — at PAID the PI / PO facts (quantity, price, packing, incoterm, port)
  become the proven requirement / offer (source ``deal``);
- **staff** — ``PUT /trade-min/{buyers|suppliers}/{id}/products/{hs}`` (a phone call, a trade
  fair), audited; never overwritten by a reply or a scan.

``GET /trade-min/{buyers|suppliers}/{id}/products`` shows, per product, every fact with its
source and date, how complete it is against the catalogue template, the one-line summary and
the questions still open. Precedence rules: shared/product_spec.py. Writers never raise.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from shared import product_spec
from shared.product_spec import ProductFacts

from . import directory
from .schemas import CounterpartyRecord

logger = logging.getLogger(__name__)


def hs6(code: Any) -> str:
    return "".join(ch for ch in str(code or "") if ch.isdigit())[:6]


def facts_of(rec: CounterpartyRecord | None, hs_code: str) -> ProductFacts | None:
    if rec is None:
        return None
    raw = (rec.product_facts or {}).get(hs6(hs_code))
    if not raw:
        return None
    return raw if isinstance(raw, ProductFacts) else ProductFacts.model_validate(raw)


def _domain(value: str | None) -> str:
    v = (value or "").lower().strip()
    if "@" in v:
        v = v.split("@", 1)[1]
    return directory._domain(v if "://" in v or "@" in (value or "") else f"https://{v}") if v else ""  # noqa: SLF001


async def find_record(role: str, *, name: str | None = None, website: str | None = None,
                      email: str | None = None) -> CounterpartyRecord | None:
    """The directory record of a company: its key first, else the same website / e-mail domain
    (never a free-mail domain), else the same e-mail address."""
    store = await directory.get_directory()
    if name:
        rec = await store.get(directory.record_key(role, name, website))
        if rec is not None:
            return rec
    from agents.negotiation_agent.email_safety import FREEMAIL
    want_email = (email or "").lower().strip()
    want_domain = _domain(website) or (_domain(email) if want_email and want_email.split("@")[-1] not in FREEMAIL else "")
    if not (want_email or want_domain):
        return None
    for rec in await store.list(role, limit=100_000):
        if want_email and (rec.email or "").lower() == want_email:
            return rec
        if want_domain and (_domain(rec.website) == want_domain or
                            ((rec.email or "").lower().split("@")[-1] == want_domain)):
            return rec
    return None


async def record(role: str, hs_code: str, values: dict[str, Any], *, source: str,
                 name: str | None = None, website: str | None = None, email: str | None = None,
                 country: str | None = None, by: str | None = None, ref: str | None = None,
                 create: bool = True, at: datetime | None = None) -> list[str]:
    """Merge statements about one product onto the company's record. Returns the keys changed."""
    code = hs6(hs_code)
    clean = {k: v for k, v in (values or {}).items() if v not in (None, "", [])}
    if len(code) < 4 or not clean:
        return []
    try:
        store = await directory.get_directory()
        rec = await find_record(role, name=name, website=website, email=email)
        if rec is None:
            if not (create and name):
                return []
            now = at or datetime.now(timezone.utc)
            rec = CounterpartyRecord(id=directory.record_key(role, name, website), role=role, name=name,
                                     country=(country or "").upper() or None, email=email, website=website,
                                     source=source, sources=[source], hs_codes=[code],
                                     first_seen_at=now, last_seen_at=now)
        try:
            from shared import catalogue
            product = catalogue.product_name_for(code)
        except Exception:  # noqa: BLE001
            product = None
        merged, changed = product_spec.merge(facts_of(rec, code), code, clean, source=source,
                                             at=at, by=by, ref=ref, product=product)
        if not changed:
            return []
        facts = dict(rec.product_facts or {})
        facts[code] = merged.model_dump(mode="json")
        update: dict[str, Any] = {"product_facts": facts}
        if code not in rec.hs_codes:
            update["hs_codes"] = [*rec.hs_codes, code]
        await store.replace(rec.model_copy(update=update))
        return changed
    except Exception as exc:  # noqa: BLE001 - facts never break the reply / deal path
        logger.warning("ProductFacts: %s %s not recorded: %s", role, name or email, exc)
        return []


async def record_buyer_requirement(deal: Any, cand: Any, requirement: Any, *, ref: str | None = None) -> list[str]:
    """A buyer's reply, read into its requirement, kept on its directory record."""
    return await record("buyer", getattr(deal, "hs_code", ""), product_spec.from_buyer_requirement(requirement),
                        source="email", name=getattr(cand, "legal_name", None), website=getattr(cand, "website", None),
                        email=getattr(cand, "email", None), country=getattr(cand, "country", None),
                        ref=ref or getattr(deal, "deal_id", None))


async def record_supplier_offer(deal: Any, offer: Any, *, source: str = "email") -> list[str]:
    """A supplier's quote kept on its directory record."""
    return await record("supplier", getattr(deal, "hs_code", ""), product_spec.from_supplier_offer(offer),
                        source=source, name=getattr(offer, "company_name", None) or None,
                        email=getattr(offer, "email", None), country="IN",
                        ref=getattr(offer, "quote_id", None) or getattr(deal, "deal_id", None))


async def record_from_deal(deal: Any, facts: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """At PAID: what was actually traded (PI / PO facts) becomes both companies' proven
    requirement / offer, source ``deal`` (feeds "already-traded first")."""
    out: dict[str, list[str]] = {}
    try:
        if facts is None:
            from shared.documents.facts import deal_facts
            facts = await deal_facts(getattr(deal, "deal_id", ""), deal)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ProductFacts: deal facts unavailable for %s: %s", getattr(deal, "deal_id", "?"), exc)
        facts = {}
    hs = getattr(deal, "hs_code", "") or facts.get("hs_code", "")
    pi, po = facts.get("pi"), facts.get("po")
    packing = getattr(pi, "packing_spec", None) or getattr(po, "packing_spec", None)
    packing_txt = None
    if packing:
        try:
            from shared.orchestration import packing as packing_mod
            packing_txt = packing_mod.describe(packing) or None
        except Exception:  # noqa: BLE001
            packing_txt = None
    qty = facts.get("shipped_quantity_mt") or getattr(deal, "quantity_mt", None)
    for p in getattr(deal, "parties", None) or []:
        role = str(getattr(getattr(p, "role", ""), "value", getattr(p, "role", ""))).lower()
        if role == "buyer":
            values = {"quantity_per_order_mt": qty, "packing": packing_txt,
                      "destination_port": facts.get("port_of_destination"), "incoterm": facts.get("incoterm"),
                      "target_price_usd_per_mt": facts.get("unit_price"), "traded": True}
        elif role == "supplier":
            values = {"price_usd_per_mt": getattr(po, "agreed_price", None) or getattr(po, "unit_price", None),
                      "packing": packing_txt, "port_of_loading": facts.get("port_of_loading"),
                      "incoterm": "FOB", "traded": True}
        else:
            continue
        out[role] = await record(role, hs, values, source="deal",
                                 name=getattr(p, "legal_name", None), website=getattr(p, "website", None),
                                 email=getattr(p, "email", None), country=getattr(p, "country", None),
                                 ref=getattr(deal, "deal_id", None))
    return out


async def staff_edit(role: str, record_id: str, hs_code: str, values: dict[str, Any], *, by: str,
                     reason: str | None = None) -> dict[str, Any]:
    """Staff enter / correct facts (audited). Raises LookupError for an unknown record."""
    store = await directory.get_directory()
    rec = await store.get(record_id)
    if rec is None or rec.role != role:
        raise LookupError(f"{record_id} not in the {role} directory")
    changed = await record(role, hs_code, values, source="staff", name=rec.name, website=rec.website,
                           email=rec.email, by=by, ref=reason, create=False)
    try:
        from shared import audit_log
        await audit_log.record("product_facts_edited", by, record_id,
                               {"hs_code": hs6(hs_code), "changed": changed, "reason": reason})
    except Exception as exc:  # noqa: BLE001
        logger.debug("ProductFacts: audit not written: %s", exc)
    return await view(role, record_id)


async def view(role: str, record_id: str) -> dict[str, Any]:
    store = await directory.get_directory()
    rec = await store.get(record_id)
    if rec is None or rec.role != role:
        raise LookupError(f"{record_id} not in the {role} directory")
    from shared import catalogue
    products = []
    codes = list(dict.fromkeys([*(rec.product_facts or {}).keys(), *[hs6(h) for h in rec.hs_codes]]))
    for code in codes:
        if not code:
            continue
        line = catalogue.line_for(code)
        facts = facts_of(rec, code)
        products.append({
            "hs_code": code, "product": (facts.product if facts else None) or (line.name if line else None),
            "facts": {k: f.model_dump(mode="json") for k, f in (facts.facts.items() if facts else [])},
            "completeness": product_spec.completeness(line, facts, role),
            "summary": product_spec.summary_line(facts, line),
            "open_questions": product_spec.missing_questions(line, facts),
        })
    return {"id": rec.id, "role": rec.role, "name": rec.name, "products": products}


async def note_person(role: str, person: dict[str, Any], *, name: str | None = None, website: str | None = None,
                      email: str | None = None) -> bool:
    """Record a contact person on the company's record — the person a prospect wrote to
    (``written_at``) or the one who replied (``replied_at``). Joined by e-mail. Never raises."""
    try:
        rec = await find_record(role, name=name, website=website, email=email or person.get("email"))
        if rec is None or not (person.get("email") or person.get("name")):
            return False
        people = directory._merge_people(rec.people, [person])  # noqa: SLF001
        for p in people:  # timestamps move forward even on a known person
            if (p.get("email") or "").lower() == (person.get("email") or "").lower():
                p.update({k: v for k, v in person.items() if k.endswith("_at") and v})
        store = await directory.get_directory()
        await store.replace(rec.model_copy(update={"people": people}))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("ProductFacts: person not noted: %s", exc)
        return False


async def facts_for(role: str, hs_code: str, *, name: str | None = None, website: str | None = None,
                    email: str | None = None) -> ProductFacts | None:
    """Read side for matching / e-mails: the company's facts for the product, or None."""
    try:
        return facts_of(await find_record(role, name=name, website=website, email=email), hs_code)
    except Exception:  # noqa: BLE001
        return None
