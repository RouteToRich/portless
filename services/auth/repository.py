"""Auth persistence — users + organizations collections.

Two backends per entity behind a Protocol (mirrors the other services):
  InMemory*Repository — process-local dict; used when no Mongo URI resolves.
  Mongo*Repository    — Motor + MongoDB; selected once a URI resolves.

Persists into the SAME shared database as every other module (see
shared MONGO_URI / MONGO_DB_NAME in the root .env) via config.py's
AliasChoices fallback — not a separate "portless_auth" database.
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from .config import settings
from .schemas import OrganizationRecord, UserRecord

logger = logging.getLogger(__name__)

_user_repository: "UserRepository | None" = None
_org_repository: "OrganizationRepository | None" = None
_motor_client = None

_MONGO_URI = settings.auth_mongo_uri
_DB_NAME = settings.auth_mongo_db_name
_USERS_COLLECTION = settings.auth_users_collection
_ORGS_COLLECTION = settings.auth_organizations_collection


@runtime_checkable
class UserRepository(Protocol):
    async def get_by_email(self, email: str) -> UserRecord | None: ...
    async def get_by_id(self, user_id: str) -> UserRecord | None: ...
    async def create(self, user: UserRecord) -> None: ...
    async def list_all(self) -> list[UserRecord]: ...
    async def update_org(self, user_id: str, org_id: str) -> UserRecord | None: ...


@runtime_checkable
class OrganizationRepository(Protocol):
    async def create(self, org: OrganizationRecord) -> None: ...
    async def get(self, org_id: str) -> OrganizationRecord | None: ...
    async def get_by_name(self, name: str) -> OrganizationRecord | None: ...
    async def list_all(self) -> list[OrganizationRecord]: ...
    async def add_known_email(self, org_id: str, email: str) -> OrganizationRecord | None: ...


# ---------------------------------------------------------------------------
# In-memory
# ---------------------------------------------------------------------------

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

    async def list_all(self) -> list[UserRecord]:
        return sorted(self._by_id.values(), key=lambda u: u.created_at)

    async def update_org(self, user_id: str, org_id: str) -> UserRecord | None:
        user = self._by_id.get(user_id)
        if user is None:
            return None
        updated = user.model_copy(update={"org_id": org_id})
        self._by_id[user_id] = updated
        return updated


class InMemoryOrganizationRepository:
    def __init__(self) -> None:
        self._by_id: dict[str, OrganizationRecord] = {}

    async def create(self, org: OrganizationRecord) -> None:
        self._by_id[org.org_id] = org

    async def get(self, org_id: str) -> OrganizationRecord | None:
        return self._by_id.get(org_id)

    async def get_by_name(self, name: str) -> OrganizationRecord | None:
        key = name.strip().lower()
        for o in self._by_id.values():
            if o.name.strip().lower() == key:
                return o
        return None

    async def list_all(self) -> list[OrganizationRecord]:
        return sorted(self._by_id.values(), key=lambda o: o.created_at)

    async def add_known_email(self, org_id: str, email: str) -> OrganizationRecord | None:
        org = self._by_id.get(org_id)
        if org is None:
            return None
        email = email.strip().lower()
        if email in org.known_emails:
            return org
        updated = org.model_copy(update={"known_emails": [*org.known_emails, email]})
        self._by_id[org_id] = updated
        return updated


# ---------------------------------------------------------------------------
# MongoDB (Motor)
# ---------------------------------------------------------------------------

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

    async def list_all(self) -> list[UserRecord]:
        cursor = self._col.find({}).sort("created_at", 1)
        docs = await cursor.to_list(length=10_000)
        users = []
        for d in docs:
            d.pop("_id", None)
            try:
                users.append(UserRecord(**d))
            except Exception as exc:
                logger.warning("Skipping malformed user document: %s", exc)
        return users

    async def update_org(self, user_id: str, org_id: str) -> UserRecord | None:
        from pymongo import ReturnDocument

        d = await self._col.find_one_and_update(
            {"user_id": user_id},
            {"$set": {"org_id": org_id}},
            return_document=ReturnDocument.AFTER,
        )
        if d is None:
            return None
        d.pop("_id", None)
        return UserRecord(**d)


class MongoOrganizationRepository:
    def __init__(self, col) -> None:
        self._col = col

    async def create(self, org: OrganizationRecord) -> None:
        await self._col.insert_one(org.model_dump(mode="json"))

    async def get(self, org_id: str) -> OrganizationRecord | None:
        d = await self._col.find_one({"org_id": org_id})
        if d is None:
            return None
        d.pop("_id", None)
        return OrganizationRecord(**d)

    async def get_by_name(self, name: str) -> OrganizationRecord | None:
        import re

        d = await self._col.find_one(
            {"name": {"$regex": f"^{re.escape(name.strip())}$", "$options": "i"}}
        )
        if d is None:
            return None
        d.pop("_id", None)
        return OrganizationRecord(**d)

    async def list_all(self) -> list[OrganizationRecord]:
        cursor = self._col.find({}).sort("created_at", 1)
        docs = await cursor.to_list(length=10_000)
        orgs = []
        for d in docs:
            d.pop("_id", None)
            try:
                orgs.append(OrganizationRecord(**d))
            except Exception as exc:
                logger.warning("Skipping malformed organization document: %s", exc)
        return orgs

    async def add_known_email(self, org_id: str, email: str) -> OrganizationRecord | None:
        from pymongo import ReturnDocument

        d = await self._col.find_one_and_update(
            {"org_id": org_id},
            {"$addToSet": {"known_emails": email.strip().lower()}},
            return_document=ReturnDocument.AFTER,
        )
        if d is None:
            return None
        d.pop("_id", None)
        return OrganizationRecord(**d)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def init_repository() -> None:
    global _user_repository, _org_repository, _motor_client

    if not _MONGO_URI:
        logger.warning(
            "Neither AUTH_MONGO_URI nor MONGO_URI is set — Auth falling back to "
            "in-memory user/organization store. Data will not survive a restart."
        )
        _user_repository = InMemoryUserRepository()
        _org_repository = InMemoryOrganizationRepository()
        return

    try:
        import motor.motor_asyncio
        from pymongo import ASCENDING, IndexModel

        client = motor.motor_asyncio.AsyncIOMotorClient(_MONGO_URI, serverSelectionTimeoutMS=5_000)
        await client.admin.command("ping")
        _motor_client = client

        users_col = client[_DB_NAME][_USERS_COLLECTION]
        await users_col.create_indexes([
            IndexModel([("user_id", ASCENDING)], unique=True, name="user_id_unique"),
            IndexModel([("email", ASCENDING)], unique=True, name="email_unique"),
        ])
        _user_repository = MongoUserRepository(users_col)

        orgs_col = client[_DB_NAME][_ORGS_COLLECTION]
        await orgs_col.create_indexes([
            IndexModel([("org_id", ASCENDING)], unique=True, name="org_id_unique"),
        ])
        _org_repository = MongoOrganizationRepository(orgs_col)

        logger.info("Auth repository: MongoDB (db=%s)", _DB_NAME)
    except Exception as exc:
        logger.error("Auth: MongoDB connection failed (%s) — falling back to in-memory.", exc)
        _user_repository = InMemoryUserRepository()
        _org_repository = InMemoryOrganizationRepository()


async def close_repository() -> None:
    global _motor_client
    if _motor_client is not None:
        _motor_client.close()
        _motor_client = None


def get_repository() -> UserRepository:
    if _user_repository is None:
        raise RuntimeError("Auth repository not initialised — call init_repository() at startup.")
    return _user_repository


def get_org_repository() -> OrganizationRepository:
    if _org_repository is None:
        raise RuntimeError("Auth repository not initialised — call init_repository() at startup.")
    return _org_repository
