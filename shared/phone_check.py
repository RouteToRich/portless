"""Phone check for phone-only local suppliers (todo: number valid, WhatsApp reachable).

Offline, no paid lookup: the number is normalised to E.164 and checked against the numbering
plan of its country —

* **India** (+91): a mobile is 10 digits starting 6–9; a landline is STD code + subscriber number,
  10 digits starting 1–5 once the trunk ``0`` is dropped; toll-free / premium (1800 / 1860 / 1900)
  are not a company line.
* elsewhere: 8–15 digits with the country code (ITU E.164 length), country code taken from
  the number or the company's country.

``whatsapp`` is ``unknown`` until staff confirm it (a message answered on WhatsApp —
``POST /trade-min/suppliers/{id}/phone-check``): there is no free, permitted way to probe it.
Pure functions; never raises.
"""
from __future__ import annotations

import re
from typing import Any

_CC = {"IN": "91", "AE": "971", "SA": "966", "QA": "974", "OM": "968", "KW": "965", "BH": "973",
       "GB": "44", "DE": "49", "FR": "33", "IT": "39", "ES": "34", "NL": "31", "BE": "32", "US": "1",
       "CA": "1", "SG": "65", "MY": "60", "BD": "880", "LK": "94", "NP": "977", "AU": "61", "JP": "81"}
_BAD_IN = ("1800", "1860", "1900")


def _digits(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


def check(raw: str | None, country: str | None = "IN") -> dict[str, Any]:
    """``{status, e164, kind, reason}`` — status ``valid`` / ``invalid`` / ``missing``;
    kind ``mobile`` / ``landline`` / ``other``."""
    text = (raw or "").strip()
    if not text:
        return {"status": "missing", "e164": None, "kind": None, "reason": "no phone number"}
    # several numbers in one field ("98xxxxxxx / 0712-…"): check the first that parses
    parts = [p for p in re.split(r"[,;/|]| or ", text) if _digits(p)]
    if len(parts) > 1:
        for part in parts:
            r = check(part, country)
            if r["status"] == "valid":
                return r
        return check(parts[0], country)
    cc = (country or "IN").upper()
    plus = text.startswith("+") or text.startswith("00")
    d = _digits(text)
    if text.startswith("00"):
        d = d[2:]
    if cc == "IN" or d.startswith("91") and plus:
        if plus or (d.startswith("91") and len(d) == 12):
            if not d.startswith("91"):
                return _generic(d, plus=True)
            d = d[2:]
        std = bool(re.match(r"^\s*0\d{2,4}[\s\-)]", text))  # "0712 2551234": trunk 0 + STD code
        d = d.lstrip("0")
        if d.startswith(_BAD_IN):
            return {"status": "invalid", "e164": "+91" + d, "kind": "other",
                    "reason": "toll-free / premium number, not a company line"}
        if len(d) != 10:
            return {"status": "invalid", "e164": None, "kind": None,
                    "reason": f"an Indian number has 10 digits after +91 / 0 ({len(d)} given)"}
        if d[0] in "6789" and not std:
            return {"status": "valid", "e164": "+91" + d, "kind": "mobile", "reason": "Indian mobile"}
        if d[0] in "12345" or std:
            return {"status": "valid", "e164": "+91" + d, "kind": "landline", "reason": "Indian landline (STD + number)"}
        return {"status": "invalid", "e164": None, "kind": None, "reason": "Indian numbers start 1–9"}
    if not plus:
        code = _CC.get(cc)
        if code and not d.startswith(code):
            d = code + d.lstrip("0")
    return _generic(d, plus=True)


def _generic(d: str, *, plus: bool) -> dict[str, Any]:
    if 8 <= len(d) <= 15 and d[0] != "0":
        return {"status": "valid", "e164": "+" + d, "kind": "other", "reason": "E.164 length"}
    return {"status": "invalid", "e164": None, "kind": None, "reason": f"{len(d)} digits is not a full international number"}


def whatsapp_link(e164: str | None) -> str | None:
    """``https://wa.me/<digits>`` for staff to open a chat (no message is sent by PortLess)."""
    d = _digits(e164 or "")
    return f"https://wa.me/{d}" if d else None
