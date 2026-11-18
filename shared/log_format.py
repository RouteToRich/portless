"""Structured (JSON) logs for the hosting platform's log search (todo: structured logs).

``LOG_FORMAT=json`` (default ``text``) puts one JSON object per line on every root and uvicorn
handler: time (UTC ISO), level, logger, message, and — when set — the deal the work belongs
to (``shared.deal_costs`` scope), plus the exception text. PII is masked before formatting
(the redaction filter runs on the same handlers). Retention is a hosting setting.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

_installed = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        try:
            from shared.deal_costs import current
            deal = current()
            if deal:
                out["deal_id"] = deal
        except Exception:  # noqa: BLE001
            pass
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def enabled() -> bool:
    return os.getenv("LOG_FORMAT", "text").strip().lower() == "json"


def install() -> bool:
    """Switch every root / uvicorn handler to JSON. Idempotent; False when not asked for."""
    global _installed
    if not enabled() or _installed:
        return _installed
    fmt = JsonFormatter()
    handlers = list(logging.getLogger().handlers)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        handlers += logging.getLogger(name).handlers
    if not logging.getLogger().handlers:
        h = logging.StreamHandler()
        logging.getLogger().addHandler(h)
        handlers.append(h)
    for h in handlers:
        h.setFormatter(fmt)
    _installed = True
    return True


def reset_for_tests() -> None:
    global _installed
    _installed = False
