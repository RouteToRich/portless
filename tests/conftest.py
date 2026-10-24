"""Suite-wide test configuration.

The unit-test suite must never talk to the real NATS broker: a Synadia
``*.creds`` file sitting in the repository root (gitignored) would otherwise make
``EVENT_BUS_BACKEND=auto`` pick the NATS backend inside any lifespan a test
happens to run. Pin the in-process bus for the whole suite; the NATS code
paths are tested with fakes in ``test_event_bus.py``.
"""
from __future__ import annotations

import os

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
