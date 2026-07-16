"""Combined monolith app — all services on a single port (dev / staging).

All service routers are mounted directly on this app, so there are no
inter-service HTTP round-trips. The UI (and Swagger UI at /docs) can reach
every module at its own prefix:

  /health                 meta health
  /deals/...              Deals service
  /trade-min/...          Trade-Min (opportunity scanner)
  /match-engine/...       Match Engine (CMS scoring)
  /sourcing/...           Sourcing Agent
  /negotiator/...         Negotiator Agent
  /dealmaker/...          DealMaker Agent
  /shield-link/...        Shield-Link (QC + freight)
  /docugen/...            DocuGen (document generation)
  /escrow-guard/...       Escrow Guard
  /hitl-approval/...      HITL Approval Service
  /exception-resolver/... Exception Resolver
  /email-gateway/...      Email Gateway (IMAP fetch + SMTP send transport)
  /email-processor/...    Email Processor (inbound matching + persistence + webhooks)

Run:
    cd backend
    uvicorn app:app --reload --port 8081

For production each service has its own ``services/<name>/main.py`` and runs
on an isolated port (see ``Makefile`` and ``docker-compose.yml``).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from shared.config import settings
from shared.auth.middleware import AuthMiddleware
from services.auth.router import router as auth_router
from services.deals.router import router as deals_router
from services.trade_min.router import router as trade_min_router
from services.match_engine.router import router as match_engine_router
from services.sourcing.router import router as sourcing_router
from agents.negotiation_agent.router import router as negotiation_agent_router
from services.dealmaker.router import router as dealmaker_router
from services.shield_link.router import router as shield_link_router
from services.docugen.router import router as docugen_router
from services.escrow_guard.router import router as escrow_guard_router
from services.hitl_approval.router import router as hitl_approval_router
from services.exception_resolver.router import router as exception_resolver_router
from services.email_processor.router import router as email_processor_router
from services.email_gateway.router import router as email_gateway_router

logger = logging.getLogger(__name__)

_SERVICE_ROUTERS = [
    deals_router,
    trade_min_router,
    match_engine_router,
    sourcing_router,
    negotiation_agent_router,
    dealmaker_router,
    shield_link_router,
    docugen_router,
    escrow_guard_router,
    hitl_approval_router,
    exception_resolver_router,
    email_processor_router,
    email_gateway_router,
]


# ---------------------------------------------------------------------------
# Application lifespan (shared across all service modules)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ── Trade-Min: MongoDB + APScheduler ─────────────────────────────────
    from services.trade_min.main import on_startup as tm_startup, on_shutdown as tm_shutdown
    try:
        await tm_startup()
    except Exception as exc:
        logger.error("Trade-Min startup failed (service will run degraded): %s", exc)

    # ── Deals: MongoDB repository ─────────────────────────────────────────
    from services.deals.main import on_startup as deals_startup, on_shutdown as deals_shutdown
    try:
        await deals_startup()
    except Exception as exc:
        logger.error("Deals startup failed (service will run degraded): %s", exc)

    # ── Sourcing: MongoDB repository ──────────────────────────────────────
    from services.sourcing.main import on_startup as sourcing_startup, on_shutdown as sourcing_shutdown
    try:
        await sourcing_startup()
    except Exception as exc:
        logger.error("Sourcing startup failed (service will run degraded): %s", exc)

    # ── Negotiation Agent: MongoDB repository + inbox monitor ─────────────
    from agents.negotiation_agent.main import on_startup as neg_startup, on_shutdown as neg_shutdown
    try:
        await neg_startup()
    except Exception as exc:
        logger.error("NegotiationAgent startup failed (service will run degraded): %s", exc)

    # ── DealMaker: MongoDB repository ─────────────────────────────────────
    from services.dealmaker.repository import init_repository as dm_init, close_repository as dm_close
    try:
        await dm_init()
    except Exception as exc:
        logger.error("DealMaker startup failed (service will run degraded): %s", exc)

    # ── DocuGen: MongoDB repository ───────────────────────────────────────
    from services.docugen.repository import init_repository as dg_init, close_repository as dg_close
    try:
        await dg_init()
    except Exception as exc:
        logger.error("DocuGen startup failed (service will run degraded): %s", exc)

    # ── Shield-Link: MongoDB repository ──────────────────────────────────
    from services.shield_link.repository import init_repository as sl_init, close_repository as sl_close
    try:
        await sl_init()
    except Exception as exc:
        logger.error("ShieldLink startup failed (service will run degraded): %s", exc)

    # ── Escrow-Guard: MongoDB repository ─────────────────────────────────
    from services.escrow_guard.repository import init_repository as eg_init, close_repository as eg_close
    try:
        await eg_init()
    except Exception as exc:
        logger.error("EscrowGuard startup failed (service will run degraded): %s", exc)

    # ── HITL Approval: MongoDB repository + timeout watcher ──────────────
    import asyncio as _asyncio
    from services.hitl_approval.repository import init_repository as hitl_init, close_repository as hitl_close
    from services.hitl_approval.service import run_timeout_watcher
    _hitl_watcher: _asyncio.Task | None = None
    try:
        await hitl_init()
        _hitl_watcher = _asyncio.create_task(run_timeout_watcher())
    except Exception as exc:
        logger.error("HitlApproval startup failed (service will run degraded): %s", exc)

    # ── Exception Resolver: MongoDB repository ────────────────────────────
    from services.exception_resolver.repository import init_repository as er_init, close_repository as er_close
    try:
        await er_init()
    except Exception as exc:
        logger.error("ExceptionResolver startup failed (service will run degraded): %s", exc)

    # ── Auth: MongoDB user repository (falls back to in-memory) ──────────
    from services.auth.repository import init_repository as auth_init, close_repository as auth_close
    try:
        await auth_init()
    except Exception as exc:
        logger.error("Auth startup failed (service will run degraded): %s", exc)

    # ── RAG: ensure MongoDB vector indexes (optional module) ─────────────
    try:
        from shared.rag.src.router import startup as rag_startup
        await rag_startup()
    except Exception as exc:
        logger.warning("RAG startup skipped (knowledge base will run degraded): %s", exc)

    # ── Email Processor: inbound repository (start before the Gateway, which
    #    forwards fetched mail to the Processor in-process) ──────────────────
    from services.email_processor.main import on_startup as ep_startup, on_shutdown as ep_shutdown
    try:
        await ep_startup()
    except Exception as exc:
        logger.error("EmailProcessor startup failed (service will run degraded): %s", exc)

    # ── Email Gateway: IMAP pollers + SMTP send ──────────────────────────
    from services.email_gateway.main import on_startup as eg_startup, on_shutdown as eg_shutdown
    try:
        await eg_startup()
    except Exception as exc:
        logger.error("EmailGateway startup failed (service will run degraded): %s", exc)

    # ── Event bus: in-process pub/sub + pipeline continuations ───────────
    # Makes deal state changes event-driven (NEGOTIATION_AGREED → PI issuance)
    # instead of depending on the dev auto-flow poller staying alive.
    try:
        from shared.bus import InProcessEventBus, set_publisher
        from shared.orchestration.continuations import register_continuations
        _bus = InProcessEventBus()
        register_continuations(_bus)
        set_publisher(_bus)
    except Exception as exc:
        logger.error("EventBus startup failed (events stay no-op): %s", exc)

    yield

    # ── Teardown (reverse order) ──────────────────────────────────────────
    try:
        await eg_shutdown()
    except Exception as exc:
        logger.warning("EmailGateway shutdown error: %s", exc)

    try:
        await ep_shutdown()
    except Exception as exc:
        logger.warning("EmailProcessor shutdown error: %s", exc)

    try:
        await auth_close()
    except Exception as exc:
        logger.warning("Auth shutdown error: %s", exc)

    try:
        await er_close()
    except Exception as exc:
        logger.warning("ExceptionResolver shutdown error: %s", exc)

    if _hitl_watcher and not _hitl_watcher.done():
        _hitl_watcher.cancel()
        try:
            await _hitl_watcher
        except _asyncio.CancelledError:
            pass
    try:
        await hitl_close()
    except Exception as exc:
        logger.warning("HitlApproval shutdown error: %s", exc)

    try:
        await eg_close()
    except Exception as exc:
        logger.warning("EscrowGuard shutdown error: %s", exc)

    try:
        await sl_close()
    except Exception as exc:
        logger.warning("ShieldLink shutdown error: %s", exc)

    try:
        await dg_close()
    except Exception as exc:
        logger.warning("DocuGen shutdown error: %s", exc)

    try:
        await dm_close()
    except Exception as exc:
        logger.warning("DealMaker shutdown error: %s", exc)

    try:
        await neg_shutdown()
    except Exception as exc:
        logger.warning("NegotiationAgent shutdown error: %s", exc)

    try:
        await sourcing_shutdown()
    except Exception as exc:
        logger.warning("Sourcing shutdown error: %s", exc)

    try:
        await deals_shutdown()
    except Exception as exc:
        logger.warning("Deals shutdown error: %s", exc)

    try:
        await tm_shutdown()
    except Exception as exc:
        logger.warning("Trade-Min shutdown error: %s", exc)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    app = FastAPI(
        title="PortLess · Combined Dev Server",
        version="0.1.0",
        description=(
            "All PortLess service modules on one port for local development. "
            "Each module keeps its own router prefix so the URL structure is "
            "identical to the production microservices setup."
        ),
        lifespan=lifespan,
    )

    # JWT validation runs on every request except the public allowlist
    # (/auth/login, /auth/register, *​/health, docs). Added BEFORE CORS so that
    # CORS remains the outermost middleware and 401 responses still carry CORS
    # headers (Starlette wraps last-added outermost).
    app.add_middleware(AuthMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "portless-combined"}

    app.include_router(auth_router)
    for router in _SERVICE_ROUTERS:
        app.include_router(router)

    # RAG module is optional — its imports pull in heavy deps (embeddings,
    # vector store). Mount defensively so a missing dep can't break the app.
    try:
        from shared.rag.src.router import router as rag_router

        app.include_router(rag_router, prefix="/rag")
    except Exception as exc:  # noqa: BLE001
        logger.warning("RAG router not mounted (knowledge upload disabled): %s", exc)

    return app


app = create_app()
