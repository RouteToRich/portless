"""Is this supplier address on the do-not-contact list?

Buyers on the list were already skipped by outreach; suppliers were not (review board B-04):
an RFQ, a reconfirmation or a best-and-final e-mail could still reach a supplier that asked
PortLess to stop. Every supplier e-mail path calls ``supplier_blocked`` right before sending:
the address (exact) or its company domain (never a free-mail domain) matching a directory
supplier with ``do_not_contact``. Never raises (a lookup failure blocks nothing).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def blocked_supplier_keys() -> set[str]:
    try:
        from services.trade_min import directory, relationships  # type: ignore[import]
        store = await directory.get_directory()
        keys: set[str] = set()
        for r in await store.list("supplier", limit=100_000):
            if r.do_not_contact:
                keys |= {k for k in relationships.record_keys(r) if k.startswith("d:")}
                if r.email:
                    keys.add("e:" + r.email.strip().lower())
        return keys
    except Exception as exc:  # noqa: BLE001
        logger.debug("ContactGuard: directory unavailable: %s", exc)
        return set()


async def supplier_blocked(email: str | None, *, keys: set[str] | None = None) -> bool:
    if not email:
        return False
    from services.trade_min.relationships import keys_for  # type: ignore[import]
    mine = {k for k in keys_for(email) if k.startswith("d:")} | {"e:" + email.strip().lower()}
    return bool(mine & (keys if keys is not None else await blocked_supplier_keys()))
