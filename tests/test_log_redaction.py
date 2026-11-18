"""Personal data masked in log lines (shared/log_redaction.py)."""
from __future__ import annotations

import logging

import pytest

from shared.log_redaction import RedactingFilter, redact


@pytest.mark.parametrize("text,expected", [
    ("Outreach: intro to m.turre@glaconchemie.de sent", "Outreach: intro to m***@glaconchemie.de sent"),
    ("call +91 98450 12345 now", "call +91 ********45 now"),
    ("supplier phone: 9845012345", "supplier phone: ********45"),
    ("WhatsApp 98450-12345 (owner)", "WhatsApp ********45 (owner)"),
])
def test_mailboxes_and_phones_are_masked(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize("text", [
    "deal D-1 moved on 2026-11-18 at 10:05",
    "PO total USD 1,250,000.00 for 25 MT",
    "IEC 0512345678 GSTIN 32ABCDE1234F1Z5",
    "RFQ-ABC123 / EMAIL-0A1B2C3D4E",
])
def test_business_facts_stay_readable(text):
    assert redact(text) == text


def test_the_filter_rewrites_the_formatted_record():
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "sent to %s (%s)", ("a.b@gulf.ae", "+971 4 123 4567"), None)
    assert RedactingFilter().filter(rec) is True
    assert rec.getMessage() == "sent to a***@gulf.ae (+971 ******67)"
