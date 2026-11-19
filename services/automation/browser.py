"""Browser runner for the ``@ui`` catalogue cases (Playwright, headless Chromium).

The console is the real production bundle (``npm run build`` of portless-ui, built with
``VITE_PORTLESS_ACCOUNT_ID`` set, at ``AUTOMATION_UI_DIST``, default ``../portless-ui/dist-qa``). Nothing
listens on a port: every request the page makes is answered inside this process —

- ``/api/*`` → the PortLess app itself (the worker's in-process ASGI client) with the signed-in persona's
  gateway headers, exactly as the backend cases call it;
- ``/auth/*`` → auth-service is replaced by the scenario: a refresh is granted unless the scenario
  refuses it or breaks the network, and a sign-in is refused when the scenario says so;
- anything else → the file from the bundle, or ``index.html`` for a client-side route.

A scenario can replace one API answer (``the API answers 500 for "GET /deals"``), make one slow, or
answer an empty list. Each iteration gets a fresh browser context (clean storage). Used by the worker
when the bundle and Chromium are present; otherwise ``@ui`` cases stay ``needs_browser``.
"""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any

from .safety import REPO_ROOT

BASE = "http://console.qa"
TOKEN_KEY, USER_KEY = "portless_token", "portless_user"


def dist_dir() -> Path:
    return Path(os.environ.get("AUTOMATION_UI_DIST") or (REPO_ROOT.parent / "portless-ui" / "dist-qa"))


def available() -> tuple[bool, str]:
    """(ready, why not) — the bundle and Playwright's Chromium must both be there."""
    if os.environ.get("AUTOMATION_BROWSER", "auto").strip().lower() in ("0", "false", "no", "off"):
        return False, "AUTOMATION_BROWSER is off"
    if not (dist_dir() / "index.html").exists():
        return False, f"no console bundle at {dist_dir()} (npm run build with VITE_PORTLESS_ACCOUNT_ID set)"
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, "Playwright is not installed (pip install playwright && playwright install chromium)"
    return True, ""


def fake_jwt(subject: str, hours: float = 10.0) -> str:
    """An unsigned token with a far expiry — the console only reads ``exp`` to plan its refresh."""
    def b64(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'none'})}.{b64({'sub': subject, 'exp': int(time.time() + hours * 3600)})}.qa"


class Console:
    """One browser context for one iteration, wired to the in-process backend.

    Playwright lives on its own thread and event loop (the worker's loop is a selector loop on Windows —
    the network guard needs it — and cannot start Chromium). ``run`` executes a coroutine there; the
    page's API calls are sent back to the backend on the worker's loop."""

    def __init__(self, browser: Any, client: Any, pw_loop: Any = None, main_loop: Any = None) -> None:
        self.browser, self.client = browser, client
        self.pw_loop, self.main_loop = pw_loop, main_loop
        self.context: Any = None
        self.page: Any = None
        self.persona: str | None = None
        self.mocks: list[dict[str, Any]] = []
        self.auth = {"refresh": "ok", "login": "ok"}
        self.calls: list[str] = []
        self.bodies: list[tuple[str, str, str]] = []
        self.downloads: list[Any] = []

    async def run(self, coro: Any) -> Any:
        """Run a coroutine on the browser's loop (from the worker's loop)."""
        if self.pw_loop is None:
            return await coro
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, self.pw_loop))

    async def _backend(self, coro: Any) -> Any:
        """Run a backend call on the worker's loop (from the browser's loop)."""
        if self.main_loop is None:
            return await coro
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, self.main_loop))

    async def open_context(self) -> None:
        self.context = await self.browser.new_context(base_url=BASE, viewport={"width": 1440, "height": 900},
                                                      accept_downloads=True)
        await self.context.route("**/*", self._route)
        self.page = await self.context.new_page()
        self.page.on("download", lambda d: self.downloads.append(d))
        # window.confirm / alert: the operator answers OK (a scenario that needs "Cancel" says so)
        self.page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))

    async def close(self) -> None:
        if self.context is not None:
            await self.context.close()

    async def sign_in(self, persona: str) -> None:
        """Called on the worker's loop: the profile from the backend, then the storage in the browser."""
        from .engine import PERSONAS
        self.persona = persona
        headers = PERSONAS[persona.replace("-", "_")] or {}
        r = await self.client.get("/identity/me", headers=headers)
        user = r.json() if r.status_code == 200 else {"email": headers.get("X-User-Email"), "is_portless": persona.startswith("staff")}
        token = fake_jwt(headers.get("X-User-ID", persona))
        await self.run(self.context.add_init_script(
            f"window.localStorage.setItem({json.dumps(TOKEN_KEY)}, {json.dumps(token)});"
            f"window.localStorage.setItem({json.dumps(USER_KEY)}, {json.dumps(json.dumps(user))});"))

    # ── request routing ──────────────────────────────────────────────────
    def _mock_for(self, method: str, path: str) -> dict[str, Any] | None:
        for m in reversed(self.mocks):
            if m["method"] == method and re.fullmatch(m["path"], path.split("?")[0]):
                return m
        return None

    async def _route(self, route: Any) -> None:
        req = route.request
        url = req.url
        path = url[len(BASE):] if url.startswith(BASE) else None
        if path is None:
            await route.abort()  # no request leaves the test
            return
        try:
            if path.startswith("/api/"):
                await self._api(route, req, path[len("/api"):])
            elif path.startswith("/auth/"):
                await self._auth(route, path)
            else:
                await self._static(route, path)
        except Exception as exc:  # noqa: BLE001 — a routing failure is a 502, never a hung page
            try:
                await route.fulfill(status=502, body=json.dumps({"detail": f"automation route failed: {exc}"}),
                                    content_type="application/json")
            except Exception:  # noqa: BLE001
                pass

    async def _api(self, route: Any, req: Any, path: str) -> None:
        method = req.method
        self.calls.append(f"{method} {path}")
        if method not in ("GET", "HEAD"):
            self.bodies.append((method, path, req.post_data or ""))
        mock = self._mock_for(method, path)
        if mock and mock.get("delay"):
            await asyncio.sleep(mock["delay"])
        if mock and mock.get("status"):
            await route.fulfill(status=mock["status"], body=json.dumps(mock.get("body", {"detail": mock.get("detail", "automation")})),
                                content_type="application/json")
            return
        from .engine import PERSONAS
        headers = dict(PERSONAS[(self.persona or "anonymous").replace("-", "_")] or {})
        ct = req.headers.get("content-type")
        if ct:
            headers["content-type"] = ct
        body = req.post_data_buffer if method not in ("GET", "HEAD") else None
        r = await self._backend(self.client.request(method, path, content=body, headers=headers))
        out_headers = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "content-disposition")}
        await route.fulfill(status=r.status_code, body=r.content, headers=out_headers)

    async def _auth(self, route: Any, path: str) -> None:
        self.calls.append(f"AUTH {path}")
        if "refresh" in path:
            if self.auth["refresh"] == "network":
                await route.abort("connectionfailed")
                return
            if self.auth["refresh"] == "refused":
                await route.fulfill(status=401, body=json.dumps({"detail": "refresh refused"}), content_type="application/json")
                return
            await route.fulfill(status=200, body=json.dumps({"access_token": fake_jwt(self.persona or "qa")}),
                                content_type="application/json")
            return
        if "login" in path and self.auth["login"] == "refused":
            await route.fulfill(status=401, body=json.dumps({"detail": "Invalid email or password"}), content_type="application/json")
            return
        if "logout" in path:
            await route.fulfill(status=200, body="{}", content_type="application/json")
            return
        await route.fulfill(status=401, body=json.dumps({"detail": "auth-service is not part of the automation run"}),
                            content_type="application/json")

    async def _static(self, route: Any, path: str) -> None:
        root = dist_dir().resolve()
        rel = path.split("?")[0].lstrip("/")
        file = (root / rel).resolve() if rel else root / "index.html"
        if not rel or not file.is_file() or not str(file).startswith(str(root)):
            file = root / "index.html"  # a client-side route (or a path outside the bundle)
        ctype = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
        await route.fulfill(status=200, body=file.read_bytes(), content_type=ctype)


class BrowserSession:
    """Playwright + one Chromium for the whole run, on a thread with its own (proactor) loop."""

    def __init__(self) -> None:
        self.pw: Any = None
        self.browser: Any = None
        self.loop: Any = None
        self.thread: Any = None
        self.main_loop: Any = None

    async def start(self) -> None:
        import threading
        self.main_loop = asyncio.get_running_loop()
        self.loop = asyncio.ProactorEventLoop() if os.name == "nt" else asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="automation-browser", daemon=True)
        self.thread.start()

        async def launch() -> None:
            from playwright.async_api import async_playwright
            self.pw = await async_playwright().start()
            self.browser = await self.pw.chromium.launch(headless=True)
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(launch(), self.loop))

    async def stop(self) -> None:
        async def shutdown() -> None:
            if self.browser is not None:
                await self.browser.close()
            if self.pw is not None:
                await self.pw.stop()
        if self.loop is not None:
            try:
                await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(shutdown(), self.loop))
            finally:
                self.loop.call_soon_threadsafe(self.loop.stop)

    def prepare(self, client: Any) -> Any:
        """The ``prepare`` hook of engine.run_iteration: a fresh console per iteration."""
        async def hook(ctx: Any) -> None:
            console = Console(self.browser, client, self.loop, self.main_loop)
            await console.run(console.open_context())

            async def close() -> None:
                await console.run(console.close())
            ctx.extra["ui"] = console
            ctx.undo.append(close)
        return hook
