"""HTTP routes for the API Gateway service."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from . import service
from .schemas import CreateDealRequest, DealResponse

router = APIRouter(tags=["api-gateway"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "api-gateway"}


@router.get("/health/downstream")
async def downstream_health() -> dict[str, str]:
    """Aggregate health check across all downstream services."""
    return await service.downstream_health()


@router.get("/health/readiness")
async def readiness() -> dict[str, Any]:
    """Production readiness report (same checks as the monolith's /health/readiness)."""
    from shared.readiness import evaluate
    return evaluate()


@router.post("/deals", response_model=DealResponse, status_code=201)
async def create_deal(payload: CreateDealRequest) -> DealResponse:
    """Edge entry point: validate then delegate to the Deals service via REST."""
    return await service.create_deal(payload)


# --- Read / aggregation endpoints consumed by the Ops Console UI ---


@router.get("/deals")
async def list_deals(status: str | None = None) -> Any:
    return await service.list_deals(status)


@router.get("/deals/{deal_id}")
async def get_deal(deal_id: str) -> Any:
    return await service.get_deal(deal_id)


@router.patch("/deals/{deal_id}/status")
async def update_deal_status(deal_id: str, payload: dict[str, Any]) -> Any:
    return await service.update_deal_status(deal_id, payload)


@router.get("/opportunities")
async def list_opportunities() -> Any:
    return await service.list_opportunities()


@router.post("/opportunities/scan")
async def scan_opportunities(payload: dict[str, Any]) -> Any:
    return await service.scan_opportunities(payload)


@router.get("/approvals")
async def list_approvals(status: str | None = None) -> Any:
    return await service.list_approvals(status)


@router.post("/approvals/{approval_id}/resolve")
async def resolve_approval(approval_id: str, payload: dict[str, Any]) -> Any:
    return await service.resolve_approval(approval_id, payload)


@router.get("/exceptions/{exception_id}")
async def get_exception(exception_id: str) -> Any:
    return await service.get_exception(exception_id)


@router.post("/webhooks/{provider}")
async def ingest_webhook(provider: str, request: Request) -> dict[str, str]:
    """Single webhook ingress point (idempotency + signature verification).

    TODO: verify signature, dedupe on idempotency key, route to the owning service.
    """
    return await service.ingest_webhook(provider, request)
