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
    """Every variable holds a real value — a ``.env`` placeholder (``your_..._here``) counts as unset."""
    from shared.integration_mode import configured
    return configured(*names)


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
    from shared.connectors.escrow import default_provider, display_name, enabled_providers
    escrow_creds = {"tazapay": ("TAZAPAY_API_KEY", "TAZAPAY_API_SECRET"),
                    "escrow_com": ("ESCROW_COM_API_KEY", "ESCROW_COM_EMAIL")}
    offered = enabled_providers()
    missing = [display_name(p) for p in offered if not _set(*escrow_creds[p])]
    checks.append(_check(
        "escrow_provider", "money", "Neutral escrow providers the buyer can choose", not missing,
        ok_detail=f"credentials set for {', '.join(display_name(p) for p in offered)} (default {display_name(default_provider())})",
        fail_detail=(f"no credentials for {', '.join(missing)} — a buyer who chooses it gets an escrow the provider "
                     "refuses; set the keys or drop it from ESCROW_PROVIDERS_ENABLED"),
    ))
    checks.append(_check(
        "escrow_proof_links", "money", "Release documents reach the escrow provider",
        _set("PORTLESS_PUBLIC_BASE_URL", "ESCROW_PROOF_LINK_SECRET"), warn_only=True,
        ok_detail="each stage's signed proof sheet is sent to the provider with the release request",
        fail_detail=("PORTLESS_PUBLIC_BASE_URL / ESCROW_PROOF_LINK_SECRET unset — only proofs that are URLs "
                     "(QC report, tracking page) reach Tazapay; it may ask for the documents by e-mail"),
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
    try:
        from shared.connectors.apify import last_tracking_outcome
        last_tracking = last_tracking_outcome()
    except Exception:  # noqa: BLE001
        last_tracking = {}
    refused = last_tracking.get("status") == "source_refused"
    fallback = (f"the acceptance window opens by operator entry or "
                f"{os.getenv('INSPECTION_WINDOW_FALLBACK_DAYS', '21')} days after clearance (INSPECTION_WINDOW_FALLBACK_DAYS)")
    checks.append(_check(
        "carrier_tracking", "operations", "Carrier tracking (delivery → acceptance window)",
        bool(apify.get("token_present")) and bool(tracking) and not refused, warn_only=True,
        ok_detail=f"{tracking.get('actor', 'multi-carrier-tracking-ai')} polled every TRACKING_POLL_HOURS; DELIVERED opens the buyer's window",
        fail_detail=(f"last tracking poll refused: {last_tracking.get('detail')} — {fallback}" if refused
                     else f"no carrier-tracking actor — {fallback}"),
    ))
    cap = os.getenv("APIFY_MAX_RUNS_PER_DAY", "").strip()
    checks.append(_check(
        "apify_run_cap", "data", "Apify daily run budget", bool(cap) and cap != "0", warn_only=True,
        ok_detail=f"APIFY_MAX_RUNS_PER_DAY={cap} — scans, enrichment and tracking stop at the cap",
        fail_detail="APIFY_MAX_RUNS_PER_DAY unset — nothing caps actor spend if a schedule misfires",
    ))
    try:
        from services.trade_min.config import settings as _tm
        validation_on = bool(_tm.trade_min_validation_enabled)
    except Exception:  # noqa: BLE001
        validation_on = False
    screening_live = bool(apify.get("token_present"))
    checks.append(_check(
        "counterparty_validation", "data", "Scan-time buyer / supplier check (radar \"Verified only\")",
        validation_on and screening_live, warn_only=not prod,
        ok_detail="top buyers / suppliers of every opportunity are screened before the radar shows them",
        fail_detail=("TRADE_MIN_VALIDATION_ENABLED=false — " if not validation_on else
                     "APIFY_TOKEN unset, the screening actor runs as a stub — ")
                    + "no counterparty is ever verified: the radar's \"Verified only\" view stays empty "
                      "and every counterparty reaches outreach unscreened",
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
        fail_detail="no Comtrade / Census key and no Apify token — scans find no demand data, so no opportunities",
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
    qima_live = (_set("QIMA_ACCOUNT") and (_set("QIMA_API_ACCESS_TOKEN") or _set("QIMA_API_KEY"))
                 and (_set("QIMA_PASSWORD") or _set("QIMA_PASSWORD_MD5")))
    portal_desks = [p for p in ("SGS", "BUREAU_VERITAS") if _set(f"{p}_BOOKING_EMAIL")]
    generic = [p for p in ("INTERTEK", "TUV_SUD") if _set(f"{p}_API_KEY")]
    qc_live = qima_live or bool(portal_desks) or bool(generic)
    qc_detail = ", ".join(filter(None, ["QIMA API" if qima_live else "",
                                        *[f"{p} booking desk" for p in portal_desks], *[f"{p} API" for p in generic]]))
    checks.append(_check(
        "qc_provider", "operations", "QC inspection provider", qc_live, warn_only=not prod,
        ok_detail=f"inspection bookings reach: {qc_detail}",
        fail_detail="no inspector configured (QIMA_API_ACCESS_TOKEN + QIMA_ACCOUNT + QIMA_PASSWORD, or "
                    "SGS_BOOKING_EMAIL / BUREAU_VERITAS_BOOKING_EMAIL) — QC bookings are stubs or wait for an operator",
    ))
    from shared import integration_mode
    checks.append(_check(
        "integration_mode", "operations", "QC, freight and escrow call the real providers", integration_mode.live(),
        warn_only=not prod,
        ok_detail="INTEGRATION_MODE=live — every QC / freight / customs / escrow / payout call goes to the provider",
        fail_detail="INTEGRATION_MODE=sandbox — QC, freight, customs, escrow and payouts answer from stubs (automation only)",
    ))
    sandbox = os.getenv("QC_SANDBOX_PROVIDERS", "").strip() if integration_mode.sandbox() else ""
    checks.append(_check(
        "qc_sandbox", "operations", "QC providers are real (no sandbox)", not sandbox, warn_only=not prod,
        ok_detail="no QC provider on the simulator",
        fail_detail=f"QC_SANDBOX_PROVIDERS={sandbox} — those inspection bookings go to the simulator, not the provider",
    ))
    escrow_sandbox = os.getenv("ESCROW_SANDBOX_PROVIDERS", "").strip() if integration_mode.sandbox() else ""
    checks.append(_check(
        "escrow_sandbox", "operations", "Escrow providers are real (no sandbox)", not escrow_sandbox, warn_only=not prod,
        ok_detail="no escrow provider on the simulator",
        fail_detail=f"ESCROW_SANDBOX_PROVIDERS={escrow_sandbox} — those escrows open on the simulator, not the provider",
    ))
    booking_provider = (os.getenv("FREIGHT_PROVIDER", "") or "maersk").strip().lower()
    if booking_provider == "maersk":
        maersk_ok = _set("MAERSK_CONSUMER_KEY", "MAERSK_CLIENT_SECRET", "MAERSK_CARRIER_SERVICE_CODE")
        checks.append(_check(
            "freight_provider", "operations", "Carrier booking (Maersk)", maersk_ok, warn_only=not prod,
            ok_detail="Maersk consumer key, secret and service code set — booking, tracking, VGM and B/L run on Maersk",
            fail_detail="MAERSK_CONSUMER_KEY / MAERSK_CLIENT_SECRET / MAERSK_CARRIER_SERVICE_CODE unset — Maersk refuses "
                        "the booking and no container milestone arrives",
        ))
    else:
        checks.append(_check(
            "freight_provider", "operations", "Freight forwarder", _set("COGOPORT_API_KEY") or _set("FREIGHTOS_API_KEY"),
            warn_only=not prod,
            ok_detail="freight booking API configured",
            fail_detail="no Cogoport / Freightos key — bookings are stubs and no ARRIVED milestone ever arrives",
        ))
    carrier_sandbox = os.getenv("CARRIER_SANDBOX_PROVIDERS", "").strip() if integration_mode.sandbox() else ""
    checks.append(_check(
        "carrier_sandbox", "operations", "Carrier is real (no sandbox)", not carrier_sandbox, warn_only=not prod,
        ok_detail="no carrier on the simulator",
        fail_detail=f"CARRIER_SANDBOX_PROVIDERS={carrier_sandbox} — bookings go to the simulator, not the carrier",
    ))
    from shared.orchestration import inspection_plan
    peo_offered = "PEO" in inspection_plan.default_types() or os.getenv("QC_OFFER_PEO", "true").lower() in ("1", "true", "yes")
    checks.append(_check(
        "qc_peo_service_type", "operations", "QIMA can book a pre-customs inspection (PEO)",
        (not peo_offered) or _set("QIMA_SERVICE_TYPE_PEO"), warn_only=True,
        ok_detail="QIMA_SERVICE_TYPE_PEO set (or PEO not offered)",
        fail_detail="QIMA documents no booking id for PEO — set QIMA_SERVICE_TYPE_PEO to the id QIMA gives the account, "
                    "or a buyer's PEO request is refused at booking",
    ))
    checks.append(_check(
        "customs", "operations", "Customs filing (ICEGATE)", _set("ICEGATE_API_KEY"), warn_only=not prod,
        ok_detail="ICEGATE key present",
        fail_detail="ICEGATE_API_KEY unset — the Shipping Bill / LEO is a stub",
    ))

    # ── LLM ────────────────────────────────────────────────────────────────
    # Every LLM call goes to the shared LLM Gateway (shared/llm_gateway/remote.py):
    # it needs the URL and PortLess's service-account credential, nothing else.
    llm = _set("LLM_GATEWAY_URL") and (
        _set("LLM_GATEWAY_TOKEN") or _set("LLM_GATEWAY_EMAIL", "LLM_GATEWAY_PASSWORD")
    )
    checks.append(_check(
        "llm", "llm", "LLM gateway", llm, warn_only=not prod,
        ok_detail="negotiation drafts and exception triage go through the shared LLM Gateway",
        fail_detail="LLM_GATEWAY_URL / service-account credential unset — negotiation e-mails are degraded templates (always L0) and the resolver escalates everything",
    ))

    # ── Sign-in (auth-service via the shared API gateway) ─────────────────
    # shared/auth/identity.py checks every token with the gateway's /auth/me.
    checks.append(_check(
        "auth_gateway", "auth", "Sign-in gateway", _set("AUTH_GATEWAY_URL") or _set("LLM_GATEWAY_URL"),
        ok_detail="tokens are checked with auth-service through the RouteToRich API gateway",
        fail_detail="AUTH_GATEWAY_URL / LLM_GATEWAY_URL unset — no token can be checked, every request is refused",
    ))
    checks.append(_check(
        "auth_account", "auth", "PortLess app account", _set("PORTLESS_ACCOUNT_ID") or _set("LLM_GATEWAY_ACCOUNT_ID"),
        warn_only=not prod,
        ok_detail="only tokens scoped to PortLess's app account are accepted",
        fail_detail="PORTLESS_ACCOUNT_ID unset — a sign-in to ANY RouteToRich application (e.g. MakeMeRich) is accepted here",
    ))
    checks.append(_check(
        "auth_header_trust", "auth", "Token check on every request", not _flag("AUTH_TRUST_GATEWAY_HEADERS"),
        warn_only=True,
        ok_detail="each token is checked with auth-service (cached 60 s) — forged identity headers are ignored",
        fail_detail="AUTH_TRUST_GATEWAY_HEADERS=true — X-User-* headers are believed; only safe while nothing but the gateway can reach this service",
    ))

    # ── Trade compliance (shared/orchestration/compliance.py) ─────────────
    checks.append(_check(
        "compliance_trade", "compliance", "Trade compliance check", _flag("COMPLIANCE_ENABLED", "true"),
        ok_detail="HS export policy, destination, parties and prices are checked before outreach and the PI",
        fail_detail="COMPLIANCE_ENABLED=false — prohibited / SCOMET products and embargoed destinations are not stopped",
    ))
    checks.append(_check(
        "compliance_loading", "compliance", "Supervised loading + seal",
        _flag("COMPLIANCE_LOADING_CHECK_REQUIRED", "true"),
        ok_detail="freight and the QC tranche wait for the inspector's container + seal record",
        fail_detail="COMPLIANCE_LOADING_CHECK_REQUIRED=false — nothing proves the container holds only the inspected goods",
    ))
    checks.append(_check(
        "compliance_unscreened", "compliance", "Unscreened parties stop the PI",
        (os.getenv("COMPLIANCE_UNCHECKED_SCREENING") or "review").strip().lower() in ("review", "block"),
        ok_detail="a party whose sanctions screening never ran needs an operator before the PI",
        fail_detail="COMPLIANCE_UNCHECKED_SCREENING=allow — a PI can go to a never-screened buyer",
    ))
    from shared.documents import issuer
    missing_export = issuer.missing(("address", "iec", "gstin", "lut_arn", "ad_code"))
    checks.append(_check(
        "documents_exporter", "documents", "Exporter details on trade documents", not missing_export,
        ok_detail="PI, invoice, shipping bill and SDF carry PortLess's address, IEC, GSTIN, LUT and AD code",
        fail_detail="missing " + ", ".join(missing_export) + " — those documents are issued as DRAFT",
    ))
    checks.append(_check(
        "documents_contract_law", "documents", "Governing law on the sales contract", _set("PORTLESS_CONTRACT_LAW"),
        warn_only=True,
        ok_detail="the sales contract states the governing law and dispute forum",
        fail_detail="PORTLESS_CONTRACT_LAW is not set — the sales contract is issued as DRAFT",
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
