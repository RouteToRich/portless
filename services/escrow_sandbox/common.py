"""Shared helpers for the escrow provider simulator."""
from __future__ import annotations

import base64
from datetime import UTC, datetime

from fastapi import HTTPException, Request


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def require_basic_auth(request: Request) -> tuple[str, str]:
    """Both providers use HTTP Basic auth; a call without it is refused as the real API would."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        raise HTTPException(401, detail={"status": "error", "message": "authentication required"})
    try:
        user, _, secret = base64.b64decode(header.split(" ", 1)[1]).decode().partition(":")
    except Exception as exc:
        raise HTTPException(401, detail={"status": "error", "message": "malformed credentials"}) from exc
    if not user or not secret:
        raise HTTPException(401, detail={"status": "error", "message": "invalid credentials"})
    return user, secret
