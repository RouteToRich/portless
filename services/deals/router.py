"""HTTP routes for the Deals service (LLD 4.2)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from shared.auth.dependencies import AuthUser, get_current_user, require_org_scope

from . import service
from .schemas import (
    BuyerRequirement,
    SupplierOffer,
    BuyerCandidate,
    CreateDealRequest,
    DealRecord,
    DealStatus,
    EnrichmentResult,
    MatchResult,
    UpdateFinancialsRequest,
    UpdatePartyRequest,
    UpdateStatusRequest,
)
from .service import DealNotFoundError, InvalidTransitionError

router = APIRouter(prefix="/deals", tags=["deals"])


async def _org_deal(deal_id: str, user: AuthUser) -> DealRecord:
    """Fetch a deal and enforce that the caller's organization is a party to
    it — owner org, buyer org, or seller org (else 404). Portless platform
    staff (org_filter is None) bypass this."""
    org_filter = require_org_scope(user)
    try:
        deal = await service.get_deal(deal_id)
    except DealNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if org_filter is not None and org_filter not in (
        deal.org_id, deal.buyer_org_id, deal.seller_org_id
    ):
        # Hide existence of other organizations' deals.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Deal {deal_id!r} not found")
    return deal


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "deals"}


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("", response_model=list[DealRecord])
async def list_deals(
    limit: int = Query(default=100, ge=1, le=500),
    status: DealStatus | None = Query(default=None),
    user: AuthUser = Depends(get_current_user),
) -> list[DealRecord]:
    """List deals visible to the authenticated user, newest first.

    Scoped to every deal in the caller's organization (multi-tenancy) —
    Portless platform staff see deals across every organization. Use
    ``status`` to filter by lifecycle state (e.g. ``MATCHING``).
    """
    return await service.list_deals(
        limit=limit,
        status=status,
        org_id=require_org_scope(user),
    )


@router.post("", response_model=DealRecord, status_code=status.HTTP_201_CREATED)
async def create_deal(
    payload: CreateDealRequest,
    user: AuthUser = Depends(get_current_user),
) -> DealRecord:
    """Create a new deal owned by the authenticated user.

    The deal's ``operator_id`` and ``org_id`` are forced to the caller's
    identity/organization so it is tied to their account regardless of the
    request body.
    """
    payload.operator_id = user.user_id
    org_id = user.org_id if user.is_portless else require_org_scope(user)
    return await service.create_deal(payload, org_id=org_id)


@router.get("/{deal_id}", response_model=DealRecord)
async def get_deal(
    deal_id: str,
    user: AuthUser = Depends(get_current_user),
) -> DealRecord:
    """Fetch a single deal in the caller's organization (404 if not theirs)."""
    return await _org_deal(deal_id, user)


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------

@router.patch("/{deal_id}/status", response_model=DealRecord)
async def update_status(
    deal_id: str,
    payload: UpdateStatusRequest,
    user: AuthUser = Depends(get_current_user),
) -> DealRecord:
    """Transition one of the caller's deals to a new lifecycle state.

    Topology is enforced by the state machine in ``shared.domain.state_machine``.
    Returns ``422`` when the transition is not permitted, ``404`` if not theirs.
    """
    await _org_deal(deal_id, user)  # organization ownership gate
    try:
        return await service.update_status(deal_id, payload)
    except DealNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# Financial patch (Sourcing / Negotiation phase updates)
# ---------------------------------------------------------------------------

@router.patch("/{deal_id}/financials", response_model=DealRecord)
async def update_financials(
    deal_id: str, payload: UpdateFinancialsRequest
) -> DealRecord:
    """Patch financial fields after Sourcing or Negotiation phases update them.

    Used by the Negotiation Agent when a price is agreed, and by the Sourcing
    service when regulatory verification results change the composite score.
    """
    try:
        return await service.update_financials(deal_id, payload)
    except DealNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


# ---------------------------------------------------------------------------
# Parties: operator corrections + Apify enrichment
# ---------------------------------------------------------------------------

@router.patch("/{deal_id}/parties/{role}", response_model=DealRecord)
async def update_party(
    deal_id: str,
    role: str,
    payload: UpdatePartyRequest,
    user: AuthUser = Depends(get_current_user),
) -> DealRecord:
    """Correct a counterparty's identity or contact (buyer / supplier).

    Operator-entered contacts are stamped ``contact_source="operator"`` and are
    never overwritten by a later enrichment run.
    """
    await _org_deal(deal_id, user)
    try:
        return await service.update_party(
            deal_id, role, payload.model_dump(exclude_none=True), source="operator"
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except DealNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{deal_id}/enrich", response_model=EnrichmentResult)
async def enrich_deal(
    deal_id: str,
    screen: bool = Query(default=True, description="Run sanctions / registry screening"),
    contacts: bool = Query(default=True, description="Run the company contact finder"),
    user: AuthUser = Depends(get_current_user),
) -> EnrichmentResult:
    """Identify and verify the deal's counterparties with the PortLess Apify
    actors: website + checked e-mail + phone (company-contact-finder-ai) and
    OFAC/UN/EU/UK sanctions + LEI + VIES (counterparty-check-ai). Results are
    written onto the deal's parties; ``blocked`` is true when any party is
    sanctioned (a deal exception is raised as well)."""
    await _org_deal(deal_id, user)
    try:
        result = await service.enrich_deal(deal_id, screen=screen, contacts=contacts)
    except DealNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return EnrichmentResult(**result)


# ---------------------------------------------------------------------------
# Timeline + evidence pack
# ---------------------------------------------------------------------------

@router.get("/{deal_id}/timeline")
async def deal_timeline(
    deal_id: str,
    user: AuthUser = Depends(get_current_user),
) -> dict:
    """Every dated fact of the deal from every service, merged and sorted."""
    await _org_deal(deal_id, user)
    from shared.orchestration.evidence import build_timeline
    try:
        return await build_timeline(deal_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{deal_id}/evidence-pack")
async def deal_evidence_pack(
    deal_id: str,
    format: str = "json",
    user: AuthUser = Depends(get_current_user),
):
    """Hand-over pack: deal, parties, PI/PO, document hashes, orders, ledger +
    double-entry journal, approvals, exceptions, correspondence, timeline — with
    a SHA-256 over the whole pack. ``?format=html`` renders a printable page."""
    await _org_deal(deal_id, user)
    from fastapi.responses import HTMLResponse
    from shared.orchestration.evidence import build_evidence_pack, render_evidence_html
    try:
        pack = await build_evidence_pack(deal_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if format == "html":
        return HTMLResponse(render_evidence_html(pack))
    return pack


# ---------------------------------------------------------------------------
# Buyer outreach funnel
# ---------------------------------------------------------------------------

@router.post("/{deal_id}/buyer-candidates", response_model=DealRecord, status_code=status.HTTP_201_CREATED)
async def add_buyer_candidate(
    deal_id: str,
    candidate: BuyerCandidate,
    user: AuthUser = Depends(get_current_user),
) -> DealRecord:
    """Add a buyer to the deal's outreach funnel (operator-sourced lead)."""
    await _org_deal(deal_id, user)
    try:
        return await service.add_buyer_candidate(deal_id, candidate)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except DealNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{deal_id}/outreach/start", response_model=dict)
async def start_outreach(
    deal_id: str,
    user: AuthUser = Depends(get_current_user),
) -> dict:
    """Contact the next pending buyer candidate(s) now (gated by SEND_BUYER_OUTREACH)."""
    deal = await _org_deal(deal_id, user)
    from shared.orchestration import outreach
    sent = await outreach.start_outreach(deal)
    return {"deal_id": deal_id, "contacted": sent, "waiting_for_buyer": outreach.waiting_for_buyer(deal)}


@router.post("/{deal_id}/buyer-candidates/{candidate_id}/skip", response_model=dict)
async def skip_buyer_candidate(
    deal_id: str,
    candidate_id: str,
    user: AuthUser = Depends(get_current_user),
) -> dict:
    """Operator drops a candidate; the funnel moves to the next one."""
    await _org_deal(deal_id, user)
    from shared.orchestration import outreach
    try:
        result = await outreach.skip_candidate(deal_id, candidate_id, "skipped by operator")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return {"deal_id": deal_id, "candidate_id": candidate_id, **result}


# ---------------------------------------------------------------------------
# Match board — buyer requirements × supplier offers (prospect stage)
# ---------------------------------------------------------------------------

class _PairRef(BaseModel):
    candidate_id: str
    offer_id: str


@router.get("/{deal_id}/match-board", response_model=dict)
async def get_match_board(deal_id: str, user: AuthUser = Depends(get_current_user)) -> dict:
    """Every buyer × supplier pair of the prospect, scored: prices (stated or price-book
    estimate), landed cost, margin, chance of closing, blockers, what is missing."""
    deal = await _org_deal(deal_id, user)
    from shared.orchestration import matchmaking
    board = await matchmaking.build_board(deal)
    return board.model_dump(mode="json")


@router.put("/{deal_id}/buyer-candidates/{candidate_id}/requirement", response_model=DealRecord)
async def put_buyer_requirement(
    deal_id: str, candidate_id: str, requirement: BuyerRequirement,
    user: AuthUser = Depends(get_current_user),
) -> DealRecord:
    """Operator sets / corrects what a buyer wants (grade, quantity, port, target price…)."""
    await _org_deal(deal_id, user)
    try:
        deal = await service.set_buyer_requirement(deal_id, candidate_id, requirement)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if requirement.target_price_usd_per_mt:
        from shared import pricebook
        from shared.orchestration import landed_cost as lc
        await pricebook.record(pricebook.PriceObservation(
            hs_code=deal.hs_code, side="buyer", kind="buyer_target", market=deal.market,
            price_usd_per_mt=lc.to_cif(requirement.target_price_usd_per_mt,
                                       requirement.target_price_incoterm or requirement.incoterm, deal.market),
            grade=requirement.grade, quantity_mt=requirement.quantity_mt, deal_id=deal_id,
            counterparty_id=candidate_id, note="entered by operator",
        ))
    return deal


@router.post("/{deal_id}/supplier-offers", response_model=DealRecord, status_code=status.HTTP_201_CREATED)
async def post_supplier_offer(
    deal_id: str, offer: SupplierOffer, user: AuthUser = Depends(get_current_user),
) -> DealRecord:
    """Operator adds / replaces a supplier's offer (e.g. a quote received by phone)."""
    deal = await _org_deal(deal_id, user)
    offer = offer.model_copy(update={"source": offer.source or "operator"})
    updated = await service.upsert_supplier_offer(deal_id, offer)
    if offer.fob_price_usd_per_mt:
        from shared import pricebook
        await pricebook.record(pricebook.PriceObservation(
            hs_code=deal.hs_code, side="supplier", kind="supplier_offer",
            price_usd_per_mt=offer.fob_price_usd_per_mt, grade=offer.grade, deal_id=deal_id,
            counterparty_id=offer.supplier_id, note="entered by operator",
        ))
    return updated


@router.post("/{deal_id}/match/evaluate", response_model=dict)
async def evaluate_match(deal_id: str, user: AuthUser = Depends(get_current_user)) -> dict:
    """Re-score the board now and open the next gate (confirm match / indicative offer)."""
    await _org_deal(deal_id, user)
    from shared.orchestration import matchmaking
    return await matchmaking.evaluate(deal_id)


async def _pair(deal, ref: _PairRef):
    from shared.orchestration import matchmaking
    board = await matchmaking.build_board(deal)
    pair = next((p for p in board.pairs if p.candidate_id == ref.candidate_id and p.offer_id == ref.offer_id), None)
    if pair is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pair not on the match board")
    return board, pair


@router.post("/{deal_id}/match/indicative-offer", response_model=dict)
async def request_indicative_offer(deal_id: str, ref: _PairRef, user: AuthUser = Depends(get_current_user)) -> dict:
    """Operator asks for an indicative offer to this buyer from this supplier's quote (SEND_INDICATIVE_OFFER gate)."""
    deal = await _org_deal(deal_id, user)
    board, pair = await _pair(deal, ref)
    if not pair.landed or pair.supplier_price.basis != "quoted":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="an indicative offer needs this supplier's quoted price")
    if pair.blockers:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=pair.blockers[0])
    from shared.orchestration import matchmaking
    lead = next((o.get("lead_time_days") for o in board.offers if o["offer_id"] == ref.offer_id), None)
    gid = await matchmaking.propose_indicative_offer(deal, pair, board, lead)
    if gid is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="buyer has no e-mail address")
    return {"deal_id": deal_id, "approval_id": gid}


@router.post("/{deal_id}/match/confirm", response_model=dict)
async def request_match(deal_id: str, ref: _PairRef, user: AuthUser = Depends(get_current_user)) -> dict:
    """Operator proposes this pair as the deal (CONFIRM_MATCH gate) — even below the
    probability threshold, as long as nothing blocks it."""
    deal = await _org_deal(deal_id, user)
    _, pair = await _pair(deal, ref)
    if pair.blockers:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=pair.blockers[0])
    if not pair.buyer_price.value or not pair.supplier_price.value:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="both prices are needed")
    from shared.orchestration import matchmaking
    gid = await matchmaking.propose_match(deal, pair)
    return {"deal_id": deal_id, "approval_id": gid}


# ---------------------------------------------------------------------------
# Match Engine results sub-resource
# ---------------------------------------------------------------------------

@router.get("/{deal_id}/match-scores", response_model=list[MatchResult])
async def get_match_scores(
    deal_id: str,
    user: AuthUser = Depends(get_current_user),
) -> list[MatchResult]:
    """Return Match Engine scores for one of the caller's organization's deals.

    The list is sorted by composite score descending so the winning candidate
    appears first.  ``decision`` is either ``SOURCE`` (viable) or ``DISCARD``.
    """
    deal = await _org_deal(deal_id, user)

    return sorted(
        deal.match_results,
        key=lambda mr: mr.composite_score,
        reverse=True,
    )
