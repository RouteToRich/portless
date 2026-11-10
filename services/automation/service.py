"""Start, read, stop and clear automation runs. Everything here touches only ``portless_automation``."""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import tempfile
from collections import defaultdict
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


class AutomationError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


def _db() -> Any:
    from shared.config import settings
    if not settings.mongo_uri:
        raise AutomationError(503, "No MONGO_URI is set, so there is no automation database to use.")
    return database(settings.mongo_uri)


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
    with log.open("wb") as out:
        env = worker_env(live_apify=scope in ("all", "live"))
        _process = subprocess.Popen(command, env=env, cwd=str(REPO_ROOT), stdout=out, stderr=subprocess.STDOUT)
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


async def _ensure_view_server(client: httpx.AsyncClient) -> None:
    """Start the automation backend the first time the console needs it."""
    global _view_process
    if await _view_ready(client):
        return
    _refuse_in_production()
    _db()
    async with _view_lock:
        if await _view_ready(client):
            return
        if _view_process is None or _view_process.poll() is not None:
            log = Path(tempfile.gettempdir()) / "portless-automation-view.log"
            with log.open("ab") as out:
                _view_process = subprocess.Popen(
                    [sys.executable, "-X", "utf8", "-W", "ignore", "-m", "services.automation.view_server"],
                    env=worker_env(view=True), cwd=str(REPO_ROOT), stdout=out, stderr=subprocess.STDOUT,
                )
        for _ in range(90):
            await asyncio.sleep(1)
            if await _view_ready(client):
                return
            if _view_process.poll() is not None:
                break
    raise AutomationError(503, "The automation backend did not start; see portless-automation-view.log.")


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
