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
    """Validate at the edge, then create the deal via the Deals service. Sign-in and the staff
    check are the deployed edge's job (the monolith's AuthMiddleware); creating a deal has no
    money effect and duplicates are handled by the deal's own idempotent creation legs."""
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


# provider → (setting holding the owning service's URL, that service's own signed webhook path).
# The owning route verifies the signature and dedupes on the event id (replay-safe), so the
# gateway forwards the request untouched — raw body, signature headers and query string.
WEBHOOK_ROUTES: dict[str, tuple[str, str]] = {
    "tazapay": ("escrow_guard_url", "/escrow-guard/webhooks/tazapay"),
    "escrow-com": ("escrow_guard_url", "/escrow-guard/webhooks/escrow-com"),
    "escrow-funded": ("escrow_guard_url", "/escrow-guard/webhooks/escrow-funded"),
    "stripe": ("escrow_guard_url", "/escrow-guard/webhooks/escrow-funded"),
    "payoneer": ("escrow_guard_url", "/escrow-guard/webhooks/payout"),
    "payout": ("escrow_guard_url", "/escrow-guard/webhooks/payout"),
    "qc": ("shield_link_url", "/shield-link/qc/webhook"),
    "sgs": ("shield_link_url", "/shield-link/qc/webhook"),
    "bureau-veritas": ("shield_link_url", "/shield-link/qc/webhook"),
    "freight": ("shield_link_url", "/shield-link/freight/webhook"),
    "cogoport": ("shield_link_url", "/shield-link/freight/webhook"),
    "freightos": ("shield_link_url", "/shield-link/freight/webhook"),
    "maersk": ("shield_link_url", "/shield-link/carrier/maersk/webhook"),
    "sendgrid": ("email_processor_url", "/email-processor/inbound/sendgrid"),
    "mailgun": ("email_processor_url", "/email-processor/inbound/mailgun"),
}
_HOP_BY_HOP = {"host", "content-length", "connection", "transfer-encoding", "keep-alive", "accept-encoding"}


class UnknownWebhookProvider(LookupError):
    pass


async def ingest_webhook(provider: str, request: Request, *, transport: Any = None) -> tuple[int, bytes, str]:
    """Single webhook ingress point: forward to the owning service's signed route and answer
    with its status and body (a refused signature stays a refusal). Unknown provider → error."""
    import httpx

    from shared.config import settings
    route = WEBHOOK_ROUTES.get(provider.lower())
    if route is None:
        raise UnknownWebhookProvider(provider)
    url_attr, path = route
    base = str(getattr(settings, url_attr)).rstrip("/")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    body = await request.body()
    async with httpx.AsyncClient(base_url=base, timeout=settings.http_timeout_seconds, transport=transport) as client:
        resp = await client.post(path, content=body, headers=headers, params=dict(request.query_params))
    return resp.status_code, resp.content, resp.headers.get("content-type", "application/json")
