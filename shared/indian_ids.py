"""Indian company identifiers read from text a supplier publishes (its website, GST record,
e-mail signature) — todo 12: Indian supplier age and size at scan time.

- **CIN** (Corporate Identity Number, MCA): ``L/U`` + 5-digit industry code + 2-letter state +
  4-digit year of incorporation + 3-letter company type + 6-digit registration number, e.g.
  ``U01100MH2005PTC123456``. The year gives the company's age without an MCA call.
- **Udyam** registration (MSME): ``UDYAM-MH-12-0012345`` — the company is a registered
  micro / small / medium enterprise.

Pure functions; nothing here calls a registry.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

_CIN = re.compile(r"\b([LU])(\d{5})([A-Z]{2})(\d{4})(PLC|PTC|FLC|FTC|GOI|SGC|NPL|GAP|GAT|ULL|ULT|OPC)(\d{6})\b")
_UDYAM = re.compile(r"\bUDYAM-?([A-Z]{2})-?(\d{2})-?(\d{7})\b", re.IGNORECASE)


def find_cin(*texts: object) -> str | None:
    """The first valid CIN in the texts (year of incorporation between 1850 and this year)."""
    this_year = datetime.now(timezone.utc).year
    for text in texts:
        for m in _CIN.finditer(str(text or "").upper()):
            if 1850 <= int(m.group(4)) <= this_year:
                return m.group(0)
    return None


def incorporation_year(cin: str | None) -> int | None:
    m = _CIN.fullmatch(str(cin or "").strip().upper())
    if not m:
        return None
    year = int(m.group(4))
    return year if 1850 <= year <= datetime.now(timezone.utc).year else None


def company_age_years(cin: str | None, now: datetime | None = None) -> int | None:
    year = incorporation_year(cin)
    if year is None:
        return None
    return max(0, (now or datetime.now(timezone.utc)).year - year)


def is_listed(cin: str | None) -> bool:
    """``L`` = listed on a stock exchange."""
    return bool(cin) and str(cin).strip().upper().startswith("L") and incorporation_year(cin) is not None


def find_udyam(*texts: object) -> str | None:
    for text in texts:
        m = _UDYAM.search(str(text or ""))
        if m:
            return f"UDYAM-{m.group(1).upper()}-{m.group(2)}-{m.group(3)}"
    return None
