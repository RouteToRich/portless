"""One Mongo client per process for the shared modules (shared/mongo.py, review board B-11)."""
from __future__ import annotations

import pathlib
import re

from shared import mongo


def test_one_client_per_uri_and_client_class(monkeypatch):
    made = []

    class FakeClient:
        def __init__(self, uri, **opts):
            made.append((uri, opts))
            self.closed = False

        def __getitem__(self, name):
            return {"db": name}

        def close(self):
            self.closed = True

    monkeypatch.setattr("motor.motor_asyncio.AsyncIOMotorClient", FakeClient)
    mongo.close_all()
    a = mongo.get_client("mongodb://x")
    b = mongo.get_client("mongodb://x")
    c = mongo.get_client("mongodb://y")
    assert a is b and a is not c and len(made) == 2 and made[0][1]["serverSelectionTimeoutMS"] == 8000
    mongo.close_all()
    assert a.closed and c.closed and mongo._clients == {}


def test_shared_modules_no_longer_open_their_own_client():
    root = pathlib.Path(__file__).resolve().parents[1]
    files = ["shared/audit_log.py", "shared/catalogue.py", "shared/heartbeat.py", "shared/leader_lock.py",
             "shared/orchestration/autonomy_policy.py", "shared/orchestration/compliance.py",
             "shared/orchestration/fee_check.py", "shared/orchestration/market_check.py",
             "shared/orchestration/outcomes.py", "shared/orchestration/supplier_commitment.py", "shared/pricebook.py"]
    for f in files:
        text = (root / f).read_text(encoding="utf-8")
        assert not re.search(r"AsyncIOMotorClient\(", text), f
        assert "get_client(settings.mongo_uri)" in text, f
