"""The supplier's facility address — where the goods are produced and inspected.

A scan knows a supplier's town, never its street, and the inspection agency
(QIMA's ``supplierAddress``, the SGS / BV booking request) needs the place the
inspector drives to. So the address is:

  asked      — in the negotiation (``missing_information``), in the firm order
               request, in the PO e-mail, and on its own (``facility_address_request``)
               when the inspection is waiting for it;
  read       — without a model, from the new text of any verified e-mail of the
               deal's supplier (``capture``): a labelled address ("Factory address: …")
               or a line with an Indian PIN code;
  required   — ``QC_FACILITY_ADDRESS_REQUIRED`` (default on; pinned off in
               tests/conftest.py and the automation catalogue, like the goods-ready
               date) holds the QC booking until it is known. Staff can set it with
               ``PATCH /deals/{id}/parties/supplier`` (``address``).

An address an operator entered is never overwritten by one read from an e-mail.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

_PIN = re.compile(r"(?<![\d+])[1-9]\d{2}\s?\d{3}(?!\d)")
_LABEL = re.compile(
    r"(?:factory|facility|plant|mill|works|unit|warehouse|godown|production|registered|our|the|works)?\s*"
    r"address(?:\s+(?:of|for)\s+(?:the|our)\s+(?:factory|facility|plant|mill|unit|warehouse))?\s*(?:is)?\s*[:\-–]\s*(.*)",
    re.IGNORECASE)
_LOCATED = re.compile(r"\b(?:located at|situated at|our (?:factory|facility|plant|mill) is at)\s+(.*)", re.IGNORECASE)
_STOP = re.compile(r"(?:regards|thank|sincerely|best|cheers|phone|tel|mobile|e-?mail|gstin|iec|www\.|http|@)", re.IGNORECASE)


def required() -> bool:
    return os.getenv("QC_FACILITY_ADDRESS_REQUIRED", "true").strip().lower() not in ("0", "false", "no", "off")


def is_complete(address: Any, country: Any = "IN") -> bool:
    """An address an inspector can drive to: an Indian PIN code, or a street
    number with at least a town after it. "Thanjavur, Tamil Nadu" is not."""
    text = str(address or "").strip()
    if len(text) < 8:
        return False
    if str(country or "IN").upper()[:2] == "IN" and _PIN.search(text):
        return True
    parts = [p for p in re.split(r"[,\n]", text) if p.strip()]
    return bool(re.search(r"\d", text)) and len(parts) >= 2


def _new_text(body: str) -> str:
    lines = []
    for line in (body or "").splitlines():
        if re.match(r"^\s*on .{0,200}wrote:\s*$", line, re.IGNORECASE) or line.strip().startswith("-----"):
            break
        if not line.lstrip().startswith(">"):
            lines.append(line.rstrip())
    return "\n".join(lines)


def _clean(parts: list[str]) -> str:
    out = re.sub(r"\s{2,}", " ", ", ".join(p.strip(" ,.;") for p in parts if p.strip(" ,.;")))
    pin = _PIN.search(out)
    return out[:pin.end()] if pin else out  # an Indian address ends with its PIN code


def parse(text: str | None, country: Any = "IN") -> str | None:
    """The facility address a supplier's e-mail gives, or None."""
    lines = _new_text(text or "").splitlines()
    for i, line in enumerate(lines):
        m = _LABEL.search(line) or _LOCATED.search(line)
        if not m:
            continue
        parts = [m.group(1)] if m.group(1).strip() else []
        for nxt in lines[i + 1:i + 5]:
            if not nxt.strip() or _STOP.search(nxt) or len(nxt) > 120:
                break
            parts.append(nxt)
            if _PIN.search(nxt):
                break
        candidate = _clean(parts)
        if is_complete(candidate, country):
            return candidate[:300]
    for i, line in enumerate(lines):  # an unlabelled address block ending in a PIN code
        if _PIN.search(line) and not _STOP.search(line) and len(line) <= 160:
            parts = [line]
            for prev in reversed(lines[max(0, i - 2):i]):
                if not prev.strip() or _STOP.search(prev) or len(prev) > 120 or prev.rstrip().endswith((".", "?", ":")):
                    break
                parts.insert(0, prev)
            candidate = _clean(parts)
            if is_complete(candidate, country):
                return candidate[:300]
    return None


async def current(deal_id: str) -> tuple[str | None, bool]:
    """(address on the deal's supplier party, whether it is complete)."""
    try:
        from services.deals.service import get_deal  # type: ignore[import]
        deal = await get_deal(deal_id)
    except Exception:  # noqa: BLE001
        return None, False
    party = next((p for p in deal.parties if str(getattr(p.role, "value", p.role)).lower() == "supplier"), None)
    if party is None:
        return None, False
    address = getattr(party, "address", None)
    return address, is_complete(address, getattr(party, "country", None) or "IN")


async def capture(deal_id: str, body: str, *, by: str) -> str | None:
    """Record the facility address a verified supplier e-mail gives. Returns it
    when it is new (and complete), else None. Never raises."""
    try:
        from services.deals.service import get_deal, update_party  # type: ignore[import]
        deal = await get_deal(deal_id)
        party = next((p for p in deal.parties if str(getattr(p.role, "value", p.role)).lower() == "supplier"), None)
        if party is None:
            return None
        found = parse(body, party.country)
        if not found or (party.address or "").strip().lower() == found.lower():
            return None
        if party.contact_source == "operator" and is_complete(party.address, party.country):
            return None  # a person entered it; an e-mail never overrides that
        await update_party(deal_id, "supplier", {"address": found}, source=None)
        logger.info("SupplierAddress: deal %s — facility address from %s: %s", deal_id, by, found)
        return found
    except Exception as exc:  # noqa: BLE001
        logger.warning("SupplierAddress: deal %s — address not read: %s", deal_id, exc)
        return None
