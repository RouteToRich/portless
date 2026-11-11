"""Live or sandbox — how the third-party connectors that move goods and money behave.

QC inspection (``shared/connectors/qc_providers.py``), freight + customs
(``shields.py``) and escrow + payouts (``escrow.py``, ``payments.py``) share one
implementation for the real workflow and the automated one. Only this switch
differs:

  ``INTEGRATION_MODE=live`` (default)
      Every call goes to the real provider with the configured credentials. A
      missing key or a ``.env`` placeholder (``your_..._here``) is sent as-is,
      so the provider refuses it and the failure surfaces (ConnectorError →
      502 / ``deal.exception``) — never a made-up success. Replacing the
      placeholder with the real key is all it takes to go live.

  ``INTEGRATION_MODE=sandbox``
      Stub answers (and the QC provider simulator, ``services/qc_sandbox``, for
      the providers in ``QC_SANDBOX_PROVIDERS``). Set only by the automation
      worker / view server (``services/automation/safety.worker_env``) and the
      pytest suite (``tests/conftest.py``). Readiness ``integration_mode``
      fails in production while it is set.
"""
from __future__ import annotations

import os

LIVE = "live"
SANDBOX = "sandbox"


def mode() -> str:
    value = os.getenv("INTEGRATION_MODE", LIVE).strip().lower()
    return SANDBOX if value == SANDBOX else LIVE


def sandbox() -> bool:
    return mode() == SANDBOX


def live() -> bool:
    return mode() == LIVE


def is_placeholder(value: str | None) -> bool:
    """A ``.env`` placeholder such as ``your_tazapay_api_key_here``."""
    v = (value or "").strip().lower()
    return v.startswith("your_") or v.endswith("_here") or v in ("changeme", "change_me", "placeholder", "todo")


def configured(*names: str) -> bool:
    """Every named variable holds a real value (not empty, not a placeholder)."""
    return all((v := os.getenv(n, "").strip()) and not is_placeholder(v) for n in names)
