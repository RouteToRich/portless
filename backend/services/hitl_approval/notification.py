"""HITL notification manager.

Two delivery channels — both are used in parallel:

1. Server-Sent Events (SSE)
   Connected UI clients maintain a persistent GET /hitl-approval/stream
   connection.  The manager holds one asyncio.Queue per connected client
   and fans out every approval event to all queues.

2. Webhook POST
   When HITL_NOTIFY_WEBHOOK_URL is set, the manager POSTs a signed JSON
   payload to that URL.  This lets a future PortLess UI backend receive push
   notifications without a long-lived SSE connection.  The payload is signed
   with HMAC-SHA256 using HITL_NOTIFY_WEBHOOK_SECRET.

Both channels are fire-and-forget — delivery failures are logged but never
bubble up to the caller.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-process SSE fan-out
# ---------------------------------------------------------------------------

# client_id → asyncio.Queue
_sse_clients: dict[str, asyncio.Queue[dict[str, Any]]] = {}

# In-memory ring buffer of the 50 most recent events so a newly connected
# UI client can catch up without missing anything since the last reconnect.
_recent_events: list[dict[str, Any]] = []
_MAX_RECENT = 50


def register_sse_client() -> tuple[str, asyncio.Queue[dict[str, Any]]]:
    """Register a new SSE client connection.  Returns (client_id, queue)."""
    client_id = str(uuid4())
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
    _sse_clients[client_id] = q
    logger.debug("SSE client registered: %s (total=%d)", client_id, len(_sse_clients))
    return client_id, q


def deregister_sse_client(client_id: str) -> None:
    _sse_clients.pop(client_id, None)
    logger.debug("SSE client deregistered: %s (total=%d)", client_id, len(_sse_clients))


def get_recent_events() -> list[dict[str, Any]]:
    """Return buffered events for replay on new SSE connections."""
    return list(_recent_events)


async def broadcast(event: dict[str, Any]) -> None:
    """Push event to all connected SSE clients + append to replay buffer."""
    _recent_events.append(event)
    if len(_recent_events) > _MAX_RECENT:
        _recent_events.pop(0)

    for client_id, q in list(_sse_clients.items()):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("SSE queue full for client %s — dropping event", client_id)


# ---------------------------------------------------------------------------
# Webhook delivery
# ---------------------------------------------------------------------------

async def _post_webhook(url: str, secret: str, payload: dict[str, Any]) -> None:
    """POST signed JSON payload to the configured webhook URL."""
    try:
        import httpx
        body = json.dumps(payload, default=str).encode()
        sig = hmac.new(secret.encode() if secret else b"", body, hashlib.sha256).hexdigest()
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-PortLess-Signature": f"sha256={sig}",
                    "X-PortLess-Event": payload.get("event_type", "hitl"),
                },
            )
            if resp.status_code not in range(200, 300):
                logger.warning("HITL webhook to %s returned HTTP %d", url, resp.status_code)
    except Exception as exc:
        logger.warning("HITL webhook delivery failed: %s", exc)


# ---------------------------------------------------------------------------
# Public notification entry point
# ---------------------------------------------------------------------------

async def notify(event_type: str, approval_dict: dict[str, Any]) -> None:
    """Fan-out a HITL event to SSE clients and the configured webhook.

    ``approval_dict`` should be a serialisable dict of the approval record.
    """
    event = {
        "event_type": event_type,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "approval": approval_dict,
    }

    # SSE broadcast (fire-and-forget — errors logged in broadcast)
    await broadcast(event)

    # Webhook delivery (if configured)
    from .config import settings
    url = settings.notify_webhook_url
    if url:
        secret = settings.notify_webhook_secret
        asyncio.create_task(_post_webhook(url, secret, event))
