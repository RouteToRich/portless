"""PortLess as the exporter of record, for every document it issues.

Values come from the environment (the same ``PORTLESS_*`` settings the e-mail
signature uses, see ``shared/company_profile.py``) with the services' entity
settings as fallback. The services ship a placeholder address
("12 Commerce Street, Mumbai") as their default: it is never printed on a trade
document — an unset address makes the document a DRAFT instead.

Environment (all optional; a missing required one makes the document DRAFT):
  PORTLESS_LEGAL_NAME / PORTLESS_ADDRESS / PORTLESS_IEC / PORTLESS_GSTIN / PORTLESS_PAN
  PORTLESS_LUT_ARN          GST Letter of Undertaking ARN (export without payment of IGST)
  PORTLESS_AD_CODE          Authorised Dealer bank code registered on ICEGATE
  PORTLESS_BANK_NAME / PORTLESS_BANK_ACCOUNT / PORTLESS_BANK_SWIFT
  PORTLESS_SIGNATORY_NAME / PORTLESS_SIGNATORY_TITLE   who signs documents
  PORTLESS_PHONE / PORTLESS_CONTACT_EMAIL / PORTLESS_WEBSITE
"""
from __future__ import annotations

import os
from typing import Any

_PLACEHOLDER_ADDRESSES = ("12 commerce street",)


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _entity(attr: str) -> str:
    try:
        from services.docugen.config import settings  # type: ignore[import]
        return str(getattr(settings, attr, "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _real_address(value: str) -> str:
    return "" if any(p in value.lower() for p in _PLACEHOLDER_ADDRESSES) else value


def exporter() -> dict[str, Any]:
    from shared import company_profile
    signatory = _env("PORTLESS_SIGNATORY_NAME") or _env("PORTLESS_SENDER_NAME")
    title = _env("PORTLESS_SIGNATORY_TITLE") or _env("PORTLESS_SENDER_TITLE") or ("Authorised Signatory" if signatory else "")
    return {
        "brand": company_profile.brand(),
        "name": company_profile.legal_name(),
        "address": _env("PORTLESS_ADDRESS") or _real_address(_entity("portless_entity_address")),
        "iec": _env("PORTLESS_IEC") or _entity("portless_entity_iec"),
        "gstin": _env("PORTLESS_GSTIN") or _entity("portless_entity_gstin"),
        "pan": _env("PORTLESS_PAN") or _entity("portless_entity_pan"),
        "lut_arn": _env("PORTLESS_LUT_ARN"),
        "ad_code": _env("PORTLESS_AD_CODE"),
        "bank_name": _env("PORTLESS_BANK_NAME"),
        "bank_account": _env("PORTLESS_BANK_ACCOUNT"),
        "bank_swift": _env("PORTLESS_BANK_SWIFT"),
        "phone": _env("PORTLESS_PHONE"),
        "email": _env("PORTLESS_CONTACT_EMAIL"),
        "website": _env("PORTLESS_WEBSITE"),
        "country": "India",
        "signatory": signatory,
        "signatory_title": title,
    }


EXPORTER_LABELS = {
    "address": "exporter address (PORTLESS_ADDRESS)",
    "iec": "exporter IEC (PORTLESS_IEC)",
    "gstin": "exporter GSTIN (PORTLESS_GSTIN)",
    "lut_arn": "GST LUT ARN (PORTLESS_LUT_ARN)",
    "ad_code": "AD bank code (PORTLESS_AD_CODE)",
}


def missing(fields: tuple[str, ...]) -> list[str]:
    """Human labels of the exporter facts a document needs but the environment does not have."""
    data = exporter()
    return [EXPORTER_LABELS.get(f, f) for f in fields if not data.get(f)]
