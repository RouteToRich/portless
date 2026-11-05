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
    import asyncio as _asyncio  # shared by the background watchers below

    # ── Production readiness gate (P1-1) ──────────────────────────────────
    # Names every silent fallback (stub escrow, unsigned money webhooks,
    # log-only e-mail, missing Apify token …). Informational in dev/staging;
    # ENV_PROFILE=production refuses to boot while a check fails.
    from shared.readiness import assert_ready_or_log
    assert_ready_or_log()

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
    from agents.negotiation_agent.service import run_stale_watcher
    _neg_stale_watcher: _asyncio.Task | None = None
    try:
        await neg_startup()
        # Sweep negotiations stuck in a non-terminal status: re-notify a
        # never-actioned round-0 email approval, and fail long-idle later rounds
        # so the deal leaves NEGOTIATING instead of going stale on the board.
        _neg_stale_watcher = _asyncio.create_task(run_stale_watcher())
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

    # ── Autonomy policy: per-action L0–L3 overrides (Mongo-persisted) ─────
    try:
        from shared.orchestration.autonomy_policy import load_policy
        await load_policy()
    except Exception as exc:
        logger.error("AutonomyPolicy load failed (built-in defaults apply): %s", exc)

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

    # ── Event bus: pipeline continuations over NATS JetStream or in-process ──
    # Makes deal state changes event-driven (NEGOTIATION_AGREED → PI issuance)
    # instead of depending on the dev auto-flow poller staying alive.
    # Backend selected by EVENT_BUS_BACKEND (auto → NATS when a *.creds file /
    # NATS_CREDS_FILE is present, else in-process). init_event_bus never
    # raises — any NATS failure degrades loudly to the in-process bus.
    try:
        from shared.bus import init_event_bus
        await init_event_bus(consume=True)
    except Exception as exc:
        logger.error("EventBus startup failed (events stay no-op): %s", exc)

    # ── Continuations catch-up: start a session for any deal already parked ──
    # in NEGOTIATING with no negotiation session behind it. The NEGOTIATION_STARTED
    # subscriber (registered by init_event_bus above) only fires for deals that
    # transition after startup — this one-shot idempotent sweep covers deals that
    # were already stuck, including operator-created ones with no opportunity_id.
    try:
        from shared.orchestration.continuations import resume_parked_negotiations
        await resume_parked_negotiations()
    except Exception as exc:
        logger.error("Continuation resume failed (parked NEGOTIATING deals may stay idle): %s", exc)

    # ── Auto-flow: re-arm the in-memory poller for deals already in-flight ──
    # (dev/test only; no-op when AUTO_FLOW_ENABLED=false). auto_flow's task is
    # a bare asyncio task that dies with the process — without this, a deal
    # sitting in MATCHING/NEGOTIATING at restart never gets its PI/escrow
    # hand-off after the negotiation converges, even though the negotiation
    # itself keeps working fine via Mongo + the event bus.
    try:
        from shared.orchestration.auto_flow import resume_in_flight
        await resume_in_flight()
    except Exception as exc:
        logger.error("AutoFlow resume failed (in-flight deals may stay orphaned): %s", exc)

    # ── Deal SLA watcher: reminders + stall exceptions for parked deals ──────
    # PI_SENT with no buyer acceptance, ESCROW_PENDING with no funding, and any
    # status unchanged past DEAL_STALL_SLA_HOURS. Reminders go out as templated
    # counterparty notifications; expiries/stalls become deal.exception events
    # for the Exception Resolver. See shared/orchestration/deal_watcher.py.
    _deal_watcher: _asyncio.Task | None = None
    try:
        from shared.orchestration.deal_watcher import run_deal_watcher
        _deal_watcher = _asyncio.create_task(run_deal_watcher())
    except Exception as exc:
        logger.error("DealWatcher start failed (parked deals get no reminders): %s", exc)

    # ── Daily operator digest (Telegram / e-mail at DIGEST_HOUR_UTC) ────────
    _digest: _asyncio.Task | None = None
    try:
        from shared.orchestration.digest import run_digest_scheduler
        _digest = _asyncio.create_task(run_digest_scheduler())
    except Exception as exc:
        logger.error("Digest scheduler start failed: %s", exc)

    yield

    # ── Teardown (reverse order) ──────────────────────────────────────────
    for task in (_digest, _deal_watcher):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except _asyncio.CancelledError:
                pass

    try:
        from shared.bus import close_event_bus
        await close_event_bus()
    except Exception as exc:
        logger.warning("EventBus shutdown error: %s", exc)

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

    try:
        from shared.llm_gateway.remote import aclose_remote_client
        await aclose_remote_client()
    except Exception as exc:
        logger.warning("LLM Gateway client shutdown error: %s", exc)

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

    if _neg_stale_watcher and not _neg_stale_watcher.done():
        _neg_stale_watcher.cancel()
        try:
            await _neg_stale_watcher
        except _asyncio.CancelledError:
            pass
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

    # Every request except the public allowlist (*/health, docs) must carry an
    # auth-service identity (shared/auth/identity.py). Added BEFORE CORS so that
    # CORS remains the outermost middleware and 401 responses still carry CORS
    # headers (Starlette wraps last-added outermost).
    app.add_middleware(AuthMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "portless-combined"}

    @app.get("/health/readiness", tags=["meta"])
    async def readiness() -> dict:
        """Production readiness report: every stub / fallback the pipeline is
        currently running on, with ok / warn / fail per check (System Health page)."""
        from shared.readiness import evaluate
        return evaluate()

    @app.get("/health/downstream", tags=["meta"])
    async def downstream() -> dict[str, str]:
        """In the monolith every module is in-process: report each router's
        health so the System Health page works without the standalone gateway."""
        names = [
            "deals", "trade-min", "match-engine", "sourcing", "negotiation-agent", "dealmaker",
            "shield-link", "docugen", "escrow-guard", "hitl-approval", "exception-resolver",
            "email-gateway", "email-processor", "identity",
        ]
        return {n: "ok" for n in names}

    app.include_router(auth_router)
    for router in _SERVICE_ROUTERS:
        app.include_router(router)

    return app


app = create_app()
