# PortLess developer shortcuts.

.PHONY: install dev up down gateway deals trade-min match-engine sourcing negotiator \
        dealmaker shield-link docugen escrow-guard hitl-approval exception-resolver

install:
	pip install -r requirements.txt

# --- Combined monolith (all services on port 8081 — recommended for local dev) ---
dev:
	uvicorn app:app --reload --port 8081

up:
	docker compose up --build

down:
	docker compose down

# --- Run an individual service (microservice mode) ---
gateway:
	uvicorn services.api_gateway.main:app --reload --port 8000

deals:
	uvicorn services.deals.main:app --reload --port 8001

trade-min:
	uvicorn services.trade_min.main:app --reload --port 8002

match-engine:
	uvicorn services.match_engine.main:app --reload --port 8003

sourcing:
	uvicorn services.sourcing.main:app --reload --port 8004

negotiation-agent:
	uvicorn agents.negotiation_agent.main:app --reload --port 8005

dealmaker:
	uvicorn services.dealmaker.main:app --reload --port 8006

shield-link:
	uvicorn services.shield_link.main:app --reload --port 8007

docugen:
	uvicorn services.docugen.main:app --reload --port 8008

escrow-guard:
	uvicorn services.escrow_guard.main:app --reload --port 8009

hitl-approval:
	uvicorn services.hitl_approval.main:app --reload --port 8010

exception-resolver:
	uvicorn services.exception_resolver.main:app --reload --port 8011
