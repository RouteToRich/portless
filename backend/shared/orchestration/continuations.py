"""Event-driven pipeline continuations.

Subscribes handlers to the event bus so a deal state change itself triggers
the next pipeline step — instead of depending on a long-lived poller task
(the dev auto-flow) that can die and leave the deal parked.

  OPPORTUNITY_DETECTED → create the deal (opt-in: AUTO_DEAL_FROM_OPPORTUNITY_ENABLED)
  DEAL_CREATED (MATCHING) → identify + screen the counterparties on the PortLess
                        Apify actors (contact finder, e-mail check, sanctions)
                        → buyer outreach funnel (SEND_BUYER_OUTREACH introductions
                          to the identified buyers; shared/orchestration/outreach.py)
                        → DISPATCH_RFQ gate → RFQs to the shortlisted suppliers
  QUOTE_RECEIVED      → if the Match Engine did not auto-SOURCE the quote:
                        CONFIRM_SUPPLIER_SELECTION gate (operator → NEGOTIATING)
  NEGOTIATION_STARTED → start the negotiation session (round 0 draft, policy-gated);
                        waits while the buyer funnel is still open (no counter-price
                        to a buyer who has not said they are interested)
  NEGOTIATION_FAILED  → deal.exception + propose the next-best supplier
                        (CONFIRM_SUPPLIER_SELECTION → re-match)
  NEGOTIATION_AGREED  → sanctions screening of the buyer (fresh ≤ 30 days):
                        clear → issue PI → deal PI_SENT · review →
                        CONFIRM_COUNTERPARTY_RISK gate · block → deal.exception
  PI_SENT             → email the PI to the buyer   (SEND_DEAL_NOTIFICATION)
  PI_ACCEPTED         → open neutral escrow          → deal ESCROW_PENDING
                        + email funding instructions to the buyer
  ESCROW_FUNDED       → create ISSUE_PO HITL gate    (operator approves → PO)
  PO_ISSUED           → book QC (buyer-opted agency) → SUPPLIER_DISPATCH_READY
                        + email the PO to the supplier
                        + RELEASE_ESCROW_TRANCHE gate (T1, PO_ISSUED trigger)
  QC_BOOKED           → email the inspection booking to the supplier
  QC_PASSED           → generate CI / PL / CoO / SDF / Shipping Bill (DocuGen)
                        + RELEASE_ESCROW_TRANCHE gate (T2, QC_PASSED trigger)
                        + create BOOK_FREIGHT HITL gate
  QC_FAILED           → deal → DISPUTE + deal.exception (Exception Resolver)
  FREIGHT_BOOKED      → FILE_CUSTOMS gate (CHA / ICEGATE) + email shipment to buyer
  FREIGHT_MILESTONE   → DELIVERED (carrier webhook / tracking actor / operator):
                        open the buyer's acceptance window on the escrow
  ESCROW_INSPECTION_OPENED → tell the buyer the acceptance window is open (T4)
  CUSTOMS_CLEARED     → RELEASE_ESCROW_TRANCHE gate (T3, CLEARED trigger)
                        + email arrival to buyer
                        + RELEASE_SUPPLIER_PAYOUT gate — amount capped at the
                          funds already released from escrow (never pay out
                          money PortLess has not received)
  ESCROW_TRANCHE_RELEASED → re-gate the next due tranche, top up the supplier
                        payout (and, in legacy "proof" window mode, tell the
                        buyer the acceptance window is open)
  ESCROW_SETTLED      → final supplier balance payout gate + settlement notices
  PAYOUT_COMPLETED    → partial: supplier balance gate · fully paid (PAID):
                        completion emails to both parties
  DEAL_EXCEPTION      → route to the Exception Resolver agent (loop-safe:
                        resolver lifecycle echoes carry an exception_id and
                        are skipped)

Every HITL gate is created from ``resolve_gate`` of the per-action autonomy
policy (``shared/orchestration/autonomy_policy.py``): level, L1 timeout +
on-timeout default, and a value threshold that only applies when this
module's preconditions for the leg hold (``conditions_ok``). L0/L1 wait for an
operator, L2/L3 auto-approve and run the callback — that policy, not this
module, decides how autonomous the pipeline is. Money-moving gates (tranche
release, supplier payout) are additionally capped by ``AUTONOMY_MONEY_CEILING``
and by the four-eyes threshold in the HITL service.
Buyer actions (accepting the PI, funding the escrow) remain buyer actions —
no handler fakes them; the counterparty notifications exist so the buyer
actually knows what to do next.

Design rules every handler must follow:

  • Idempotent — safe to fire twice (idempotency key + deal-state validation
    inside the target service make the second call a no-op / clean rejection;
    the same topic can legitimately arrive from both the origin service and
    the Deals status-transition emitter).
  • Never raise for business no-ops (deal already advanced, feature disabled).
  • On genuine failure: log loudly and emit ``deal.exception`` so the failure
    is visible in the UI instead of only in a console.
  • Secondary effects (notifications, document generation, tranche gates) are
    best-effort and never block the primary step of the leg.

Enabled by default; set ``EVENT_CONTINUATION_ENABLED=false`` to fall back to
purely manual/auto-flow-driven progression.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Awaitable

from shared.domain.events import DomainEvent, EventTopic

logger = logging.getLogger(__name__)

_DEFAULT_QUANTITY_MT = 25.0
_DEFAULT_PAYMENT_TERMS = "30% advance + 70% on BL"
_RFQ_MAX_SUPPLIERS = 3


def _enabled() -> bool:
    return os.getenv("EVENT_CONTINUATION_ENABLED", "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _enrichment_enabled() -> bool:
    """Counterparty identification + screening on the PortLess Apify actors."""
    return os.getenv("ENRICHMENT_ON_DEAL_CREATED", "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


# Pipeline order used to decide whether a tranche trigger has already been reached.
_STATUS_ORDER = (
    "DRAFT", "MATCHING", "NEGOTIATING", "NEGOTIATION_AGREED", "PI_SENT", "PI_ACCEPTED",
    "ESCROW_PENDING", "ESCROW_FUNDED", "PO_ISSUED", "SUPPLIER_DISPATCH_READY", "QC_PASSED",
    "IN_TRANSIT", "CLEARED", "PAID",
)


def _status_reached(status: str, trigger: str) -> bool:
    try:
        return _STATUS_ORDER.index(status) >= _STATUS_ORDER.index(trigger)
    except ValueError:
        return False


def _status_of(deal) -> str:
    return deal.status.value if hasattr(deal.status, "value") else str(deal.status)


async def _get_deal_in_status(deal_id: str | None, required: str, stage: str):
    """Fetch the deal and return it only when it sits in *required* status.

    Returns None (logged, no error) otherwise — either the event is a
    duplicate and someone already advanced the deal, or it predates a manual
    intervention; both are idempotent no-ops.
    """
    if not deal_id:
        logger.error("Continuation[%s]: event without deal_id — skipped", stage)
        return None
    from services.deals.service import get_deal  # type: ignore[import]
    deal = await get_deal(deal_id)
    status = _status_of(deal)
    if status != required:
        logger.info(
            "Continuation[%s]: deal %s is %s, not %s — skipped (idempotent)",
            stage, deal_id, status, required,
        )
        return None
    return deal


async def _negotiation_numbers(deal_id: str, deal=None) -> tuple[float | None, float]:
    """(final supplier ask, quantity_mt) from the deal's negotiation session,
    falling back to the deal's own quantity (RFQ lead) and then the default."""
    fallback_qty = _deal_quantity(deal) if deal is not None else _DEFAULT_QUANTITY_MT
    try:
        from agents.negotiation_agent.repository import get_repository as _neg_repo  # type: ignore[import]
        session = await _neg_repo().get_session_by_deal(deal_id)
        if session is not None:
            return (
                session.current_ask,
                float(session.quantity_mt) if session.quantity_mt else fallback_qty,
            )
    except Exception as exc:
        logger.warning("Continuation: could not read negotiation session for %s: %s", deal_id, exc)
    return None, fallback_qty


def _gate_level(action: str, deal=None) -> str:
    """Autonomy level for a gate from the per-action policy (L0 if unavailable)."""
    try:
        from shared.orchestration.autonomy_policy import resolve_level
        return resolve_level(action, deal)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Continuation: autonomy policy unavailable for %s (%s) — L0", action, exc)
        return "L0"


async def _create_gate(
    deal_id: str, action: str, summary: str,
    financial_impact_usd: float | None,
    callback_payload: dict, idempotency_key: str,
    autonomy_level: str | None = None,
    risk_flags: list[str] | None = None,
    deal=None,
    conditions_ok: bool = True,
) -> None:
    """Create a HITL approval gate (idempotent on *idempotency_key*).

    The level, the L1 timeout and the timeout default come from the autonomy
    policy (``resolve_gate``); money-moving gates at or below the value
    threshold with ``conditions_ok`` are created at L2. ``autonomy_level``
    forces the level (the callers use it for hard L0 cases such as a
    sanctions hit) — the timeout settings still come from the policy.
    """
    from services.hitl_approval.schemas import CreateApprovalRequest  # type: ignore[import]
    from services.hitl_approval.service import create_approval  # type: ignore[import]

    spec = None
    try:
        from shared.orchestration.autonomy_policy import resolve_gate
        spec = resolve_gate(action, deal, amount=financial_impact_usd, conditions_ok=conditions_ok)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Continuation: autonomy policy unavailable for %s (%s) — L0", action, exc)
    level = autonomy_level or (spec.level if spec else "L0")
    flags = list(risk_flags or [])
    if spec and spec.threshold_applied and not autonomy_level:
        flags.append("auto_approved_below_value_threshold")
    await create_approval(CreateApprovalRequest(
        deal_id=deal_id, action=action, summary=summary,
        financial_impact_usd=financial_impact_usd,
        risk_flags=flags,
        autonomy_level=level,
        expires_at=(spec.expires_at_iso() if (spec and level == "L1") else None),
        default_on_timeout=(spec.default_on_timeout if spec else "REJECT"),
        callback_payload=callback_payload,
        idempotency_key=idempotency_key,
    ))


def _deal_quantity(deal) -> float:
    """Shipment quantity for the deal: the buyer's stated quantity (RFQ lead /
    negotiation) when known, else the configured default."""
    qty = getattr(deal, "quantity_mt", None) if deal is not None else None
    try:
        return float(qty) if qty and float(qty) > 0 else _DEFAULT_QUANTITY_MT
    except (TypeError, ValueError):
        return _DEFAULT_QUANTITY_MT


def payment_terms_for(provider: str | None = None) -> str:
    """Payment terms the PI / PO state — derived from the staged escrow schedule,
    so the document never contradicts how the money actually moves."""
    try:
        from services.escrow_guard.service import DEFAULT_TRANCHES  # type: ignore[import]
        from services.escrow_guard.config import settings as _eg  # type: ignore[import]
        labels = {
            "PO_ISSUED": "on Purchase Order", "QC_PASSED": "on inspection pass",
            "CLEARED": "on customs clearance / arrival", "ACCEPTED": "on delivery acceptance",
        }
        parts = [f"{int(round(pct * 100))}% {labels.get(trg, trg)}" for _, trg, pct in DEFAULT_TRANCHES]
        name = (provider or _eg.escrow_default_provider or "escrow").replace("_", ".").title()
        return f"100% into neutral escrow ({name}) before production; released {', '.join(parts)}"
    except Exception:  # noqa: BLE001
        return _DEFAULT_PAYMENT_TERMS


async def _emit_exception(deal_id: str | None, stage: str, exc: Exception) -> None:
    logger.exception("Continuation[%s]: FAILED for deal %s", stage, deal_id)
    await _publish_exception(deal_id, stage, str(exc), {"category": "CALLBACK_FAILED"})


async def _publish_exception(
    deal_id: str | None, stage: str, error: str, extra: dict[str, Any] | None = None,
) -> None:
    try:
        from shared.bus import get_publisher
        await get_publisher().publish(DomainEvent(
            topic=EventTopic.DEAL_EXCEPTION,
            deal_id=deal_id,
            payload={
                "stage": stage,
                "error": error,
                "action_required": "manual_retry_or_intervention",
                **(extra or {}),
            },
        ))
    except Exception:
        logger.exception("Continuation[%s]: could not emit deal.exception for %s", stage, deal_id)


async def _best_effort(stage: str, deal_id: str | None, coro: Awaitable[Any]) -> Any:
    """Run a secondary effect; log (never raise) on failure."""
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001
        logger.error("Continuation[%s]: secondary step failed for deal %s: %s", stage, deal_id, exc)
        return None


# Buyer CIF prices typically sit ~25% above the supplier's FOB ask; when the
# deal carries a supplier ask but no buyer target (thin/mocked seeds never
# populate buyer_target_price_usd), derive one so the negotiation has a corridor
# to work in. Mirrors auto_flow._ensure_match_results' 1.25× heuristic.
_BUYER_MARKUP_OVER_FOB = 1.25


def _winning_match(deal):
    """The deal's winning MatchResult (by winning_supplier_id, else best score)."""
    results = getattr(deal, "match_results", None) or []
    if not results:
        return None
    win_id = getattr(deal, "winning_supplier_id", None)
    if win_id:
        for mr in results:
            if getattr(mr, "supplier_id", None) == win_id:
                return mr
    sourced = [m for m in results if getattr(m, "decision", None) == "SOURCE"]
    pool = sourced or results
    return max(pool, key=lambda m: getattr(m, "composite_score", 0) or 0)


def _party_email(deal, role: str) -> str | None:
    """Best-effort contact email for a deal party (buyer/supplier).

    Party carries no dedicated email field in the shared model; some deployments
    stuff a contact address into an ``email``/``contact_email`` attribute. Returns
    None when absent — the negotiation then falls back to a placeholder address and
    the L0-gated first draft simply waits for an operator to set the recipient.
    """
    for p in getattr(deal, "parties", None) or []:
        raw_role = getattr(p, "role", "")
        p_role = (raw_role.value if hasattr(raw_role, "value") else str(raw_role)).lower()
        if p_role == role:
            return getattr(p, "email", None) or getattr(p, "contact_email", None)
    return None


def _negotiation_seed(deal) -> tuple[float, float, str, str] | None:
    """Derive (buyer_target, supplier_ask, buyer_country, supplier_id) for a deal.

    Returns None when there isn't enough to open a negotiation corridor (no
    supplier price anywhere) — the caller logs and leaves the deal in NEGOTIATING
    for an operator to complete sourcing.
    """
    win = _winning_match(deal)
    supplier_ask = getattr(win, "fob_price_usd", None) if win is not None else None
    buyer_target = getattr(deal, "buyer_target_price_usd", None)

    if supplier_ask is None and buyer_target is None:
        return None
    if supplier_ask is None:
        # Only a buyer target — assume the supplier opens at parity; the agent
        # negotiates the real ask in from the supplier's first reply.
        supplier_ask = buyer_target
    if buyer_target is None:
        buyer_target = round(float(supplier_ask) * _BUYER_MARKUP_OVER_FOB, 2)

    supplier_id = (
        getattr(deal, "winning_supplier_id", None)
        or (getattr(win, "supplier_id", None) if win is not None else None)
        or "UNKNOWN"
    )
    return float(buyer_target), float(supplier_ask), getattr(deal, "market", "") or "", supplier_id


def _auto_flow_owns(deal) -> bool:
    """True when the dev auto-flow drives this (opportunity-seeded) deal itself."""
    if not getattr(deal, "opportunity_id", None):
        return False
    try:
        from shared.orchestration import auto_flow  # type: ignore[import]
        return bool(auto_flow.enabled())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Continuation: could not check auto-flow ownership: %s", exc)
        return False


# ---------------------------------------------------------------------------
# OPPORTUNITY_DETECTED → deal (opt-in)
# ---------------------------------------------------------------------------

async def on_opportunity_detected(event: DomainEvent) -> None:
    """OPPORTUNITY_DETECTED → create the deal automatically (opt-in).

    Guards (all from ``shared.config.settings``):
      • ``auto_deal_from_opportunity_enabled`` must be true (default false);
      • ``arbitrage_rank`` ≥ ``auto_deal_min_arbitrage_rank`` and
        ``confidence_score`` ≥ ``auto_deal_min_confidence``;
      • the opportunity is ACTIVE and its demand data is not ``simulated``;
      • no deal exists for this opportunity, and no non-terminal deal exists
        for the same HS code × market (duplicate-demand guard).
    """
    if not _enabled():
        return
    from shared.config import settings
    if not settings.auto_deal_from_opportunity_enabled:
        return
    payload = event.payload or {}
    opportunity_id = payload.get("opportunity_id")
    if not opportunity_id:
        return
    rank = float(payload.get("arbitrage_rank") or 0.0)
    confidence = float(payload.get("confidence_score") or 0.0)
    if rank < settings.auto_deal_min_arbitrage_rank or confidence < settings.auto_deal_min_confidence:
        logger.info(
            "Continuation[OPPORTUNITY]: %s below thresholds (rank %.1f/%.1f, confidence %.1f/%.1f) — skipped",
            opportunity_id, rank, settings.auto_deal_min_arbitrage_rank,
            confidence, settings.auto_deal_min_confidence,
        )
        return
    try:
        from services.trade_min.service import get_opportunity  # type: ignore[import]
        opp = await get_opportunity(opportunity_id)
        if opp is None:
            logger.warning("Continuation[OPPORTUNITY]: %s not found — skipped", opportunity_id)
            return
        if getattr(opp, "status", "ACTIVE") != "ACTIVE":
            logger.info("Continuation[OPPORTUNITY]: %s is %s — skipped", opportunity_id, opp.status)
            return
        if getattr(opp, "source", "") == "simulated":
            logger.warning(
                "Continuation[OPPORTUNITY]: %s rests on simulated demand data — never auto-converted",
                opportunity_id,
            )
            return

        from services.deals.service import create_deal, list_deals  # type: ignore[import]
        from services.deals.schemas import CreateDealRequest  # type: ignore[import]
        from shared.domain.enums import AutonomyLevel

        terminal = {"PAID", "CANCELLED", "REFUNDED", "NEGOTIATION_FAILED"}
        for existing in await list_deals(limit=500):
            if existing.opportunity_id == opportunity_id:
                logger.info("Continuation[OPPORTUNITY]: %s already has deal %s — skipped",
                            opportunity_id, existing.deal_id)
                return
            if (
                existing.hs_code == opp.hs_code and existing.market == opp.market
                and _status_of(existing) not in terminal
            ):
                logger.info(
                    "Continuation[OPPORTUNITY]: live deal %s already covers %s×%s — skipped",
                    existing.deal_id, opp.hs_code, opp.market,
                )
                return

        deal = await create_deal(
            CreateDealRequest(
                operator_id=settings.auto_deal_operator_id,
                hs_code=opp.hs_code,
                market=opp.market,
                autonomy_profile=AutonomyLevel(settings.auto_deal_autonomy_profile),
                opportunity_id=opportunity_id,
            ),
            org_id=settings.auto_deal_org_id,
        )
        logger.info(
            "Continuation: deal %s auto-created from opportunity %s (rank %.1f, confidence %.1f, status %s)",
            deal.deal_id, opportunity_id, rank, confidence, _status_of(deal),
        )
    except Exception as exc:
        logger.exception("Continuation[OPPORTUNITY]: auto-create failed for %s", opportunity_id)
        await _publish_exception(None, "OPPORTUNITY_DETECTED→DEAL", str(exc),
                                 {"opportunity_id": opportunity_id})


# ---------------------------------------------------------------------------
# DEAL_CREATED (MATCHING) → RFQ dispatch
# ---------------------------------------------------------------------------

def _rfq_source(value: Any) -> str:
    v = str(value or "").lower()
    return v if v in ("indiamart", "tradeindia") else "other"


async def _rfq_candidates(deal) -> list[dict[str, Any]]:
    """Shortlist for the RFQ: SOURCE matches first, then the rest by score; when
    the Match Engine never ran (no buyer price at creation), fall back to the
    opportunity's seller candidates."""
    results = list(getattr(deal, "match_results", None) or [])
    results.sort(key=lambda m: ((m.decision != "SOURCE"), -(m.composite_score or 0)))
    out = [
        {"supplier_id": m.supplier_id, "company_name": m.company_name,
         "source": "other", "contact_id": None, "fob_price_usd": m.fob_price_usd}
        for m in results
    ]
    if not out and getattr(deal, "opportunity_id", None):
        try:
            from services.trade_min.service import get_match_seed  # type: ignore[import]
            seed = await get_match_seed(deal.opportunity_id)
            for s in (seed.top_sellers if seed else []):
                out.append({
                    "supplier_id": s.get("supplier_id"),
                    "company_name": s.get("company_name", ""),
                    "source": _rfq_source(s.get("source")),
                    "contact_id": s.get("contact_id"),
                    "fob_price_usd": s.get("fob_price_usd"),
                    "email": s.get("email"),
                })
        except Exception as exc:  # noqa: BLE001
            logger.warning("Continuation[RFQ]: match seed lookup failed for %s: %s", deal.deal_id, exc)
    return [c for c in out if c.get("supplier_id")][:_RFQ_MAX_SUPPLIERS]


async def _ensure_buyer_target(deal, candidates: list[dict[str, Any]]) -> float | None:
    """Return the buyer target price, deriving + persisting the 1.25× heuristic
    when the opportunity carried none (loudly — this is a data gap, see P1-D1)."""
    target = getattr(deal, "buyer_target_price_usd", None)
    if target:
        return float(target)
    fobs = [float(c["fob_price_usd"]) for c in candidates if c.get("fob_price_usd")]
    if not fobs:
        return None
    target = round(min(fobs) * _BUYER_MARKUP_OVER_FOB, 2)
    logger.warning(
        "Continuation[RFQ]: deal %s has no buyer target price — HEURISTIC %.2f "
        "(1.25× cheapest FOB %.2f) persisted so sourcing can re-score; replace with "
        "real buyer pricing before relying on the margin guard",
        deal.deal_id, target, min(fobs),
    )
    try:
        from services.deals.schemas import UpdateFinancialsRequest  # type: ignore[import]
        from services.deals.service import update_financials  # type: ignore[import]
        await update_financials(deal.deal_id, UpdateFinancialsRequest(buyer_target_price_usd=target))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Continuation[RFQ]: could not persist buyer target for %s: %s", deal.deal_id, exc)
    return target


async def on_deal_created(event: DomainEvent) -> None:
    """DEAL_CREATED at MATCHING → DISPATCH_RFQ gate (policy default L2 → sent).

    Fills the first structural gap of an unattended run: a deal reached
    MATCHING and nothing ever asked a supplier for a quote. Defers to the dev
    auto-flow for the deals it owns (it submits its own quote).
    """
    if not _enabled():
        return
    if (event.payload or {}).get("status") != "MATCHING":
        return
    deal_id = event.deal_id
    deal = await _get_deal_in_status(deal_id, "MATCHING", "DEAL_CREATED→RFQ")
    if deal is None:
        return
    if _auto_flow_owns(deal):
        logger.info("Continuation[RFQ]: deal %s is auto-flow's — deferring RFQ dispatch", deal_id)
        return

    # Identify + screen the counterparties on PortLess's own Apify actors
    # (contact finder → e-mail check → sanctions / LEI / VIES) before anybody
    # is written to. Best-effort: a missing APIFY_TOKEN just leaves the
    # parties "unchecked" / "unverified" for the UI to show.
    risk_flags: list[str] = []
    if _enrichment_enabled():
        from shared.orchestration import enrichment
        result = await _best_effort("DEAL_CREATED→ENRICH", deal_id,
                                    enrichment.enrich_deal_parties(deal_id))
        if result:
            deal = result.get("deal") or deal
            for role in ("buyer", "supplier"):
                verdict = enrichment.screening_verdict(deal, role)
                if verdict in ("review", "block"):
                    risk_flags.append(f"{role}_sanctions_{verdict}")
    # Buyer outreach funnel: introduce PortLess to the identified buyers now,
    # in parallel with the supplier RFQ — the negotiation waits for a "yes".
    from shared.orchestration import outreach
    await _best_effort("DEAL_CREATED→OUTREACH", deal_id, outreach.start_outreach(deal))

    try:
        candidates = await _rfq_candidates(deal)
        if not candidates:
            logger.warning(
                "Continuation[RFQ]: deal %s has no supplier candidates — leaving in MATCHING "
                "for an operator to add suppliers", deal_id,
            )
            return
        target = await _ensure_buyer_target(deal, candidates)
        names = ", ".join(c["company_name"] or c["supplier_id"] for c in candidates)
        supplier_email = _party_email(deal, "supplier")
        # A sanctions hit anywhere on the deal forces a human onto the first
        # outbound step, whatever the autonomy policy says.
        level = "L0" if any(f.endswith("_block") for f in risk_flags) else None
        await _create_gate(
            deal_id, "DISPATCH_RFQ",
            summary=f"Deal {deal_id} ({deal.hs_code} → {deal.market}): send RFQ to {names}"
                    + (f" [{', '.join(risk_flags)}]" if risk_flags else ""),
            financial_impact_usd=None,
            callback_payload={
                "deal_id": deal_id,
                "hs_code": deal.hs_code,
                "suppliers": [
                    {"supplier_id": c["supplier_id"], "company_name": c["company_name"],
                     "source": c["source"], "contact_id": c["contact_id"],
                     "email": c.get("email") or (
                         supplier_email if c["supplier_id"] == getattr(deal, "winning_supplier_id", None) else None
                     )}
                    for c in candidates
                ],
                "quantity_mt": _deal_quantity(deal),
                "target_price_usd": target,
                "validity_days": 7,
            },
            idempotency_key=f"auto-rfq-{deal_id}",
            autonomy_level=level,
            risk_flags=risk_flags,
            deal=deal,
        )
        logger.info("Continuation: DISPATCH_RFQ gate created for deal %s (%d supplier(s), event %s)",
                    deal_id, len(candidates), event.event_id)
    except Exception as exc:
        await _emit_exception(deal_id, "DEAL_CREATED→DISPATCH_RFQ", exc)


# ---------------------------------------------------------------------------
# QUOTE_RECEIVED → supplier selection when the Match Engine did not SOURCE
# ---------------------------------------------------------------------------

async def on_quote_received(event: DomainEvent) -> None:
    """QUOTE_RECEIVED while still MATCHING → CONFIRM_SUPPLIER_SELECTION gate.

    ``receive_quote`` already moves the deal to NEGOTIATING when the re-scored
    decision is SOURCE. When it is DISCARD (the reliability leg defaults to 0
    for any supplier without MCA/Udyam history, which alone caps the CMS below
    the SOURCE threshold) or unscored (no buyer price), the deal used to park
    in MATCHING forever. This proposes the quoting supplier to the operator
    instead; APPROVE records the match and moves the deal to NEGOTIATING.
    Verification hard-fails (GST inactive, IEC inactive) force L0 with a risk flag.
    """
    if not _enabled():
        return
    deal_id = event.deal_id
    payload = event.payload or {}
    if payload.get("re_scored_decision") == "SOURCE":
        return  # receive_quote transitioned the deal itself
    deal = await _get_deal_in_status(deal_id, "MATCHING", "QUOTE_RECEIVED→SELECT")
    if deal is None:
        return
    supplier_id = payload.get("supplier_id")
    if not supplier_id:
        return
    try:
        flags: list[str] = []
        if payload.get("gst_verified") is False:
            flags.append("gst_not_verified")
        if payload.get("iec_active") is False:
            flags.append("iec_inactive")
        if payload.get("re_scored_decision") is None:
            flags.append("unscored_quote")
        else:
            flags.append("match_engine_discard")
        price = payload.get("unit_price_usd")
        company = payload.get("company_name") or ""
        try:
            from services.sourcing.service import get_quote  # type: ignore[import]
            if payload.get("quote_id"):
                q = await get_quote(payload["quote_id"])
                company = company or q.company_name
        except Exception:  # noqa: BLE001
            pass
        hard = "gst_not_verified" in flags or "iec_inactive" in flags
        # Auto-source rule: a supplier whose GSTIN and IEC were actually checked
        # and passed, quoting inside the margin corridor, needs no human to
        # start negotiating — the Match Engine's DISCARD came from the empty
        # reliability leg, not from a real risk signal.
        verified = (
            payload.get("gst_checked") is True and payload.get("gst_verified") is True
            and payload.get("iec_checked") is True and payload.get("iec_active") is True
        )
        in_corridor = _price_in_corridor(price, deal)
        auto_source = verified and in_corridor and not hard and _auto_source_enabled()
        if auto_source:
            flags.append("auto_sourced_verified_in_corridor")
        level = "L0" if hard else ("L2" if auto_source else None)
        await _create_gate(
            deal_id, "CONFIRM_SUPPLIER_SELECTION",
            summary=(
                f"Quote {price} USD/MT from {company or supplier_id} "
                + ("verified (GST + IEC) and inside the margin corridor — auto-sourced"
                   if auto_source else
                   f"was not auto-sourced ({', '.join(flags)}) — confirm supplier to start negotiating")
            ),
            financial_impact_usd=(round(float(price) * _deal_quantity(deal), 2) if price else None),
            callback_payload={
                "deal_id": deal_id,
                "supplier_id": supplier_id,
                "company_name": company,
                "fob_price_usd": price,
                "composite_score": payload.get("re_scored_cms"),
                "match_id": payload.get("re_scored_match_id"),
                "buyer_target_price_usd": getattr(deal, "buyer_target_price_usd", None),
                "reason": f"Quote {payload.get('quote_id')} confirmed by operator",
            },
            idempotency_key=f"auto-select-{deal_id}-{supplier_id}",
            autonomy_level=level,
            risk_flags=flags,
            deal=deal,
        )
        logger.info("Continuation: CONFIRM_SUPPLIER_SELECTION gate for deal %s / %s (%s)",
                    deal_id, supplier_id, ", ".join(flags))
    except Exception as exc:
        await _emit_exception(deal_id, "QUOTE_RECEIVED→CONFIRM_SUPPLIER_SELECTION", exc)


def _auto_source_enabled() -> bool:
    return os.getenv("SOURCING_AUTO_SOURCE_VERIFIED", "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _price_in_corridor(price: Any, deal) -> bool:
    """Supplier ask leaves at least the deal's margin floor under the buyer target."""
    try:
        target = float(getattr(deal, "buyer_target_price_usd", None) or 0)
        floor = float(getattr(deal, "margin_floor_pct", None) or 8.0)
        return bool(price) and target > 0 and float(price) <= target * (1.0 - floor / 100.0)
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# NEGOTIATION_STARTED → open the session
# ---------------------------------------------------------------------------

async def on_negotiation_started(event: DomainEvent) -> None:
    """NEGOTIATION_STARTED → open the negotiation session (round-0 draft).

    Fills the structural gap where a deal reaches NEGOTIATING (via a Sourcing
    SOURCE decision, a manual/UI status change, or a restart that lost the dev
    auto-flow task) but nothing ever calls ``start_negotiation`` — leaving the
    deal parked with no session behind it.

    Ownership boundary: the dev auto-flow (``AUTO_FLOW_ENABLED``) already starts
    the negotiation for OPPORTUNITY-SEEDED deals, and it does so with the real
    buyer/seller contact addresses. This handler therefore DEFERS to auto-flow
    for those deals and only drives deals auto-flow doesn't own (no
    opportunity_id, or auto-flow disabled). Without this deferral both paths race
    on the same NEGOTIATING transition and create two sessions ~1s apart — each
    drafting its own round-1 email to a different buyer address (real vs. the
    placeholder), producing two pending approvals for one deal.

    Idempotent: ``start_negotiation`` itself claims the deal atomically and
    reuses an existing non-terminal session, so a duplicate NEGOTIATION_STARTED
    (the topic can arrive from the status-transition emitter more than once) is a
    no-op. The round-0 email is gated per the autonomy policy (first outbound
    stays L0 while ``hitl_on_first_outbound`` is on) — this creates the draft +
    approval, it never sends anything itself.
    """
    if not _enabled():
        logger.info(
            "Continuation disabled — skipping negotiation start for deal %s", event.deal_id
        )
        return
    deal_id = event.deal_id
    deal = await _get_deal_in_status(deal_id, "NEGOTIATING", "NEGOTIATING→NEGOTIATE")
    if deal is None:
        return

    if _auto_flow_owns(deal):
        logger.info(
            "Continuation[NEGOTIATING]: deal %s is opportunity-seeded and auto-flow "
            "is enabled — deferring negotiation start to auto-flow", deal_id,
        )
        return

    # Skip if a negotiation session already exists in a non-terminal state
    # (the dev auto-flow, or a prior firing of this handler, already opened one).
    try:
        from agents.negotiation_agent.repository import get_repository as _neg_repo  # type: ignore[import]
        existing = await _neg_repo().get_session_by_deal(deal_id)
        if existing is not None and existing.status not in ("FAILED", "ESCALATED"):
            logger.info(
                "Continuation[NEGOTIATING]: deal %s already has session %s (%s) — skipped",
                deal_id, existing.negotiation_id, existing.status,
            )
            return
    except Exception as exc:
        logger.warning(
            "Continuation[NEGOTIATING]: could not check existing session for %s: %s",
            deal_id, exc,
        )

    # Never pitch a counter-price to a buyer who has not said they are
    # interested: while the outreach funnel is open the session waits; the
    # funnel re-fires this leg once a buyer replies with interest.
    from shared.orchestration import outreach
    if outreach.waiting_for_buyer(deal):
        logger.info(
            "Continuation[NEGOTIATING]: deal %s is waiting for an interested buyer "
            "(outreach funnel open) — negotiation not started yet", deal_id,
        )
        return

    seed = _negotiation_seed(deal)
    if seed is None:
        logger.warning(
            "Continuation[NEGOTIATING]: deal %s has no supplier/buyer price yet — "
            "leaving in NEGOTIATING for an operator to complete sourcing", deal_id,
        )
        return
    buyer_target, supplier_ask, market, supplier_id = seed

    try:
        from agents.negotiation_agent.schemas import (  # type: ignore[import]
            NegotiationPolicy,
            StartNegotiationRequest,
        )
        from agents.negotiation_agent.service import start_negotiation  # type: ignore[import]

        gap = abs(buyer_target - supplier_ask)
        policy = NegotiationPolicy(concession_step=max(round(gap * 0.08, 2), 1.0))
        session = await start_negotiation(StartNegotiationRequest(
            deal_id=deal_id,
            trade_match_id=(getattr(_winning_match(deal), "match_id", None) or f"MATCH-{deal_id}"),
            buyer_id=f"BUYER-{market or 'NA'}",
            seller_id=supplier_id,
            product_code=getattr(deal, "hs_code", "") or "",
            buyer_country_code=market,
            buyer_target_price=buyer_target,
            supplier_quote_price=supplier_ask,
            buyer_email=_party_email(deal, "buyer"),
            seller_email=_party_email(deal, "supplier"),
            quantity_mt=_deal_quantity(deal),
            policy=policy,
        ))
        logger.info(
            "Continuation: negotiation %s opened for deal %s (status=%s, buyer=%s ask=%s, event %s)",
            session.negotiation_id, deal_id, session.status,
            buyer_target, supplier_ask, event.event_id,
        )
    except Exception as exc:
        await _emit_exception(deal_id, "NEGOTIATING→NEGOTIATION_STARTED", exc)


# ---------------------------------------------------------------------------
# NEGOTIATION_FAILED → triage + re-match
# ---------------------------------------------------------------------------

async def on_negotiation_failed(event: DomainEvent) -> None:
    """NEGOTIATION_FAILED → deal.exception (resolver triage) + next-best supplier.

    Before this leg a failed negotiation parked the deal in NEGOTIATION_FAILED
    with nobody told. Now the Exception Resolver gets the failure, and when the
    Match Engine scored another SOURCE candidate, the operator is offered it
    through CONFIRM_SUPPLIER_SELECTION (APPROVE → MATCHING → NEGOTIATING with
    the new supplier; the old session is FAILED so a fresh one opens).
    """
    if not _enabled():
        return
    deal_id = event.deal_id
    deal = await _get_deal_in_status(deal_id, "NEGOTIATION_FAILED", "NEGOTIATION_FAILED→TRIAGE")
    if deal is None:
        return
    payload = event.payload or {}
    await _publish_exception(
        deal_id, "NEGOTIATION_FAILED",
        payload.get("reason") or "negotiation failed",
        {"category": "NEGOTIATION_FAILED", "winning_supplier_id": deal.winning_supplier_id},
    )
    try:
        failed_id = getattr(deal, "winning_supplier_id", None)
        alternates = [
            m for m in (getattr(deal, "match_results", None) or [])
            if m.supplier_id != failed_id and m.decision == "SOURCE"
        ]
        if not alternates:
            logger.info("Continuation[NEGOTIATION_FAILED]: deal %s has no alternate SOURCE "
                        "candidate — resolver/operator decides", deal_id)
            return
        nxt = max(alternates, key=lambda m: m.composite_score or 0)
        await _create_gate(
            deal_id, "CONFIRM_SUPPLIER_SELECTION",
            summary=(
                f"Negotiation with {failed_id} failed — re-match with next-best supplier "
                f"{nxt.company_name or nxt.supplier_id} (CMS {nxt.composite_score:.1f}, "
                f"FOB {nxt.fob_price_usd})?"
            ),
            financial_impact_usd=(
                round(float(nxt.fob_price_usd) * _deal_quantity(deal), 2) if nxt.fob_price_usd else None
            ),
            callback_payload={
                "deal_id": deal_id,
                "supplier_id": nxt.supplier_id,
                "company_name": nxt.company_name,
                "fob_price_usd": nxt.fob_price_usd,
                "composite_score": nxt.composite_score,
                "match_id": nxt.match_id,
                "reason": f"Re-match after failed negotiation with {failed_id}",
            },
            idempotency_key=f"auto-rematch-{deal_id}-{nxt.supplier_id}",
            deal=deal,
            risk_flags=["re_match_after_failed_negotiation"],
        )
        logger.info("Continuation: re-match gate for deal %s → %s", deal_id, nxt.supplier_id)
    except Exception as exc:
        await _emit_exception(deal_id, "NEGOTIATION_FAILED→REMATCH", exc)


# ---------------------------------------------------------------------------
# NEGOTIATION_AGREED → PI
# ---------------------------------------------------------------------------

async def on_negotiation_agreed(event: DomainEvent) -> None:
    """NEGOTIATION_AGREED → issue the Proforma Invoice automatically.

    The PI is the next commercial artifact after agreement; issuing it flips
    the deal to PI_SENT. Buyer acceptance of the PI remains a buyer action —
    this handler never auto-accepts.
    """
    if not _enabled():
        logger.info("Continuation disabled — skipping PI issuance for deal %s", event.deal_id)
        return
    deal_id = event.deal_id
    agreed_price = event.payload.get("agreed_price")
    if not deal_id or not agreed_price or float(agreed_price) <= 0:
        logger.error(
            "Continuation: cannot issue PI for deal %s — missing/invalid agreed_price %r",
            deal_id, agreed_price,
        )
        return

    deal = await _get_deal_in_status(deal_id, "NEGOTIATION_AGREED", "AGREED→PI")
    if deal is None:
        return
    _, quantity_mt = await _negotiation_numbers(deal_id, deal)

    # Know who you are invoicing: a Proforma Invoice never goes to a buyer the
    # sanctions / registry screening has not cleared. Re-screen when the last
    # check is older than 30 days or never ran (best-effort — without an
    # APIFY_TOKEN the verdict stays "unchecked" and the PI proceeds, loudly).
    from shared.orchestration import enrichment
    verdict = enrichment.screening_verdict(deal, "buyer")
    if verdict == "unchecked" or not enrichment.screening_is_fresh(deal, "buyer"):
        result = await _best_effort(
            "AGREED→SCREEN_BUYER", deal_id,
            enrichment.enrich_deal_parties(deal_id, roles=("buyer",), contacts=False, screen=True),
        )
        if result:
            deal = result.get("deal") or deal
            verdict = enrichment.screening_verdict(deal, "buyer")
    if verdict == "block":
        await _publish_exception(
            deal_id, "NEGOTIATION_AGREED→PI_BLOCKED",
            "buyer failed sanctions / registry screening — Proforma Invoice withheld",
            {"category": "APPROVAL_REJECTED", "screening": "block",
             "action_required": "sanctions_block_review"},
        )
        logger.error("Continuation: PI for deal %s WITHHELD — buyer screening verdict is BLOCK", deal_id)
        return
    if verdict == "review":
        try:
            buyer = next((p for p in (getattr(deal, "parties", None) or [])
                          if (getattr(getattr(p, "role", ""), "value", getattr(p, "role", "")) or "").lower() == "buyer"), None)
            await _create_gate(
                deal_id, "CONFIRM_COUNTERPARTY_RISK",
                summary=(
                    f"Buyer {getattr(buyer, 'legal_name', '?')} screening = REVIEW "
                    f"({(getattr(buyer, 'sanctions_reason', None) or 'possible list / registry hit')[:160]}) "
                    f"— confirm before issuing PI at {agreed_price} × {quantity_mt} MT"
                ),
                financial_impact_usd=round(float(agreed_price) * quantity_mt, 2),
                callback_payload={
                    "deal_id": deal_id, "agreed_price": float(agreed_price),
                    "quantity_mt": quantity_mt, "market": getattr(deal, "market", None),
                    "buyer": getattr(buyer, "legal_name", None),
                    "screening_reason": getattr(buyer, "sanctions_reason", None),
                },
                idempotency_key=f"auto-risk-{deal_id}",
                deal=deal,
                risk_flags=["buyer_sanctions_review"],
            )
            logger.info("Continuation: CONFIRM_COUNTERPARTY_RISK gate for deal %s (review)", deal_id)
        except Exception as exc:
            await _emit_exception(deal_id, "NEGOTIATION_AGREED→RISK_GATE", exc)
        return
    if verdict == "unchecked":
        logger.warning(
            "Continuation: buyer of deal %s is UNSCREENED (no APIFY_TOKEN / actor unavailable) — "
            "PI proceeds; configure the counterparty-check actor for production", deal_id,
        )

    try:
        pi = await issue_pi_for_deal(
            deal_id, agreed_price=float(agreed_price), quantity_mt=quantity_mt,
            market=getattr(deal, "market", None),
        )
        logger.info(
            "Continuation: PI %s issued for deal %s at %s × %s MT (event %s)",
            pi.pi_id, deal_id, agreed_price, quantity_mt, event.event_id,
        )
    except Exception as exc:
        await _emit_exception(deal_id, "NEGOTIATION_AGREED→PI_SENT", exc)


async def issue_pi_for_deal(
    deal_id: str, *, agreed_price: float, quantity_mt: float | None, market: str | None,
):
    """Issue the Proforma Invoice (idempotent per deal). Shared by the AGREED leg
    and the CONFIRM_COUNTERPARTY_RISK approve callback."""
    from services.dealmaker.schemas import IssuePiRequest  # type: ignore[import]
    from services.dealmaker.service import issue_pi  # type: ignore[import]

    if not quantity_mt:
        _, quantity_mt = await _negotiation_numbers(deal_id)
    return await issue_pi(IssuePiRequest(
        deal_id=deal_id,
        agreed_price=float(agreed_price),
        quantity_mt=float(quantity_mt),
        port_of_destination=f"Destination Port, {market or 'TBD'}",
        payment_terms=payment_terms_for(),
        idempotency_key=f"auto-pi-{deal_id}",
    ))


# ---------------------------------------------------------------------------
# PI_SENT → email the PI to the buyer
# ---------------------------------------------------------------------------

async def on_pi_sent(event: DomainEvent) -> None:
    """PI_SENT → send the Proforma Invoice to the buyer (SEND_DEAL_NOTIFICATION).

    The buyer cannot accept a PI nobody sent them. Fires from both the
    DealMaker emit (carries pi_id) and the Deals status emitter; the
    notification is keyed per deal so the second firing is a no-op.
    """
    if not _enabled():
        return
    deal_id = event.deal_id
    deal = await _get_deal_in_status(deal_id, "PI_SENT", "PI_SENT→NOTIFY_BUYER")
    if deal is None:
        return
    try:
        from services.dealmaker.repository import get_repository as _dm_repo  # type: ignore[import]
        pi = None
        if (event.payload or {}).get("pi_id"):
            pi = await _dm_repo().get_pi(event.payload["pi_id"])
        if pi is None:
            pi = await _dm_repo().get_pi_by_deal(deal_id)
        if pi is None:
            raise RuntimeError(f"no PI found for deal {deal_id}")
        from shared.orchestration import notifications
        email_id = await notifications.notify_pi_issued(deal, pi)
        logger.info("Continuation: PI %s notification for deal %s → %s", pi.pi_id, deal_id, email_id)
    except Exception as exc:
        await _emit_exception(deal_id, "PI_SENT→NOTIFY_BUYER", exc)


# ---------------------------------------------------------------------------
# PI_ACCEPTED → escrow (+ funding instructions)
# ---------------------------------------------------------------------------

async def on_pi_accepted(event: DomainEvent) -> None:
    """PI_ACCEPTED → open the neutral escrow (deal → ESCROW_PENDING).

    Funding the escrow remains the buyer's action; this only opens it so the
    buyer has something to pay into — and then emails them the funding
    instructions (amount, provider, checkout link, tranche schedule).
    Amount comes from the accepted PI itself.
    """
    if not _enabled():
        return
    deal_id = event.deal_id
    deal = await _get_deal_in_status(deal_id, "PI_ACCEPTED", "PI_ACCEPTED→ESCROW")
    if deal is None:
        return

    try:
        from services.dealmaker.repository import get_repository as _dm_repo  # type: ignore[import]
        pi = None
        pi_id = event.payload.get("pi_id")
        if pi_id:
            pi = await _dm_repo().get_pi(pi_id)
        if pi is None:
            pi = await _dm_repo().get_pi_by_deal(deal_id)
        if pi is None:
            raise RuntimeError(f"no PI found for deal {deal_id} — cannot size the escrow")

        # Buyer-opted QC provider drives the escrow's QC-proof tranche context.
        qc_provider = None
        try:
            from agents.negotiation_agent.repository import get_repository as _neg_repo  # type: ignore[import]
            session = await _neg_repo().get_session_by_deal(deal_id)
            qc_provider = getattr(session, "qc_provider", None) if session else None
        except Exception:
            pass

        from services.escrow_guard.schemas import CreateEscrowRequest  # type: ignore[import]
        from services.escrow_guard.service import create_escrow  # type: ignore[import]
        escrow = await create_escrow(CreateEscrowRequest(
            deal_id=deal_id,
            amount=round(float(pi.total_value), 2),
            qc_provider=qc_provider,
            idempotency_key=f"auto-escrow-{deal_id}",
        ))
        logger.info(
            "Continuation: escrow %s opened for deal %s (amount=%s, event %s)",
            escrow.escrow_id, deal_id, pi.total_value, event.event_id,
        )
    except Exception as exc:
        await _emit_exception(deal_id, "PI_ACCEPTED→ESCROW_PENDING", exc)
        return

    from shared.orchestration import notifications
    await _best_effort(
        "ESCROW_PENDING→NOTIFY_BUYER", deal_id,
        notifications.notify_escrow_opened(deal, escrow, getattr(pi, "pi_id", None)),
    )


# ---------------------------------------------------------------------------
# Escrow tranche release gates (money-moving; policy + ceiling)
# ---------------------------------------------------------------------------

async def _tranche_gate(
    deal, trigger: str, proof_refs: list[str], stage: str, key_suffix: str = "",
) -> None:
    """Create a RELEASE_ESCROW_TRANCHE gate for the PENDING tranche with *trigger*.

    No-op (logged) when the deal has no funded escrow or no such pending
    tranche — the legacy Stripe-hold path has no tranches at all. ``key_suffix``
    lets the tranche-released leg re-gate a tranche whose earlier gate ran
    before its predecessor was released (sequential release rule).
    """
    deal_id = deal.deal_id
    try:
        from services.escrow_guard.repository import get_repository as _eg_repo  # type: ignore[import]
        hold = await _eg_repo().get_hold_by_deal(deal_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Continuation[%s]: escrow lookup failed for %s: %s", stage, deal_id, exc)
        return
    if hold is None or hold.status not in ("FUNDED", "RELEASED"):
        logger.info("Continuation[%s]: deal %s has no funded escrow — tranche %s not gated",
                    stage, deal_id, trigger)
        return
    tranche = next(
        (t for t in (hold.tranches or []) if t.trigger == trigger and t.status == "PENDING"), None,
    )
    if tranche is None:
        logger.info("Continuation[%s]: no pending %s tranche on escrow %s — skipped",
                    stage, trigger, hold.escrow_id)
        return
    amount = tranche.amount if tranche.amount is not None else round(hold.amount * tranche.pct, 2)
    # Preconditions for an unattended release below the value threshold: the
    # proof refs are there and the escrow ledger reconciles.
    conditions_ok = bool([p for p in proof_refs if p])
    if conditions_ok:
        try:
            from services.escrow_guard.service import _ledger_issues  # type: ignore[import]
            conditions_ok = not _ledger_issues(hold)[1]
        except Exception:  # noqa: BLE001
            conditions_ok = False
    await _create_gate(
        deal_id, "RELEASE_ESCROW_TRANCHE",
        summary=(
            f"{trigger} reached — release tranche {tranche.tranche_id} "
            f"({int(round(tranche.pct * 100))}% = {amount:.2f} {hold.currency}) from escrow {hold.escrow_id}"
        ),
        financial_impact_usd=float(amount),
        callback_payload={
            "deal_id": deal_id,
            "escrow_id": hold.escrow_id,
            "tranche_id": tranche.tranche_id,
            "trigger": trigger,
            "proof_refs": [p for p in proof_refs if p],
            "idempotency_key": f"auto-tranche-{deal_id}-{tranche.tranche_id}",
        },
        idempotency_key=f"auto-tranche-gate-{deal_id}-{tranche.tranche_id}{key_suffix}",
        deal=deal,
        conditions_ok=conditions_ok,
        risk_flags=["money_movement"],
    )
    logger.info("Continuation[%s]: RELEASE_ESCROW_TRANCHE gate for %s/%s (%.2f)",
                stage, deal_id, tranche.tranche_id, amount)


# ---------------------------------------------------------------------------
# ESCROW_FUNDED → PO gate
# ---------------------------------------------------------------------------

async def on_escrow_funded(event: DomainEvent) -> None:
    """ESCROW_FUNDED → create the ISSUE_PO HITL gate (operator approves → PO)."""
    if not _enabled():
        return
    deal_id = event.deal_id
    deal = await _get_deal_in_status(deal_id, "ESCROW_FUNDED", "ESCROW_FUNDED→PO_GATE")
    if deal is None:
        return
    try:
        supplier_ask, quantity_mt = await _negotiation_numbers(deal_id, deal)
        if not supplier_ask:
            raise RuntimeError(
                f"no supplier price available for deal {deal_id} — PO gate needs one"
            )
        from datetime import date, timedelta
        await _create_gate(
            deal_id, "ISSUE_PO",
            summary=f"Escrow funded — issue PO for deal {deal_id} at ${supplier_ask}/unit",
            financial_impact_usd=round(supplier_ask * quantity_mt, 2),
            callback_payload={
                "deal_id": deal_id, "agreed_price": supplier_ask,
                "quantity_mt": quantity_mt, "incoterm": "FOB",
                "port_of_loading": "Chennai, IN",
                "delivery_deadline": (date.today() + timedelta(days=21)).isoformat(),
                "payment_terms": payment_terms_for(),
            },
            idempotency_key=f"auto-po-gate-{deal_id}",
            deal=deal,
        )
        logger.info("Continuation: ISSUE_PO gate created for deal %s (event %s)",
                    deal_id, event.event_id)
    except Exception as exc:
        await _emit_exception(deal_id, "ESCROW_FUNDED→ISSUE_PO_GATE", exc)


# ---------------------------------------------------------------------------
# PO_ISSUED → QC booking (+ PO to supplier, tranche T1)
# ---------------------------------------------------------------------------

async def on_po_issued(event: DomainEvent) -> None:
    """PO_ISSUED → book the QC inspection with the buyer-opted provider, email
    the PO to the supplier, and gate the PO_ISSUED escrow tranche."""
    if not _enabled():
        return
    deal_id = event.deal_id
    deal = await _get_deal_in_status(deal_id, "PO_ISSUED", "PO_ISSUED→QC")
    if deal is None:
        return
    try:
        _, quantity_mt = await _negotiation_numbers(deal_id, deal)
        from services.shield_link.schemas import QcBookingRequest  # type: ignore[import]
        from services.shield_link.service import book_qc  # type: ignore[import]
        tpo = await book_qc(QcBookingRequest(
            deal_id=deal_id,
            quantity_mt=quantity_mt,
            hs_code=getattr(deal, "hs_code", None),
            idempotency_key=f"auto-qc-{deal_id}",
        ))
        logger.info(
            "Continuation: QC booked %s (provider=%s) for deal %s (event %s)",
            tpo.tpo_id, tpo.provider, deal_id, event.event_id,
        )
    except Exception as exc:
        await _emit_exception(deal_id, "PO_ISSUED→QC_BOOKED", exc)

    po_id = (event.payload or {}).get("po_id")

    async def _notify_po() -> None:
        from services.dealmaker.repository import get_repository as _dm_repo  # type: ignore[import]
        po = (await _dm_repo().get_po(po_id)) if po_id else None
        if po is None:
            po = await _dm_repo().get_po_by_deal(deal_id)
        if po is None:
            logger.info("Continuation[PO_ISSUED]: no PO record for %s — supplier not notified", deal_id)
            return
        from shared.orchestration import notifications
        await notifications.notify_po_issued(deal, po)

    await _best_effort("PO_ISSUED→NOTIFY_SUPPLIER", deal_id, _notify_po())
    await _best_effort(
        "PO_ISSUED→TRANCHE_T1", deal_id,
        _tranche_gate(deal, "PO_ISSUED", [po_id or ""], "PO_ISSUED→TRANCHE_T1"),
    )


# ---------------------------------------------------------------------------
# QC_BOOKED → tell the supplier
# ---------------------------------------------------------------------------

async def on_qc_booked(event: DomainEvent) -> None:
    """QC_BOOKED → email the inspection booking (agency, ref, date) to the supplier."""
    if not _enabled():
        return
    deal_id = event.deal_id
    if not deal_id:
        return
    try:
        from services.deals.service import get_deal  # type: ignore[import]
        deal = await get_deal(deal_id)
        po_id = None
        try:
            from services.dealmaker.repository import get_repository as _dm_repo  # type: ignore[import]
            po = await _dm_repo().get_po_by_deal(deal_id)
            po_id = po.po_id if po else None
        except Exception:  # noqa: BLE001
            pass
        from shared.orchestration import notifications
        await notifications.notify_qc_booked(deal, event.payload or {}, po_id)
    except Exception as exc:  # noqa: BLE001 — a missed notice must not raise an exception
        logger.error("Continuation[QC_BOOKED→NOTIFY_SUPPLIER]: failed for %s: %s", deal_id, exc)


# ---------------------------------------------------------------------------
# QC_PASSED → documents + tranche T2 + freight gate
# ---------------------------------------------------------------------------

_SHIPPING_DOCS = ("CI", "PL", "CoO", "SDF", "SHIPPING_BILL")


async def _generate_documents(deal_id: str) -> list[str]:
    """Render the export document pack (idempotent per deal × type)."""
    from services.docugen.schemas import GenerateDocumentRequest  # type: ignore[import]
    from services.docugen.service import generate_document  # type: ignore[import]
    from shared.domain.enums import DocumentType

    doc_ids: list[str] = []
    for dt in (
        DocumentType.COMMERCIAL_INVOICE, DocumentType.PACKING_LIST,
        DocumentType.CERTIFICATE_OF_ORIGIN, DocumentType.SDF, DocumentType.SHIPPING_BILL,
    ):
        try:
            doc = await generate_document(GenerateDocumentRequest(
                deal_id=deal_id, doc_type=dt,
                idempotency_key=f"auto-doc-{deal_id}-{dt.value}",
            ))
            doc_ids.append(doc.doc_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Continuation[DOCS]: %s for deal %s failed: %s", dt.value, deal_id, exc)
    if doc_ids:
        logger.info("Continuation[DOCS]: %d document(s) ready for deal %s", len(doc_ids), deal_id)
    return doc_ids


async def on_qc_passed(event: DomainEvent) -> None:
    """QC_PASSED → generate the document pack, gate tranche T2, create the
    BOOK_FREIGHT HITL gate."""
    if not _enabled():
        return
    deal_id = event.deal_id
    deal = await _get_deal_in_status(deal_id, "QC_PASSED", "QC_PASSED→FREIGHT_GATE")
    if deal is None:
        return
    payload = event.payload or {}
    await _best_effort("QC_PASSED→DOCS", deal_id, _generate_documents(deal_id))
    await _best_effort(
        "QC_PASSED→TRANCHE_T2", deal_id,
        _tranche_gate(
            deal, "QC_PASSED",
            [payload.get("crf_hash") or "", payload.get("report_url") or "", payload.get("ext_ref") or ""],
            "QC_PASSED→TRANCHE_T2",
        ),
    )
    try:
        await _create_gate(
            deal_id, "BOOK_FREIGHT",
            summary=f"QC passed — book freight for deal {deal_id}",
            financial_impact_usd=None,
            callback_payload={"deal_id": deal_id, "provider": "cogoport", "incoterm": "FOB"},
            idempotency_key=f"auto-freight-gate-{deal_id}",
            deal=deal,
        )
        logger.info("Continuation: BOOK_FREIGHT gate created for deal %s (event %s)",
                    deal_id, event.event_id)
    except Exception as exc:
        await _emit_exception(deal_id, "QC_PASSED→BOOK_FREIGHT_GATE", exc)


# ---------------------------------------------------------------------------
# QC_FAILED → DISPUTE + resolver
# ---------------------------------------------------------------------------

async def on_qc_failed(event: DomainEvent) -> None:
    """QC_FAILED → deal → DISPUTE, deal.exception for the Exception Resolver.

    DISPUTE is the only forward transition the state machine allows from
    QC_FAILED; from there the resolver / operator chooses re-inspection via a
    new deal, supplier replacement, or ``refund`` (→ REFUNDED).
    """
    if not _enabled():
        return
    deal_id = event.deal_id
    deal = await _get_deal_in_status(deal_id, "QC_FAILED", "QC_FAILED→DISPUTE")
    if deal is None:
        return
    payload = event.payload or {}
    try:
        from services.deals.schemas import UpdateStatusRequest  # type: ignore[import]
        from services.deals.service import update_status  # type: ignore[import]
        from shared.domain.enums import DealStatus
        await update_status(deal_id, UpdateStatusRequest(
            target_status=DealStatus.DISPUTE,
            reason=f"QC failed (ref {payload.get('ext_ref')}): {payload.get('notes') or 'see report'}",
        ))
    except Exception as exc:
        await _emit_exception(deal_id, "QC_FAILED→DISPUTE", exc)
        return
    await _publish_exception(
        deal_id, "QC_FAILED",
        f"QC inspection failed: {payload.get('notes') or 'no notes'}",
        {"category": "QC_FAILED", "tpo_id": payload.get("tpo_id"), "ext_ref": payload.get("ext_ref")},
    )
    logger.info("Continuation: deal %s → DISPUTE after QC failure (event %s)", deal_id, event.event_id)


# ---------------------------------------------------------------------------
# FREIGHT_BOOKED → customs filing + buyer notice
# ---------------------------------------------------------------------------

async def on_freight_booked(event: DomainEvent) -> None:
    """FREIGHT_BOOKED → FILE_CUSTOMS gate (CHA/ICEGATE) + shipment email to buyer.

    The document pack rendered at QC_PASSED is attached by id; if it is missing
    (QC_PASSED leg failed) it is rendered here first.
    """
    if not _enabled():
        return
    deal_id = event.deal_id
    deal = await _get_deal_in_status(deal_id, "IN_TRANSIT", "FREIGHT_BOOKED→CUSTOMS")
    if deal is None:
        return
    payload = event.payload or {}
    try:
        doc_ids: list[str] = []
        try:
            from services.docugen.service import list_deal_documents  # type: ignore[import]
            docs = await list_deal_documents(deal_id)
            doc_ids = [d.doc_id for d in docs.documents if d.status == "RENDERED"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Continuation[CUSTOMS]: document listing failed for %s: %s", deal_id, exc)
        if not doc_ids:
            doc_ids = await _generate_documents(deal_id)
        await _create_gate(
            deal_id, "FILE_CUSTOMS",
            summary=(
                f"Freight booked ({payload.get('provider')} ref {payload.get('ext_ref')}) — "
                f"file Shipping Bill with CHA for deal {deal_id} ({len(doc_ids)} document(s))"
            ),
            financial_impact_usd=None,
            callback_payload={
                "deal_id": deal_id,
                "provider": "icegate",
                "documents": doc_ids,
                "rodtep_declaration": True,
                "idempotency_key": f"auto-cha-{deal_id}",
            },
            idempotency_key=f"auto-customs-gate-{deal_id}",
            deal=deal,
        )
        logger.info("Continuation: FILE_CUSTOMS gate created for deal %s (event %s)",
                    deal_id, event.event_id)
    except Exception as exc:
        await _emit_exception(deal_id, "FREIGHT_BOOKED→FILE_CUSTOMS_GATE", exc)

    from shared.orchestration import notifications
    await _best_effort("FREIGHT_BOOKED→NOTIFY_BUYER", deal_id,
                       notifications.notify_freight_booked(deal, payload))


# ---------------------------------------------------------------------------
# CUSTOMS_CLEARED → tranche T3 + buyer notice + payout gate
# ---------------------------------------------------------------------------

async def on_customs_cleared(event: DomainEvent) -> None:
    """Deal CLEARED → tranche T3 gate, arrival email, RELEASE_SUPPLIER_PAYOUT gate."""
    if not _enabled():
        return
    deal_id = event.deal_id
    deal = await _get_deal_in_status(deal_id, "CLEARED", "CLEARED→PAYOUT_GATE")
    if deal is None:
        return
    payload = event.payload or {}

    async def _cha_proofs() -> list[str]:
        try:
            from services.shield_link.service import list_deal_orders  # type: ignore[import]
            orders = await list_deal_orders(deal_id)
            return [o.ext_ref for o in orders.orders if o.order_type.upper() == "CHA" and o.ext_ref]
        except Exception:  # noqa: BLE001
            return []

    proofs = [payload.get("ext_ref") or ""] + (await _best_effort("CLEARED→PROOFS", deal_id, _cha_proofs()) or [])
    await _best_effort("CLEARED→TRANCHE_T3", deal_id,
                       _tranche_gate(deal, "CLEARED", proofs, "CLEARED→TRANCHE_T3"))
    from shared.orchestration import notifications
    await _best_effort("CLEARED→NOTIFY_BUYER", deal_id,
                       notifications.notify_cargo_cleared(deal, payload))

    try:
        await _supplier_payout_gate(deal, stage="CLEARED→PAYOUT_GATE", first=True)
        logger.info("Continuation: RELEASE_SUPPLIER_PAYOUT gate created for deal %s (event %s)",
                    deal_id, event.event_id)
    except Exception as exc:
        await _emit_exception(deal_id, "CLEARED→PAYOUT_GATE", exc)


async def _supplier_owed_and_paid(deal, ask: float | None, qty: float) -> tuple[float | None, float, float, float, float, bool]:
    """→ (owed, paid, pending, available_for_payout, released_total, ledger_reconciled).

    ``owed`` is the Purchase Order value, else supplier ask × quantity.
    ``available`` is what the escrow ledger says PortLess can pay out now;
    when the ledger is unreachable it falls back to ``owed`` (uncapped) so a
    broken ledger lookup never silently under-pays a supplier — Escrow-Guard
    enforces the real cap again at request time.
    """
    owed: float | None = None
    try:
        from services.dealmaker.repository import get_repository as _dm_repo  # type: ignore[import]
        po = await _dm_repo().get_po_by_deal(deal.deal_id)
        if po is not None and po.total_value:
            owed = round(float(po.total_value), 2)
    except Exception:  # noqa: BLE001
        pass
    if owed is None and ask:
        owed = round(float(ask) * qty, 2)
    paid = pending = released = 0.0
    available: float | None = None
    reconciled = False
    try:
        from services.escrow_guard.service import get_deal_ledger  # type: ignore[import]
        ledger = await get_deal_ledger(deal.deal_id)
        paid, pending = float(ledger.payouts_total), float(ledger.payouts_pending)
        available = float(ledger.available_for_payout)
        released = float(ledger.released_total)
        reconciled = bool(getattr(ledger, "reconciled", False))
        if ledger.supplier_owed:
            owed = float(ledger.supplier_owed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Continuation: escrow ledger unavailable for %s (%s) — payout not capped",
                       deal.deal_id, exc)
    if available is None:
        available = owed or 0.0
    return owed, paid, pending, available, released, reconciled


async def _pending_payout_gate_exists(deal_id: str) -> bool:
    try:
        from services.hitl_approval.service import list_approvals  # type: ignore[import]
        pending = await list_approvals(status="PENDING", deal_id=deal_id)
        return any(a.action == "RELEASE_SUPPLIER_PAYOUT" for a in pending)
    except Exception:  # noqa: BLE001
        return False


async def _supplier_payout_gate(deal, stage: str, first: bool = False) -> bool:
    """RELEASE_SUPPLIER_PAYOUT gate for whatever the supplier is still owed,
    capped at the funds released from escrow. Returns True when a gate was
    created. Used at CLEARED (``first``) and again after every later tranche
    release / settlement until the supplier is paid in full."""
    deal_id = deal.deal_id
    supplier_ask, quantity_mt = await _negotiation_numbers(deal_id, deal)
    if first and not supplier_ask:
        raise RuntimeError(f"no supplier price available for deal {deal_id} — payout gate needs one")
    owed, paid, pending, available, released, reconciled = await _supplier_owed_and_paid(
        deal, supplier_ask, quantity_mt,
    )
    if not owed:
        logger.info("Continuation[%s]: supplier entitlement unknown for %s — no payout gate", stage, deal_id)
        return False
    due = round(owed - paid - pending, 2)
    if due <= 0.01:
        logger.info("Continuation[%s]: supplier of deal %s already paid in full (%.2f)", stage, deal_id, owed)
        return False
    if not first and await _pending_payout_gate_exists(deal_id):
        logger.info("Continuation[%s]: a RELEASE_SUPPLIER_PAYOUT gate is already pending for %s", stage, deal_id)
        return False
    amount = round(min(due, available), 2)
    if amount <= 0.01:
        logger.info(
            "Continuation[%s]: deal %s — supplier owed %.2f but nothing released from escrow yet; "
            "payout waits for the next tranche", stage, deal_id, due,
        )
        return False
    supplier_id = getattr(deal, "winning_supplier_id", None) or "UNKNOWN"
    kind = "supplier_payout" if (paid == 0 and pending == 0) else "supplier_balance"
    key = f"auto-payout-gate-{deal_id}" if first else f"auto-payout-balance-{deal_id}-{int(round(released))}-{int(round(paid + pending))}"
    partial = amount < due - 0.01
    await _create_gate(
        deal_id, "RELEASE_SUPPLIER_PAYOUT",
        summary=(
            f"{'Cargo cleared' if first else 'Escrow released funds'} — pay supplier ${amount:,.2f}"
            + (f" (of ${due:,.2f} still owed; capped at funds released from escrow)" if partial else "")
            + f" for deal {deal_id}"
        ),
        financial_impact_usd=amount,
        callback_payload={
            "deal_id": deal_id,
            "payee_id": f"SUPPLIER-{supplier_id}",
            "amount": amount,
            "total_owed": owed,
            "kind": kind,
            "idempotency_key": f"auto-payout-{deal_id}" if first else f"auto-payout-{deal_id}-{int(round(paid + pending))}-{int(round(released))}",
        },
        idempotency_key=key,
        deal=deal,
        conditions_ok=reconciled,
        risk_flags=["money_movement"] + (["partial_payout_capped_by_escrow"] if partial else []),
    )
    return True


# ---------------------------------------------------------------------------
# PAYOUT_COMPLETED → close-out
# ---------------------------------------------------------------------------

async def on_payout_completed(event: DomainEvent) -> None:
    """PAYOUT_COMPLETED → partial payout: gate the supplier balance; fully paid
    (deal PAID): completion emails to both parties."""
    if not _enabled():
        return
    deal_id = event.deal_id
    payload = event.payload or {}
    if payload.get("fully_paid") is False and deal_id:
        try:
            from services.deals.service import get_deal  # type: ignore[import]
            deal = await get_deal(deal_id)
            await _best_effort("PAYOUT_PARTIAL→BALANCE_GATE", deal_id,
                               _supplier_payout_gate(deal, stage="PAYOUT_PARTIAL→BALANCE_GATE"))
        except Exception as exc:  # noqa: BLE001
            logger.error("Continuation[PAYOUT_PARTIAL]: balance gate failed for %s: %s", deal_id, exc)
        return
    deal = await _get_deal_in_status(deal_id, "PAID", "PAYOUT_COMPLETED→CLOSE")
    if deal is None:
        return
    from shared.orchestration import notifications
    sent = await _best_effort("PAID→NOTIFY_PARTIES", deal_id, notifications.notify_deal_completed(deal))
    logger.info("Continuation: deal %s closed out (PAID) — %d completion notice(s) (event %s)",
                deal_id, len(sent or []), event.event_id)


# ---------------------------------------------------------------------------
# ESCROW_TRANCHE_RELEASED → buyer notice (T4 window), next tranche, payout top-up
# ---------------------------------------------------------------------------

async def on_escrow_tranche_released(event: DomainEvent) -> None:
    """A tranche left the escrow: tell the buyer when the acceptance window opens,
    re-gate the next tranche whose trigger is already reached (sequential
    release), and top up the supplier payout with the newly available funds."""
    if not _enabled():
        return
    deal_id = event.deal_id
    payload = event.payload or {}
    if not deal_id:
        return
    try:
        from services.deals.service import get_deal  # type: ignore[import]
        deal = await get_deal(deal_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Continuation[TRANCHE_RELEASED]: deal %s lookup failed: %s", deal_id, exc)
        return
    status = _status_of(deal)

    if payload.get("inspection_window_opened"):
        async def _notify_window() -> None:
            from services.escrow_guard.repository import get_repository as _eg_repo  # type: ignore[import]
            from shared.orchestration import notifications
            hold = await _eg_repo().get_hold_by_deal(deal_id)
            if hold is None:
                return
            final = [t for t in (hold.tranches or []) if t.trigger == "ACCEPTED" and t.status == "PENDING"]
            final_amount = round(sum((t.amount or 0.0) for t in final), 2) if final else None
            await notifications.notify_inspection_window_open(
                deal, hold, final_amount, payload.get("inspection_deadline"),
            )
        await _best_effort("TRANCHE_RELEASED→NOTIFY_WINDOW", deal_id, _notify_window())

    nxt_trigger = payload.get("next_pending_trigger")
    nxt_id = payload.get("next_pending_tranche")
    if nxt_trigger and nxt_trigger != "ACCEPTED" and _status_reached(status, nxt_trigger):
        await _best_effort(
            "TRANCHE_RELEASED→NEXT_GATE", deal_id,
            _tranche_gate(deal, nxt_trigger, [], "TRANCHE_RELEASED→NEXT_GATE",
                          key_suffix=f"-after-{payload.get('tranche_id')}"),
        )
        logger.info("Continuation: re-gated tranche %s (%s) for deal %s after %s",
                    nxt_id, nxt_trigger, deal_id, payload.get("tranche_id"))

    if status in ("CLEARED", "PAID"):
        await _best_effort("TRANCHE_RELEASED→PAYOUT_TOPUP", deal_id,
                           _supplier_payout_gate(deal, stage="TRANCHE_RELEASED→PAYOUT_TOPUP"))


# ---------------------------------------------------------------------------
# FREIGHT_MILESTONE (DELIVERED / ARRIVED) → inspection window
# ---------------------------------------------------------------------------

def _inspection_window_milestones() -> set[str]:
    raw = os.getenv("INSPECTION_WINDOW_MILESTONES", "DELIVERED")
    return {m.strip().upper() for m in raw.split(",") if m.strip()}


async def on_freight_milestone(event: DomainEvent) -> None:
    """A carrier milestone arrived (webhook or the tracking actor): once the
    goods are delivered the buyer's acceptance window for the final tranche
    opens — not when the proof tranches released weeks earlier at sea."""
    if not _enabled():
        return
    deal_id = event.deal_id
    payload = event.payload or {}
    milestone = str(payload.get("milestone") or "").upper()
    if not deal_id or milestone not in _inspection_window_milestones():
        return
    try:
        from services.escrow_guard.repository import get_repository as _eg_repo  # type: ignore[import]
        from services.escrow_guard.service import open_inspection_window  # type: ignore[import]
        hold = await _eg_repo().get_hold_by_deal(deal_id)
        if hold is None:
            return
        await open_inspection_window(hold.escrow_id, reason=f"carrier milestone {milestone}")
    except Exception as exc:  # noqa: BLE001
        logger.error("Continuation[FREIGHT_MILESTONE]: window open failed for %s: %s", deal_id, exc)


async def on_escrow_inspection_opened(event: DomainEvent) -> None:
    """Tell the buyer the acceptance window is open (final tranche + deadline)."""
    if not _enabled():
        return
    deal_id = event.deal_id
    payload = event.payload or {}
    if not deal_id:
        return
    try:
        from services.deals.service import get_deal  # type: ignore[import]
        from services.escrow_guard.repository import get_repository as _eg_repo  # type: ignore[import]
        from shared.orchestration import notifications
        deal = await get_deal(deal_id)
        hold = await _eg_repo().get_hold_by_deal(deal_id)
        if hold is None:
            return
        await notifications.notify_inspection_window_open(
            deal, hold, payload.get("final_amount"), payload.get("deadline"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Continuation[INSPECTION_OPENED]: notice failed for %s: %s", deal_id, exc)


# ---------------------------------------------------------------------------
# ESCROW_SETTLED → supplier balance + settlement notices
# ---------------------------------------------------------------------------

async def on_escrow_settled(event: DomainEvent) -> None:
    """Every tranche released: pay the supplier whatever is still owed (the
    final 10 % that waited on buyer acceptance) and tell both parties the money
    side is closed."""
    if not _enabled():
        return
    deal_id = event.deal_id
    payload = event.payload or {}
    if not deal_id:
        return
    try:
        from services.deals.service import get_deal  # type: ignore[import]
        deal = await get_deal(deal_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Continuation[ESCROW_SETTLED]: deal %s lookup failed: %s", deal_id, exc)
        return
    created = await _best_effort("ESCROW_SETTLED→BALANCE_GATE", deal_id,
                                 _supplier_payout_gate(deal, stage="ESCROW_SETTLED→BALANCE_GATE"))
    from shared.orchestration import notifications
    await _best_effort(
        "ESCROW_SETTLED→NOTIFY", deal_id,
        notifications.notify_deal_settled(deal, payload.get("supplier_balance_due"),
                                          payload.get("currency") or "USD"),
    )
    logger.info("Continuation: escrow settled for deal %s (balance gate created=%s, event %s)",
                deal_id, bool(created), event.event_id)


# ---------------------------------------------------------------------------
# DEAL_EXCEPTION → resolver
# ---------------------------------------------------------------------------

async def on_deal_exception(event: DomainEvent) -> None:
    """DEAL_EXCEPTION → route to the Exception Resolver agent.

    Completes the documented flow (\"On REJECT / callback failure →
    deal.exception emitted; Exception Resolver handles next step\") — until
    this subscriber existed, those events were emitted and never consumed.

    Loop safety: ``raise_exception()`` itself emits DEAL_EXCEPTION lifecycle
    events (OPEN, then the final status) whose payload carries the
    ``exception_id`` of the record it just persisted. Those echoes are
    notifications, not new failures — skip them, or every exception would
    re-trigger the resolver forever.

    Failure safety: this handler must NEVER call ``_emit_exception`` — a
    failure while handling deal.exception must not publish more
    deal.exception events. Log and stop; the OPEN record (when reached) is
    the crash-safe audit trace.
    """
    if not _enabled():
        return
    payload = event.payload or {}
    if payload.get("exception_id"):
        return  # resolver lifecycle echo — already being handled
    if not event.deal_id:
        logger.error("Continuation[DEAL_EXCEPTION]: event without deal_id — skipped")
        return
    try:
        from services.exception_resolver.schemas import RaiseExceptionRequest  # type: ignore[import]
        from services.exception_resolver.service import raise_exception  # type: ignore[import]

        result = await raise_exception(RaiseExceptionRequest(
            deal_id=event.deal_id,
            source_event=str(payload.get("stage") or payload.get("failed_topic") or event.topic.value),
            source_service=str(payload.get("source") or payload.get("source_service") or "event_bus"),
            detail=str(payload.get("error") or payload.get("detail") or ""),
            context=payload,
        ))
        logger.info(
            "Continuation: exception %s (%s) raised for deal %s (event %s)",
            result.exception_id, result.status.value, event.deal_id, event.event_id,
        )
    except Exception:
        logger.exception(
            "Continuation[DEAL_EXCEPTION]: resolver failed for deal %s (event %s) "
            "— manual intervention required",
            event.deal_id, event.event_id,
        )


# ---------------------------------------------------------------------------
# Startup catch-up
# ---------------------------------------------------------------------------

async def resume_parked_negotiations() -> None:
    """Startup catch-up: open a session for any deal already stuck in NEGOTIATING.

    The NEGOTIATION_STARTED event that would trigger ``on_negotiation_started``
    only helps deals that transition AFTER the subscriber is live. A deal that
    was already sitting in NEGOTIATING before this handler existed (or whose
    event fired while nothing was subscribed, or during a restart) never gets a
    session. This one-shot sweep — called once from the app lifespan, mirroring
    ``auto_flow.resume_in_flight`` — replays ``on_negotiation_started`` for every
    such deal. It is fully idempotent: the handler skips any deal that already
    has a non-terminal session, so re-running never double-opens.

    Covers ALL deals, including operator-created ones with no opportunity_id
    (which auto_flow.resume_in_flight deliberately skips).
    """
    if not _enabled():
        return
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        from services.deals.schemas import DealStatus  # type: ignore[import]
        deals = await get_repository().list_active(limit=500, status=DealStatus.NEGOTIATING)
    except Exception:
        logger.exception("Continuation: resume scan for parked NEGOTIATING deals failed")
        return

    resumed = 0
    for deal in deals:
        try:
            # Reuse the handler so the existing-session guard + seed logic apply.
            await on_negotiation_started(DomainEvent(
                topic=EventTopic.NEGOTIATION_STARTED,
                deal_id=deal.deal_id,
                payload={"resumed": True},
            ))
            resumed += 1
        except Exception:
            logger.exception(
                "Continuation: could not resume parked negotiation for deal %s", deal.deal_id
            )
    if resumed:
        logger.info("Continuation: swept %d deal(s) parked in NEGOTIATING at startup", resumed)


def register_continuations(bus) -> None:
    """Subscribe every pipeline continuation on the given bus
    (InProcessEventBus or NatsJetStreamBus — same subscribe surface)."""
    bus.subscribe(EventTopic.OPPORTUNITY_DETECTED, on_opportunity_detected)
    bus.subscribe(EventTopic.DEAL_CREATED, on_deal_created)
    bus.subscribe(EventTopic.QUOTE_RECEIVED, on_quote_received)
    bus.subscribe(EventTopic.NEGOTIATION_STARTED, on_negotiation_started)
    bus.subscribe(EventTopic.NEGOTIATION_FAILED, on_negotiation_failed)
    bus.subscribe(EventTopic.NEGOTIATION_AGREED, on_negotiation_agreed)
    bus.subscribe(EventTopic.PI_SENT, on_pi_sent)
    bus.subscribe(EventTopic.PI_ACCEPTED, on_pi_accepted)
    bus.subscribe(EventTopic.ESCROW_FUNDED, on_escrow_funded)
    bus.subscribe(EventTopic.PO_ISSUED, on_po_issued)
    bus.subscribe(EventTopic.QC_BOOKED, on_qc_booked)
    bus.subscribe(EventTopic.QC_PASSED, on_qc_passed)
    bus.subscribe(EventTopic.QC_FAILED, on_qc_failed)
    bus.subscribe(EventTopic.FREIGHT_BOOKED, on_freight_booked)
    bus.subscribe(EventTopic.CUSTOMS_CLEARED, on_customs_cleared)
    bus.subscribe(EventTopic.FREIGHT_MILESTONE, on_freight_milestone)
    bus.subscribe(EventTopic.ESCROW_INSPECTION_OPENED, on_escrow_inspection_opened)
    bus.subscribe(EventTopic.ESCROW_TRANCHE_RELEASED, on_escrow_tranche_released)
    bus.subscribe(EventTopic.ESCROW_SETTLED, on_escrow_settled)
    bus.subscribe(EventTopic.PAYOUT_COMPLETED, on_payout_completed)
    bus.subscribe(EventTopic.DEAL_EXCEPTION, on_deal_exception)
    logger.info("Pipeline continuations registered (EVENT_CONTINUATION_ENABLED=%s)", _enabled())
