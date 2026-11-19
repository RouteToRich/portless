"""Facts read from a company's own website text at scan time (todo 15.2 / 13.6).

The contact finder already opens the site and returns a summary / page title (``fit_reason`` on
the buyer / supplier). From that text, deterministically — a value is only ever one the site
wrote:

- the catalogue template attributes it names (variety, size, packing …), via
  ``product_spec.read_attributes`` (allowed values only);
- **certificates** (supplier quality check): FSSAI licence (14 digits), APEDA RCMC, Spices
  Board, organic (NPOP / NOP / EU organic), ISO 22000 / 9001, FSSC 22000, BRC / BRCGS, HACCP,
  GlobalG.A.P., Halal, Kosher, IFS.

They land in the record's ``product_facts`` with source ``website`` — the lowest precedence:
they fill gaps and never replace what the company wrote by e-mail, a deal or staff.
"""
from __future__ import annotations

import re
from typing import Any

_CERTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("FSSAI", re.compile(r"\bFSSAI\b|\b1\d{13}\b")),
    ("APEDA RCMC", re.compile(r"\bAPEDA\b", re.IGNORECASE)),
    ("Spices Board", re.compile(r"\bspices board\b", re.IGNORECASE)),
    ("Organic (NPOP)", re.compile(r"\b(?:NPOP|india organic|USDA organic|NOP|EU organic|certified organic)\b", re.IGNORECASE)),
    ("ISO 22000", re.compile(r"\bISO\s?22000\b", re.IGNORECASE)),
    ("ISO 9001", re.compile(r"\bISO\s?9001\b", re.IGNORECASE)),
    ("FSSC 22000", re.compile(r"\bFSSC\s?22000\b", re.IGNORECASE)),
    ("BRCGS", re.compile(r"\bBRC(?:GS)?\b")),
    ("HACCP", re.compile(r"\bHACCP\b", re.IGNORECASE)),
    ("GlobalG.A.P.", re.compile(r"\bglobal\s?g\.?a\.?p\b", re.IGNORECASE)),
    ("Halal", re.compile(r"\bhalal\b", re.IGNORECASE)),
    ("Kosher", re.compile(r"\bkosher\b", re.IGNORECASE)),
    ("IFS", re.compile(r"\bIFS (?:Food|certified)\b", re.IGNORECASE)),
)


def certificates(text: str | None) -> list[str]:
    t = text or ""
    return [name for name, rx in _CERTS if rx.search(t)]


def facts_from_text(hs_code: str, text: str | None) -> dict[str, Any]:
    """Template attributes + certificates the text names ({} when nothing)."""
    if not text:
        return {}
    out: dict[str, Any] = {}
    try:
        from shared import catalogue, product_spec
        out.update(product_spec.read_attributes(catalogue.line_for(hs_code), text))
    except Exception:  # noqa: BLE001
        pass
    certs = certificates(text)
    if certs:
        out["certificates"] = certs
    return out


def product_facts_json(hs_code: str, text: str | None, ref: str | None = None) -> dict[str, Any]:
    """``CounterpartyRecord.product_facts`` entry for a scan (empty dict when nothing was read)."""
    values = facts_from_text(hs_code, text)
    if not values:
        return {}
    from shared import product_spec
    code = "".join(ch for ch in str(hs_code or "") if ch.isdigit())[:6]
    pf, _ = product_spec.merge(None, code, values, source="website", ref=ref)
    return {code: pf.model_dump(mode="json")}
