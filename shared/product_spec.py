"""Exact product requirement per buyer and supplier (todo 15) — the pure part.

**Template** (15.1): each catalogue ``ProductLine`` may list ``attributes`` — what defines a
tradeable lot of that product (onion: variety, colour, size, packing; rice: variety, broken %,
packing …), each with allowed values and a unit. A line without its own template uses the
**base** attributes every product has (grade, packing, quantity per order, destination port,
incoterm, certificates), but only staff-defined attributes ask questions or block a pair.

**Facts** (15.1 "every fact carries its source and date"): what a company told us about one
product, one ``Fact`` per attribute — value, source (``website`` / ``email`` / ``deal`` /
``staff`` / ``scan``), date, who, reference. A fact nobody stated is absent (shown "not known"),
never filled from the HS code or a guess.

**Precedence** when a new statement arrives: a staff entry is never overwritten except by staff;
a completed deal is never overwritten by an e-mail, the website or a scan; a newer e-mail
replaces an older one; the website / a scan only fills what is empty.

Everything here is pure (no I/O); the directory side is services/trade_min/product_facts.py.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

SOURCES = ("scan", "website", "email", "deal", "staff")
_RANK = {"scan": 0, "website": 0, "email": 1, "deal": 2, "staff": 3}


class SpecAttribute(BaseModel):
    """One attribute of a product's specification template."""
    key: str
    label: str | None = None
    values: list[str] = Field(default_factory=list)
    """Allowed values (empty = free text / number)."""
    unit: str | None = None
    question: str | None = None
    """How to ask for it in an e-mail ("Which variety — red or pink?")."""
    hard: bool = True
    """A conflict on this attribute blocks a buyer × supplier pair (variety, size, form, packing)."""


BASE_ATTRIBUTES: tuple[SpecAttribute, ...] = (
    SpecAttribute(key="grade", label="grade / specification", hard=False),
    SpecAttribute(key="packing", label="packing", hard=False),
    SpecAttribute(key="quantity_per_order_mt", label="quantity per order", unit="MT", hard=False),
    SpecAttribute(key="destination_port", label="destination port", hard=False),
    SpecAttribute(key="incoterm", label="incoterm", hard=False),
    SpecAttribute(key="certificates", label="certificates / documents", hard=False),
)
OFFER_BASE: tuple[SpecAttribute, ...] = (
    SpecAttribute(key="grade", label="grades it packs", hard=False),
    SpecAttribute(key="packing", label="packing options", hard=False),
    SpecAttribute(key="capacity_mt_per_month", label="capacity", unit="MT / month", hard=False),
    SpecAttribute(key="moq_mt", label="minimum order", unit="MT", hard=False),
    SpecAttribute(key="price_usd_per_mt", label="price", unit="USD / MT", hard=False),
    SpecAttribute(key="incoterm", label="price basis (EXW / FOB)", hard=False),
    SpecAttribute(key="port_of_loading", label="port of loading", hard=False),
    SpecAttribute(key="certificates", label="certificates held", hard=False),
)


class Fact(BaseModel):
    value: Any
    source: str = "email"
    at: datetime | None = None
    by: str | None = None
    ref: str | None = None
    """Where it came from: deal id, e-mail id, page URL."""


class ProductFacts(BaseModel):
    """What one company wants (buyer) or can supply (supplier) of one product."""
    hs_code: str
    product: str | None = None
    facts: dict[str, Fact] = Field(default_factory=dict)
    updated_at: datetime | None = None


def template(line: Any, role: str = "buyer") -> list[SpecAttribute]:
    """The line's own attributes, then the base attributes it does not repeat."""
    own = [a if isinstance(a, SpecAttribute) else SpecAttribute(**a) for a in (getattr(line, "attributes", None) or [])]
    keys = {a.key for a in own}
    base = OFFER_BASE if role == "supplier" else BASE_ATTRIBUTES
    return own + [a for a in base if a.key not in keys]


def own_attributes(line: Any) -> list[SpecAttribute]:
    return [a if isinstance(a, SpecAttribute) else SpecAttribute(**a) for a in (getattr(line, "attributes", None) or [])]


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (list, tuple)):
        out = [v for v in (_clean(x) for x in value) if v not in (None, "")]
        return out or None
    return value


def merge(current: ProductFacts | None, hs_code: str, values: dict[str, Any], *, source: str,
          at: datetime | None = None, by: str | None = None, ref: str | None = None,
          product: str | None = None) -> tuple[ProductFacts, list[str]]:
    """New statements onto the stored facts by precedence. Returns (facts, keys changed)."""
    at = at or datetime.now(timezone.utc)
    cur = current.model_copy(deep=True) if current else ProductFacts(hs_code=hs_code, product=product)
    if product and not cur.product:
        cur.product = product
    changed: list[str] = []
    rank = _RANK.get(source, 0)
    for key, raw in values.items():
        value = _clean(raw)
        if value is None:
            continue
        old = cur.facts.get(key)
        if old is not None:
            old_rank = _RANK.get(old.source, 0)
            if old.value == value and old_rank >= rank:
                continue
            if old_rank > rank:
                continue  # staff > deal > e-mail > website / scan
            if old_rank == rank == 0:
                continue  # the website / a scan only fills what is empty
        cur.facts[key] = Fact(value=value, source=source, at=at, by=by, ref=ref)
        changed.append(key)
    if changed:
        cur.updated_at = at
    return cur, changed


def completeness(line: Any, facts: ProductFacts | None, role: str = "buyer") -> dict[str, Any]:
    attrs = template(line, role)
    known = [a.key for a in attrs if facts and a.key in facts.facts]
    missing = [a.key for a in attrs if not (facts and a.key in facts.facts)]
    return {"known": len(known), "total": len(attrs), "missing": missing}


def missing_questions(line: Any, facts: ProductFacts | None, *, limit: int = 5) -> list[str]:
    """Short questions for the line's own (staff-defined) attributes still unanswered — the
    introduction, the RFQ and the negotiation ask exactly these (15.2 "ask for what is missing").
    Empty when the line has no template: the e-mails keep their general ask."""
    out: list[str] = []
    for a in own_attributes(line):
        if facts and a.key in facts.facts:
            continue
        if a.question:
            out.append(a.question)
        else:
            label = a.label or a.key.replace("_", " ")
            choice = f" ({' / '.join(a.values[:5])})" if a.values else ""
            out.append(f"{label}{choice}" + (f" in {a.unit}" if a.unit else ""))
    return out[:limit]


def _norm(v: Any) -> set[str]:
    items = v if isinstance(v, (list, tuple, set)) else [v]
    return {" ".join(str(x).lower().split()) for x in items if x not in (None, "")}


def conflicts(line: Any, buyer: ProductFacts | dict[str, Any] | None,
              supplier: ProductFacts | dict[str, Any] | None) -> tuple[list[str], int]:
    """(conflicts on hard attributes, attributes that match) between what a buyer wants and what
    a supplier supplies (15.3 "match on the specification"). A supplier fact may be a list
    (the sizes / varieties it packs); a match is any overlap. Unknown on either side is neither."""
    b = {k: f.value for k, f in buyer.facts.items()} if isinstance(buyer, ProductFacts) else dict(buyer or {})
    s = {k: f.value for k, f in supplier.facts.items()} if isinstance(supplier, ProductFacts) else dict(supplier or {})
    out: list[str] = []
    matches = 0
    for a in own_attributes(line):
        if a.key not in b or a.key not in s:
            continue
        want, have = _norm(b[a.key]), _norm(s[a.key])
        if want & have:
            matches += 1
        elif a.hard:
            out.append(f"{a.label or a.key}: buyer wants {', '.join(sorted(want))}, supplier has {', '.join(sorted(have))}")
    return out, matches


def read_attributes(line: Any, text: str | None) -> dict[str, Any]:
    """Allowed values of the line's attributes named in a reply ("red onion, 55mm+, 25 kg mesh")
    — deterministic, so a value is only ever one the company wrote."""
    t = " " + " ".join((text or "").lower().split()) + " "
    out: dict[str, Any] = {}
    for a in own_attributes(line):
        found = [v for v in a.values if v and re.search(r"(?<![a-z0-9])" + re.escape(v.lower()) + r"(?![a-z0-9])", t)]
        if found:
            out[a.key] = found[0] if len(found) == 1 else found
    return out


def summary_line(facts: ProductFacts | None, line: Any = None) -> str:
    """One crisp line: "Onion · red · 55 mm+ · 25 kg mesh · 40 MT/order · CIF Jebel Ali"."""
    if not facts:
        return ""
    parts: list[str] = [facts.product] if facts.product else []
    order = [a.key for a in template(line)] if line is not None else list(facts.facts)
    for key in order + [k for k in facts.facts if k not in order]:
        f = facts.facts.get(key)
        if f is None or key in ("destination_port", "incoterm"):
            continue
        v = ", ".join(map(str, f.value)) if isinstance(f.value, list) else str(f.value)
        if key in ("quantity_per_order_mt",):
            v = f"{v} MT/order"
        elif key == "capacity_mt_per_month":
            v = f"{v} MT/month"
        elif key == "price_usd_per_mt":
            v = f"USD {v}/MT"
        parts.append(v)
    inc, port = facts.facts.get("incoterm"), facts.facts.get("destination_port")
    if inc or port:
        parts.append(" ".join(str(x.value) for x in (inc, port) if x))
    return " · ".join(p for p in parts if p)


# ── Mappings from the deal-side records ─────────────────────────────────────

def from_buyer_requirement(req: Any) -> dict[str, Any]:
    if req is None:
        return {}
    g = lambda k: getattr(req, k, None)  # noqa: E731
    return {"grade": g("grade"), "packing": g("packaging"), "quantity_per_order_mt": g("quantity_mt"),
            "destination_port": g("destination_port"), "incoterm": g("incoterm"),
            "target_price_usd_per_mt": g("target_price_usd_per_mt"), "certificates": g("certifications") or None,
            "delivery_window": g("delivery_window"), "payment_terms": g("payment_terms"),
            **(getattr(req, "spec", None) or {})}


def from_supplier_offer(offer: Any) -> dict[str, Any]:
    if offer is None:
        return {}
    g = lambda k: getattr(offer, k, None)  # noqa: E731
    return {"grade": g("grade"), "capacity_mt_per_month": g("capacity_mt_per_month"), "moq_mt": g("moq_mt"),
            "price_usd_per_mt": g("fob_price_usd_per_mt"), "incoterm": g("incoterm"),
            "port_of_loading": g("port_of_loading"), "certificates": g("certifications") or None,
            "lead_time_days": g("lead_time_days"), "payment_terms": g("payment_terms"),
            **(getattr(offer, "spec", None) or {})}


# ── The agreed specification of a deal (todo 15.3 "carried into the deal") ──────────────

def agreed_spec(deal: Any) -> dict[str, Any]:
    """What the buyer asked for and the supplier supplies, from the confirmed pair: the buyer's
    requirement spec (and grade), narrowed to the supplier's value where both name one. Empty
    when nothing was stated — the documents then keep the HS description alone."""
    sel = getattr(deal, "selected_pair", None) or {}
    cand = next((c for c in getattr(deal, "buyer_candidates", None) or []
                 if getattr(c, "candidate_id", None) == sel.get("candidate_id")), None)
    if cand is None:
        cand = next((c for c in getattr(deal, "buyer_candidates", None) or []
                     if str(getattr(c, "status", "")) == "selected"), None)
    offer = next((o for o in getattr(deal, "supplier_offers", None) or []
                  if getattr(o, "offer_id", None) == sel.get("offer_id")), None)
    req = getattr(cand, "requirement", None) if cand is not None else None
    out: dict[str, Any] = {}
    if req is not None and getattr(req, "grade", None):
        out["grade"] = req.grade
    out.update(dict(getattr(req, "spec", None) or {}) if req is not None else {})
    for k, v in (getattr(offer, "spec", None) or {}).items():
        if k not in out:
            continue
        have = v if isinstance(v, list) else [v]
        want = out[k] if isinstance(out[k], list) else [out[k]]
        both = [w for w in want if w in have]
        if both:
            out[k] = both[0] if len(both) == 1 else both
    return out


def spec_text(deal: Any) -> str:
    """"red · 55 mm+ · 25 kg mesh" for documents and booking notes ("" when nothing agreed)."""
    parts = []
    for v in agreed_spec(deal).values():
        parts.append(", ".join(map(str, v)) if isinstance(v, list) else str(v))
    return " · ".join(p for p in parts if p)
