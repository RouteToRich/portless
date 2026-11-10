"""Results of automation runs, kept in the ``portless_automation`` database."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .safety import AUTOMATION_DB

CASES, RUNS, RESULTS = "qa_test_cases", "qa_test_runs", "qa_test_results"
QA_COLLECTIONS = (CASES, RUNS, RESULTS, "qa_fixtures", "qa_data_registry")
_client: Any = None


def database(uri: str) -> Any:
    """The automation database. The name is a constant: no setting can redirect it."""
    global _client
    if _client is None:
        import motor.motor_asyncio as motor  # type: ignore[import]
        _client = motor.AsyncIOMotorClient(uri, serverSelectionTimeoutMS=15000)
    return _client[AUTOMATION_DB]


def now() -> datetime:
    return datetime.now(timezone.utc)


class Sink:
    """Where a worker writes. Without a database (dev check) results stay in memory."""

    def __init__(self, db: Any | None, run_id: str) -> None:
        self.db, self.run_id = db, run_id
        self.results: list[dict[str, Any]] = []

    async def catalogue(self, cases: list[dict[str, Any]]) -> None:
        if self.db is None:
            return
        await self.db[CASES].delete_many({})
        if cases:
            await self.db[CASES].insert_many(cases)

    async def result(self, doc: dict[str, Any]) -> None:
        self.results.append(doc)
        if self.db is not None:
            await self.db[RESULTS].insert_one({"run_id": self.run_id, "created_at": now(), **doc})
            await self.progress()

    async def progress(self, **fields: Any) -> None:
        if self.db is None:
            return
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        await self.db[RUNS].update_one({"_id": self.run_id}, {"$set": {
            "heartbeat_at": now(), "totals": counts, "done": len(self.results), **fields,
        }})
