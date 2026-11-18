"""Rate limit for the endpoints anyone can call without signing in.

The anonymous doors — provider webhooks (accepted only when signed), the unsubscribe link and
the escrow proof-sheet link — are checked before any work: at most
``RATE_LIMIT_PUBLIC_PER_MINUTE`` (default 60) requests per client address and door per
minute; more answer 429 with ``Retry-After``. Signed-in staff are not limited here.

The window is a sliding minute kept in memory per process (behind a load balancer each
instance counts on its own — the WAF in front of the backend is the outer limit).
``RATE_LIMIT_PUBLIC_PER_MINUTE=0`` switches it off.
"""
from __future__ import annotations

import os
import time
from collections import deque

_hits: dict[str, deque[float]] = {}
_MAX_KEYS = 50_000


def per_minute() -> int:
    try:
        return max(0, int(os.getenv("RATE_LIMIT_PUBLIC_PER_MINUTE", "60")))
    except ValueError:
        return 60


def allow(key: str, *, now: float | None = None, limit: int | None = None, window: float = 60.0) -> tuple[bool, int]:
    """(allowed, seconds until the next request would be allowed)."""
    limit = per_minute() if limit is None else limit
    if limit <= 0:
        return True, 0
    now = time.monotonic() if now is None else now
    q = _hits.get(key)
    if q is None:
        if len(_hits) >= _MAX_KEYS:  # a flood of new addresses: forget the oldest windows
            for k in list(_hits)[: _MAX_KEYS // 10]:
                _hits.pop(k, None)
        q = _hits[key] = deque()
    while q and now - q[0] >= window:
        q.popleft()
    if len(q) >= limit:
        return False, max(1, int(window - (now - q[0])) + 1)
    q.append(now)
    return True, 0


def door(path: str) -> str:
    """Which public door a path is (webhooks of one provider count together)."""
    if "/webhooks/" in path or path.endswith("/webhook") or "/inbound/" in path:
        return "webhook:" + path.rsplit("/", 1)[-1]
    if "/escrow-guard/proof/" in path:
        return "proof"
    if path.rstrip("/").endswith("/unsubscribe"):
        return "unsubscribe"
    return "public"


def reset_for_tests() -> None:
    _hits.clear()
