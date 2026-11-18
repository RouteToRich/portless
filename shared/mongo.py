"""One Mongo client per process for the shared modules.

Every Motor client keeps its own connection pool. The shared modules (audit log, heartbeat,
leader lock, fees, Kill Switch, compliance, autonomy policy, outcomes, price book, catalogue,
market check, supplier commitment) each opened one — a dozen pools per instance for the same
database (review board B-11). They now take their collection from ``collection(name)``.

``get_client`` is keyed by URI and the client class in use, so a test that swaps the class
(mongomock) never gets a real client and vice versa. ``close_all()`` runs at app shutdown.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_clients: dict[tuple[str, int], Any] = {}


def get_client(uri: str, **options: Any) -> Any:
    import motor.motor_asyncio as motor  # type: ignore[import]
    cls = motor.AsyncIOMotorClient
    key = (uri, id(cls))
    client = _clients.get(key)
    if client is None:
        opts = {"serverSelectionTimeoutMS": 8000, **options}
        client = cls(uri, **opts)
        _clients[key] = client
    return client


def collection(name: str, *, uri: str | None = None, db: str | None = None) -> Any | None:
    """The named collection in the app's database, or None without MONGO_URI."""
    from shared.config import settings
    uri = uri or settings.mongo_uri
    if not uri:
        return None
    return get_client(uri)[db or settings.mongo_db_name][name]


def close_all() -> None:
    for client in list(_clients.values()):
        try:
            client.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Mongo: close failed: %s", exc)
    _clients.clear()
