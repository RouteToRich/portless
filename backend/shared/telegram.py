"""Telegram push for the operator (HITL gates, countersign requests, the daily
digest). Configured with ``HITL_TELEGRAM_BOT_TOKEN`` + ``HITL_TELEGRAM_CHAT_ID``
(a bot created with @BotFather, the chat id of the operator or an ops group).
Never raises — a push failure must not block a gate."""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org"


def token() -> str:
    return os.getenv("HITL_TELEGRAM_BOT_TOKEN", "").strip()


def chat_id() -> str:
    return os.getenv("HITL_TELEGRAM_CHAT_ID", "").strip()


def configured() -> bool:
    return bool(token() and chat_id())


async def send_message(text: str, *, disable_preview: bool = True) -> bool:
    """POST sendMessage; True when Telegram accepted it. Text is sent as plain
    text (no parse mode) so operator-supplied strings cannot break markup."""
    if not configured():
        return False
    body = {"chat_id": chat_id(), "text": text[:4000], "disable_web_page_preview": disable_preview}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{_API}/bot{token()}/sendMessage", json=body)
        if resp.status_code != 200:
            logger.warning("Telegram: sendMessage HTTP %s — %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram: sendMessage failed: %s", exc)
        return False
