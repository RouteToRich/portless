"""Auth persistence — users collection.

Two backends behind a Protocol (mirrors the other services):
  InMemoryUserRepository — process-local dict; used when AUTH_MONGO_URI absent.
  MongoUserRepository    — Motor + MongoDB; selected when AUTH_MONGO_URI is set.
"""
from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

from .schemas import UserRecord

logger = logging.getLogger(__name__)

_repository: "UserRepository | None" = None
_motor_client = None

_MONGO_URI = os.getenv("AUTH_MONGO_URI", "")
_DB_NAME = os.getenv("AUTH_MONGO_DB", "portless_auth")
_COLLECTION = os.getenv("AUTH_USERS_COLLECTION", "users")


@runtime_checkable
class UserRepository(Protocol):
    async def get_by_email(self, email: str) -> UserRecord | None: ...
    async def get_by_id(self, user_id: str) -> UserRecord | None: ...
    async def create(self, user: UserRecord) -> None: ...


class InMemoryUserRepository:
    def __init__(self) -> None:
        self._by_id: dict[str, UserRecord] = {}

    async def get_by_email(self, email: str) -> UserRecord | None:
        key = email.strip().lower()
        for u in self._by_id.values():
            if u.email.lower() == key:
                return u
        return None

    async def get_by_id(self, user_id: str) -> UserRecord | None:
        return self._by_id.get(user_id)

    async def create(self, user: UserRecord) -> None:
        self._by_id[user.user_id] = user


class MongoUserRepository:
    def __init__(self, col) -> None:
        self._col = col

    async def get_by_email(self, email: str) -> UserRecord | None:
        d = await self._col.find_one({"email": email.strip().lower()})
        if d is None:
            return None
        d.pop("_id", None)
        return UserRecord(**d)

    async def get_by_id(self, user_id: str) -> UserRecord | None:
        d = await self._col.find_one({"user_id": user_id})
        if d is None:
            return None
        d.pop("_id", None)
        return UserRecord(**d)

    async def create(self, user: UserRecord) -> None:
        doc = user.model_dump(mode="json")
        doc["email"] = doc["email"].strip().lower()
        await self._col.insert_one(doc)


async def init_repository() -> None:
    global _repository, _motor_client

    if not _MONGO_URI:
        logger.warning(
            "AUTH_MONGO_URI not set — Auth falling back to in-memory user store. "
            "Registered users will not survive a restart."
        )
        _repository = InMemoryUserRepository()
        return

    try:
        import motor.motor_asyncio
        from pymongo import ASCENDING, IndexModel

        client = motor.motor_asyncio.AsyncIOMotorClient(_MONGO_URI, serverSelectionTimeoutMS=5_000)
        await client.admin.command("ping")
        _motor_client = client
        col = client[_DB_NAME][_COLLECTION]
        await col.create_indexes([
            IndexModel([("user_id", ASCENDING)], unique=True, name="user_id_unique"),
            IndexModel([("email", ASCENDING)], unique=True, name="email_unique"),
        ])
        _repository = MongoUserRepository(col)
        logger.info("Auth repository: MongoDB (db=%s)", _DB_NAME)
    except Exception as exc:
        logger.error("Auth: MongoDB connection failed (%s) — falling back to in-memory.", exc)
        _repository = InMemoryUserRepository()


async def close_repository() -> None:
    global _motor_client
    if _motor_client is not None:
        _motor_client.close()
        _motor_client = None


def get_repository() -> UserRepository:
    if _repository is None:
        raise RuntimeError("Auth repository not initialised — call init_repository() at startup.")
    return _repository
