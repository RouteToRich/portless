"""Production readiness gate (PRODUCTION_TRACKER P1-1).

The pipeline degrades gracefully everywhere — stub connectors, log-only
e-mail, in-memory stores, an in-process bus — which is exactly right for
dev/CI and exactly wrong for real money. This module turns every one of those
silent fallbacks into a named check:

  GET /health/readiness  → {profile, ready, checks: [{id, category, label,
                            status: ok|warn|fail, detail}]}

and, when ``ENV_PROFILE=production`` (``READINESS_ENFORCE`` defaults to true
there), refuses to let the monolith boot while any check FAILS. In ``dev`` /
``staging`` the same report is informational (the System Health page shows
it) so nobody is surprised at go-live.

Severity rule: a check is ``fail`` when running without it would let the
system *pretend* a money / legal step happened (fake escrow, unsigned money
webhook, placeholder entity on the PI); ``warn`` when the system still does
the right thing, just manually or more slowly (no Apify token → no buyer
identification, operator types the parties in).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROFILES = ("dev", "staging", "production")


def profile() -> str:
    raw = os.getenv("ENV_PROFILE", "dev").strip().lower()
    return raw if raw in PROFILES else "dev"


def enforce() -> bool:
    raw = os.getenv("READINESS_ENFORCE", "").strip().lower()
    if raw:
        return raw not in ("0", "false", "no", "off")
    return profile() == "production"


def _set(*names: str) -> bool:
    return all(bool(os.getenv(n, "").strip()) for n in names)


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _check(id_: str, category: str, label: str, ok: bool, *, warn_only: bool = False,
           ok_detail: str = "", fail_detail: str = "") -> dict[str, Any]:
    status = "ok" if ok else ("warn" if warn_only else "fail")
    return {"id": id_, "category": category, "label": label, "status": status,
            "detail": ok_detail if ok else fail_detail}


def evaluate() -> dict[str, Any]:
    """Compute the readiness report (pure: environment + settings only)."""
    from shared.config import settings

    checks: list[dict[str, Any]] = []
    prod = profile() == "production"

    # ── Persistence ────────────────────────────────────────────────────────
    checks.append(_check(
        "mongo", "persistence", "MongoDB configured", bool(settings.mongo_uri),
        ok_detail=f"database {settings.mongo_db_name}",
        fail_detail="MONGO_URI unset — every service runs on an in-memory store (data lost on restart)",
    ))

    # ── Event bus ──────────────────────────────────────────────────────────
    creds = os.getenv("NATS_CREDS_FILE", "").strip()
    creds_present = bool(creds and Path(creds).is_file()) or any(
        Path(__file__).resolve().parent.parent.glob("*.creds")
    )
    backend = os.getenv("EVENT_BUS_BACKEND", "auto").strip().lower()
    durable = backend == "nats" or (backend == "auto" and creds_present)
    checks.append(_check(
        "event_bus", "bus", "Durable event bus (NATS JetStream)", durable,
        warn_only=not prod,
        ok_detail="NATS credentials present — continuations replay after a restart",
        fail_detail="in-process bus — an event published during a crash is lost (deal watcher catches the stall later)",
    ))

    # ── Money ──────────────────────────────────────────────────────────────
    escrow_live = _set("TAZAPAY_API_KEY", "TAZAPAY_API_SECRET") or _set("ESCROW_COM_API_KEY", "ESCROW_COM_EMAIL")
    checks.append(_check(
        "escrow_provider", "money", "Neutral escrow provider (Tazapay / Escrow.com)", escrow_live,
        ok_detail="escrow opens on the provider; checkout links are real",
        fail_detail="no escrow credentials — create_escrow returns a stub and the funding link in the buyer e-mail is fake",
    ))
    secrets_needed = ["TAZAPAY_WEBHOOK_SECRET", "ESCROW_COM_WEBHOOK_SECRET", "STRIPE_WEBHOOK_SECRET",
                      "PAYONEER_WEBHOOK_SECRET"]
    money_secrets = [n for n in secrets_needed if _set(n)]
    checks.append(_check(
        "money_webhook_secrets", "money", "Money webhook signatures enforced",
        len(money_secrets) >= 2 and (_set("TAZAPAY_WEBHOOK_SECRET") or _set("ESCROW_COM_WEBHOOK_SECRET")),
        ok_detail=f"secrets set: {', '.join(money_secrets)}",
        fail_detail="escrow-funded / payout webhooks are accepted unsigned when their *_WEBHOOK_SECRET is unset",
    ))
    checks.append(_check(
        "payout_rail", "money", "Supplier payout rail (Payoneer)", _set("PAYONEER_CLIENT_ID", "PAYONEER_CLIENT_SECRET"),
        ok_detail="Payoneer credentials present",
        fail_detail="PAYONEER_CLIENT_ID unset — supplier payouts are stubbed (no money moves)",
    ))
    ceiling = os.getenv("AUTONOMY_MONEY_CEILING", "L1").strip().upper() or "L1"
    checks.append(_check(
        "money_ceiling", "money", "Money autonomy ceiling", ceiling in ("L0", "L1"),
        warn_only=True,
        ok_detail=f"AUTONOMY_MONEY_CEILING={ceiling} — a human confirms every tranche release and payout",
        fail_detail=f"AUTONOMY_MONEY_CEILING={ceiling} — money moves without a human; only raise after the ledger + four-eyes controls exist",
    ))
    try:
        from shared.orchestration.autonomy_policy import money_auto_approve_below_usd
        threshold = money_auto_approve_below_usd()
    except Exception:  # noqa: BLE001
        threshold = None
    four_eyes = os.getenv("FOUR_EYES_THRESHOLD_USD", "").strip()
    checks.append(_check(
        "money_threshold", "money", "Unattended money threshold", threshold is None or bool(four_eyes),
        warn_only=True,
        ok_detail=("no value threshold — every money gate waits for a human"
                   if threshold is None else
                   f"tranche releases / payouts up to {threshold:,.0f} USD auto-approve; four-eyes above {four_eyes} USD"),
        fail_detail=f"money gates up to {threshold or 0:,.0f} USD auto-approve while FOUR_EYES_THRESHOLD_USD is unset — set it so larger amounts need two approvers",
    ))

    # ── Counterparty identification (PortLess Apify actors) ────────────────
    try:
        from shared.connectors.apify import describe_actors
        apify = describe_actors()
    except Exception as exc:  # noqa: BLE001
        apify = {"token_present": False, "actors": [], "owner": "?", "error": str(exc)}
    checks.append(_check(
        "apify_token", "data", "Apify actors (buyer/seller identification + screening)",
        bool(apify.get("token_present")), warn_only=not prod,
        ok_detail=f"{len(apify.get('actors', []))} PortLess actors reachable as {apify.get('owner')}",
        fail_detail="APIFY_TOKEN unset — no named buyers / supplier contacts / sanctions screening; parties stay unchecked",
    ))
    tracking = next((a for a in apify.get("actors", []) if a.get("connector") == "apify_carrier_tracking"), {})
    checks.append(_check(
        "carrier_tracking", "operations", "Carrier tracking (delivery → acceptance window)",
        bool(apify.get("token_present")) and bool(tracking), warn_only=True,
        ok_detail=f"{tracking.get('actor', 'container-tracking-ai')} polled every TRACKING_POLL_HOURS; DELIVERED opens the buyer's window",
        fail_detail=(f"no carrier-tracking actor — the acceptance window opens by operator entry or "
                     f"{os.getenv('INSPECTION_WINDOW_FALLBACK_DAYS', '21')} days after clearance (INSPECTION_WINDOW_FALLBACK_DAYS)"),
    ))
    cap = os.getenv("APIFY_MAX_RUNS_PER_DAY", "").strip()
    checks.append(_check(
        "apify_run_cap", "data", "Apify daily run budget", bool(cap) and cap != "0", warn_only=True,
        ok_detail=f"APIFY_MAX_RUNS_PER_DAY={cap} — scans, enrichment and tracking stop at the cap",
        fail_detail="APIFY_MAX_RUNS_PER_DAY unset — nothing caps actor spend if a schedule misfires",
    ))
    try:
        from shared import catalogue as _cat
        lines = _cat.active_lines()
    except Exception:  # noqa: BLE001
        lines = []
    checks.append(_check(
        "catalogue", "data", "Product catalogue / corridors", bool(lines), warn_only=True,
        ok_detail=f"{len(lines)} product line(s), {len(_cat.corridors()) if lines else 0} corridor(s) drive the scanner",
        fail_detail="catalogue empty — the scanner runs the built-in sample HS codes / markets (PUT /trade-min/catalogue)",
    ))
    checks.append(_check(
        "trade_data", "data", "Live demand data (Comtrade / Census)",
        _set("COMTRADE_SUBSCRIPTION_KEY") or _set("CENSUS_API_KEY") or bool(apify.get("token_present")),
        ok_detail="at least one live demand source configured",
        fail_detail="no Comtrade / Census key and no Apify token — opportunities fall back to simulated demand numbers",
    ))
    checks.append(_check(
        "mock_mode_off", "data", "Trade-Min mock mode off", not _flag("TRADE_MIN_MOCK_MODE"),
        ok_detail="scanner uses live connectors",
        fail_detail="TRADE_MIN_MOCK_MODE=true — every opportunity is canned scenario data",
    ))
    checks.append(_check(
        "auto_flow_off", "automation", "Dev auto-flow off", not _flag("AUTO_FLOW_ENABLED"),
        ok_detail="production legs run on the event bus only",
        fail_detail="AUTO_FLOW_ENABLED=true — the dev poller fakes buyer actions (PI acceptance, escrow funding)",
    ))

    # ── E-mail ─────────────────────────────────────────────────────────────
    smtp = _set("GMAIL_SENDER_ADDRESS", "GMAIL_APP_PASSWORD")
    imap = _set("IMAP_USER", "IMAP_PASSWORD")
    checks.append(_check(
        "smtp", "email", "Outbound e-mail configured", smtp,
        ok_detail=f"sending as {os.getenv('GMAIL_SENDER_ADDRESS', '')}",
        fail_detail="GMAIL_SENDER_ADDRESS/GMAIL_APP_PASSWORD unset — every PI, PO and negotiation e-mail is log-only",
    ))
    checks.append(_check(
        "imap", "email", "Inbound e-mail polling configured", imap, warn_only=not prod,
        ok_detail=f"polling {os.getenv('IMAP_USER', '')}",
        fail_detail="IMAP_USER unset — counterparty replies must be pasted in by hand",
    ))
    domain = os.getenv("PORTLESS_EMAIL_DOMAIN", "").strip()
    sender = os.getenv("GMAIL_SENDER_ADDRESS", "").strip()
    own_domain = bool(domain and sender and sender.lower().endswith("@" + domain.lower()))
    checks.append(_check(
        "email_domain", "email", "Own sending domain", own_domain, warn_only=True,
        ok_detail=f"sending from the PortLess domain {domain}",
        fail_detail="sender is not on PORTLESS_EMAIL_DOMAIN — buyers see a personal mailbox (deliverability / trust)",
    ))

    # ── Documents / entity ─────────────────────────────────────────────────
    checks.append(_check(
        "document_store", "documents", "Google Drive document store",
        _set("GOOGLE_DRIVE_CLIENT_ID", "GOOGLE_DRIVE_CLIENT_SECRET", "GOOGLE_DRIVE_REFRESH_TOKEN"),
        warn_only=not prod,
        ok_detail="PI / PO / document pack upload to Drive",
        fail_detail="GOOGLE_DRIVE_* unset — documents land on local disk (lost with the container)",
    ))
    try:
        from services.dealmaker.config import settings as dm  # type: ignore[import]
        iec = (getattr(dm, "portless_entity_iec", "") or "").strip()
        gstin = (getattr(dm, "portless_entity_gstin", "") or "").strip()
        entity_ok = bool(iec and gstin)
        entity_detail = f"IEC {iec[:4]}… / GSTIN {gstin[:4]}…" if entity_ok else "DEALMAKER_PORTLESS_ENTITY_IEC / _GSTIN unset — the PI/PO carry a placeholder exporter identity"
    except Exception:  # noqa: BLE001
        entity_ok, entity_detail = False, "dealmaker settings unavailable"
    checks.append(_check(
        "exporter_identity", "documents", "Exporter identity on PI/PO (IEC + GSTIN)", entity_ok,
        ok_detail=entity_detail, fail_detail=entity_detail,
    ))

    # ── Operations partners ────────────────────────────────────────────────
    qc_live = any(_set(f"{p}_API_KEY") for p in ("SGS", "QIMA", "BUREAU_VERITAS", "INTERTEK", "TUV_SUD"))
    checks.append(_check(
        "qc_provider", "operations", "QC inspection provider", qc_live, warn_only=not prod,
        ok_detail="at least one inspector API configured",
        fail_detail="no inspector API key — QC bookings are stubs; the deal waits at SUPPLIER_DISPATCH_READY for a manual result",
    ))
    checks.append(_check(
        "freight_provider", "operations", "Freight forwarder", _set("COGOPORT_API_KEY") or _set("FREIGHTOS_API_KEY"),
        warn_only=not prod,
        ok_detail="freight booking API configured",
        fail_detail="no Cogoport / Freightos key — bookings are stubs and no ARRIVED milestone ever arrives",
    ))
    checks.append(_check(
        "customs", "operations", "Customs filing (ICEGATE)", _set("ICEGATE_API_KEY"), warn_only=not prod,
        ok_detail="ICEGATE key present",
        fail_detail="ICEGATE_API_KEY unset — the Shipping Bill / LEO is a stub",
    ))

    # ── LLM ────────────────────────────────────────────────────────────────
    llm = _set("LLM_GROQ_API_KEY") or _set("LLM_PRIMARY_API_KEY")
    checks.append(_check(
        "llm", "llm", "LLM gateway key", llm, warn_only=not prod,
        ok_detail="negotiation drafts + exception triage use a live model",
        fail_detail="no LLM key — negotiation e-mails are degraded templates (always L0) and the resolver escalates everything",
    ))

    failures = [c for c in checks if c["status"] == "fail"]
    warnings = [c for c in checks if c["status"] == "warn"]
    return {
        "profile": profile(),
        "enforced": enforce(),
        "ready": not failures,
        "fail_count": len(failures),
        "warn_count": len(warnings),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }


class NotProductionReady(RuntimeError):
    """Raised at startup when ``ENV_PROFILE=production`` and a check fails."""


def assert_ready_or_log() -> dict[str, Any]:
    """Startup hook: log the report; raise in an enforced profile when not ready."""
    report = evaluate()
    for c in report["checks"]:
        if c["status"] == "fail":
            logger.error("Readiness[%s] FAIL — %s: %s", report["profile"], c["label"], c["detail"])
        elif c["status"] == "warn":
            logger.warning("Readiness[%s] warn — %s: %s", report["profile"], c["label"], c["detail"])
    if report["ready"]:
        logger.info("Readiness[%s]: ready (%d warning(s))", report["profile"], report["warn_count"])
    elif report["enforced"]:
        raise NotProductionReady(
            f"{report['fail_count']} readiness check(s) failed in profile {report['profile']} — "
            "see the log above; set READINESS_ENFORCE=false to boot anyway (not for real money)"
        )
    else:
        logger.warning(
            "Readiness[%s]: NOT production-ready — %d failing check(s); the pipeline runs on "
            "stubs/fallbacks (GET /health/readiness)", report["profile"], report["fail_count"],
        )
    return report
