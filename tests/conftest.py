"""Suite-wide test configuration.

The unit-test suite must never talk to the real NATS broker: a Synadia
``*.creds`` file sitting in the repository root (gitignored) would otherwise make
``EVENT_BUS_BACKEND=auto`` pick the NATS backend inside any lifespan a test
happens to run. Pin the in-process bus for the whole suite; the NATS code
paths are tested with fakes in ``test_event_bus.py``.
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path

# langgraph warns once, at import time, about a langchain default it does not pass yet — not
# ours. langchain resets the filter for that category during its own import, which beats any
# filter, so the module is imported here once with its warnings recorded and dropped (later
# imports are cached and do not warn again).
with warnings.catch_warnings(record=True):
    try:
        import langgraph.cache.base  # noqa: F401
    except Exception:  # noqa: BLE001 - langgraph absent: nothing to silence
        pass

os.environ["EVENT_BUS_BACKEND"] = "inprocess"

# Hundreds of anonymous webhook calls run in one process: the public rate limit
# (shared/rate_limit.py) is off for the suite; tests/test_rate_limit.py sets its own.
os.environ["RATE_LIMIT_PUBLIC_PER_MINUTE"] = "0"

# The PortLess-owned Apify actors cost money per run. A developer logged in
# with the Apify CLI (~/.apify/auth.json) must never have a unit test start a
# real actor run: pin the connector to stub mode for the whole suite. Tests
# that exercise the connector itself monkeypatch ``run()`` explicitly.
os.environ["APIFY_TOKEN"] = ""
os.environ["APIFY_TOKEN_AUTODISCOVER"] = "false"
os.environ["NATIONAL_TRADE_SOURCES_ENABLED"] = "false"  # HMRC / Comext never called from the suite
os.environ["APIFY_CACHE_ENABLED"] = "false"  # result cache tested on its own (test_apify_usage.py)

# .env (gitignored) holds the real MongoDB cluster and the shared LLM
# Gateway credential, and shared/config.py loads it into os.environ. A unit
# test must never write to the live database or spend gateway budget: pin both
# off (an env var set here wins over the .env file). Gateway calls are tested
# with a mocked transport in tests/llm_gateway/test_remote.py.
os.environ["MONGO_URI"] = ""
os.environ["LLM_GATEWAY_URL"] = ""

# The LLM tracker appends one JSONL line per call to data/llm_gateway_tracking.txt
# (its fallback when Mongo is off — as it always is here), a file tracked in git:
# every test run left it modified. Keep the suite's lines in the temp folder.
import tempfile  # noqa: E402

os.environ["LLM_TRACKING_FILE_PATH"] = str(
    Path(tempfile.gettempdir()) / "portless-tests" / "llm_gateway_tracking.txt")

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
# The first supplier payout waits for a person when its bank account was never verified
# (services/trade_min/supplier_onboarding.py); tests/test_supplier_ops_batch.py turns it on.
os.environ["SUPPLIER_BANK_VERIFICATION_REQUIRED"] = "false"
os.environ["REORDER_AGENT_ENABLED"] = "false"
os.environ["REGISTRATIONS_REQUIRED"] = "false"  # PortLess registrations before the PI: tests/test_country_markets.py
os.environ["COUNTRY_PROFILE_REQUIRED"] = "false"  # outreach waits for a country profile: tests/test_country_markets.py
os.environ["REGULATORY_WATCH_ENABLED"] = "false"  # no DGFT / CBIC fetch from the suite: tests/test_country_markets.py
os.environ["QUESTION_AGENT_ENABLED"] = "false"  # questions answered from deal facts: tests/test_country_markets.py
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

# Load .env now, once (shared.config calls load_dotenv on import). Imported lazily inside a
# test instead, it would refill a provider variable that test had just deleted with the
# .env placeholder — the escrow journey failed that way when its file ran on its own.
import shared.config  # noqa: E402,F401

# The pre-trade market check searches the web; the suite never goes online.
# tests/test_trade_costs.py exercises it with a mocked search.
os.environ["MARKET_CHECK_ENABLED"] = "false"
# No DGFT denied list is loaded in tests: a never-loaded list would put every deal into a
# compliance review. tests/test_denied_list.py turns it back on where it is tested.
os.environ["COMPLIANCE_DGFT_LIST"] = "allow"
os.environ["EXPORT_CERTS_REQUIRED"] = "false"  # tests/test_export_certs.py turns it on
os.environ["FX_REFERENCE_AUTO_REFRESH"] = "false"  # never call the rate feed from the suite
os.environ["ESCROW_LEGACY_STRIPE_HOLD"] = "true"  # the legacy hold tests; tests/test_legacy_hold.py turns it off
os.environ["VIEW_CACHE_SECONDS"] = "0"  # every test reads fresh views; tests/test_supplier_performance.py turns it on


# Suite markers (unit / integration / smoke / regression / slow / money), applied by
# file — see tests/markers.py; registered here so `pytest --strict-markers` passes.
def pytest_configure(config):
    from tests.markers import MARKERS
    for name, help_text in MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {help_text}")


def pytest_collection_modifyitems(config, items):
    import pytest

    from tests.markers import markers_for
    for item in items:
        for name in markers_for(item.path):
            item.add_marker(getattr(pytest.mark, name))


# One Mongo client per process (shared/mongo.py) would carry a test's mongomock data into
# the next test that patches the same client class; every test starts with no cached client.
import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _fresh_shared_mongo_clients():
    from shared import mongo
    mongo._clients.clear()
    yield
    mongo._clients.clear()
