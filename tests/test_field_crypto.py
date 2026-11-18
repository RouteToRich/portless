"""Sensitive fields encrypted at rest (todo: encrypt bank details …)."""
from __future__ import annotations

import base64
import os
from datetime import UTC, datetime

import pytest
from mongomock_motor import AsyncMongoMockClient

from shared import field_crypto as fc


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())


def test_round_trip_and_plain_values_pass(key):
    enc = fc.encrypt("HDFC0001234")
    assert enc.startswith(fc.PREFIX) and "HDFC" not in enc and fc.encrypt(enc) == enc
    assert fc.decrypt(enc) == "HDFC0001234" and fc.decrypt("plain") == "plain" and fc.decrypt(None) is None
    assert fc.encrypt("HDFC0001234") != enc  # a fresh nonce each time


def test_without_or_with_a_wrong_key(monkeypatch, key):
    enc = fc.encrypt("4321")
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    assert fc.decrypt(enc) is None          # wrong key: unreadable, never a crash
    monkeypatch.delenv("FIELD_ENCRYPTION_KEY")
    assert fc.encrypt("4321") == "4321" and not fc.configured()
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", "short")
    assert fc.encrypt("4321") == "4321"


async def test_directory_stores_bank_details_encrypted_and_reads_them_plain(key):
    from services.trade_min.directory import MongoDirectory
    from services.trade_min.schemas import CounterpartyRecord
    col = AsyncMongoMockClient()["db"]["dir"]
    store = MongoDirectory(col)
    now = datetime.now(UTC)
    rec = CounterpartyRecord(id="supplier:mill.in", role="supplier", name="Mill", source="manual", first_seen_at=now,
                             last_seen_at=now, manual={"bank_name": "HDFC", "bank_ifsc": "HDFC0001234",
                                                       "bank_account_last4": "4321", "gstin": "33AAAAA0000A1Z5"})
    await store.upsert(rec)
    raw = await col.find_one({"_id": "supplier:mill.in"})
    assert raw["manual"]["bank_ifsc"].startswith(fc.PREFIX) and raw["manual"]["gstin"] == "33AAAAA0000A1Z5"
    back = await store.get("supplier:mill.in")
    assert back.manual["bank_ifsc"] == "HDFC0001234" and back.manual["bank_account_last4"] == "4321"
    assert rec.model_dump(mode="json")["manual"]["bank_ifsc"] == "HDFC0001234"  # API answers stay plain
