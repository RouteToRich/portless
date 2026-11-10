"""HTTP routes behind the console's Automation mode. Portless staff only."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from shared.auth.dependencies import AuthUser, require_portless

from . import service
from .service import AutomationError

router = APIRouter(prefix="/automation", tags=["automation"], dependencies=[Depends(require_portless)])


class StartRunRequest(BaseModel):
    scope: Literal["all", "catalogue", "journeys", "live"] = "all"
    phase: str = ""
    priority: str = ""


class ClearRequest(BaseModel):
    scope: Literal["test_data", "everything"]


async def _answer(call: Any) -> Any:
    try:
        return await call
    except AutomationError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


@router.get("/status")
async def api_status() -> dict[str, Any]:
    """Database in use, the latest run, catalogue size and how much test data exists."""
    return await _answer(service.status())


@router.get("/cases")
async def api_cases(run_id: str | None = None) -> dict[str, Any]:
    """Every catalogue case with its outcome in the given (default latest) run."""
    return await _answer(service.list_cases(run_id))


@router.get("/cases/{tc_id}")
async def api_case(tc_id: str, run_id: str | None = None) -> dict[str, Any]:
    """One case's steps, each iteration's result and the e-mails it composed."""
    return await _answer(service.case_detail(tc_id, run_id))


@router.get("/runs")
async def api_runs() -> list[dict[str, Any]]:
    return await _answer(service.list_runs())


@router.post("/runs", status_code=202)
async def api_start(body: StartRunRequest, user: AuthUser = Depends(require_portless)) -> dict[str, Any]:
    """Start a run in its own process, pointed only at the automation database."""
    return await _answer(service.start_run(user.email or user.user_id, scope=body.scope, phase=body.phase, priority=body.priority))


@router.post("/runs/stop")
async def api_stop() -> dict[str, Any]:
    return await _answer(service.stop_run())


@router.api_route("/view/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
async def api_view(path: str, request: Request, user: AuthUser = Depends(require_portless)) -> Response:
    """Automation mode: any console request, answered from the automation database."""
    answer = await _answer(service.forward_to_view(
        request.method, path, request.url.query, await request.body(), request.headers.get("content-type"), user,
    ))
    passed = {k: v for k, v in answer.headers.items() if k.lower() == "content-disposition"}
    return Response(content=answer.content, status_code=answer.status_code, media_type=answer.headers.get("content-type"), headers=passed)


@router.post("/clear")
async def api_clear(body: ClearRequest) -> dict[str, Any]:
    """Delete what automation stored. Never touches any other database."""
    return await _answer(service.clear(body.scope))
