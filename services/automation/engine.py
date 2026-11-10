"""Runs one catalogue case against the application, step by step."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from .catalogue import Case, Step
from .safety import QA_ACCOUNT_ID, STAFF_EMAILS

PERSONAS: dict[str, dict[str, str] | None] = {
    "staff": {"X-User-ID": "qa-staff", "X-User-Email": STAFF_EMAILS[0], "X-User-Name": "QA Staff", "X-Account-ID": QA_ACCOUNT_ID},
    "staff2": {"X-User-ID": "qa-staff2", "X-User-Email": STAFF_EMAILS[1], "X-User-Name": "QA Staff Two", "X-Account-ID": QA_ACCOUNT_ID},
    "org_user": {"X-User-ID": "qa-org-user", "X-User-Email": "user@qa-org-a.invalid", "X-User-Name": "Org User", "X-Account-ID": QA_ACCOUNT_ID},
    "other_org_user": {"X-User-ID": "qa-other-org", "X-User-Email": "user@qa-org-b.invalid", "X-User-Name": "Other Org", "X-Account-ID": QA_ACCOUNT_ID},
    "other_app": {"X-User-ID": "qa-other-app", "X-User-Email": "user@other-app.invalid", "X-User-Name": "Other App", "X-Account-ID": "another-application"},
    "anonymous": None,
}


class NotAutomated(Exception):
    """This step needs something the runner cannot do (yet): reported, never counted as a pass."""


StepFn = Callable[..., Awaitable[None]]
_STEPS: list[tuple[re.Pattern[str], StepFn]] = []


def step(pattern: str) -> Callable[[StepFn], StepFn]:
    def register(fn: StepFn) -> StepFn:
        _STEPS.append((re.compile(f"^{pattern}$"), fn))
        return fn
    return register


def find_step(text: str) -> tuple[StepFn, tuple[str, ...]] | None:
    for pattern, fn in _STEPS:
        match = pattern.match(text)
        if match:
            return fn, match.groups()
    return None


@dataclass
class Ctx:
    client: httpx.AsyncClient
    background: set[asyncio.Task]
    persona: str = "staff"
    ids: dict[str, Any] = field(default_factory=dict)
    response: httpx.Response | None = None
    keyword: str = "Given"
    doc: str | None = None
    table: list[list[str]] | None = None
    emails: list[dict[str, str]] = field(default_factory=list)
    llm_calls: list[str] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)
    undo: list[Callable[[], None]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def fill(self, text: str) -> str:
        for key, value in self.ids.items():
            if isinstance(value, (str, int, float)):
                text = text.replace("{" + key + "}", str(value))
        return text

    def body(self) -> Any:
        if self.doc is None or not self.doc.strip():
            return None
        data = json.loads(self.fill(self.doc))
        if isinstance(data, dict) and isinstance(data.get("idempotency_key"), str) and "uid" in self.ids:
            data["idempotency_key"] = f"{data['idempotency_key']}-{self.ids['uid']}"
        return data

    def set_env(self, name: str, value: str) -> None:
        before = os.environ.get(name)

        def restore() -> None:
            if before is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = before

        self.undo.append(restore)
        os.environ[name] = value

    def patch(self, owner: Any, name: str, value: Any) -> None:
        before = getattr(owner, name)
        self.undo.append(lambda: setattr(owner, name, before))
        setattr(owner, name, value)

    async def call(self, method: str, path: str, *, json_body: Any = None, content: bytes | None = None,
                   headers: dict[str, str] | None = None, persona: str | None = None, data: Any = None) -> httpx.Response:
        who = PERSONAS[persona or self.persona]
        sent = {**(who or {}), **(headers or {})}
        self.extra["acted"] = True
        if any(part in path for part in ("autonomy-policy", "hs-policy", "/trade-min/catalogue")):
            self.extra["shared_changed"] = True
        self.response = await self.client.request(method, self.fill(path), json=json_body, content=content, headers=sent, data=data)
        await self.settle()
        return self.response

    async def settle(self, timeout: float = 30.0) -> None:
        """Wait until every background handler started by the last action has finished."""
        me = asyncio.current_task()
        loop = asyncio.get_running_loop()
        end, quiet = loop.time() + timeout, 0
        while loop.time() < end and quiet < 3:
            await asyncio.sleep(0.02)
            busy = [t for t in asyncio.all_tasks() if t is not me and t not in self.background and not t.done()]
            quiet = quiet + 1 if not busy else 0


_ONCE = ("the response", "no ", "exactly ", "deal \"")


async def _attempt(fn: StepFn, ctx: Ctx, args: tuple[str, ...], keyword: str, text: str) -> None:
    """A state check waits up to 8 s for background work (catalogue 3.4). A check that
    something did NOT happen, or on the last response, is read once."""
    end = time.monotonic() + 8.0
    while True:
        try:
            await fn(ctx, *args)
            return
        except AssertionError:
            if keyword != "Then" or text.startswith(_ONCE) or " is not " in text or "unchanged" in text or time.monotonic() > end:
                raise
            await asyncio.sleep(0.5)


async def run_iteration(case: Case, row: dict[str, str], client: httpx.AsyncClient, background: set[asyncio.Task],
                        mail_log: list[dict[str, str]], llm_log: list[str]) -> dict[str, Any]:
    """Result document for one iteration: passed, failed or not_automated."""
    started = time.monotonic()
    steps: list[Step] = [s.filled(row) for s in case.steps]
    done: list[dict[str, Any]] = []
    missing = [s.text for s in steps if find_step(s.text) is None]
    result: dict[str, Any] = {"tc_id": case.tc_id, "row": row, "status": "passed", "error": None, "steps": done, "emails": [], "deal_id": None}
    if missing:
        result.update(status="not_automated", error=f"No automation for step: {missing[0]}", duration_ms=0)
        return result

    mail_log.clear()
    llm_log.clear()
    ctx = Ctx(client=client, background=background, emails=mail_log, llm_calls=llm_log)
    keyword = "Given"
    try:
        for s in steps:
            if s.keyword not in ("And", "But"):
                keyword = s.keyword
            if keyword != "Given" and ctx.keyword == "Given" and "D" in ctx.ids:
                from .steps import take_snapshot
                await take_snapshot(ctx)
            ctx.keyword, ctx.doc, ctx.table = keyword, s.doc, s.table
            fn, args = find_step(s.text)  # type: ignore[misc]
            began = time.monotonic()
            try:
                await _attempt(fn, ctx, args, keyword, s.text)
                done.append({"text": f"{s.keyword} {s.text}", "ok": True, "ms": int((time.monotonic() - began) * 1000)})
            except Exception:
                done.append({"text": f"{s.keyword} {s.text}", "ok": False, "ms": int((time.monotonic() - began) * 1000)})
                raise
    except NotAutomated as exc:
        result.update(status="not_automated", error=str(exc))
    except AssertionError as exc:
        result.update(status="failed", error=str(exc) or traceback.format_exc(limit=2)[-500:])
    except Exception as exc:  # noqa: BLE001 — one broken scenario never stops the run
        result.update(status="failed", error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=-3)[-900:]}")
    finally:
        for restore in reversed(ctx.undo):
            outcome = restore()
            if inspect.isawaitable(outcome):
                await outcome
        from shared.orchestration import kill_switch
        await kill_switch.resume("automation", "scenario finished")
        if ctx.extra.get("shared_changed"):
            from .steps import reset_shared_settings
            await reset_shared_settings()
        try:
            await ctx.settle(5.0)
        except Exception:  # noqa: BLE001
            pass
    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    result["deal_id"] = ctx.ids.get("D")
    result["emails"] = list(mail_log)[:20]
    result["llm_calls"] = len(llm_log)
    if ctx.response is not None:
        result["last_response"] = {"status": ctx.response.status_code, "body": ctx.response.text[:600]}
    return result
