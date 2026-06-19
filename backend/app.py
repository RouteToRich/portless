"""Combined monolith app — all services on a single port (dev / staging).

All service routers are mounted directly on this app, so there are no
inter-service HTTP round-trips. The UI (and Swagger UI at /docs) can reach every
module at its own prefix:

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

Run:
    cd backend
    uvicorn app:app --reload --port 8081

For real-world deployment each service has its own ``services/<name>/main.py``
and runs on an isolated port (see ``Makefile`` and ``docker-compose.yml``).
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from shared.config import settings
from services.deals.router import router as deals_router
from services.trade_min.router import router as trade_min_router
from services.match_engine.router import router as match_engine_router
from services.sourcing.router import router as sourcing_router
from services.negotiator.router import router as negotiator_router
from services.dealmaker.router import router as dealmaker_router
from services.shield_link.router import router as shield_link_router
from services.docugen.router import router as docugen_router
from services.escrow_guard.router import router as escrow_guard_router
from services.hitl_approval.router import router as hitl_approval_router
from services.exception_resolver.router import router as exception_resolver_router

_SERVICE_ROUTERS = [
    deals_router,
    trade_min_router,
    match_engine_router,
    sourcing_router,
    negotiator_router,
    dealmaker_router,
    shield_link_router,
    docugen_router,
    escrow_guard_router,
    hitl_approval_router,
    exception_resolver_router,
]


def create_app() -> FastAPI:
    app = FastAPI(
        title="PortLess · Combined Dev Server",
        version="0.1.0",
        description=(
            "All PortLess service modules on one port for local development. "
            "Each module keeps its own router prefix so the URL structure is "
            "identical to the production microservices setup."
        ),
    )

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

    for router in _SERVICE_ROUTERS:
        app.include_router(router)

    return app


app = create_app()
