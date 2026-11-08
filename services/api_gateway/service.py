"""Gateway orchestration logic — delegates to downstream services over REST."""
from __future__ import annotations

from typing import Any

from fastapi import Request

from . import clients
from .schemas import CreateDealRequest, DealResponse


async def downstream_health() -> dict[str, str]:
    """Ping the health endpoint of every downstream service."""
    results: dict[str, str] = {}
    for name, client in clients.ALL_CLIENTS.items():
        try:
            await client.health()
            results[name] = "ok"
        except Exception:  # noqa: BLE001 - report any unreachable service
            results[name] = "unreachable"
    return results


async def create_deal(payload: CreateDealRequest) -> DealResponse:
    """Validate at the edge, then create the deal via the Deals service.

    TODO: add auth, idempotency key handling, and request enrichment.
    """
    data = await clients.deals.create_deal(payload.model_dump(mode="json"))
    return DealResponse.model_validate(data)


# --- Thin read/aggregation delegations for the Ops Console UI ---


async def list_deals(status: str | None) -> Any:
    return await clients.deals.list_deals(status=status)


async def get_deal(deal_id: str) -> Any:
    return await clients.deals.get_deal(deal_id)


async def update_deal_status(deal_id: str, payload: dict[str, Any]) -> Any:
    return await clients.deals.update_status(deal_id, payload)


async def list_opportunities() -> Any:
    return await clients.trade_min.list_opportunities()


async def scan_opportunities(payload: dict[str, Any]) -> Any:
    return await clients.trade_min.post("/trade-min/scan", json=payload)


async def list_approvals(status: str | None) -> Any:
    return await clients.hitl_approval.list_approvals(status)


async def resolve_approval(approval_id: str, payload: dict[str, Any]) -> Any:
    return await clients.hitl_approval.resolve_approval(approval_id, payload)


async def get_exception(exception_id: str) -> Any:
    return await clients.exception_resolver.get_exception(exception_id)


async def ingest_webhook(provider: str, request: Request) -> dict[str, str]:
    """Single webhook ingress point.

    TODO: verify signature, dedupe on idempotency key, route to owning service.
    """
    raise NotImplementedError
