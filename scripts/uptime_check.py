"""External synthetic check (todo: an uptime monitor calling /health and /health/watchers with a
token, alerting on Telegram). Run every few minutes from .github/workflows/uptime.yml.

  python scripts/uptime_check.py --base https://backend.example --token $TOKEN

Checks ``/health`` (answers 200 within ``--timeout`` seconds) and ``/health/watchers`` (no stale
watcher). On a failure it sends one Telegram message (``--telegram-token`` / ``--telegram-chat``,
or ``HITL_TELEGRAM_BOT_TOKEN`` / ``HITL_TELEGRAM_CHAT_ID``) and exits 1; exit 0 when healthy.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Any

import httpx


async def check(base: str, token: str | None, *, timeout: float = 15.0,
                transport: Any = None) -> list[str]:
    """The problems found (empty = healthy)."""
    problems: list[str] = []
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(base_url=base.rstrip("/"), timeout=timeout, transport=transport) as client:
        started = time.monotonic()
        try:
            r = await client.get("/health")
            took = time.monotonic() - started
            if r.status_code != 200:
                problems.append(f"/health answered {r.status_code}")
            elif took > timeout * 0.8:
                problems.append(f"/health took {took:.1f}s")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"/health unreachable: {type(exc).__name__}: {exc}")
            return problems
        try:
            r = await client.get("/health/watchers", headers=headers)
            if r.status_code != 200:
                problems.append(f"/health/watchers answered {r.status_code}")
            else:
                body = r.json()
                stale = [w.get("name") for w in body.get("watchers", []) if w.get("stale")]
                stale = stale or list(body.get("stale") or [])
                if stale:
                    problems.append("stale watcher(s): " + ", ".join(str(s) for s in stale))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"/health/watchers unreadable: {exc}")
    return problems


async def alert(text: str, bot: str | None, chat: str | None, *, transport: Any = None) -> bool:
    if not bot or not chat:
        return False
    async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
        r = await client.post(f"https://api.telegram.org/bot{bot}/sendMessage", json={"chat_id": chat, "text": text})
        return r.status_code == 200


async def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default=os.getenv("PORTLESS_BACKEND_URL", ""))
    p.add_argument("--token", default=os.getenv("PORTLESS_SMOKE_TOKEN"))
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("--telegram-token", default=os.getenv("HITL_TELEGRAM_BOT_TOKEN"))
    p.add_argument("--telegram-chat", default=os.getenv("HITL_TELEGRAM_CHAT_ID"))
    a = p.parse_args(argv)
    if not a.base:
        print("no --base / PORTLESS_BACKEND_URL")
        return 2
    problems = await check(a.base, a.token, timeout=a.timeout)
    if not problems:
        print("healthy")
        return 0
    text = "PORTLESS DOWN / DEGRADED (uptime check): " + "; ".join(problems)
    print(text)
    await alert(text, a.telegram_token, a.telegram_chat)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
