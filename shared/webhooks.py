"""Parsing for inbound provider webhooks.

A body that cannot be read is the sender's error, so it answers 400 / 422. A
500 would tell the provider to keep retrying something that can never succeed.
"""
from __future__ import annotations

import json
import logging
from typing import TypeVar

from fastapi import HTTPException, status
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

_Payload = TypeVar("_Payload", bound=BaseModel)


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
