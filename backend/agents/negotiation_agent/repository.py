"""Persistence layer for negotiation sessions and rounds.

Follows the same Motor + InMemoryFallback pattern used by Deals and Sourcing.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from .config import settings
from .schemas import NegotiationRound, NegotiationSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class NegotiationRepository(Protocol):
    async def save_session(self, session: NegotiationSession) -> None: ...
    async def get_session(self, negotiation_id: str) -> NegotiationSession | None: ...
    async def get_session_by_deal(self, deal_id: str) -> NegotiationSession | None: ...
    async def list_sessions(self, deal_id: str | None = None) -> list[NegotiationSession]: ...
    async def save_round(self, round_: NegotiationRound) -> None: ...
    async def list_rounds(self, negotiation_id: str) -> list[NegotiationRound]: ...


# ---------------------------------------------------------------------------
# In-memory fallback (dev / CI — no MongoDB needed)
# ---------------------------------------------------------------------------

class InMemoryNegotiationRepository:
    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._rounds: dict[str, list[dict[str, Any]]] = {}

    async def save_session(self, session: NegotiationSession) -> None:
        self._sessions[session.negotiation_id] = session.model_dump()

    async def get_session(self, negotiation_id: str) -> NegotiationSession | None:
        doc = self._sessions.get(negotiation_id)
        return NegotiationSession(**doc) if doc else None

    async def get_session_by_deal(self, deal_id: str) -> NegotiationSession | None:
        for doc in self._sessions.values():
            if doc.get("deal_id") == deal_id:
                return NegotiationSession(**doc)
        return None

    async def list_sessions(self, deal_id: str | None = None) -> list[NegotiationSession]:
        docs = list(self._sessions.values())
        if deal_id:
            docs = [d for d in docs if d.get("deal_id") == deal_id]
        return [NegotiationSession(**d) for d in docs]

    async def save_round(self, round_: NegotiationRound) -> None:
        bucket = self._rounds.setdefault(round_.negotiation_id, [])
        bucket.append(round_.model_dump())

    async def list_rounds(self, negotiation_id: str) -> list[NegotiationRound]:
        return [NegotiationRound(**d) for d in self._rounds.get(negotiation_id, [])]


# ---------------------------------------------------------------------------
# MongoDB Motor repository
# ---------------------------------------------------------------------------

class MongoNegotiationRepository:
    """Async MongoDB backend using Motor."""

    def __init__(self, uri: str, db_name: str, sessions_col: str, rounds_col: str) -> None:
        import motor.motor_asyncio as motor  # type: ignore[import]

        client: Any = motor.AsyncIOMotorClient(uri)
        db = client[db_name]
        self._sessions = db[sessions_col]
        self._rounds = db[rounds_col]

    async def _ensure_indexes(self) -> None:
        from pymongo import ASCENDING, DESCENDING  # type: ignore[import]

        await self._sessions.create_index(
            [("negotiation_id", ASCENDING)], unique=True, background=True
        )
        await self._sessions.create_index(
            [("deal_id", ASCENDING)], background=True
        )
        await self._sessions.create_index(
            [("status", ASCENDING)], background=True
        )
        await self._sessions.create_index(
            [("created_at", DESCENDING)], background=True
        )
        await self._rounds.create_index(
            [("round_id", ASCENDING)], unique=True, background=True
        )
        await self._rounds.create_index(
            [("negotiation_id", ASCENDING)], background=True
        )
        await self._rounds.create_index(
            [("deal_id", ASCENDING)], background=True
        )

    async def save_session(self, session: NegotiationSession) -> None:
        # mode="json": BSON cannot encode Decimal (cost_envelope fields) — JSON
        # mode serializes them (and datetimes) to strings; pydantic parses them
        # back on read. Plain model_dump() here aborted every session save once
        # the QC/freight cost seeding started populating the envelope.
        doc = session.model_dump(mode="json")
        await self._sessions.replace_one(
            {"negotiation_id": session.negotiation_id}, doc, upsert=True
        )

    async def get_session(self, negotiation_id: str) -> NegotiationSession | None:
        doc = await self._sessions.find_one(
            {"negotiation_id": negotiation_id}, {"_id": 0}
        )
        return NegotiationSession(**doc) if doc else None

    async def get_session_by_deal(self, deal_id: str) -> NegotiationSession | None:
        doc = await self._sessions.find_one({"deal_id": deal_id}, {"_id": 0})
        return NegotiationSession(**doc) if doc else None

    async def list_sessions(self, deal_id: str | None = None) -> list[NegotiationSession]:
        query: dict[str, Any] = {}
        if deal_id:
            query["deal_id"] = deal_id
        cursor = self._sessions.find(query, {"_id": 0}).sort("created_at", -1)
        return [NegotiationSession(**d) async for d in cursor]

    async def save_round(self, round_: NegotiationRound) -> None:
        doc = round_.model_dump(mode="json")  # BSON-safe (see save_session)
        await self._rounds.replace_one({"round_id": round_.round_id}, doc, upsert=True)

    async def list_rounds(self, negotiation_id: str) -> list[NegotiationRound]:
        cursor = self._rounds.find(
            {"negotiation_id": negotiation_id}, {"_id": 0}
        ).sort("round_no", 1)
        return [NegotiationRound(**d) async for d in cursor]


# ---------------------------------------------------------------------------
# Module-level repository instance (initialized in main.py on_startup)
# ---------------------------------------------------------------------------

_repo: NegotiationRepository = InMemoryNegotiationRepository()


def get_repository() -> NegotiationRepository:
    return _repo


async def init_repository() -> None:
    global _repo
    uri = settings.negotiation_mongo_uri
    if not uri:
        logger.info("NEGOTIATION_MONGO_URI not set — using in-memory negotiation repository")
        _repo = InMemoryNegotiationRepository()
        return
    try:
        mongo_repo = MongoNegotiationRepository(
            uri=uri,
            db_name=settings.negotiation_mongo_db_name,
            sessions_col=settings.negotiation_sessions_collection,
            rounds_col=settings.negotiation_rounds_collection,
        )
        await mongo_repo._ensure_indexes()
        _repo = mongo_repo
        logger.info("MongoNegotiationRepository initialised (db=%s)", settings.negotiation_mongo_db_name)
    except Exception as exc:
        logger.error("Mongo init failed — falling back to in-memory: %s", exc)
        _repo = InMemoryNegotiationRepository()
