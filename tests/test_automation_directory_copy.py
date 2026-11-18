"""Automation mode: the live directory copied in as test data, mailboxes made .invalid."""
from __future__ import annotations

import pytest
from mongomock_motor import AsyncMongoMockClient

from services.automation import service


async def test_copy_turns_every_mailbox_into_invalid(monkeypatch):
    client = AsyncMongoMockClient()
    from shared.config import settings
    monkeypatch.setattr(settings, "mongo_db_name", "portless")
    await client["portless"]["trade_counterparties"].insert_many([
        {"_id": "buyer:gulf.ae", "role": "buyer", "name": "Gulf", "email": "buy@gulf.ae"},
        {"_id": "supplier:mill.in", "role": "supplier", "name": "Mill", "email": None},
    ])
    monkeypatch.setattr(service, "_db", lambda: client[service.AUTOMATION_DB])
    monkeypatch.setattr(service, "_alive", lambda: False)
    out = await service.copy_live_directory()
    assert out["copied"] == {"buyer": 1, "supplier": 1}
    rows = {d["_id"]: d async for d in client[service.AUTOMATION_DB]["trade_counterparties"].find({})}
    assert rows["buyer:gulf.ae"]["email"] == "buy@gulfae.invalid" and rows["supplier:mill.in"]["email"] is None
    assert (await client["portless"]["trade_counterparties"].find_one({"_id": "buyer:gulf.ae"}))["email"] == "buy@gulf.ae"


async def test_refused_when_the_live_database_is_the_automation_one(monkeypatch):
    from shared.config import settings
    monkeypatch.setattr(settings, "mongo_db_name", service.AUTOMATION_DB)
    monkeypatch.setattr(service, "_db", lambda: AsyncMongoMockClient()[service.AUTOMATION_DB])
    with pytest.raises(service.AutomationError):
        await service.copy_live_directory()
