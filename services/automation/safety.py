"""What keeps an automation run away from real people and real data.

The worker starts from a blanked environment: only the Mongo connection and
the LLM gateway login survive, the database name is forced, and nothing can be
delivered. ``assert_isolated`` refuses to run if any of that is not true, and
the network guard allows only Mongo, the LLM gateway and DNS.
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

AUTOMATION_DB = "portless_automation"
VIEW_PORT = 8082
REPO_ROOT = Path(__file__).resolve().parents[2]

STAFF_EMAILS = ("qa-staff@portless-qa.invalid", "qa-staff2@portless-qa.invalid")
QA_ACCOUNT_ID = "qa-portless-account"

_KEPT_SECRETS = ("MONGO_URI", "LLM_GATEWAY_URL", "LLM_GATEWAY_EMAIL", "LLM_GATEWAY_PASSWORD", "LLM_GATEWAY_ACCOUNT_ID")
_SERVICE_PREFIXES = ("TRADE", "DEALS", "SOURCING", "NEGOTIATION", "DEALMAKER", "DOCUGEN", "SHIELD_LINK", "ESCROW_GUARD",
                     "HITL_APPROVAL", "EXCEPTION_RESOLVER", "EMAIL_PROCESSOR", "LLM", "CACHE", "AUTH")
_MUST_BE_BLANK = ("GMAIL_SENDER_ADDRESS", "GMAIL_APP_PASSWORD", "IMAP_USER", "IMAP_PASSWORD", "APIFY_TOKEN",
                  "GOOGLE_DRIVE_REFRESH_TOKEN", "HITL_TELEGRAM_BOT_TOKEN", "HITL_NOTIFY_WEBHOOK_URL",
                  "TAZAPAY_API_KEY", "ESCROW_COM_API_KEY", "STRIPE_SECRET_KEY", "PAYONEER_CLIENT_ID",
                  "SGS_API_KEY", "QIMA_API_KEY", "COGOPORT_API_KEY", "FREIGHTOS_API_KEY", "ICEGATE_API_KEY")

WEBHOOK_SECRETS = {
    "TAZAPAY_WEBHOOK_SECRET": "qa-tazapay", "ESCROW_COM_WEBHOOK_SECRET": "qa-escrowcom",
    "STRIPE_WEBHOOK_SECRET": "qa-stripe", "PAYONEER_WEBHOOK_SECRET": "qa-payoneer",
    "SGS_WEBHOOK_SECRET": "qa-sgs", "QIMA_WEBHOOK_SECRET": "qa-qima", "BUREAU_VERITAS_WEBHOOK_SECRET": "qa-bv",
    "INTERTEK_WEBHOOK_SECRET": "qa-intertek", "TUV_SUD_WEBHOOK_SECRET": "qa-tuv",
    "COGOPORT_WEBHOOK_SECRET": "qa-cogoport", "FREIGHTOS_WEBHOOK_SECRET": "qa-freightos",
    "MAILGUN_SIGNING_KEY": "qa-mailgun",
}


def worker_env(use_mongo: bool = True, *, live_apify: bool = False, view: bool = False) -> dict[str, str]:
    """The environment of a run: everything configured is blanked first.

    ``live_apify`` keeps the Apify token, for the scan that finds real buyers
    and suppliers. ``view`` also lets the real staff list sign in, for the
    console's Automation mode."""
    from dotenv import dotenv_values

    real = {k: (v or "") for k, v in dotenv_values(REPO_ROOT / ".env").items()}
    env = dict(os.environ)
    for key in real:
        env[key] = ""
    for prefix in _SERVICE_PREFIXES:  # removed, not blanked: a blank value would switch that service to memory
        env.pop(f"{prefix}_MONGO_URI", None)
        env.pop(f"{prefix}_MONGO_DB_NAME", None)
    for key in _MUST_BE_BLANK:
        env[key] = ""
    for key in _KEPT_SECRETS:
        env[key] = real.get(key) or os.environ.get(key, "")
    if not use_mongo:
        env["MONGO_URI"] = ""
    env.pop("APIFY_BASE_URL", None)  # a blank value would break the connector's own default
    env.update(WEBHOOK_SECRETS)
    env.update({
        "ENV_PROFILE": "dev",
        "MONGO_DB_NAME": AUTOMATION_DB,
        "AUTOMATION_WORKER": "1",
        "EVENT_BUS_BACKEND": "inprocess",
        "AUTH_TRUST_GATEWAY_HEADERS": "true",
        "AUTH_PORTLESS_EMAILS": ",".join(STAFF_EMAILS),
        "PORTLESS_ACCOUNT_ID": QA_ACCOUNT_ID,
        "APIFY_TOKEN_AUTODISCOVER": "false",
        "APIFY_MAX_RUNS_PER_DAY": "0",
        "APIFY_ENABLE_AI": "false",
        "KNOWLEDGE_ENABLED": "false",
        "DEAL_WATCHER_ENABLED": "false",
        "DAILY_DIGEST_ENABLED": "false",
        "AUTO_FLOW_ENABLED": "false",
        "AUTO_DEAL_FROM_OPPORTUNITY_ENABLED": "false",
        "NEGOTIATION_MARKET_SEARCH_ENABLED": "false",
        "FOUR_EYES_THRESHOLD_USD": "",
        "PORTLESS_ENTITY_NAME": "PortLess QA Exports",
    })
    if live_apify:
        env.update({"APIFY_TOKEN": real.get("APIFY_TOKEN", ""), "AUTOMATION_APIFY": "1",
                    "APIFY_MAX_RUNS_PER_DAY": real.get("APIFY_MAX_RUNS_PER_DAY") or "40"})
    if view:
        env["AUTH_PORTLESS_EMAILS"] = ",".join([*STAFF_EMAILS, *filter(None, (real.get("AUTH_PORTLESS_EMAILS") or "").split(","))])
    return env


def assert_on_automation_database() -> None:
    """Every service must really be storing in the automation database."""
    from agents.negotiation_agent.repository import get_repository as negotiation
    from services.dealmaker.repository import get_repository as dealmaker
    from services.deals.repository import get_repository as deals
    from services.docugen.repository import get_repository as docugen
    from services.escrow_guard.repository import get_repository as escrow
    from services.exception_resolver.repository import get_repository as resolver
    from services.hitl_approval.repository import get_repository as hitl
    from services.shield_link.repository import get_repository as shield

    in_memory = [type(r()).__name__ for r in (negotiation, dealmaker, deals, docugen, escrow, resolver, hitl, shield)
                 if "memory" in type(r()).__name__.lower()]
    if in_memory:
        raise SystemExit("Automation refused: these services are not on the database: " + ", ".join(in_memory))


def without_startup_resume() -> None:
    """On boot the app re-opens parked negotiations. Here those are test deals,
    so re-opening them would only redraft e-mails nobody asked for."""
    from shared.orchestration import continuations

    async def nothing() -> None:
        return None

    continuations.resume_parked_negotiations = nothing  # type: ignore[assignment]


def assert_isolated() -> None:
    """Refuse to run unless the environment is exactly the safe one."""
    problems = []
    if os.environ.get("MONGO_DB_NAME") != AUTOMATION_DB:
        problems.append(f"MONGO_DB_NAME is {os.environ.get('MONGO_DB_NAME')!r}, not {AUTOMATION_DB}")
    allowed = {"APIFY_TOKEN"} if os.environ.get("AUTOMATION_APIFY") == "1" else set()
    problems += [f"{key} is set" for key in _MUST_BE_BLANK if os.environ.get(key) and key not in allowed]
    if os.environ.get("AUTOMATION_WORKER") != "1":
        problems.append("not started as an automation worker")
    if problems:
        raise SystemExit("Automation refused: " + "; ".join(problems))


class NetworkGuard:
    """Only the LLM gateway, Mongo and DNS may be reached; the rest is recorded and refused."""

    def __init__(self) -> None:
        self.blocked: list[str] = []
        self._hosts = {"localhost"}
        self._suffixes: tuple[str, ...] = ()
        self._ips = {"127.0.0.1", "::1"}

    def install(self) -> None:
        gateway = urlparse(os.environ.get("LLM_GATEWAY_URL", "")).hostname
        if gateway:
            self._hosts.add(gateway)
        if os.environ.get("AUTOMATION_APIFY") == "1":
            self._hosts.update({"api.apify.com", "comtradeapi.un.org", "api.gdeltproject.org", "ted.europa.eu"})  # read-only market data
        mongo = os.environ.get("MONGO_URI", "")
        if mongo:
            hosts = mongo.split("@")[-1].split("/")[0].split("?")[0]
            first = hosts.split(",")[0].split(":")[0]
            self._suffixes = (".mongodb.net",) if first.endswith(".mongodb.net") else ()
            self._hosts.update(h.split(":")[0] for h in hosts.split(","))

        connect, connect_ex, lookup = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
        guard = self

        def allowed(address) -> bool:
            return isinstance(address, tuple) and (str(address[0]) in guard._ips or address[1] == 53)

        def guarded_connect(sock, address):
            if not isinstance(address, tuple) or allowed(address):
                return connect(sock, address)
            guard.blocked.append(f"connect {address}")
            raise ConnectionRefusedError(f"AUTOMATION: blocked connection to {address}")

        def guarded_connect_ex(sock, address):
            if not isinstance(address, tuple) or allowed(address):
                return connect_ex(sock, address)
            guard.blocked.append(f"connect_ex {address}")
            return 111

        def guarded_lookup(host, *args, **kwargs):
            name = bytes(host).decode("ascii") if isinstance(host, (bytes, bytearray)) else host
            if name is None or str(name) in guard._ips:
                return lookup(host, *args, **kwargs)
            if str(name) in guard._hosts or (guard._suffixes and str(name).endswith(guard._suffixes)):
                answers = lookup(host, *args, **kwargs)
                guard._ips.update(str(a[4][0]) for a in answers)
                return answers
            guard.blocked.append(f"dns {name}")
            raise socket.gaierror(f"AUTOMATION: blocked DNS lookup of {name}")

        socket.socket.connect = guarded_connect
        socket.socket.connect_ex = guarded_connect_ex
        socket.getaddrinfo = guarded_lookup
        if sys.platform == "win32":
            # The proactor loop's ConnectEx would bypass socket.socket.connect.
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
