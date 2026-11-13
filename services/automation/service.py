"""Start, read, stop and clear automation runs. Everything here touches only ``portless_automation``."""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import threading
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from . import catalogue
from .journeys_index import JOURNEYS
from .safety import AUTOMATION_DB, QA_ACCOUNT_ID, REPO_ROOT, VIEW_PORT, worker_env
from .store import CASES, QA_COLLECTIONS, RESULTS, RUNS, database, now

logger = logging.getLogger(__name__)

_process: subprocess.Popen[bytes] | None = None
_STALE_AFTER = timedelta(minutes=10)
_SEVERITY = ("failed", "not_automated", "needs_browser", "passed")
_LIVE_DATA = ("trade_counterparties", "opportunities")
"""What the paid Apify scan found: kept by "Clear test data"."""
_view_process: subprocess.Popen[bytes] | None = None
_view_lock = asyncio.Lock()
_VIEW_URL = f"http://127.0.0.1:{VIEW_PORT}"
_logs: dict[str, deque[str]] = {"view": deque(maxlen=200), "worker": deque(maxlen=200)}
_started: dict[str, Any] = {}


def _view_wait_seconds() -> float:
    """How long one console request waits for the view server before answering "starting".
    Kept short: a serverless gateway in front of the backend gives up long before 90 s."""
    try:
        return float(os.getenv("AUTOMATION_VIEW_WAIT_SECONDS", "8") or 8)
    except ValueError:
        return 8.0


def _spawn(kind: str, command: list[str], env: dict[str, str], log: Path) -> subprocess.Popen[bytes]:
    """Start a child process whose output goes to its log file, to this server's own log
    (visible on any hosting platform) and to an in-memory tail for /automation/diagnostics."""
    process = subprocess.Popen(command, env=env, cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    tail = _logs[kind]
    tail.clear()
    _started[kind] = now()
    child_logger = logging.getLogger(f"{__name__}.{kind}")

    def pump() -> None:
        assert process.stdout is not None
        with log.open("ab") as out:
            for raw in iter(process.stdout.readline, b""):
                out.write(raw)
                out.flush()
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    tail.append(line)
                    child_logger.info("[automation-%s] %s", kind, line[:500])
        child_logger.warning("[automation-%s] process exited with code %s", kind, process.wait())

    threading.Thread(target=pump, name=f"automation-{kind}-log", daemon=True).start()
    return process


class AutomationError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


def _db() -> Any:
    from shared.config import settings
    if not settings.mongo_uri:
        raise AutomationError(503, "No MONGO_URI is set, so there is no automation database to use.")
    return database(settings.mongo_uri)


def serverless() -> str | None:
    """The serverless platform this backend runs on, if any (Vercel, AWS Lambda, Google Cloud Run jobs aside).

    A serverless function is frozen once it answers and killed after its time limit, so it
    cannot keep the automation view server or a run worker alive."""
    if os.getenv("VERCEL") or os.getenv("VERCEL_ENV"):
        return "Vercel"
    if os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        return "AWS Lambda"
    if os.getenv("FUNCTIONS_WORKER_RUNTIME"):
        return "Azure Functions"
    return None


def _refuse_on_serverless() -> None:
    platform = serverless()
    if platform:
        raise AutomationError(
            501,
            f"Automation mode needs a long-running server: this backend runs on {platform} serverless functions, "
            "which cannot keep the automation backend or a test run alive between requests. Run the backend on a "
            "long-running host (VM, Render, Railway, Docker) or run automation locally.",
        )


def _refuse_in_production() -> None:
    import os
    if os.environ.get("ENV_PROFILE", "dev").lower() == "production":
        raise AutomationError(403, "Automation runs are not started on a production server.")


def _alive() -> bool:
    return _process is not None and _process.poll() is None


async def _latest_run(db: Any) -> dict[str, Any] | None:
    return await db[RUNS].find_one(sort=[("started_at", -1)])


async def _settle_dead_run(db: Any, run: dict[str, Any] | None) -> dict[str, Any] | None:
    """A run whose worker has gone cannot stay RUNNING forever."""
    if run is None or run.get("status") not in ("STARTING", "RUNNING"):
        return run
    beat = run.get("heartbeat_at") or run.get("started_at")
    if beat is not None and beat.tzinfo is None:
        from datetime import timezone
        beat = beat.replace(tzinfo=timezone.utc)
    if _alive() or (beat is not None and now() - beat < _STALE_AFTER and _process is None):
        return run
    await db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"status": "INTERRUPTED", "finished_at": now()}})
    return {**run, "status": "INTERRUPTED"}


def _catalogue_summary() -> dict[str, Any]:
    sha, cases = catalogue.load()
    return {"sha": sha, "cases": len(cases) + len(JOURNEYS), "iterations": sum(len(c.iterations()) for c in cases) + len(JOURNEYS)}


async def status() -> dict[str, Any]:
    db = _db()
    run = await _settle_dead_run(db, await _latest_run(db))
    counts: dict[str, int] = {}
    for name in sorted(await db.list_collection_names()):
        if name not in QA_COLLECTIONS:
            n = await db[name].estimated_document_count()
            if n:
                counts[name] = n
    live = {role: await db["trade_counterparties"].count_documents({"role": role}) for role in ("buyer", "supplier")}
    return {"database": AUTOMATION_DB, "catalogue": _catalogue_summary(), "run": _clean(run),
            "test_data": counts, "test_data_total": sum(counts.values()), "live_counterparties": live}


def _clean(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    return {("run_id" if k == "_id" else k): v for k, v in doc.items()}


async def start_run(by: str, *, scope: str = "all", phase: str = "", priority: str = "") -> dict[str, Any]:
    global _process
    _refuse_in_production()
    _refuse_on_serverless()
    db = _db()
    if _alive():
        raise AutomationError(409, "A run is already in progress.")
    run_id = f"RUN-{now():%Y%m%d-%H%M%S}-{uuid4().hex[:4]}"
    await db[RUNS].insert_one({"_id": run_id, "status": "STARTING", "scope": scope, "phase": phase, "priority": priority,
                               "started_by": by, "started_at": now(), "heartbeat_at": now(), "done": 0, "totals": {}})
    command = [sys.executable, "-X", "utf8", "-W", "ignore", "-m", "services.automation.worker", "--run-id", run_id, "--scope", scope]
    if phase:
        command += ["--phase", phase]
    if priority:
        command += ["--priority", priority]
    log = Path(tempfile.gettempdir()) / f"portless-automation-{run_id}.log"
    _process = _spawn("worker", command, worker_env(live_apify=scope in ("all", "live")), log)
    logger.info("Automation: run %s started by %s (%s)", run_id, by, scope)
    return {"run_id": run_id, "status": "STARTING"}


async def stop_run() -> dict[str, Any]:
    global _process
    db = _db()
    if not _alive():
        raise AutomationError(409, "No run is in progress.")
    assert _process is not None
    _process.kill()
    _process.wait(timeout=20)
    run = await _latest_run(db)
    if run:
        await db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"status": "STOPPED", "finished_at": now()}})
    return {"status": "STOPPED"}


async def clear(scope: str) -> dict[str, Any]:
    """``test_data``: what the runs created. ``everything``: results and catalogue too."""
    db = _db()
    if _alive():
        raise AutomationError(409, "Stop the run before clearing its data.")
    run = await _latest_run(db)
    if run and run.get("status") in ("STARTING", "RUNNING") and (await _settle_dead_run(db, run) or {}).get("status") == "RUNNING":
        raise AutomationError(409, "A run is still reporting progress; stop it first.")
    deleted = 0
    for name in await db.list_collection_names():
        if scope == "test_data" and (name in QA_COLLECTIONS or name in _LIVE_DATA):
            continue
        deleted += (await db[name].delete_many({})).deleted_count
    logger.warning("Automation: cleared %s (%s, %d records)", AUTOMATION_DB, scope, deleted)
    return {"database": AUTOMATION_DB, "scope": scope, "deleted": deleted}


async def _view_ready(client: httpx.AsyncClient) -> bool:
    try:
        return (await client.get(f"{_VIEW_URL}/health")).status_code == 200
    except httpx.HTTPError:
        return False


def _start_view_process() -> None:
    global _view_process
    if _view_process is None or _view_process.poll() is not None:
        log = Path(tempfile.gettempdir()) / "portless-automation-view.log"
        _view_process = _spawn("view", [sys.executable, "-X", "utf8", "-W", "ignore", "-m", "services.automation.view_server"],
                               worker_env(view=True), log)
        logger.info("Automation: view server starting on 127.0.0.1:%s (pid %s)", VIEW_PORT, _view_process.pid)


async def _ensure_view_server(client: httpx.AsyncClient) -> None:
    """Make sure the automation backend is up; start it the first time the console needs it.

    Answers within AUTOMATION_VIEW_WAIT_SECONDS: while the process is still booting the
    console gets a 503 "starting" (and retries) instead of a request the gateway cuts off."""
    if await _view_ready(client):
        return
    _refuse_in_production()
    _refuse_on_serverless()
    _db()
    async with _view_lock:
        if await _view_ready(client):
            return
        _start_view_process()
        assert _view_process is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _view_wait_seconds()
        while loop.time() < deadline:
            await asyncio.sleep(0.5)
            if await _view_ready(client):
                return
            if _view_process.poll() is not None:
                break
    if _view_process is not None and _view_process.poll() is None:
        booting = int((now() - _started["view"]).total_seconds()) if _started.get("view") else 0
        raise AutomationError(503, f"The automation backend is starting ({booting} s so far) — try again in a few seconds.")
    last = " | ".join(list(_logs["view"])[-3:]) or "no output"
    raise AutomationError(503, f"The automation backend stopped while starting: {last[:400]} (see GET /automation/diagnostics).")


async def warm_up() -> None:
    """Start the view server with the backend, so the first Automation-mode page does not
    wait for a whole application to boot. Off on production, without Mongo, inside an
    automation process, or with AUTOMATION_VIEW_AUTOSTART=false."""
    if os.getenv("AUTOMATION_WORKER") == "1" or os.getenv("ENV_PROFILE", "dev").lower() == "production" or serverless():
        return
    if os.getenv("AUTOMATION_VIEW_AUTOSTART", "true").strip().lower() in ("0", "false", "no", "off"):
        return
    from shared.config import settings
    if not settings.mongo_uri:
        return
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            if not await _view_ready(client):
                _start_view_process()
    except Exception as exc:  # noqa: BLE001 — automation never stops the backend from booting
        logger.warning("Automation: view server not pre-started: %s", exc)


async def diagnostics() -> dict[str, Any]:
    """Why Automation mode does or does not work on this server (staff only)."""
    from shared.config import settings
    async with httpx.AsyncClient(timeout=3) as client:
        ready = await _view_ready(client)

    def proc(p: subprocess.Popen[bytes] | None, kind: str) -> dict[str, Any]:
        return {"running": p is not None and p.poll() is None, "pid": p.pid if p else None,
                "exit_code": p.poll() if p else None, "started_at": _started.get(kind),
                "log_tail": list(_logs[kind])[-40:]}

    return {
        "env_profile": os.getenv("ENV_PROFILE", "dev"),
        "serverless": serverless(),
        "runs_allowed": os.getenv("ENV_PROFILE", "dev").lower() != "production" and not serverless(),
        "mongo_configured": bool(settings.mongo_uri),
        "database": AUTOMATION_DB,
        "view": {"url": _VIEW_URL, "ready": ready, "wait_seconds": _view_wait_seconds(), **proc(_view_process, "view")},
        "worker": proc(_process, "worker"),
        "llm_gateway_configured": bool(os.getenv("LLM_GATEWAY_URL") or getattr(settings, "llm_gateway_url", "")),
    }


async def forward_to_view(method: str, path: str, query: str, body: bytes, content_type: str | None, user: Any) -> httpx.Response:
    """One console request, answered from the automation database as the signed-in operator."""
    headers = {"X-User-ID": user.user_id, "X-User-Email": user.email or "", "X-User-Name": user.name or "", "X-Account-ID": QA_ACCOUNT_ID}
    if content_type:
        headers["content-type"] = content_type
    async with httpx.AsyncClient(timeout=120) as client:
        await _ensure_view_server(client)
        url = f"{_VIEW_URL}/{path}" + (f"?{query}" if query else "")
        return await client.request(method, url, content=body, headers=headers)


async def _run_id(db: Any, run_id: str | None) -> str | None:
    if run_id:
        return run_id
    run = await _latest_run(db)
    return run["_id"] if run else None


def _worst(statuses: list[str]) -> str:
    return next((s for s in _SEVERITY if s in statuses), "not_run")


async def list_cases(run_id: str | None = None) -> dict[str, Any]:
    """Every case in the catalogue with its outcome in the given (or latest) run."""
    db = _db()
    rid = await _run_id(db, run_id)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if rid:
        projection = {"tc_id": 1, "status": 1, "duration_ms": 1, "error": 1, "llm_calls": 1}
        async for row in db[RESULTS].find({"run_id": rid}, projection):
            grouped[row["tc_id"]].append(row)
    _, cases = catalogue.load()
    rows: list[dict[str, Any]] = []
    entries = [(c.tc_id, c.title, c.phase, c.layer, c.priority, c.kind, len(c.iterations())) for c in cases]
    entries += [(j["id"], j["title"], "llm-journeys", "e2e", "P0", "journey", 1) for j in JOURNEYS]
    for tc_id, title, phase, layer, priority, kind, planned in entries:
        results = grouped.get(tc_id, [])
        statuses = [r["status"] for r in results]
        rows.append({
            "tc_id": tc_id, "title": title, "phase": phase, "layer": layer, "priority": priority, "kind": kind,
            "iterations": planned, "status": _worst(statuses),
            "passed": statuses.count("passed"), "failed": statuses.count("failed"),
            "duration_ms": sum(int(r.get("duration_ms") or 0) for r in results),
            "error": next((r.get("error") for r in results if r["status"] in ("failed", "not_automated")), None),
        })
    return {"run_id": rid, "cases": rows}


async def case_detail(tc_id: str, run_id: str | None = None) -> dict[str, Any]:
    db = _db()
    rid = await _run_id(db, run_id)
    results = []
    if rid:
        async for row in db[RESULTS].find({"run_id": rid, "tc_id": tc_id}, {"_id": 0}).sort("iteration", 1):
            results.append(row)
    _, cases = catalogue.load()
    case = next((c for c in cases if c.tc_id == tc_id), None)
    journey = next((j for j in JOURNEYS if j["id"] == tc_id), None)
    if case is None and journey is None:
        raise AutomationError(404, f"Unknown test case {tc_id}")
    definition = case.to_doc() if case else {"title": journey["title"], "steps": []}  # type: ignore[index]
    return {"tc_id": tc_id, "run_id": rid, "definition": definition, "results": results}


async def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    db = _db()
    return [_clean(r) async for r in db[RUNS].find().sort("started_at", -1).limit(limit)]  # type: ignore[misc]


__all__ = ["CASES", "AutomationError"]
