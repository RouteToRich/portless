"""Personal data out of the logs.

Log lines name counterparties' mailboxes and phone numbers ("Outreach: intro to
m.turre@glaconchemie.de …"). A filter on the root handlers masks them before anything is
written, keeping what an operator needs to recognise the company:

- e-mail ``m.turre@glaconchemie.de`` → ``m***@glaconchemie.de`` (first character + domain);
- phone numbers written internationally (``+91 98450 12345``) or labelled
  (``phone: 9845012345``, ``mobile``, ``tel``, ``WhatsApp``) → ``+91 *****45``-style
  (country prefix and the last two digits).

Dates, amounts, GSTIN / IEC and ids are untouched. ``LOG_REDACT_PII=false`` switches it off
(local debugging). Installed once from the app lifespan (``install()``).
"""
from __future__ import annotations

import logging
import os
import re

_EMAIL = re.compile(r"(?<![\w.+-])([\w.+-])[\w.+-]*@((?:[\w-]+\.)+[A-Za-z]{2,})")
_INTL_PHONE = re.compile(r"(?<![\w+])\+(\d{1,3})[\s\-()]*((?:\d[\s\-()]*){5,13}\d)")
_LABELLED_PHONE = re.compile(
    r"(?i)\b(phone|mobile|tel|telephone|whatsapp|cell)(\s*(?:no\.?|number)?\s*[:=#]?\s*)(\d[\d\s\-()]{6,}\d)")


def _mask_digits(digits: str) -> str:
    only = re.sub(r"\D", "", digits)
    return "*" * max(0, len(only) - 2) + only[-2:]


def redact(text: str) -> str:
    if not text:
        return text
    text = _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
    text = _INTL_PHONE.sub(lambda m: f"+{m.group(1)} {_mask_digits(m.group(2))}", text)
    text = _LABELLED_PHONE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_mask_digits(m.group(3))}", text)
    return text


def enabled() -> bool:
    return os.getenv("LOG_REDACT_PII", "true").strip().lower() not in ("0", "false", "no", "off")


class RedactingFilter(logging.Filter):
    """Masks e-mails and phone numbers in the formatted message (and exception text)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string is logged as it is
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, ()
        if record.exc_info and not record.exc_text:
            try:
                record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            except Exception:  # noqa: BLE001
                pass
        return True


_installed = False


def install() -> bool:
    """Add the filter to every root handler (and to handlers added later via the root logger's
    filters). Idempotent; False when switched off."""
    global _installed
    if not enabled() or _installed:
        return _installed
    f = RedactingFilter()
    root = logging.getLogger()
    for h in root.handlers:
        h.addFilter(f)
    root.addFilter(f)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for h in logging.getLogger(name).handlers:
            h.addFilter(f)
    _installed = True
    return True
