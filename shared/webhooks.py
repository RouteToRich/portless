"""Parsing and signature rules for inbound provider webhooks.

A body that cannot be read is the sender's error, so it answers 400 / 422. A
500 would tell the provider to keep retrying something that can never succeed.

A provider has no PortLess sign-in, so ``AuthMiddleware`` lets a webhook without
a token through and marks it ``request.state.anonymous_webhook``. Its signature
is then the only proof of who sent it: connectors accept anything while their
secret is unset, which an anonymous caller must never benefit from.
"""
from __future__ import annotations

import json
import logging
from typing import Any, TypeVar

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

_Payload = TypeVar("_Payload", bound=BaseModel)


def signature_required(request: Request) -> bool:
    """True when nothing but the provider's signature vouches for this call."""
    return bool(getattr(request.state, "anonymous_webhook", False))


async def signature_valid(
    connector: Any, headers: dict[str, str], body: bytes, *, require_signature: bool = False,
) -> bool:
    """The connector's verdict; never True for an unsigned anonymous call."""
    if require_signature and not getattr(connector, "_webhook_secret", ""):
        logger.warning("%s: anonymous webhook refused — its signing secret is not set", type(connector).__name__)
        return False
    try:
        return bool(await connector.webhook_verify(headers, body))
    except Exception:  # noqa: BLE001 — an unverifiable signature is an invalid one
        return False


def parse_webhook_body(raw_body: bytes, model: type[_Payload]) -> _Payload:
    """Validate a raw webhook body against *model*, or raise a client error."""
    try:
        data = json.loads(raw_body)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Webhook body is not valid JSON") from exc
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        logger.warning("Invalid %s: %d field error(s)", model.__name__, exc.error_count())
        raise HTTPException(422, detail="Invalid webhook payload") from exc
