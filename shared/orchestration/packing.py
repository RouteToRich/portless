"""The buyer's packing requirements — asked, passed on, inspected and priced.

Packing is part of what the buyer buys: bag type and weight, liner, the
buyer's marking or private label, pallets, fumigation, container lining. A lot
that is fine but packed the wrong way is rejected at destination, so:

1. **Asked** — the negotiation's agreement checklist (``guards.missing_agreement_info``)
   withholds the agreement until the buyer has stated its packing, or that the
   supplier's standard export packing suits it (``NEGOTIATION_PACKING_REQUIRED``,
   default on). Read from the buyer's e-mail by ``parse_packing`` (no model), from
   its outreach reply (``BuyerRequirement.packaging``) or set by staff
   (``PUT /deals/{id}/packing``). Stored on the negotiation session.
2. **Passed to the supplier** — the firm order request, the PO and the PI state it;
   the supplier's confirmation of the order request confirms its price includes it.
3. **Passed to the inspector** — the QC booking carries it (QIMA ``unitPacking`` /
   ``outerPacking`` / ``sampleReferenceClientComments``; the portal booking request's
   ``packing`` section) with the checks to make (``qc_checklist``).
4. **Priced** — special packing adds a ``packing`` line to the cost sheet
   (``surcharge``, settings below) paid by PortLess until the supplier confirms its
   price includes the packing; standard packing costs nothing extra (inside FOB).

Settings (USD; estimates until the supplier prices the packing):
  TRADE_COST_PACKING_PRINTED_BAGS_USD_PER_MT (6)    buyer's marking / private label printed bags
  TRADE_COST_PACKING_JUTE_USD_PER_MT (18)           jute instead of PP bags
  TRADE_COST_PACKING_SMALL_PACKS_USD_PER_MT (45)    retail packs of 10 kg or less / cartons
  TRADE_COST_PACKING_FIBC_USD_PER_MT (8)            jumbo bags (FIBC)
  TRADE_COST_PACKING_LINER_USD_PER_MT (5)           inner liner
  TRADE_COST_PALLETS_USD_PER_CONTAINER (220)        palletised and stretch-wrapped
  TRADE_COST_FUMIGATION_USD_PER_CONTAINER (60)      fumigation + certificate
  TRADE_COST_CONTAINER_LINING_USD_PER_CONTAINER (90) kraft lining / desiccant
  TRADE_COST_FLEXIBAG_USD_PER_CONTAINER (300)       bulk in a flexi-bag
"""
from __future__ import annotations

import os
import re
from typing import Any

from pydantic import BaseModel, Field

_MT_PER_CONTAINER = 25.0


def required() -> bool:
    return os.getenv("NEGOTIATION_PACKING_REQUIRED", "true").strip().lower() not in ("0", "false", "no", "off")


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


class PackingSpec(BaseModel):
    """What the buyer asked for. ``standard_ok`` = the supplier's usual export packing suits it."""
    standard_ok: bool = False
    material: str | None = None
    """pp | jute | bopp | paper | non_woven | carton | fibc | bulk"""
    unit_weight_kg: float | None = None
    inner_liner: bool = False
    marking: str | None = None
    """What goes on each bag / carton (buyer's marks, language, lot number …)."""
    private_label: bool = False
    palletised: bool = False
    fumigation: bool = False
    container_lining: bool = False
    notes: str | None = None
    source: str = "buyer e-mail"
    raw: str | None = None
    """The buyer's own words."""


_MATERIALS = [
    ("fibc", r"\b(?:fibc|jumbo\s+bags?|big\s+bags?|bulk\s+bags?|ton(?:ne)?\s+bags?)\b"),
    ("jute", r"\b(?:jute|gunny|hessian)\b"),
    ("bopp", r"\bbopp\b"),
    ("non_woven", r"\bnon[-\s]?woven\b"),
    ("paper", r"\b(?:paper|kraft)\s+(?:bags?|sacks?)\b"),
    ("carton", r"\b(?:cartons?|boxes|corrugated)\b"),
    ("pp", r"\b(?:pp|polypropylene|woven\s+poly|poly\s+woven|plastic)\b(?:\s+woven)?\s*(?:bags?|sacks?)?"),
    ("bulk", r"\b(?:in\s+bulk|bulk\s+shipment|loose\s+in\s+(?:the\s+)?container|flexi[-\s]?(?:bag|tank))\b"),
]
_WEIGHT = re.compile(r"\b(\d{1,4}(?:\.\d+)?)\s*(?:kg|kgs|kilo(?:gram)?s?)\b(?:\s*(?:net|each|per\s+(?:bag|sack|carton|pack)))?",
                     re.IGNORECASE)
_STANDARD = re.compile(r"\b(?:standard|usual|normal|regular|your)\s+(?:export\s+)?pack(?:ing|aging)\b(?:\s+is\s+(?:fine|ok|okay|acceptable))?"
                       r"|\bpack(?:ing|aging)\s+(?:as\s+)?(?:per\s+)?(?:your\s+)?(?:standard|usual|norms?)\b"
                       r"|\bany\s+(?:export\s+)?pack(?:ing|aging)\b", re.IGNORECASE)
_MARK = re.compile(r"\b(?:mark(?:ing|ings|ed)?|label(?:l?ed|s|ling)?|print(?:ed)?)\b[^.\n;,]{0,120}", re.IGNORECASE)
_PRIVATE = re.compile(r"\b(?:private\s+label|our\s+(?:own\s+)?(?:brand|logo|label)|own\s+brand|white\s+label)\b", re.IGNORECASE)


def parse_packing(text: str | None) -> PackingSpec | None:
    """The packing a buyer wrote, read without a model; None when it says nothing about packing."""
    if not text:
        return None
    low = text.lower()
    if not re.search(r"pack|bag|sack|carton|box|fibc|jute|label|marking|pallet|fumigat|liner|lined|lining|"
                     r"desiccant|silica|bulk|flexi", low):
        return None
    spec = PackingSpec(raw=text.strip()[:600])
    found = False
    for name, pat in _MATERIALS:
        if re.search(pat, low, re.IGNORECASE):
            if name == "pp" and not re.search(r"\b(?:bags?|sacks?)\b", low):
                continue
            spec.material, found = name, True
            break
    m = _WEIGHT.search(text)
    if m and (found or re.search(r"\b(?:bags?|sacks?|cartons?|packs?)\b", low)):
        w = float(m.group(1))
        if 0.1 <= w <= 2000:
            spec.unit_weight_kg, found = w, True
    if re.search(r"\b(?:inner\s+liner|liner|laminated|ldpe|hdpe\s+liner)\b", low):
        spec.inner_liner, found = True, True
    if _PRIVATE.search(text):
        spec.private_label, found = True, True
    mk = _MARK.search(text)
    if mk and len(mk.group(0).split()) >= 3 and re.search(
            r"\b(?:bag|sack|carton|pack|box)s?\b|\blabel|\bmark", mk.group(0), re.IGNORECASE):
        spec.marking, found = mk.group(0).strip(" ,"), True
    if re.search(r"\bpallet", low):
        spec.palletised, found = True, True
    if re.search(r"\bfumigat", low):
        spec.fumigation, found = True, True
    if re.search(r"\b(?:container\s+lining|lined\s+container|container\b[^.]{0,40}\blin(?:ed|ing)\b|kraft\s+(?:paper\s+)?lining|desiccant|silica\s+gel|dry\s+bags?)\b", low):
        spec.container_lining, found = True, True
    if _STANDARD.search(text) and not (spec.material or spec.private_label or spec.marking or spec.palletised):
        spec.standard_ok, found = True, True
    return spec if found else None


def merge(old: PackingSpec | dict | None, new: PackingSpec | None) -> PackingSpec | None:
    """A later message adds to (or corrects) what was said before."""
    if new is None:
        return PackingSpec(**old) if isinstance(old, dict) else old
    if old is None:
        return new
    base = PackingSpec(**old) if isinstance(old, dict) else old.model_copy()
    data = base.model_dump()
    for k, v in new.model_dump().items():
        if k in ("source", "raw"):
            continue
        if v not in (None, False, ""):
            data[k] = v
    if new.material or new.private_label or new.marking or new.palletised:
        data["standard_ok"] = False
    data["raw"] = "; ".join(filter(None, [base.raw, new.raw]))[-1200:]
    return PackingSpec(**data)


def as_spec(value: Any) -> PackingSpec | None:
    if value is None:
        return None
    return value if isinstance(value, PackingSpec) else PackingSpec(**value)


_MATERIAL_NAMES = {"pp": "PP woven bags", "jute": "jute bags", "bopp": "BOPP laminated bags", "paper": "paper bags",
                   "non_woven": "non-woven bags", "carton": "cartons", "fibc": "jumbo bags (FIBC)", "bulk": "bulk"}


def describe(spec: Any) -> str:
    """One line for the order request, PO, PI and the inspector."""
    s = as_spec(spec)
    if s is None:
        return ""
    if s.standard_ok and not s.material:
        return "Supplier's standard export packing (buyer accepts it)"
    parts = []
    what = _MATERIAL_NAMES.get(s.material or "", "bags")
    parts.append(f"{s.unit_weight_kg:g} kg {what}" if s.unit_weight_kg else what.capitalize())
    if s.inner_liner:
        parts.append("with inner liner")
    if s.private_label:
        parts.append("buyer's private label / brand")
    if s.marking:
        parts.append(f"marking: {s.marking}")
    if s.palletised:
        parts.append("palletised and stretch-wrapped")
    if s.fumigation:
        parts.append("fumigated (certificate required)")
    if s.container_lining:
        parts.append("container lined (kraft / desiccant)")
    if s.notes:
        parts.append(s.notes)
    return "; ".join(parts)


def special(spec: Any) -> bool:
    s = as_spec(spec)
    return bool(s) and not (s.standard_ok and not s.material)


def surcharge(spec: Any, quantity_mt: float) -> tuple[float, list[str]]:
    """Estimated extra cost per MT of the buyer's packing over the supplier's
    standard (PP bags, no extras), with the parts that make it up."""
    s = as_spec(spec)
    if s is None or not special(s):
        return 0.0, []
    qty = quantity_mt if quantity_mt and quantity_mt > 0 else 20.0
    containers = max(1, round(qty / _MT_PER_CONTAINER + 0.49))
    per_mt = 0.0
    parts: list[str] = []

    def add(amount: float, why: str) -> None:
        nonlocal per_mt
        if amount > 0:
            per_mt += amount
            parts.append(f"{why} USD {amount:,.2f}/MT")

    if s.material == "jute":
        add(_f("TRADE_COST_PACKING_JUTE_USD_PER_MT", 18.0), "jute bags")
    elif s.material == "fibc":
        add(_f("TRADE_COST_PACKING_FIBC_USD_PER_MT", 8.0), "jumbo bags")
    if s.material == "carton" or (s.unit_weight_kg is not None and s.unit_weight_kg <= 10):
        add(_f("TRADE_COST_PACKING_SMALL_PACKS_USD_PER_MT", 45.0), "retail packs / cartons")
    if s.private_label or s.marking:
        add(_f("TRADE_COST_PACKING_PRINTED_BAGS_USD_PER_MT", 6.0), "printed marking / label")
    if s.inner_liner:
        add(_f("TRADE_COST_PACKING_LINER_USD_PER_MT", 5.0), "inner liner")
    if s.material == "bulk" and "flexi" in (s.raw or "").lower():
        add(_f("TRADE_COST_FLEXIBAG_USD_PER_CONTAINER", 300.0) * containers / qty, "flexi-bag")
    if s.palletised:
        add(_f("TRADE_COST_PALLETS_USD_PER_CONTAINER", 220.0) * containers / qty, "pallets")
    if s.fumigation:
        add(_f("TRADE_COST_FUMIGATION_USD_PER_CONTAINER", 60.0) * containers / qty, "fumigation")
    if s.container_lining:
        add(_f("TRADE_COST_CONTAINER_LINING_USD_PER_CONTAINER", 90.0) * containers / qty, "container lining")
    return round(per_mt, 2), parts


def qc_checklist(spec: Any) -> list[str]:
    """What the inspector checks on the packing."""
    s = as_spec(spec)
    if s is None:
        return []
    if s.standard_ok and not s.material:
        return ["Packing: supplier's standard export packing — check it is intact, clean and sea-worthy"]
    checks = [f"Packing as ordered: {describe(s)}"]
    if s.unit_weight_kg:
        checks.append(f"Weigh a sample of units: net weight {s.unit_weight_kg:g} kg each (within tolerance)")
    if s.marking or s.private_label:
        checks.append("Marking / label on every unit matches the buyer's artwork (photos of the print)")
    if s.inner_liner:
        checks.append("Inner liner present and sealed")
    if s.palletised:
        checks.append("Pallets: count, wrapping and stability")
    if s.fumigation:
        checks.append("Fumigation done before loading; certificate number recorded")
    if s.container_lining:
        checks.append("Container lined (kraft / desiccant) before stuffing")
    return checks
