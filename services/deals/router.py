"""HTTP routes for the Deals service (LLD 4.2)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from shared.auth.dependencies import AuthUser, get_current_user, require_org_scope, require_portless

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


# ---------------------------------------------------------------------------
# Trade compliance — HS export-policy confirmations (platform-wide, staff only).
# Declared before the /{deal_id} routes so "compliance" is never read as an id.
# ---------------------------------------------------------------------------

class HsPolicyUpdate(BaseModel):
    status: str
    """free · licensed · restricted · prohibited · scomet · state_trading · dual_use"""
    note: str | None = None
    licence_ref: str | None = None
    """DGFT export authorisation / SCOMET licence number when status=licensed."""


@router.get("/compliance/hs-policy")
async def list_hs_policy(user: AuthUser = Depends(require_portless)) -> dict:
    """Operator-confirmed export policy per HS code, plus the built-in seed list."""
    from shared.orchestration import compliance
    return {
        "confirmed": [e.model_dump(mode="json") for e in await compliance.list_hs_policy()],
        "seed": {k: {"status": v[0], "note": v[1]} for k, v in compliance.HS_POLICY_SEED.items()},
    }


@router.put("/compliance/hs-policy/{hs_code}")
async def put_hs_policy(hs_code: str, body: HsPolicyUpdate, user: AuthUser = Depends(require_portless)) -> dict:
    """Record the result of checking an HS code against ITC-HS Schedule 2 / SCOMET."""
    from shared.orchestration import compliance
    try:
        entry = compliance.HsPolicyEntry(
            hs_code=hs_code, status=body.status, note=body.note, licence_ref=body.licence_ref,
            confirmed_by=getattr(user, "email", None) or getattr(user, "user_id", None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if entry.status == "licensed" and not entry.licence_ref:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="status=licensed needs the licence_ref")
    return (await compliance.set_hs_policy(entry)).model_dump(mode="json")


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

# Only these statuses may be set by hand. Every other one records something a
# gate or a provider confirmed (money received, PO issued, inspection passed,
# supplier paid …) and is set by that step, in process, never over this route.
_OPERATOR_SETTABLE_STATUSES = frozenset({
    DealStatus.MATCHING, DealStatus.NEGOTIATING, DealStatus.NEGOTIATION_FAILED,
    DealStatus.DISPUTE, DealStatus.CANCELLED,
})


async def _refuse_gated_status(deal_id: str, target: DealStatus) -> None:
    if target not in _OPERATOR_SETTABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{target.value} is set by its own approved step or provider confirmation, not by hand",
        )
    if target == DealStatus.CANCELLED:
        from services.escrow_guard.repository import get_repository as escrow_repository
        hold = await escrow_repository().get_hold_by_deal(deal_id)
        if hold is not None and (hold.funded_amount or 0) > hold.refunded_total:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="the buyer has funds in this escrow — cancel it through the unfunded-deal playbook, which refunds them",
            )


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
    await _refuse_gated_status(deal_id, payload.target_status)
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
        updates = payload.model_dump(exclude_none=True)
        deal = await service.update_party(deal_id, role, updates, source="operator")
        if role.lower() == "supplier" and updates.get("address"):
            # An inspection waiting for the facility address is booked now.
            from shared.orchestration import supplier_commitment
            try:
                if await supplier_commitment.address_known(deal_id) is not None:
                    deal = await service.get_deal(deal_id)
            except Exception as exc:  # noqa: BLE001 — the address is saved either way
                logging.getLogger(__name__).warning("Deal %s: QC booking after the address update failed: %s", deal_id, exc)
        return deal
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except DealNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


class InspectionPlanChoice(BaseModel):
    types: list[str]
    """Any of DUPRO / PSI / PEO (PSI is always included)."""
    reason: str = ""


@router.put("/{deal_id}/inspection-plan", response_model=DealRecord)
async def set_inspection_plan(
    deal_id: str, body: InspectionPlanChoice, user: AuthUser = Depends(require_portless),
) -> DealRecord:
    """Staff record the buyer's inspection requirement (DUPRO / PSI / PEO)."""
    from shared.orchestration import inspection_plan, qc_status
    try:
        await service.get_deal(deal_id)
    except DealNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    unknown = [t for t in body.types if str(t).upper() not in inspection_plan.ORDER]
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"unknown inspection type(s): {unknown}")
    await inspection_plan.set_plan(deal_id, body.types, "operator")
    await qc_status.sync(deal_id)
    logging.getLogger(__name__).info("Deals: inspection plan of %s set by %s (%s)", deal_id,
                                     user.email or user.user_id, body.reason[:200])
    return await service.get_deal(deal_id)


class EscrowProviderChoice(BaseModel):
    provider: str
    """``tazapay`` or ``escrow_com`` (``Escrow.com`` / ``Tazapay`` accepted)."""
    reason: str = ""


@router.put("/{deal_id}/escrow-provider", response_model=DealRecord)
async def set_escrow_provider(
    deal_id: str, body: EscrowProviderChoice, user: AuthUser = Depends(require_portless),
) -> DealRecord:
    """Staff record the buyer's escrow provider (said by phone / in a meeting).
    Refused once the escrow is open, or for a provider that is not enabled."""
    from shared.orchestration import escrow_status
    try:
        await service.get_deal(deal_id)
    except DealNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    if not await escrow_status.set_choice(deal_id, body.provider, "operator"):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail="Provider unknown / not enabled, or the escrow is already open")
    logging.getLogger(__name__).info("Deals: escrow provider of %s set to %s by %s (%s)", deal_id, body.provider,
                                     user.email or user.user_id, body.reason[:200])
    return await service.get_deal(deal_id)


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
# Trade compliance per deal
# ---------------------------------------------------------------------------

@router.get("/{deal_id}/compliance")
async def deal_compliance(deal_id: str, user: AuthUser = Depends(get_current_user)) -> dict:
    """Trade check, payer check, supervised loading + seal, shipment check and
    the open holds (flags that keep every money / commitment gate at L0)."""
    await _org_deal(deal_id, user)
    from shared.orchestration import compliance
    return (await compliance.get_record(deal_id)).model_dump(mode="json")


@router.post("/{deal_id}/compliance/check")
async def deal_compliance_check(deal_id: str, user: AuthUser = Depends(get_current_user)) -> dict:
    """Re-run the trade check now (e.g. after confirming an HS code or fixing a party)."""
    deal = await _org_deal(deal_id, user)
    from shared.orchestration import compliance
    from shared.orchestration.continuations import _negotiation_numbers
    supplier_ask, _ = await _negotiation_numbers(deal_id, deal)
    result = await compliance.check_trade(
        deal, stage="manual", buyer_price=getattr(deal, "buyer_target_price_usd", None),
        supplier_price=supplier_ask,
    )
    return result.model_dump(mode="json")


class ClearHoldRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    reason: str = Field(min_length=10, description="What was checked and why this is a false alarm")
    evidence_ref: str | None = None


@router.post("/{deal_id}/compliance/holds/{code}/clear")
async def deal_compliance_clear_hold(
    deal_id: str, code: str, body: ClearHoldRequest, user: AuthUser = Depends(require_portless),
) -> dict:
    """Clear a block-level hold confirmed as a false alarm (staff only, audited).

    Only a seal mismatch can be cleared here: a wrong loading record is fixed by
    recording it again, and legal blocks such as an embargo are never waived.
    """
    await _org_deal(deal_id, user)
    from shared.orchestration import compliance
    try:
        record = await compliance.clear_block(
            deal_id, code, by=user.email or user.user_id, reason=body.reason, evidence_ref=body.evidence_ref,
        )
    except compliance.HoldNotClearableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"{code} cannot be cleared by an operator") from exc
    except compliance.HoldNotOpenError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"{code} is not open on deal {deal_id}") from exc
    return record.model_dump(mode="json")


# ---------------------------------------------------------------------------
# What a price rests on: cost sheet + pre-trade market check
# ---------------------------------------------------------------------------

@router.get("/{deal_id}/cost-sheet")
async def deal_cost_sheet(deal_id: str, user: AuthUser = Depends(get_current_user)) -> dict:
    """Every PortLess cost between the supplier and the buyer's port, with its
    source and who pays it, plus the lowest buyer price that keeps the margin floor."""
    await _org_deal(deal_id, user)
    from agents.negotiation_agent.repository import get_repository as neg_repo  # type: ignore[import]
    from shared.orchestration import trade_costs
    session = await neg_repo().get_session_by_deal(deal_id)
    if session is None or not session.cost_sheet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no cost sheet yet — the negotiation has not started")
    supplier = session.supplier_last_quote or session.supplier_quote_price
    floor = session.policy.margin_floor_pct
    return {
        **session.cost_sheet,
        "supplier_price_usd_per_mt": supplier,
        "margin_floor_pct": floor,
        "min_buyer_price_usd_per_mt": round(trade_costs.min_buyer_price(session.cost_sheet, supplier, floor), 2) if supplier else None,
        "agreed_price_usd_per_mt": session.agreed_price,
        "net_margin_at_agreed_pct": (round(trade_costs.net_margin_pct(session.cost_sheet, session.agreed_price, supplier), 2)
                                     if session.agreed_price and supplier else None),
    }


_PROFIT_LABELS = {
    "ocean_freight": "Ocean freight", "inspection": "Pre-shipment inspection", "marine_insurance": "Marine insurance",
    "customs_agent": "Export customs agent (CHA)", "export_documents": "Export documents and certificates",
    "inland_transport": "Inland transport to port", "origin_handling": "Origin terminal handling",
    "escrow_fee": "Escrow provider fee",
}


@router.get("/{deal_id}/profit")
async def deal_profit(deal_id: str, user: AuthUser = Depends(get_current_user)) -> dict:
    """From sale value to PortLess's profit, line by line: sale (PI) − supplier
    (PO) = gross spread, minus every PortLess cost (actual where a provider
    order reported one or the escrow charged it, otherwise the cost sheet's
    estimate), = net profit and its % of the sale."""
    await _org_deal(deal_id, user)
    from agents.negotiation_agent.repository import get_repository as neg_repo  # type: ignore[import]
    from services.dealmaker.repository import get_repository as dm_repo  # type: ignore[import]
    from shared.orchestration import trade_costs

    session = await neg_repo().get_session_by_deal(deal_id)
    pi = await dm_repo().get_pi_by_deal(deal_id)
    po = await dm_repo().get_po_by_deal(deal_id)
    qty = float((pi.quantity_mt if pi else None) or (session.quantity_mt if session else None) or 0.0)
    buyer_price = (pi.agreed_price if pi else None) or (session.agreed_price if session else None)
    supplier_price = (po.agreed_price if po else None) or (
        (session.supplier_last_quote or session.supplier_quote_price) if session else None)
    if not qty or not buyer_price or not supplier_price:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="no agreed buyer and supplier price yet — nothing to break down")
    sale = round(pi.total_value if pi else buyer_price * qty, 2)
    supplier = round(po.total_value if po else supplier_price * qty, 2)
    gross = round(sale - supplier, 2)

    sheet = session.cost_sheet if session else None
    estimates = trade_costs.cost_usd(sheet, sale / qty, qty) if sheet else {}
    ledger = None
    try:
        from services.escrow_guard.service import get_deal_ledger  # type: ignore[import]
        ledger = await get_deal_ledger(deal_id)
    except Exception:  # noqa: BLE001 — no escrow yet: estimates only
        ledger = None
    incurred = getattr(ledger, "portless_costs", None) or {}
    basis = getattr(ledger, "portless_costs_basis", None) or {}

    lines = []
    for key, est in estimates.items():
        if key == "contingency":
            continue  # a pricing buffer, not a cost
        if key == "escrow_fee" and ledger is not None and getattr(ledger, "settled", False) and ledger.fees_total:
            usd, state = round(float(ledger.fees_total), 2), "actual"
        elif key in incurred:
            usd, state = incurred[key], ("actual" if basis.get(key) == "actual" else "incurred")
        else:
            usd, state = est, "estimate"
        lines.append({"key": key, "label": _PROFIT_LABELS.get(key, key), "usd": usd,
                      "per_mt": round(usd / qty, 2), "pct_of_sale": round(usd / sale * 100, 2), "status": state})
    total = round(sum(l["usd"] for l in lines), 2)
    net = round(gross - total, 2)
    return {
        "deal_id": deal_id, "quantity_mt": qty, "currency": "USD",
        "sale_value": sale, "buyer_price_per_mt": buyer_price, "sale_basis": "PI" if pi else "agreed price",
        "supplier_cost": supplier, "supplier_price_per_mt": supplier_price, "supplier_basis": "PO" if po else "supplier quote",
        "gross_spread": gross, "gross_spread_pct": round(gross / sale * 100, 2),
        "costs": lines, "costs_total": total, "costs_pct": round(total / sale * 100, 2),
        "net_profit": net, "net_profit_pct": round(net / sale * 100, 2),
        "realized_margin": getattr(ledger, "realized_margin", None),
        "estimates_left": sum(1 for l in lines if l["status"] == "estimate"),
    }


@router.get("/{deal_id}/market-check")
async def deal_market_check(deal_id: str, refresh: bool = False, user: AuthUser = Depends(get_current_user)) -> dict:
    """Should PortLess trade this product into this market now? Latest news,
    India's export policy for the HS code, verdict, reasons and sourced facts."""
    deal = await _org_deal(deal_id, user)
    from shared.orchestration import market_check
    check = await market_check.for_deal(deal_id, deal.hs_code, deal.market, refresh=refresh)
    if check is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="market check is switched off")
    return check.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Supplier commitment (firm order before the PI, PO acknowledgement)
# ---------------------------------------------------------------------------

class OperatorNote(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    reason: str = Field(min_length=10, description="What the supplier said, how and when")


@router.get("/{deal_id}/supplier-commitment")
async def supplier_commitment_status(deal_id: str, user: AuthUser = Depends(get_current_user)) -> dict:
    """Which supplier holds the firm order request, who was asked before and
    why standby suppliers were passed over, and the PO acknowledgement."""
    await _org_deal(deal_id, user)
    from shared.orchestration import supplier_commitment
    rec = await supplier_commitment.get(deal_id)
    return rec.model_dump(mode="json") if rec else {"deal_id": deal_id, "status": "none"}


@router.post("/{deal_id}/supplier-commitment/confirm")
async def supplier_commitment_confirm(
    deal_id: str, body: OperatorNote, user: AuthUser = Depends(require_portless),
) -> dict:
    """The supplier confirmed outside e-mail (phone, WhatsApp): open the
    CONFIRM_SUPPLIER_ORDER approval exactly as an e-mailed confirmation does."""
    await _org_deal(deal_id, user)
    from shared.orchestration import supplier_commitment
    rec = await supplier_commitment.get(deal_id)
    if rec is None or rec.status != "requested" or rec.current is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no open supplier order request")
    await supplier_commitment._gate_confirmation(rec, f"operator:{user.email or user.user_id}", body.reason)
    return {"deal_id": deal_id, "status": "approval_requested", "supplier_id": rec.current.supplier_id}


@router.post("/{deal_id}/supplier-commitment/next")
async def supplier_commitment_next(
    deal_id: str, body: OperatorNote, user: AuthUser = Depends(require_portless),
) -> dict:
    """The current supplier said no outside e-mail: ask the next-ranked supplier now."""
    await _org_deal(deal_id, user)
    from shared.orchestration import supplier_commitment
    outcome = await supplier_commitment.mark_and_advance(
        deal_id, "declined", note=f"{user.email or user.user_id}: {body.reason}",
    )
    return {"deal_id": deal_id, "status": outcome}


@router.post("/{deal_id}/supplier-commitment/po-ack")
async def supplier_commitment_po_ack(
    deal_id: str, body: OperatorNote, user: AuthUser = Depends(require_portless),
) -> dict:
    """The supplier acknowledged the PO outside e-mail: record it and book the inspection."""
    deal = await _org_deal(deal_id, user)
    if (getattr(deal.status, "value", deal.status)) != "PO_ISSUED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="deal is not at PO_ISSUED")
    from shared.orchestration import supplier_commitment
    tpo = await supplier_commitment.acknowledge_po(deal_id, by=f"{user.email or user.user_id}: {body.reason}")
    return {"deal_id": deal_id, "status": "acknowledged", "qc_order": getattr(tpo, "tpo_id", None)}


# ---------------------------------------------------------------------------
# Buyer's packing requirements (shared/orchestration/packing.py)
# ---------------------------------------------------------------------------

class PackingUpdate(BaseModel):
    """The buyer's packing — structured fields, or its own words in ``text``."""
    text: str | None = None
    standard_ok: bool | None = None
    material: str | None = None
    unit_weight_kg: float | None = Field(default=None, gt=0, le=2000)
    inner_liner: bool | None = None
    marking: str | None = None
    private_label: bool | None = None
    palletised: bool | None = None
    fumigation: bool | None = None
    container_lining: bool | None = None
    notes: str | None = None


@router.get("/{deal_id}/packing")
async def get_packing(deal_id: str, user: AuthUser = Depends(get_current_user)) -> dict:
    """What the buyer asked for, what it adds to the cost, and what the inspector checks."""
    await _org_deal(deal_id, user)
    from agents.negotiation_agent.repository import get_repository as neg_repo  # type: ignore[import]
    from shared.orchestration import packing
    session = await neg_repo().get_session_by_deal(deal_id)
    spec = getattr(session, "packing", None) if session else None
    extra, parts = packing.surcharge(spec, getattr(session, "quantity_mt", None) or 20.0)
    return {"deal_id": deal_id, "packing": spec, "description": packing.describe(spec) or None,
            "special": packing.special(spec), "surcharge_usd_per_mt": extra, "surcharge_parts": parts,
            "in_supplier_price": bool(getattr(session, "packing_in_supplier_price", False)),
            "qc_checks": packing.qc_checklist(spec)}


@router.put("/{deal_id}/packing")
async def set_packing(deal_id: str, body: PackingUpdate, user: AuthUser = Depends(require_portless)) -> dict:
    """Record the buyer's packing heard by phone, or correct it. The cost sheet
    is repriced; the order request, PO, PI and inspection carry it from here."""
    await _org_deal(deal_id, user)
    from agents.negotiation_agent.repository import get_repository as neg_repo  # type: ignore[import]
    from agents.negotiation_agent.service import refresh_cost_sheet  # type: ignore[import]
    from shared.orchestration import packing
    session = await neg_repo().get_session_by_deal(deal_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="the deal has no negotiation yet")
    fields = body.model_dump(exclude_none=True, exclude={"text"})
    spec = packing.parse_packing(body.text) if body.text else None
    if body.text and spec is None:
        spec = packing.PackingSpec(notes=body.text[:300], raw=body.text)
    if fields:
        spec = packing.PackingSpec(**{**(spec.model_dump() if spec else {}), **fields})
    if spec is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="give the packing as text or fields")
    spec.source = f"operator:{user.email or user.user_id}"
    session.packing = packing.merge(session.packing, spec).model_dump(mode="json") if not fields else spec.model_dump(mode="json")
    session.packing_in_supplier_price = False  # a changed packing must be confirmed by the supplier again
    session = await refresh_cost_sheet(session)
    await neg_repo().save_session(session)
    return await get_packing(deal_id, user)


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


@router.get("/{deal_id}/notifications")
async def deal_notifications(
    deal_id: str,
    user: AuthUser = Depends(get_current_user),
) -> dict:
    """Correspondence log: every e-mail to / from the buyer and the supplier
    (status notices, negotiation, outreach, RFQ) with its delivery outcome —
    delivered, logged_only (no mailbox), pending, skipped (no address / thread),
    failed, blocked — newest first, plus per-party counts."""
    await _org_deal(deal_id, user)
    from shared.orchestration.notifications import deal_correspondence_log
    return await deal_correspondence_log(deal_id)


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
    if not pair.buyer_price.value or not pair.supplier_price.value or not pair.firm:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="both prices are needed: one the buyer stated and the supplier's own quote, not a market estimate",
        )
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
