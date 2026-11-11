"""Suite-wide test configuration.

The unit-test suite must never talk to the real NATS broker: a Synadia
``*.creds`` file sitting in the repository root (gitignored) would otherwise make
``EVENT_BUS_BACKEND=auto`` pick the NATS backend inside any lifespan a test
happens to run. Pin the in-process bus for the whole suite; the NATS code
paths are tested with fakes in ``test_event_bus.py``.
"""
from __future__ import annotations

import os
from pathlib import Path

os.environ["EVENT_BUS_BACKEND"] = "inprocess"

# The PortLess-owned Apify actors cost money per run. A developer logged in
# with the Apify CLI (~/.apify/auth.json) must never have a unit test start a
# real actor run: pin the connector to stub mode for the whole suite. Tests
# that exercise the connector itself monkeypatch ``run()`` explicitly.
os.environ["APIFY_TOKEN"] = ""
os.environ["APIFY_TOKEN_AUTODISCOVER"] = "false"

# .env (gitignored) holds the real MongoDB cluster and the shared LLM
# Gateway credential, and shared/config.py loads it into os.environ. A unit
# test must never write to the live database or spend gateway budget: pin both
# off (an env var set here wins over the .env file). Gateway calls are tested
# with a mocked transport in tests/llm_gateway/test_remote.py.
os.environ["MONGO_URI"] = ""
os.environ["LLM_GATEWAY_URL"] = ""

# Trade compliance (shared/orchestration/compliance.py) is on by default and
# holds most fixtures (an unconfirmed HS code is a REVIEW, a deal without an
# inspector's seal never gets a freight gate). The pipeline tests exercise the
# flow around it, so the suite runs with it off; tests/test_compliance.py turns
# it on per test.
os.environ["COMPLIANCE_ENABLED"] = "false"
os.environ["COMPLIANCE_LOADING_CHECK_REQUIRED"] = "false"

# The supplier holdback until buyer acceptance is on by default and changes
# every payout amount; the money-chain tests pin the payout arithmetic around
# it. tests/test_supplier_retention.py turns it on per test.
os.environ["SUPPLIER_RETENTION_ENABLED"] = "false"

# The supplier confirms the order before the PI, and acknowledges the PO before
# the inspection (shared/orchestration/supplier_commitment.py). Both are on by
# default and add a supplier reply to every fixture's path; the pipeline tests
# exercise the flow around them. tests/test_supplier_commitment.py turns them on.
os.environ["SUPPLIER_CONFIRMATION_REQUIRED"] = "false"
os.environ["PO_ACK_REQUIRED"] = "false"
# Likewise the inspection waits for the supplier's goods-ready date
# (shared/orchestration/qc_schedule.py); tests/test_qc_inspection.py turns it on.
os.environ["QC_READINESS_DATE_REQUIRED"] = "false"
os.environ["QC_FACILITY_ADDRESS_REQUIRED"] = "false"  # the suites book QC without a street address
# The negotiation also waits for the buyer's packing requirements before an
# agreement (shared/orchestration/packing.py); tests/test_packing.py turns it on.
os.environ["NEGOTIATION_PACKING_REQUIRED"] = "false"
# QC, freight, customs, escrow and payout connectors answer from stubs only in
# INTEGRATION_MODE=sandbox (shared/integration_mode.py); live mode always calls
# the provider. The suite runs in sandbox; tests of the live HTTP paths set
# INTEGRATION_MODE=live with a mocked transport. The QC provider simulator is
# opt-in per test (tests/test_qc_inspection.py).
os.environ["INTEGRATION_MODE"] = "sandbox"
# Only QIMA is booked in production (QC_PROVIDERS_ENABLED default "qima"); the
# suite also covers the dormant SGS / BV / Intertek / TUV connectors, so all are
# enabled here. tests/test_qc_inspection.py checks the QIMA-only default.
os.environ["QC_PROVIDERS_ENABLED"] = "all"
os.environ.pop("QC_SANDBOX_PROVIDERS", None)
# The escrow provider simulator is opt-in per test too (tests/test_escrow_providers.py);
# both providers are offered, Tazapay is the default.
os.environ.pop("ESCROW_SANDBOX_PROVIDERS", None)
# Carrier simulator opt-in per test (tests/test_maersk_freight.py); Maersk books freight,
# Cogoport quotes; every deal gets the pre-shipment inspection only unless a test asks.
os.environ.pop("CARRIER_SANDBOX_PROVIDERS", None)
os.environ["FREIGHT_PROVIDER"] = "maersk"
os.environ["FREIGHT_QUOTE_PROVIDER"] = "cogoport"
os.environ["QC_DEFAULT_INSPECTION_TYPES"] = "PSI"
os.environ.pop("ESCROW_PROVIDERS_ENABLED", None)
os.environ.pop("ESCROW_DEFAULT_PROVIDER", None)
for _proof in ("PORTLESS_PUBLIC_BASE_URL", "ESCROW_PROOF_LINK_SECRET"):
    os.environ.pop(_proof, None)

# The developer's .env holds provider placeholders (your_..._here) until the real
# keys arrive. Blank them before anything loads .env (load_dotenv never
# overrides a variable that is already set), so the suite behaves the same with
# or without a local .env — e.g. a placeholder webhook secret would otherwise
# reject every test webhook.
try:
    from dotenv import dotenv_values

    from shared.integration_mode import is_placeholder

    for _key, _value in dotenv_values(Path(__file__).resolve().parent.parent / ".env").items():
        if _value and is_placeholder(_value):
            os.environ[_key] = ""
except Exception:  # noqa: BLE001 — no .env, no python-dotenv: nothing to neutralise
    pass

# The pre-trade market check searches the web; the suite never goes online.
# tests/test_trade_costs.py exercises it with a mocked search.
os.environ["MARKET_CHECK_ENABLED"] = "false"
