"""Suite-wide test configuration.

The unit-test suite must never talk to the real NATS broker: a Synadia
``*.creds`` file sitting in ``backend/`` (gitignored) would otherwise make
``EVENT_BUS_BACKEND=auto`` pick the NATS backend inside any lifespan a test
happens to run. Pin the in-process bus for the whole suite; the NATS code
paths are tested with fakes in ``test_event_bus.py``.
"""
from __future__ import annotations

import os

os.environ["EVENT_BUS_BACKEND"] = "inprocess"
