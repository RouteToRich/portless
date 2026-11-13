"""Business logic for the Deals service (LLD 4.2).

Orchestration
-------------
``create_deal`` is the entry point for a new deal.  When ``opportunity_id``
is supplied the service performs a three-step orchestration entirely within
the same process (combined monolith) or across HTTP (microservice):

  1. Fetch ``MatchEngineSeed`` from Trade-Min
     → ``services.trade_min.service.get_match_seed()``
  2. Score all top-seller candidates via Match Engine (concurrent)
     → ``services.match_engine.service.score_match()``
  3. Pick the highest-scoring SOURCE candidate as the winning supplier
  4. Emit ``deal.created`` and mark the opportunity CONVERTED

``update_status`` enforces the deal state machine topology from
``shared.domain.state_machine``.  The deterministic business guards
(``margin_floor_ok``, ``dual_doc_match_ok``) are implemented there but are
applied by the caller at the point the financial / document inputs exist
(e.g. the margin floor at PO issuance in DealMaker), not inside this
topology-only transition check.

Exceptions
----------
``DealNotFoundError`` — 404 in the router
``InvalidTransitionError`` — 422 in the router
All inter-service calls are wrapped in try/except; failures are logged as
WARNING and the deal continues without that data (graceful degradation).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from shared.bus import get_publisher
from shared.domain.events import DomainEvent, EventTopic
from shared.domain.state_machine import can_transition

from shared.domain.models import Party

from .repository import get_repository, init_repository
from .schemas import (
    BuyerCandidate,
    CreateDealRequest,
    DealRecord,
    DealStatus,
    MatchResult,
    StatusHistoryEntry,
    UpdateFinancialsRequest,
    UpdateStatusRequest,
)

logger = logging.getLogger(__name__)

# Applied when the caller doesn't specify one — matches NegotiationPolicy.
# margin_floor_pct and the DealMaker PO margin guard, so the Deal Board's
# "Margin Floor" card reflects the floor actually enforced downstream.
_DEFAULT_MARGIN_FLOOR_PCT = 8.0

# ---------------------------------------------------------------------------
# Status → event topic mapping (only statuses that produce domain events)
# ---------------------------------------------------------------------------

_STATUS_TO_EVENT: dict[DealStatus, EventTopic] = {
    DealStatus.NEGOTIATING: EventTopic.NEGOTIATION_STARTED,
    DealStatus.NEGOTIATION_AGREED: EventTopic.NEGOTIATION_AGREED,
    DealStatus.NEGOTIATION_FAILED: EventTopic.NEGOTIATION_FAILED,
    DealStatus.PI_SENT: EventTopic.PI_SENT,
    DealStatus.PI_ACCEPTED: EventTopic.PI_ACCEPTED,
    DealStatus.ESCROW_FUNDED: EventTopic.ESCROW_FUNDED,
    DealStatus.PO_ISSUED: EventTopic.PO_ISSUED,
    DealStatus.QC_PASSED: EventTopic.QC_PASSED,
    DealStatus.QC_FAILED: EventTopic.QC_FAILED,
    DealStatus.CLEARED: EventTopic.CUSTOMS_CLEARED,
    DealStatus.PAID: EventTopic.PAYOUT_COMPLETED,
}


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class DealNotFoundError(Exception):
    def __init__(self, deal_id: str) -> None:
        super().__init__(f"Deal {deal_id!r} not found")
        self.deal_id = deal_id


class InvalidTransitionError(Exception):
    def __init__(self, current: DealStatus, target: DealStatus) -> None:
        super().__init__(
            f"Cannot transition deal from {current.value!r} to {target.value!r}"
        )
        self.current = current
        self.target = target


# ---------------------------------------------------------------------------
# Internal helpers — inter-service calls (direct function calls in monolith)
# ---------------------------------------------------------------------------

async def _fetch_match_seed(opportunity_id: str) -> dict[str, Any] | None:
    """Fetch pre-seeded Match Engine inputs from Trade-Min.

    Uses a direct in-process import so the combined monolith avoids the
    overhead of an HTTP round-trip to itself.  In a microservice deployment
    swap this for ``TradeMinClient().get_match_seed(opportunity_id)``.
    """
    try:
        from services.trade_min.service import get_match_seed  # type: ignore[import]
        seed = await get_match_seed(opportunity_id)
        return seed.model_dump() if seed is not None else None
    except Exception as exc:
        logger.warning(
            "Could not fetch match seed for opportunity %s: %s", opportunity_id, exc
        )
        return None


async def _mark_opportunity_converted(opportunity_id: str) -> None:
    """Mark the Trade-Min opportunity CONVERTED (best-effort, non-blocking)."""
    try:
        from services.trade_min.service import mark_opportunity_converted  # type: ignore[import]
        await mark_opportunity_converted(opportunity_id)
    except Exception as exc:
        logger.warning(
            "Could not mark opportunity %s as CONVERTED: %s", opportunity_id, exc
        )


async def _score_seller(
    deal_id: str,
    seller: dict[str, Any],
    buyer_target_price: float,
) -> MatchResult | None:
    """Score one seller candidate via Match Engine.

    The gatekeeper flags come from the opportunity's scan-time check
    (``seller["validation"]``, trade_min/validation.py): a registry that answered
    "inactive" / "denied", or a failed check (e.g. sanctions block), fails the
    gatekeeper → DISCARD. A check that has not run yet is scored provisionally
    (gatekeeper pass) and the result says ``verification="unverified"`` — never
    "verified"; the real GSTN / DGFT checks still run at quote intake (Sourcing),
    which re-scores with ``update_financials``.
    """
    fob = seller.get("fob_price_usd")
    if fob is None:
        return None
    check = seller.get("validation") or {}
    status = check.get("status") or "unverified"
    failed = status == "failed"
    try:
        from services.match_engine.service import score_match  # type: ignore[import]
        from services.match_engine.schemas import ScoreRequest, SupplierVerification  # type: ignore[import]

        req = ScoreRequest(
            deal_id=deal_id,
            supplier_id=seller["supplier_id"],
            buyer_target_price=buyer_target_price,
            supplier_quote_price=float(fob),
            currency="USD",
            verification=SupplierVerification(
                gst_verified=not failed and check.get("gst") != "inactive",
                iec_active=not failed and check.get("iec") != "inactive",
                dgft_denied=check.get("dgft_denied") == "denied",
            ),
            supplier_to_port_km=float(seller.get("supplier_to_port_km") or 300.0),
            business_age_years=0.0,
            udyam_registered=bool(seller.get("udyam")),
            annual_shipment_count=int(seller.get("export_shipments_12m") or 0),
        )
        resp = await score_match(req)
        return MatchResult(
            match_id=resp.match_id or f"MATCH-{uuid4().hex[:12].upper()}",
            supplier_id=resp.supplier_id,
            company_name=seller.get("company_name", ""),
            composite_score=resp.composite_score,
            gatekeeper_passed=resp.gatekeeper_passed,
            arbitrage_score=resp.arbitrage_score,
            proximity_score=resp.proximity_score,
            reliability_score=resp.reliability_score,
            decision=resp.decision,
            fob_price_usd=float(fob),
            supplier_to_port_km=seller.get("supplier_to_port_km"),
            export_shipments_12m=(int(seller["export_shipments_12m"]) if seller.get("export_shipments_12m") else None),
            verification=status if status in ("verified", "unverified", "failed") else "unverified",
            verification_reasons=list(check.get("reasons") or [])[:10],
        )
    except Exception as exc:
        logger.warning(
            "Match Engine scoring failed for seller %s in deal %s: %s",
            seller.get("supplier_id"),
            deal_id,
            exc,
        )
        return None


async def _emit_event(
    topic: EventTopic,
    deal_id: str,
    payload: dict[str, Any],
) -> None:
    """Emit a domain event; swallows errors so the calling flow is never blocked."""
    try:
        await get_publisher().publish(
            DomainEvent(topic=topic, deal_id=deal_id, payload=payload)
        )
    except Exception as exc:
        logger.warning("Failed to emit %s for deal %s: %s", topic.value, deal_id, exc)


def _new_deal_id() -> str:
    return f"DEAL-{uuid4().hex[:12].upper()}"


# ---------------------------------------------------------------------------
# Public service API
# ---------------------------------------------------------------------------

def _party_check_fields(check: Any, email: str | None) -> dict[str, Any]:
    """What a Party may say about its checks when seeded from an opportunity:
    exactly what the scan-time check found, never more. No check → unverified /
    unchecked, so the enrichment and compliance legs run in full."""
    if check is None:
        return {"email_status": "unverified" if email else None,
                "sanctions_status": "unchecked", "verification_status": "unverified"}
    sanctions = getattr(check, "sanctions", None) or "unchecked"
    email_word = getattr(check, "email", None)
    return {
        "email_status": (email_word if email_word in ("ok", "risky", "invalid") else
                         ("unverified" if email else None)),
        "sanctions_status": sanctions,
        "sanctions_reason": getattr(check, "sanctions_reason", None),
        "sanctions_checked_at": getattr(check, "checked_at", None) if sanctions != "unchecked" else None,
        # "scan_verified" only on a positive check; anything else stays "unverified"
        # so the enrichment leg can still record a registry match.
        "verification_status": "scan_verified" if getattr(check, "status", None) == "verified" else "unverified",
    }


def _seed_parties_from_opportunity(
    opportunity: Any,
    winning_supplier_id: str | None,
    market: str,
    *,
    buyer_funnel: bool = False,
) -> list[Party]:
    """Initial buyer/supplier Party entries — only for counterparties that are
    actually decided.

    A deal created from an opportunity starts as a *prospect* (MATCHING): the
    buyers are candidates the outreach funnel works through and the suppliers
    get an RFQ. Naming one of them "the buyer" / "the supplier" before anyone
    said yes made the prospect read as a closed deal (and an interested reply
    from candidate #2 inherited candidate #1's screening). So:

    - buyer — only when there is no outreach funnel (``buyer_funnel=False``);
      otherwise the funnel sets it when a candidate replies "interested";
    - supplier — only the Match Engine's SOURCE winner; otherwise it is set when
      a verified quote wins or an operator confirms one (``set_supplier_party``).
    """
    parties: list[Party] = []
    try:
        signals = list(getattr(opportunity, "buyer_signals", None) or [])
        if signals and not buyer_funnel:
            # Prefer the buyer the actors scored highest / that carries a
            # contact — a named importer with a website beats an anonymous RFQ.
            def _rank(b: Any) -> tuple:
                has_contact = bool(getattr(b, "email", None) or getattr(b, "website", None)
                                   or getattr(b, "contact_url", None))
                return (-(getattr(b, "fit_score", None) or 0), not has_contact,
                        -(getattr(b, "urgency_tier", None) or 0))
            top = sorted(signals, key=_rank)[0]
            email = getattr(top, "email", None)
            parties.append(Party(
                party_id=getattr(top, "company_id", None),
                role="buyer",
                legal_name=top.buyer_name,
                country=top.buyer_country or market,
                risk_score=getattr(top, "risk_score", None),
                email=email,
                website=getattr(top, "website", None),
                phone=getattr(top, "phone", None),
                address=getattr(top, "address", None),
                contact_source=(f"opportunity:{getattr(top, 'source', '')}"
                                if (email or getattr(top, "website", None)) else None),
                **_party_check_fields(getattr(top, "validation", None), email),
            ))
        candidates = getattr(opportunity, "seller_candidates", None) or []
        winner = next(
            (c for c in candidates if winning_supplier_id and c.candidate_id == winning_supplier_id),
            None,
        )
        if winner is not None:
            s_email = getattr(winner, "email", None)
            parties.append(Party(
                party_id=winner.candidate_id,
                role="supplier",
                legal_name=winner.company_name,
                country="IN",
                gstin=getattr(winner, "gstin", None),
                iec=getattr(winner, "iec", None),
                email=s_email,
                website=getattr(winner, "website", None),
                phone=getattr(winner, "phone", None),
                contact_source=(f"opportunity:{getattr(winner, 'source', '')}"
                                if (s_email or getattr(winner, "website", None)) else None),
                **_party_check_fields(getattr(winner, "validation", None), s_email),
            ))
    except Exception as exc:  # noqa: BLE001 — party seeding must never block creation
        logger.warning("Could not seed parties from opportunity: %s", exc)
    return parties


def _quantity_to_mt(quantity: Any, unit: Any) -> float | None:
    """Buyer-stated quantity → metric tons (None when the unit is not a weight)."""
    try:
        q = float(quantity)
    except (TypeError, ValueError):
        return None
    if q <= 0:
        return None
    u = str(unit or "").strip().lower()
    if u in ("mt", "ton", "tons", "tonne", "tonnes", "metric ton", "metric tons", "t"):
        return round(q, 3)
    if u in ("kg", "kgs", "kilogram", "kilograms"):
        return round(q / 1000.0, 3)
    if u in ("lb", "lbs", "pound", "pounds"):
        return round(q * 0.000453592, 3)
    return None


def _buyer_candidates_from_seed(seed: dict[str, Any]) -> list[BuyerCandidate]:
    """The outreach funnel's starting list: the seed's top buyers, best fit first."""
    out: list[BuyerCandidate] = []
    seen: set[str] = set()
    for i, b in enumerate(seed.get("top_buyers") or []):
        name = (b.get("buyer_name") or "").strip()
        if not name or (b.get("validation") or {}).get("status") == "failed":
            continue  # a buyer the scan-time check failed is never contacted
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(BuyerCandidate(
            candidate_id=str(b.get("company_id") or f"BUYER-{i + 1}"),
            legal_name=name,
            country=b.get("buyer_country") or seed.get("market") or "",
            email=b.get("email"),
            website=b.get("website"),
            phone=b.get("phone"),
            source=b.get("source"),
            fit_score=b.get("fit_score"),
            pitch_angle=b.get("pitch_angle"),
            quantity_mt=_quantity_to_mt(b.get("quantity"), b.get("quantity_unit")),
            validation_status=(b.get("validation") or {}).get("status") or "unverified",
        ))
    return out


async def _rescore_opportunity(opportunity_id: str) -> None:
    """Pre-deal rescore (trade_min/rescore.py) unless it ran in the last hour — the
    deal then seeds from the checked counterparties, the supplier's plant-to-port
    distance and GSTIN found on its website. Best-effort: never blocks creation.
    ``DEAL_RESCORE_BEFORE_CREATE=false`` switches it off."""
    import os
    if os.environ.get("DEAL_RESCORE_BEFORE_CREATE", "true").strip().lower() in ("0", "false", "no", "off"):
        return
    try:
        from services.trade_min import rescore as trade_rescore  # type: ignore[import]
        from services.trade_min.service import get_opportunity  # type: ignore[import]
        opp = await get_opportunity(opportunity_id)
        if opp is None or trade_rescore.recently_rescored(opp):
            return
        await trade_rescore.rescore(opportunity_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pre-deal rescore of %s failed: %s", opportunity_id, exc)


async def _fetch_opportunity(opportunity_id: str) -> Any | None:
    """Best-effort fetch of the full EnrichedOpportunity (for party seeding)."""
    try:
        from services.trade_min.service import get_opportunity  # type: ignore[import]
        return await get_opportunity(opportunity_id)
    except Exception as exc:
        logger.warning("Could not fetch opportunity %s: %s", opportunity_id, exc)
        return None


async def create_deal(payload: CreateDealRequest, org_id: str) -> DealRecord:
    """Create a new deal, optionally orchestrating Match Engine scoring.

    ``org_id`` ties the deal to the caller's organization (stamped by the
    router from the authenticated user, never client-supplied).

    When ``opportunity_id`` is provided:
      • Fetches the Trade-Min MatchEngineSeed
      • Scores all top-seller candidates via Match Engine (max 3, concurrent)
      • Sets status = MATCHING if ≥1 SOURCE decision, otherwise DRAFT
      • Marks the opportunity CONVERTED after emitting deal.created

    Always:
      • Persists the deal to MongoDB (or in-memory fallback)
      • Emits ``deal.created``
    """
    now = datetime.now(timezone.utc)
    deal_id = _new_deal_id()

    match_results: list[MatchResult] = []
    winning_supplier_id: str | None = None
    winning_cms: float | None = None
    buyer_target_price: float | None = None
    buyer_price_source: str | None = None
    quantity_mt: float | None = None
    buyer_candidates: list[BuyerCandidate] = []
    initial_status = DealStatus.DRAFT

    if payload.opportunity_id:
        await _rescore_opportunity(payload.opportunity_id)
        seed = await _fetch_match_seed(payload.opportunity_id)
        if seed is not None:
            buyer_target_price = seed.get("buyer_target_price_usd")
            buyer_price_source = seed.get("buyer_price_source")
            buyer_candidates = _buyer_candidates_from_seed(seed)
            quantity_mt = next((c.quantity_mt for c in buyer_candidates if c.quantity_mt), None)
            initial_status = DealStatus.MATCHING  # always move to MATCHING once seeded

            if buyer_target_price:
                top_sellers: list[dict[str, Any]] = seed.get("top_sellers", [])[:3]
                score_coros = [
                    _score_seller(deal_id, s, buyer_target_price)
                    for s in top_sellers
                    if s.get("fob_price_usd") is not None
                ]
                raw_results = await asyncio.gather(*score_coros, return_exceptions=True)
                for r in raw_results:
                    if isinstance(r, BaseException):
                        logger.error("Uncaught scoring error for deal %s: %s", deal_id, r)
                    elif r is not None:
                        match_results.append(r)

            # Pick best SOURCE candidate
            source_results = [mr for mr in match_results if mr.decision == "SOURCE"]
            if source_results:
                best = max(source_results, key=lambda mr: mr.composite_score)
                winning_supplier_id = best.supplier_id
                winning_cms = best.composite_score

    # Seed parties from the opportunity when the caller didn't supply any —
    # otherwise the deal's Overview tab has no buyer/supplier for its lifetime.
    parties = payload.parties
    if not parties and payload.opportunity_id:
        opportunity = await _fetch_opportunity(payload.opportunity_id)
        if opportunity is not None:
            from shared.orchestration import outreach
            parties = _seed_parties_from_opportunity(
                opportunity, winning_supplier_id, payload.market,
                buyer_funnel=outreach.enabled() and any(c.email for c in buyer_candidates),
            )

    deal = DealRecord(
        deal_id=deal_id,
        operator_id=payload.operator_id,
        org_id=org_id,
        hs_code=payload.hs_code,
        market=payload.market,
        status=initial_status,
        autonomy_profile=payload.autonomy_profile,
        opportunity_id=payload.opportunity_id,
        match_results=match_results,
        winning_supplier_id=winning_supplier_id,
        winning_cms=winning_cms,
        buyer_target_price_usd=buyer_target_price,
        buyer_price_source=buyer_price_source,
        quantity_mt=quantity_mt,
        buyer_candidates=buyer_candidates,
        margin_floor_pct=(
            payload.margin_floor_pct
            if payload.margin_floor_pct is not None
            else _DEFAULT_MARGIN_FLOOR_PCT
        ),
        parties=parties,
        status_history=[
            StatusHistoryEntry(
                status=initial_status,
                transitioned_at=now,
                reason="Initial deal creation",
            )
        ],
        created_at=now,
        updated_at=now,
    )

    repo = get_repository()
    await repo.save(deal)

    if initial_status == DealStatus.MATCHING:
        try:
            from shared.orchestration.auto_flow import start as start_auto_flow
            start_auto_flow(deal_id)
        except Exception as exc:
            logger.warning("Could not start auto-flow for deal %s: %s", deal_id, exc)

    await _emit_event(
        EventTopic.DEAL_CREATED,
        deal_id=deal_id,
        payload={
            "operator_id": payload.operator_id,
            "hs_code": payload.hs_code,
            "market": payload.market,
            "status": initial_status.value,
            "opportunity_id": payload.opportunity_id,
            "winning_supplier_id": winning_supplier_id,
            "winning_cms": winning_cms,
            "match_results_count": len(match_results),
        },
    )

    # Convert the originating opportunity after the event is emitted (best-effort).
    if payload.opportunity_id:
        await _mark_opportunity_converted(payload.opportunity_id)

    return deal


async def get_deal(deal_id: str) -> DealRecord:
    """Fetch a deal by ID.  Raises ``DealNotFoundError`` when absent."""
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        raise DealNotFoundError(deal_id)
    return deal


async def list_deals(
    limit: int = 100,
    status: DealStatus | None = None,
    operator_id: str | None = None,
    org_id: str | None = None,
) -> list[DealRecord]:
    """List deals, sorted by creation time descending.

    ``org_id=None`` means unrestricted (Portless platform staff); callers that
    need per-org isolation must pass the caller's org explicitly.
    """
    repo = get_repository()
    return await repo.list_active(limit=limit, status=status, operator_id=operator_id, org_id=org_id)


async def update_status(deal_id: str, payload: UpdateStatusRequest) -> DealRecord:
    """Transition a deal's status through the state machine.

    Topology check (``can_transition``) is always enforced.

    The deterministic business guards (``margin_floor_ok``, ``dual_doc_match_ok``)
    are implemented in ``shared.domain.state_machine`` but are evaluated by the
    caller that holds the financial / document inputs (e.g. DealMaker enforces the
    margin floor at PO issuance), not inside this topology-only check.
    """
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        raise DealNotFoundError(deal_id)

    if not can_transition(deal.status, payload.target_status):
        raise InvalidTransitionError(deal.status, payload.target_status)

    now = datetime.now(timezone.utc)
    entry = StatusHistoryEntry(
        status=payload.target_status,
        transitioned_at=now,
        reason=payload.reason,
    )

    # Persist via mark_status (atomic update) then reload for response
    updated = await repo.mark_status(deal_id, entry)
    if not updated:
        raise DealNotFoundError(deal_id)

    deal = await repo.get(deal_id)
    if deal is None:
        raise DealNotFoundError(deal_id)

    # Keep the inspection status line in step with the deal (PO → QC → after).
    from shared.orchestration import qc_status
    if await qc_status.sync(deal_id) != deal.qc_status:
        deal = await repo.get(deal_id) or deal

    event_topic = _STATUS_TO_EVENT.get(payload.target_status)
    if event_topic:
        await _emit_event(
            event_topic,
            deal_id=deal_id,
            payload={
                "status": payload.target_status.value,
                "reason": payload.reason,
                "winning_supplier_id": deal.winning_supplier_id,
            },
        )

    await _learn_from_status(deal, payload.target_status.value, payload.reason)
    return deal


_SUMMARY_EVENTS = {"NEGOTIATING": "match confirmed", "PI_ACCEPTED": "contract signed (PI accepted)",
                   "PAID": "completed", "CANCELLED": "cancelled", "REFUNDED": "refunded",
                   "NEGOTIATION_FAILED": "negotiation failed", "DISPUTE": "dispute opened"}


async def _learn_from_status(deal: DealRecord, status: str, reason: str | None) -> None:
    """Feedback loop: the match outcome follows the deal, and milestones become
    deal summaries in knowledge-service. Never blocks the transition."""
    try:
        from shared.orchestration import outcomes
        await outcomes.on_status(deal, status, reason)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Outcome tracking skipped for %s: %s", deal.deal_id, exc)
    if status in _SUMMARY_EVENTS and (status != "NEGOTIATING" or deal.selected_pair):
        try:
            from shared.orchestration.memory import remember_deal_summary
            await remember_deal_summary(deal, _SUMMARY_EVENTS[status], reason)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Deal summary skipped for %s: %s", deal.deal_id, exc)


async def update_financials(deal_id: str, payload: UpdateFinancialsRequest) -> DealRecord:
    """Patch financial fields after Sourcing / Negotiation phases update them.

    Called by the Negotiation Agent when a price is agreed and by the Sourcing
    service when regulatory verification results change the CMS.
    """
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        raise DealNotFoundError(deal_id)

    updates: dict = {"updated_at": datetime.now(timezone.utc)}
    if payload.est_margin_usd is not None:
        updates["est_margin_usd"] = payload.est_margin_usd
    if payload.realized_margin_usd is not None:
        updates["realized_margin_usd"] = payload.realized_margin_usd
    if payload.est_margin_pct is not None:
        updates["est_margin_pct"] = payload.est_margin_pct
    if payload.realized_margin_pct is not None:
        updates["realized_margin_pct"] = payload.realized_margin_pct
    if payload.buyer_target_price_usd is not None:
        updates["buyer_target_price_usd"] = payload.buyer_target_price_usd
    if payload.winning_supplier_id is not None:
        updates["winning_supplier_id"] = payload.winning_supplier_id
    if payload.winning_cms is not None:
        updates["winning_cms"] = payload.winning_cms

    updated_deal = deal.model_copy(update=updates)
    await repo.save(updated_deal)
    return updated_deal


async def record_match_result(
    deal_id: str,
    supplier_id: str,
    company_name: str = "",
    fob_price_usd: float | None = None,
    composite_score: float | None = None,
    match_id: str | None = None,
    buyer_target_price_usd: float | None = None,
    make_winner: bool = False,
) -> DealRecord:
    """Upsert a supplier into ``match_results`` and optionally make it the winner.

    Used by the CONFIRM_SUPPLIER_SELECTION saga callback: a supplier that
    quoted through sourcing (or a next-best candidate proposed after a failed
    negotiation) may not be in the Match Engine results yet, and the
    negotiation seed reads the supplier's ask from the winning MatchResult —
    so it has to be there before the deal moves to NEGOTIATING.
    """
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        raise DealNotFoundError(deal_id)

    results = list(deal.match_results)
    idx = next((i for i, m in enumerate(results) if m.supplier_id == supplier_id), None)
    if idx is None:
        results.append(MatchResult(
            match_id=match_id or f"MATCH-{uuid4().hex[:12].upper()}",
            supplier_id=supplier_id,
            company_name=company_name,
            composite_score=composite_score if composite_score is not None else 0.0,
            gatekeeper_passed=True,
            arbitrage_score=0.0,
            proximity_score=0.0,
            reliability_score=0.0,
            decision="SOURCE" if make_winner else "DISCARD",
            fob_price_usd=fob_price_usd,
        ))
    else:
        current = results[idx]
        results[idx] = current.model_copy(update={
            "company_name": company_name or current.company_name,
            "fob_price_usd": fob_price_usd if fob_price_usd is not None else current.fob_price_usd,
            "composite_score": (
                composite_score if composite_score is not None else current.composite_score
            ),
            "decision": "SOURCE" if make_winner else current.decision,
        })

    updates: dict[str, Any] = {"match_results": results, "updated_at": datetime.now(timezone.utc)}
    if make_winner:
        updates["winning_supplier_id"] = supplier_id
        if composite_score is not None:
            updates["winning_cms"] = composite_score
    if buyer_target_price_usd is not None and deal.buyer_target_price_usd is None:
        updates["buyer_target_price_usd"] = buyer_target_price_usd

    updated_deal = deal.model_copy(update=updates)
    await repo.save(updated_deal)
    logger.info(
        "Deal %s: match result recorded for supplier %s (winner=%s, fob=%s)",
        deal_id, supplier_id, make_winner, fob_price_usd,
    )
    if make_winner:
        updated_deal = await set_supplier_party(deal_id, supplier_id, company_name=company_name)
    return updated_deal


async def _supplier_contact(deal: DealRecord, supplier_id: str) -> dict[str, Any]:
    """What PortLess knows about a supplier: the address its RFQ went to, and the
    name / website / phone the scan found (opportunity seller candidates)."""
    info: dict[str, Any] = {}
    try:
        from services.sourcing.service import list_rfqs  # type: ignore[import]
        for rfq in await list_rfqs(deal.deal_id):
            for d in rfq.dispatch_results:
                if d.supplier_id == supplier_id and d.email:
                    info.setdefault("email", d.email)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Deal %s: RFQ lookup for supplier %s failed: %s", deal.deal_id, supplier_id, exc)
    if deal.opportunity_id:
        try:
            opp = await _fetch_opportunity(deal.opportunity_id)
            for c in getattr(opp, "seller_candidates", None) or []:
                if c.candidate_id == supplier_id:
                    info.setdefault("legal_name", c.company_name)
                    info.setdefault("email", getattr(c, "email", None))
                    info.setdefault("website", getattr(c, "website", None))
                    info.setdefault("phone", getattr(c, "phone", None))
                    info.setdefault("gstin", getattr(c, "gstin", None))
                    info.setdefault("iec", getattr(c, "iec", None))
                    info.setdefault("country", getattr(c, "country", None))
                    info.setdefault("source", getattr(c, "source", None))
                    # A scan knows the supplier's town, not its street: still the
                    # place the inspection is booked at until the supplier corrects it.
                    place = ", ".join(filter(None, (getattr(c, "location_city", None), getattr(c, "location_state", None))))
                    if place:
                        info.setdefault("address", place)
                    break
        except Exception as exc:  # noqa: BLE001
            logger.debug("Deal %s: opportunity lookup for supplier %s failed: %s", deal.deal_id, supplier_id, exc)
    return {k: v for k, v in info.items() if v}


async def set_supplier_party(
    deal_id: str,
    supplier_id: str,
    *,
    company_name: str | None = None,
    email: str | None = None,
    gstin: str | None = None,
    iec: str | None = None,
) -> DealRecord:
    """The chosen supplier becomes the deal's supplier party.

    Called when a verified quote wins (sourcing) or an operator confirms a
    supplier (CONFIRM_SUPPLIER_SELECTION). A different company than the current
    party replaces it outright (fresh screening); the same company is updated
    in place. An operator-entered supplier is never replaced by an automated pick.
    A newly named supplier is screened (sanctions / contact) right away.
    """
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        raise DealNotFoundError(deal_id)
    current = next((p for p in deal.parties
                    if (getattr(p.role, "value", p.role) or "").lower() == "supplier"), None)
    if current is not None and current.party_id == supplier_id:
        same = True
    elif current is not None and current.contact_source == "operator":
        logger.info("Deal %s: supplier %s chosen, operator-set supplier party kept", deal_id, supplier_id)
        return deal
    else:
        same = False

    info = await _supplier_contact(deal, supplier_id)
    source = info.pop("source", None)
    updates: dict[str, Any] = {
        **info,
        "party_id": supplier_id,
        "legal_name": company_name or info.get("legal_name") or supplier_id,
        "country": info.get("country") or "IN",
    }
    if email:
        updates["email"] = email
    if gstin:
        updates["gstin"] = gstin
    if iec:
        updates["iec"] = iec
    if not same:
        updates.update({
            "contact_source": f"sourcing:{source or 'rfq'}" if updates.get("email") else None,
            "email_status": "unverified" if updates.get("email") else None,
            "sanctions_status": "unchecked",
        })
    updated = await update_party(deal_id, "supplier", updates, source=None, replace=not same)
    logger.info("Deal %s: supplier party is now %s (%s)", deal_id, updates["legal_name"], supplier_id)

    if not same:
        try:
            from shared.orchestration.continuations import _enrichment_enabled
            if _enrichment_enabled():
                from shared.orchestration import enrichment
                result = await enrichment.enrich_deal_parties(deal_id, roles=("supplier",))
                updated = result.get("deal") or updated
        except Exception as exc:  # noqa: BLE001 — screening is best-effort here; the PO gate shows it
            logger.warning("Deal %s: supplier screening after selection failed: %s", deal_id, exc)
    return updated


async def update_party(
    deal_id: str,
    role: str,
    updates: dict[str, Any],
    *,
    source: str | None = "operator",
    replace: bool = False,
) -> DealRecord:
    """Patch (or create) the deal's buyer/supplier party.

    ``source="operator"`` (the HTTP endpoint) stamps ``contact_source`` so a
    later actor run never overwrites a human correction; the enrichment module
    passes ``source=None`` and sets ``contact_source`` itself. ``replace=True``
    swaps in a fresh party built from ``updates`` — a different company, so
    nothing (screening verdict, VAT, phone…) carries over from the old one.
    """
    role = (role or "").lower()
    if role not in ("buyer", "supplier"):
        raise ValueError(f"role must be buyer or supplier, got {role!r}")
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        raise DealNotFoundError(deal_id)

    clean = {k: v for k, v in updates.items() if v is not None}
    contact_keys = {"email", "website", "phone", "address"}
    if source == "operator" and contact_keys & set(clean):
        clean["contact_source"] = "operator"
        if "email" in clean:
            clean.setdefault("email_status", "unverified")
            clean["contact_confidence"] = 1.0

    parties = list(deal.parties)
    idx = next((i for i, p in enumerate(parties)
                if (getattr(p.role, "value", p.role) or "").lower() == role), None)
    if idx is not None and replace:
        parties.pop(idx)
        idx = None
    if idx is None:
        legal_name = clean.pop("legal_name", None) or f"Unknown {role}"
        country = clean.pop("country", None) or (deal.market if role == "buyer" else "IN")
        parties.append(Party(role=role, legal_name=legal_name, country=country, **clean))
    else:
        parties[idx] = parties[idx].model_copy(update=clean)

    updated = deal.model_copy(update={"parties": parties, "updated_at": datetime.now(timezone.utc)})
    await repo.save(updated)
    logger.info("Deal %s: %s party updated (%s)", deal_id, role, ", ".join(sorted(clean)))
    return updated


async def update_buyer_candidate(deal_id: str, candidate_id: str, updates: dict[str, Any]) -> DealRecord:
    """Patch one buyer candidate of the outreach funnel (status, timestamps, reply)."""
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        raise DealNotFoundError(deal_id)
    cands = list(deal.buyer_candidates)
    idx = next((i for i, c in enumerate(cands) if c.candidate_id == candidate_id), None)
    if idx is None:
        raise ValueError(f"buyer candidate {candidate_id!r} not on deal {deal_id}")
    cands[idx] = cands[idx].model_copy(update={k: v for k, v in updates.items() if v is not None or k == "reply_preview"})
    updated = deal.model_copy(update={"buyer_candidates": cands, "updated_at": datetime.now(timezone.utc)})
    await repo.save(updated)
    return updated


async def upsert_supplier_offer(deal_id: str, offer: Any) -> DealRecord:
    """Add or replace (same supplier) a supplier offer on the prospect's match board."""
    from .schemas import SupplierOffer
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        raise DealNotFoundError(deal_id)
    offer = offer if isinstance(offer, SupplierOffer) else SupplierOffer(**offer)
    if offer.origin_deal_id is None:
        offer = offer.model_copy(update={"origin_deal_id": deal_id})
    if offer.received_at is None:
        offer = offer.model_copy(update={"received_at": datetime.now(timezone.utc)})
    offers = [o for o in deal.supplier_offers if o.supplier_id != offer.supplier_id] + [offer]
    updated = deal.model_copy(update={"supplier_offers": offers, "updated_at": datetime.now(timezone.utc)})
    await repo.save(updated)
    logger.info("Deal %s: supplier offer %s from %s (FOB %s)", deal_id, offer.offer_id, offer.supplier_id,
                offer.fob_price_usd_per_mt)
    return updated


async def set_buyer_requirement(deal_id: str, candidate_id: str, requirement: Any,
                                *, source: str = "operator") -> DealRecord:
    """Operator sets / corrects what a buyer wants (stamped ``source=operator``)."""
    from .schemas import BuyerRequirement
    req = requirement if isinstance(requirement, BuyerRequirement) else BuyerRequirement(**requirement)
    updates: dict[str, Any] = {"source": source, "updated_at": datetime.now(timezone.utc)}
    if req.target_price_usd_per_mt and not req.target_price_basis:
        updates["target_price_basis"] = "stated"
    return await update_buyer_candidate(deal_id, candidate_id, {"requirement": req.model_copy(update=updates)})


async def set_selected_pair(deal_id: str, pair: dict[str, Any], *, quantity_mt: float | None = None,
                            buyer_price_source: str | None = None) -> DealRecord:
    """Record the buyer × supplier pair confirmed at CONFIRM_MATCH."""
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        raise DealNotFoundError(deal_id)
    updates: dict[str, Any] = {"selected_pair": pair, "updated_at": datetime.now(timezone.utc)}
    if quantity_mt:
        updates["quantity_mt"] = quantity_mt
    if buyer_price_source:
        updates["buyer_price_source"] = buyer_price_source
    updated = deal.model_copy(update=updates)
    await repo.save(updated)
    return updated


async def add_buyer_candidate(deal_id: str, candidate: BuyerCandidate) -> DealRecord:
    """Operator adds a buyer to the funnel (e.g. from their own list)."""
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        raise DealNotFoundError(deal_id)
    if any(c.candidate_id == candidate.candidate_id for c in deal.buyer_candidates):
        raise ValueError(f"buyer candidate {candidate.candidate_id!r} already on deal {deal_id}")
    updated = deal.model_copy(update={
        "buyer_candidates": list(deal.buyer_candidates) + [candidate],
        "updated_at": datetime.now(timezone.utc),
    })
    await repo.save(updated)
    return updated


async def enrich_deal(deal_id: str, *, screen: bool = True, contacts: bool = True) -> dict[str, Any]:
    """Run the Apify contact finder + sanctions screening on the deal's parties
    (see ``shared/orchestration/enrichment.py``)."""
    await get_deal(deal_id)  # 404 before any actor run is started
    from shared.orchestration.enrichment import enrich_deal_parties
    return await enrich_deal_parties(deal_id, screen=screen, contacts=contacts)


async def set_counterparty_orgs(
    deal_id: str,
    buyer_org_id: str | None = None,
    seller_org_id: str | None = None,
) -> DealRecord:
    """Link the auto-provisioned buyer/seller organizations to the deal.

    Called by the Negotiation Agent's finalize_agreement() once the deal is
    agreed — from then on, users assigned to either organization see this
    deal in their portal (list/get scoping matches org_id OR buyer_org_id OR
    seller_org_id). Only provided (non-None) sides are updated."""
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        raise DealNotFoundError(deal_id)

    updates: dict = {"updated_at": datetime.now(timezone.utc)}
    if buyer_org_id is not None:
        updates["buyer_org_id"] = buyer_org_id
    if seller_org_id is not None:
        updates["seller_org_id"] = seller_org_id

    updated_deal = deal.model_copy(update=updates)
    await repo.save(updated_deal)
    logger.info(
        "Deal %s linked to counterparty orgs (buyer=%s seller=%s)",
        deal_id, buyer_org_id, seller_org_id,
    )
    return updated_deal
