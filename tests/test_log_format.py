"""LOG_FORMAT=json: one JSON object per line, with the deal and masked PII."""
from __future__ import annotations

import json
import logging

from shared import deal_costs, log_format, log_redaction


def _record(msg, *args, exc=None):
    return logging.LogRecord("portless.test", logging.WARNING, __file__, 1, msg, args, exc)


def test_json_line_with_deal_and_exception():
    fmt = log_format.JsonFormatter()
    with deal_costs.deal_scope("DEAL-9"):
        line = json.loads(fmt.format(_record("RFQ %s sent", "R1")))
    assert line["message"] == "RFQ R1 sent" and line["deal_id"] == "DEAL-9" and line["level"] == "WARNING"
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        line = json.loads(fmt.format(_record("failed", exc=sys.exc_info())))
    assert "ValueError: boom" in line["exception"] and "deal_id" not in line


def test_install_only_when_asked(monkeypatch):
    log_format.reset_for_tests()
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    assert log_format.install() is False
    monkeypatch.setenv("LOG_FORMAT", "json")
    root = logging.getLogger()
    saved = [(h, h.formatter) for h in root.handlers]
    try:
        assert log_format.install() is True
        assert all(isinstance(h.formatter, log_format.JsonFormatter) for h in root.handlers)
    finally:
        for h, f in saved:
            h.setFormatter(f)
        log_format.reset_for_tests()


def test_pii_is_masked_before_it_is_formatted():
    rec = _record("mail from buyer@gulf.ae")
    log_redaction.RedactingFilter().filter(rec)
    assert "buyer@gulf.ae" not in json.loads(log_format.JsonFormatter().format(rec))["message"]
