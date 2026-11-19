"""HTTP routes for the Deals service (LLD 4.2)."""
from __future__ import annotations

import logging
from datetime import datetime

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
from shared.orchestration.relay_evidence import Evidence as RelayEvidence

from .service import DealNotFoundError, InvalidTransitionError

logger = logging.getLogger(__name__)
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


@router.get("/compliance/dgft-denied")
async def dgft_denied_status(user: AuthUser = Depends(require_portless)) -> dict:
    """The loaded DGFT denied-entity list: count, when and by whom it was loaded, stale or not."""
    from shared.orchestration import denied_list
    return await denied_list.status()


class DgftListUpload(BaseModel):
    csv: str = Field(min_length=10, description="CSV with a header: name, iec, address, order_ref, notified_on")


@router.put("/compliance/dgft-denied")
async def dgft_denied_load(body: DgftListUpload, user: AuthUser = Depends(require_portless)) -> dict:
    """Replace the stored DGFT denied-entity list with this copy from dgft.gov.in (compliance officer)."""
    from shared.auth.roles import RoleRequired, require_compliance_officer
    try:
        require_compliance_officer(user, "Loading the DGFT denied-entity list")
    except RoleRequired as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    from shared.orchestration import denied_list
    try:
        meta = await denied_list.load(body.csv, by=getattr(user, "email", None) or "staff")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from shared.audit_log import staff_relay
    await staff_relay(user, "compliance.dgft_list_loaded", "platform", count=meta["count"])
    return meta


@router.put("/compliance/hs-policy/{hs_code}")
async def put_hs_policy(hs_code: str, body: HsPolicyUpdate, user: AuthUser = Depends(require_portless)) -> dict:
    """Record the result of checking an HS code against ITC-HS Schedule 2 / SCOMET (compliance officer)."""
    from shared.auth.roles import RoleRequired, require_compliance_officer
    try:
        require_compliance_officer(user, "Confirming an HS code's export policy")
    except RoleRequired as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    from shared.orchestration import compliance
    try:
        entry = compliance.HsPolicyEntry(
            hs_code=hs_code, status=body.status, note=body.note, licence_ref=body.licence_ref,
            confirmed_by=getattr(user, "email", None) or getattr(user, "user_id", None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if entry.status == "licensed" and not entry.licence_ref:
        raise HTTPException(status_code=422,
                            detail="status=licensed needs the licence_ref")
    return (await compliance.set_hs_policy(entry)).model_dump(mode="json")


def _officer(user: AuthUser, what: str) -> None:
    from shared.auth.roles import RoleRequired, require_compliance_officer
    try:
        require_compliance_officer(user, what)
    except RoleRequired as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc


@router.get("/compliance/hs-policy-due")
async def hs_policy_due(user: AuthUser = Depends(require_portless)) -> dict:
    """HS codes to re-confirm: marked by the regulatory watch, or confirmed longer ago than
    ``HS_POLICY_MAX_AGE_DAYS``."""
    from shared.orchestration import compliance
    return {"due": await compliance.hs_policy_due(),
            "max_age_days": compliance.hs_policy_max_age_days()}


# ---------------------------------------------------------------------------
# Trade operations data (todo 2026-11-19 batch): CHA list, regulation items, destination
# requirements, refusals, demand leads, Indian missions
# ---------------------------------------------------------------------------

class ChaAgentBody(BaseModel):
    agent_id: str | None = None
    firm: str
    licence_no: str
    licence_valid_until: str | None = None
    ports: list[str] = Field(default_factory=list)
    email: str
    phone: str | None = None
    charges_per_shipment_usd: float | None = None
    charges_per_container_usd: float | None = None


class ChaKycBody(BaseModel):
    sent_on: str
    authorisation_ref: str


class RegulatoryItemBody(BaseModel):
    title: str
    url: str | None = None
    number: str | None = None
    date: str | None = None
    hs_codes: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)


class MissionRequestBody(BaseModel):
    market: str
    hs_code: str
    product: str | None = None
    quantity_range: str | None = None


class MissionImportBody(BaseModel):
    rows: list[dict] | None = None


class ForwarderBody(BaseModel):
    forwarder_id: str | None = None
    name: str
    email: str
    lanes: list[str] = Field(default_factory=list)
    phone: str | None = None


@router.get("/freight/forwarders")
async def freight_forwarders(market: str | None = None, user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import freight_procurement
    return {"forwarders": await freight_procurement.forwarders(market)}


@router.put("/freight/forwarders")
async def put_freight_forwarder(body: ForwarderBody, user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import freight_procurement
    try:
        return await freight_procurement.put_forwarder(body.model_dump(), by=_staff_name(user))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/compliance/country-feeds/run")
async def run_country_feeds(user: AuthUser = Depends(require_portless)) -> dict:
    """Read the sanctions / FATF / OECD feeds now: blocked applies at once, the rest are suggestions."""
    _officer(user, "run the country risk feeds")
    from shared import country_feeds
    return await country_feeds.run()


@router.get("/cha/agents")
async def cha_agents(user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import cha
    return {"agents": await cha.agents()}


@router.put("/cha/agents")
async def put_cha_agent(body: ChaAgentBody, user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import cha
    try:
        return await cha.put_agent(body.model_dump(), by=_staff_name(user))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/cha/agents/{agent_id}/kyc")
async def cha_kyc(agent_id: str, body: ChaKycBody, user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import cha
    try:
        await cha.record_kyc(agent_id, sent_on=body.sent_on, authorisation_ref=body.authorisation_ref, by=_staff_name(user))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await cha.kyc_status(agent_id)


@router.get("/cha/agents/{agent_id}/kyc")
async def cha_kyc_status(agent_id: str, user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import cha
    return await cha.kyc_status(agent_id)


@router.get("/compliance/regulatory-items")
async def regulatory_items(hs: str | None = None, user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import regulatory_watch
    return {"items": await regulatory_watch.items(hs)}


@router.post("/compliance/regulatory-items")
async def record_regulatory_item(body: RegulatoryItemBody, user: AuthUser = Depends(require_portless)) -> dict:
    """Record a DGFT / CBIC notification by hand: the same effect as one the watcher found."""
    _officer(user, "record a regulatory notification")
    from shared.orchestration import regulatory_watch
    try:
        row = await regulatory_watch.record_manual(title=body.title, url=body.url, number=body.number, date=body.date,
                                                   hs_codes=body.hs_codes, products=body.products, by=_staff_name(user))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row = dict(row)
    row["item_id"] = row.pop("_id", None)
    return row


@router.get("/compliance/destination-requirements")
async def destination_requirements(hs: str, market: str, user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import destination_rules, refusals
    return {"requirements": destination_rules.requirements(hs, market),
            "preferential": destination_rules.preferential(hs, market),
            "test_for": refusals.tests_for(hs, market)}


@router.get("/leads/recent")
async def demand_leads_recent(limit: int = Query(100, ge=1, le=1000), user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import demand_leads
    return {"leads": await demand_leads.recent(limit)}


@router.get("/leads/stats")
async def demand_leads_stats(user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import gov_leads
    return await gov_leads.stats()


@router.get("/missions/requests")
async def mission_requests(user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import gov_leads
    return {"requests": await gov_leads.all_requests()}


@router.post("/missions/requests")
async def mission_request(body: MissionRequestBody, user: AuthUser = Depends(require_portless)) -> dict:
    """E-mail the commercial wing of India's mission in a market for a list of importers."""
    from shared.orchestration import gov_leads
    try:
        return await gov_leads.request_list(body.market, body.hs_code, product=body.product,
                                            quantity_range=body.quantity_range, by=_staff_name(user))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/missions/requests/{request_id}/import")
async def mission_import(request_id: str, body: MissionImportBody, user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import gov_leads
    try:
        return await gov_leads.import_list(request_id, rows=body.rows, by=_staff_name(user))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/compliance/countries")
async def list_countries(user: AuthUser = Depends(require_portless)) -> dict:
    """Every country profile (seed + stored) with its tier."""
    from shared import country_profiles
    return {"countries": [p.model_dump(mode="json") for p in country_profiles.all_profiles()]}


@router.get("/compliance/countries/{iso2}")
async def get_country(iso2: str, user: AuthUser = Depends(require_portless)) -> dict:
    from shared import country_profiles
    p = country_profiles.get(iso2)
    if p is None:
        raise HTTPException(status_code=404, detail=f"no country profile for {iso2.upper()}")
    return p.model_dump(mode="json")


class CountryUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")
    source: str | None = None
    """Where the facts come from (FATF list, ECGC rating page …) — kept per field."""


@router.put("/compliance/countries/{iso2}")
async def put_country(iso2: str, body: CountryUpdate, user: AuthUser = Depends(require_portless)) -> dict:
    """Fill in or change a country profile (compliance officer, audited). A tier change re-checks
    open deals in that country."""
    _officer(user, "Changing a country profile")
    if len(iso2.strip()) != 2:
        raise HTTPException(status_code=422, detail="iso2 must be a two-letter country code")
    fields = {k: v for k, v in body.model_dump().items() if k != "source" and v is not None}
    if not fields:
        raise HTTPException(status_code=422, detail="nothing to change")
    from shared import country_profiles
    try:
        return await country_profiles.put(iso2, fields, by=getattr(user, "email", None) or "staff", source=body.source)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/compliance/registrations")
async def list_registrations(user: AuthUser = Depends(require_portless)) -> dict:
    """PortLess's own registrations (IEC, LUT, RCMCs, FSSAI …) with expiry and what is due."""
    from shared import registrations
    held = await registrations.held()
    return {"held": held, "expiring": await registrations.expiring(), "kinds": list(registrations.KINDS)}


class RegistrationUpdate(BaseModel):
    number: str = Field(min_length=1)
    expires_on: str | None = None


@router.put("/compliance/registrations/{kind}")
async def put_registration(kind: str, body: RegistrationUpdate, user: AuthUser = Depends(require_portless)) -> dict:
    """Record a registration with its number and expiry (compliance officer, audited)."""
    _officer(user, "Recording a registration")
    from shared import registrations
    try:
        return await registrations.record(kind, body.number, body.expires_on, by=getattr(user, "email", None) or "staff")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/incentive-credits/short")
async def incentive_credits_short(user: AuthUser = Depends(require_portless)) -> dict:
    """Deals whose RoDTEP / drawback credited is below what was counted as income."""
    from shared.orchestration import incentive_credits
    return {"short": await incentive_credits.short_all()}


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


@router.get("/prospects/desk", response_model=list[dict])
async def prospect_desk(user: AuthUser = Depends(get_current_user)) -> list[dict]:
    """Every open prospect the way a trader works them: best pair (margin, chance,
    expected value, firm or estimated), walk-away prices, whose move it is and for how
    long, expiring quotes, the radar's original estimate and the next moves — highest
    priority first (shared/orchestration/prospect_desk.py)."""
    from shared import view_cache
    from shared.orchestration import prospect_desk as pd
    org = require_org_scope(user)

    async def build() -> list[dict]:
        deals = await service.list_deals(limit=500, org_id=org)
        return [row.model_dump(mode="json") for row in await pd.desk(deals)]

    # One match board per prospect is costly: served from the view cache until a deal changes
    # (or VIEW_CACHE_SECONDS pass, for quotes ageing and replies arriving elsewhere).
    return await view_cache.get_or_compute(f"prospect_desk:{org or 'staff'}", build)


class ParkRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=300)
    until: datetime | None = None


class DropRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


def _who(user: AuthUser) -> str:
    return getattr(user, "email", None) or getattr(user, "user_id", None) or "staff"


async def _prospect_action(deal_id: str, user: AuthUser, coro_factory):
    from shared.orchestration import prospect_actions as pa
    await _org_deal(deal_id, user)
    try:
        return await coro_factory(pa)
    except pa.NotAProspect as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{deal_id}/concession-ladder", response_model=dict)
async def concession_ladder(deal_id: str, user: AuthUser = Depends(get_current_user)) -> dict:
    """When the buyer counters: the 2-3 concession steps from our last offer to our walk-away
    price (the margin floor over the supplier's own price and every cost), never below it."""
    await _org_deal(deal_id, user)
    from agents.negotiation_agent.pricing import concession_ladder as ladder
    from agents.negotiation_agent.repository import get_repository as neg_repo  # type: ignore[import]
    from shared.orchestration import trade_costs
    session = await neg_repo().get_session_by_deal(deal_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no negotiation for this deal yet")
    supplier = session.supplier_last_quote or session.supplier_quote_price
    ask = session.last_offer_to_buyer
    if not supplier or not ask:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="needs the supplier's price and our offer to the buyer first")
    if session.cost_sheet:
        fixed, pct = trade_costs._nums(session.cost_sheet)
    else:
        fixed, pct = (session.cost_envelope.total() if session.cost_envelope else 0.0), 0.0
    walk = (supplier + fixed) / max(1.0 - (session.policy.margin_floor_pct + pct) / 100.0, 0.01)
    out = ladder(ask=float(ask), walk_away=float(walk), counter=session.buyer_last_offer)
    return {**out, "negotiation_id": session.negotiation_id, "margin_floor_pct": session.policy.margin_floor_pct}


@router.get("/board/money", response_model=dict)
async def board_money(user: AuthUser = Depends(get_current_user)) -> dict:
    """Buy price, sell price and profit per deal (firm from the PI / PO / settlement, else estimates)."""
    from shared.orchestration import money_glance
    deals = await service.list_deals(limit=500, org_id=require_org_scope(user))
    return await money_glance.rows(deals)


@router.get("/funnel", response_model=dict)
async def funnel(days: int = Query(default=90, ge=1, le=730), user: AuthUser = Depends(get_current_user)) -> dict:
    """Scans → prospects → interested → matched → PI → paid: counts, conversion, median days, cost."""
    from shared.orchestration import funnel as f
    return await f.build(days, org_id=require_org_scope(user))


@router.get("/{deal_id}/claim-options", response_model=dict)
async def claim_options(deal_id: str, affected_share: float = Query(1.0, gt=0, le=1),
                        supplier_at_fault: bool = True, user: AuthUser = Depends(require_portless)) -> dict:
    """After a failed QC or a complaint: re-inspection, discount, replacement, partial refund,
    cancel — each with its cost to PortLess and the supplier, cheapest first."""
    from shared.orchestration import claims
    try:
        return await claims.claim_options(deal_id, affected_share=affected_share, supplier_at_fault=supplier_at_fault)
    except DealNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class DocumentCheck(BaseModel):
    kind: str
    text: str = Field(min_length=1)
    raise_on_mismatch: bool = True


@router.post("/{deal_id}/documents/check", response_model=dict)
async def check_document(deal_id: str, body: DocumentCheck, user: AuthUser = Depends(require_portless)) -> dict:
    """Compare a document someone sent (certificate, invoice, packing list, draft B/L, report)
    with the deal's facts; a mismatch on a money / customs document raises DOCUMENT_ERROR."""
    await _org_deal(deal_id, user)
    from shared.orchestration import document_check
    try:
        return await document_check.check(deal_id, body.kind, body.text, raise_on_mismatch=body.raise_on_mismatch)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/reorders/{deal_id}/offer", response_model=dict)
async def reorder_offer(deal_id: str, user: AuthUser = Depends(require_portless)) -> dict:
    """Offer the buyer of a completed deal its repeat order now (priced by code, behind the gate)."""
    from shared.orchestration import reorder_agent
    try:
        return await reorder_agent.offer_repeat(deal_id, by=user.email or user.user_id or "staff")
    except DealNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{deal_id}/sourcing/widen", response_model=dict)
async def sourcing_widen(deal_id: str, user: AuthUser = Depends(require_portless)) -> dict:
    """Ask more directory suppliers for a quote now (the sourcing agent's widening, by hand)."""
    from shared.orchestration import sourcing_agent
    try:
        return await sourcing_agent.widen(deal_id, force=True)
    except DealNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/prospects/cleanup", response_model=list[dict])
async def prospects_cleanup_preview(user: AuthUser = Depends(require_portless)) -> list[dict]:
    """Prospects the clean-up would close: untouched for PROSPECT_RETENTION_DAYS, not parked."""
    from shared.orchestration import prospect_cleanup
    return await prospect_cleanup.stale()


@router.post("/prospects/cleanup", response_model=dict)
async def prospects_cleanup_run(user: AuthUser = Depends(require_portless)) -> dict:
    """Close them now (CANCELLED with the reason; records are kept)."""
    from shared.orchestration import prospect_cleanup
    return await prospect_cleanup.run(by=getattr(user, "email", None) or "staff")


class PrearrangedParty(BaseModel):
    legal_name: str = Field(min_length=2)
    country: str | None = None
    email: str | None = None
    phone: str | None = None
    gstin: str | None = None
    iec: str | None = None
    address: str | None = None


class PrearrangedTrade(BaseModel):
    hs_code: str = Field(min_length=4)
    market: str = Field(min_length=2, max_length=2)
    quantity_mt: float = Field(gt=0)
    buyer: PrearrangedParty
    supplier: PrearrangedParty
    buyer_price_usd: float = Field(gt=0, description="USD/MT the buyer agreed or targets (CIF)")
    supplier_price_usd: float = Field(gt=0, description="USD/MT FOB the supplier quoted")
    supplier_quote_written: bool = False
    incoterm: str = "FOB"


@router.post("/prearranged", response_model=DealRecord, status_code=status.HTTP_201_CREATED)
async def start_prearranged(body: PrearrangedTrade, user: AuthUser = Depends(require_portless)) -> DealRecord:
    """A trade where staff know both sides: no outreach or matching — the creation checks run and
    the negotiation opens from the two prices."""
    from shared.orchestration import prearranged
    try:
        deal = await prearranged.start(
            by=getattr(user, "email", None) or "staff", org_id=getattr(user, "org_id", None) or "portless",
            hs_code=body.hs_code, market=body.market.upper(), quantity_mt=body.quantity_mt,
            buyer=body.buyer.model_dump(), supplier=body.supplier.model_dump(),
            buyer_price_usd=body.buyer_price_usd, supplier_price_usd=body.supplier_price_usd,
            supplier_quote_written=body.supplier_quote_written, incoterm=body.incoterm)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    from shared.audit_log import staff_relay
    await staff_relay(user, "deal.prearranged_started", deal.deal_id, buyer=body.buyer.legal_name,
                      supplier=body.supplier.legal_name)
    return deal


@router.get("/realisation/open", response_model=list[dict])
async def realisation_open(user: AuthUser = Depends(require_portless)) -> list[dict]:
    """Shipping Bills whose export proceeds are not fully realised, overdue first (RBI time limit)."""
    from shared.orchestration import realisation
    return await realisation.open_items()


@router.get("/run-costs", response_model=list[dict])
async def all_run_costs(user: AuthUser = Depends(require_portless)) -> list[dict]:
    """Run costs of every deal (LLM, Apify, operator decisions) — Money & Margin."""
    from shared import deal_costs
    return await deal_costs.all_summaries()


@router.get("/outreach/variants", response_model=dict)
async def outreach_variants(user: AuthUser = Depends(require_portless)) -> dict:
    """Introductions sent and replies per opener variant, per product × market."""
    from shared.orchestration import outreach_ab
    deals = await service.get_repository().list_active(limit=2000)
    return {"min_sends": outreach_ab.min_sends(), "corridors": outreach_ab.stats(deals)}


@router.get("/reorders/due", response_model=list[dict])
async def reorders_due(user: AuthUser = Depends(require_portless)) -> list[dict]:
    """Buyers whose usual next order is within REORDER_REMIND_DAYS (median gap between their
    completed deals) and who have no reorder open."""
    from shared.orchestration import reorders
    return await reorders.due()


class ReorderRequest(BaseModel):
    quantity_mt: float | None = Field(default=None, gt=0)


@router.post("/{deal_id}/reorder", response_model=DealRecord, status_code=status.HTTP_201_CREATED)
async def reorder_deal(deal_id: str, body: ReorderRequest | None = None,
                       user: AuthUser = Depends(require_portless)) -> DealRecord:
    """Start a repeat order from a completed deal: same product, market, buyer and supplier,
    the last agreed price as the buyer's target; the supplier gets the RFQ for a fresh quote."""
    from shared.orchestration import reorders
    try:
        return await reorders.reorder(deal_id, by=getattr(user, "email", None) or "staff",
                                      quantity_mt=body.quantity_mt if body else None)
    except DealNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/price-alerts", response_model=list[dict])
async def price_alerts(user: AuthUser = Depends(get_current_user)) -> list[dict]:
    """Supplier price dips (buy now) and spikes (hold offers) from the price book, per product."""
    from shared.orchestration import price_alerts as pa
    return await pa.alerts()


@router.get("/fx/usd-inr", response_model=dict)
async def usd_inr(user: AuthUser = Depends(get_current_user)) -> dict:
    """The USD/INR rate the console uses to show an Indian supplier's INR figure beside the
    USD price (the contract currency stays USD)."""
    from shared.currency import rate_info
    return await rate_info("INR")


class FxReferenceIn(BaseModel):
    currency: str
    rate: float
    source: str


@router.get("/fx/reference", response_model=dict)
async def fx_reference(user: AuthUser = Depends(get_current_user)) -> dict:
    """Reference exchange rates per currency (newest first) and which ones are usable now."""
    from shared import fx_rates
    rows = await fx_rates.history(limit=100)
    usable = {}
    for cur in fx_rates.BANDS:
        row = await fx_rates.usable(cur)
        if row:
            usable[cur] = row
    return {"usable": usable, "history": rows, "max_age_days": fx_rates._f("FX_REFERENCE_MAX_AGE_DAYS", 7)}


@router.post("/fx/reference", response_model=dict)
async def fx_reference_record(body: FxReferenceIn, user: AuthUser = Depends(require_portless)) -> dict:
    """Staff record a reference rate (units per USD) with its source — audited."""
    from shared import fx_rates
    from shared.audit_log import actor_of
    try:
        return await fx_rates.record(body.currency, body.rate, source=body.source, by=actor_of(user))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/fx/reference/refresh", response_model=dict)
async def fx_reference_refresh(user: AuthUser = Depends(require_portless)) -> dict:
    """Pull the published reference feed now (staff)."""
    from shared import fx_rates
    return await fx_rates.refresh_from_source()


@router.post("/radar-estimate/backfill", response_model=dict)
async def backfill_radar_estimate(user: AuthUser = Depends(require_portless)) -> dict:
    """Store the radar's estimate once on every deal from an opportunity that has none (staff)."""
    from shared.orchestration import prospect_desk as pd
    return await pd.backfill_radar_estimates(await service.list_deals(limit=5000))


@router.post("/{deal_id}/retry-sends", response_model=dict)
async def retry_sends(deal_id: str, user: AuthUser = Depends(get_current_user)) -> dict:
    """Re-send now every outreach / RFQ e-mail of the deal that failed in transport and has tries left."""
    from shared.orchestration import send_retry
    await _org_deal(deal_id, user)
    return await send_retry.retry_now(deal_id)


@router.post("/{deal_id}/prospect/park", response_model=DealRecord)
async def park_prospect(deal_id: str, body: ParkRequest, user: AuthUser = Depends(get_current_user)) -> DealRecord:
    """Stop every reminder for a prospect and keep it (optionally until a date)."""
    return await _prospect_action(deal_id, user, lambda pa: pa.park(deal_id, body.reason, _who(user), body.until))


@router.post("/{deal_id}/prospect/unpark", response_model=DealRecord)
async def unpark_prospect(deal_id: str, user: AuthUser = Depends(get_current_user)) -> DealRecord:
    return await _prospect_action(deal_id, user, lambda pa: pa.unpark(deal_id, _who(user)))


@router.post("/{deal_id}/prospect/drop", response_model=DealRecord)
async def drop_prospect(deal_id: str, body: DropRequest, user: AuthUser = Depends(get_current_user)) -> DealRecord:
    """Close a prospect as CANCELLED with the reason; its pending prospect gates close as superseded."""
    return await _prospect_action(deal_id, user, lambda pa: pa.drop(deal_id, body.reason, _who(user)))


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
            status_code=422,
            detail=f"{target.value} is set by its own approved step or provider confirmation, not by hand",
        )
    if target == DealStatus.CANCELLED:
        from services.escrow_guard.repository import get_repository as escrow_repository
        hold = await escrow_repository().get_hold_by_deal(deal_id)
        if hold is not None and (hold.funded_amount or 0) > hold.refunded_total:
            raise HTTPException(
                status_code=422,
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
            status_code=422,
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
        updates = payload.model_dump(exclude_none=True, exclude={"evidence"})
        deal = await service.update_party(deal_id, role, updates, source="operator")
        from shared.audit_log import staff_relay
        await staff_relay(user, f"party.{role}_updated", deal_id, fields=sorted(updates),
                          evidence=payload.evidence)
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
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DealNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


class InspectionPlanChoice(BaseModel):
    types: list[str]
    """Any of DUPRO / PSI / PEO (PSI is always included)."""
    reason: str = ""


@router.get("/{deal_id}/inspection-options", response_model=dict)
async def inspection_options(deal_id: str, user: AuthUser = Depends(require_portless)) -> dict:
    """Inspectors' prices for this booking and the lighter inspection plans that would be enough."""
    from shared.orchestration import inspection_options as io
    try:
        return await io.options(deal_id)
    except DealNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
        raise HTTPException(422, detail=f"unknown inspection type(s): {unknown}")
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
    from shared.auth.roles import RoleRequired, require_compliance_officer
    try:
        require_compliance_officer(user, "Clearing a compliance hold")
    except RoleRequired as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
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

@router.get("/{deal_id}/run-costs")
async def deal_run_costs(deal_id: str, user: AuthUser = Depends(require_portless)) -> dict:
    """What the deal cost to run so far: LLM calls / tokens / USD, Apify runs / USD, operator
    decisions and how long gates waited (shared/deal_costs.py)."""
    from shared import deal_costs
    return await deal_costs.summary(deal_id)


class ExposureAccept(BaseModel):
    reason: str = Field(min_length=10)


@router.post("/{deal_id}/exposure/accept")
async def accept_exposure(deal_id: str, body: ExposureAccept, user: AuthUser = Depends(require_portless)) -> dict:
    """A money approver accepts this deal's exposure above a limit; the withheld PI can then go out."""
    from shared.auth.roles import is_money_approver
    if not is_money_approver(getattr(user, "email", None)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="accepting exposure above a limit needs a money approver (AUTH_MONEY_APPROVER_EMAILS)")
    await _org_deal(deal_id, user)
    from services.deals.repository import get_repository as deals_repo
    who = getattr(user, "email", None) or "staff"
    await deals_repo().set_fields(deal_id, {"exposure_accepted_by": who})
    from shared.audit_log import staff_relay
    await staff_relay(user, "deal.exposure_accepted", deal_id, reason=body.reason)
    return {"deal_id": deal_id, "exposure_accepted_by": who}


@router.get("/{deal_id}/export-certificates")
async def export_certificates(deal_id: str, user: AuthUser = Depends(get_current_user)) -> dict:
    """Certificates this shipment needs (phytosanitary, APEDA RCAC for basmati, origin …) and which are recorded."""
    deal = await _org_deal(deal_id, user)
    from shared.orchestration import export_certs
    return await export_certs.status(deal)


class ExportCertificate(BaseModel):
    kind: str
    number: str = Field(min_length=3, max_length=60)
    issued_on: str | None = None
    issuer: str | None = None
    evidence: str | None = Field(default=None, max_length=300)


@router.post("/{deal_id}/export-certificates")
async def record_export_certificate(deal_id: str, body: ExportCertificate,
                                    user: AuthUser = Depends(require_portless)) -> dict:
    """Record an issued certificate (number, date, issuer, evidence) — audited."""
    deal = await _org_deal(deal_id, user)
    from shared.orchestration import export_certs
    try:
        await export_certs.record(deal_id, body.kind, number=body.number, issued_on=body.issued_on,
                                  issuer=body.issuer, evidence=body.evidence, by=getattr(user, "email", None) or "staff")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from shared.audit_log import staff_relay
    await staff_relay(user, "shipment.certificate_recorded", deal_id, kind=body.kind, number=body.number)
    return await export_certs.status(deal)


@router.get("/{deal_id}/realisation")
async def deal_realisation(deal_id: str, user: AuthUser = Depends(get_current_user)) -> dict:
    """Per Shipping Bill: invoice value, realised so far, outstanding and the realisation deadline."""
    await _org_deal(deal_id, user)
    from shared.orchestration import realisation
    return await realisation.for_deal(deal_id)


@router.get("/{deal_id}/containers")
async def deal_containers(deal_id: str, user: AuthUser = Depends(get_current_user)) -> dict:
    """The cheapest container mix for the deal's lot and lane (per-size lane quotes, the
    product's stowage factor) and how full it travels."""
    deal = await _org_deal(deal_id, user)
    from shared.orchestration import container_choice
    return await container_choice.choose(deal.market, deal.hs_code, float(deal.quantity_mt or 20.0))


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
    evidence: RelayEvidence | None = None
    """Call note, WhatsApp screenshot, photo of the signed PO (relay_evidence.py)."""


@router.get("/{deal_id}/supplier-commitment")
async def supplier_commitment_status(deal_id: str, user: AuthUser = Depends(get_current_user)) -> dict:
    """Which supplier holds the firm order request, who was asked before and
    why standby suppliers were passed over, and the PO acknowledgement."""
    await _org_deal(deal_id, user)
    from shared.orchestration import supplier_commitment
    rec = await supplier_commitment.get(deal_id)
    return rec.model_dump(mode="json") if rec else {"deal_id": deal_id, "status": "none"}


def _require_evidence(evidence: RelayEvidence | None) -> None:
    from shared.orchestration import relay_evidence
    try:
        relay_evidence.check(evidence)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _stamp(evidence: RelayEvidence | None) -> dict | None:
    from shared.orchestration import relay_evidence
    return relay_evidence.stamp(evidence)


@router.get("/{deal_id}/supplier-steps", response_model=dict)
async def supplier_steps(deal_id: str, user: AuthUser = Depends(require_portless)) -> dict:
    """Each supplier-side step (quote, order confirmation, PO acknowledgement, goods-ready date,
    facility address, bank details): done by e-mail or by a staff relay, with its evidence."""
    deal = await _org_deal(deal_id, user)
    from shared.orchestration import relay_evidence
    return await relay_evidence.supplier_steps(deal)


@router.post("/{deal_id}/supplier-commitment/confirm")
async def supplier_commitment_confirm(
    deal_id: str, body: OperatorNote, user: AuthUser = Depends(require_portless),
) -> dict:
    """The supplier confirmed outside e-mail (phone, WhatsApp): open the
    CONFIRM_SUPPLIER_ORDER approval exactly as an e-mailed confirmation does."""
    await _org_deal(deal_id, user)
    _require_evidence(body.evidence)
    from shared.orchestration import supplier_commitment
    rec = await supplier_commitment.get(deal_id)
    if rec is None or rec.status != "requested" or rec.current is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no open supplier order request")
    await supplier_commitment._gate_confirmation(rec, f"operator:{user.email or user.user_id}", body.reason)
    from shared.audit_log import staff_relay
    await staff_relay(user, "supplier.order_confirmation_relayed", deal_id, supplier_id=rec.current.supplier_id,
                      reason=body.reason, evidence=_stamp(body.evidence))
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
    from shared.audit_log import staff_relay
    await staff_relay(user, "supplier.skipped_to_next", deal_id, outcome=outcome, reason=body.reason,
                      evidence=_stamp(body.evidence))
    return {"deal_id": deal_id, "status": outcome}


@router.post("/{deal_id}/supplier-commitment/po-ack")
async def supplier_commitment_po_ack(
    deal_id: str, body: OperatorNote, user: AuthUser = Depends(require_portless),
) -> dict:
    """The supplier acknowledged the PO outside e-mail: record it and book the inspection."""
    deal = await _org_deal(deal_id, user)
    _require_evidence(body.evidence)
    if (getattr(deal.status, "value", deal.status)) != "PO_ISSUED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="deal is not at PO_ISSUED")
    from shared.orchestration import supplier_commitment
    tpo = await supplier_commitment.acknowledge_po(deal_id, by=f"{user.email or user.user_id}: {body.reason}")
    from shared.audit_log import staff_relay
    await staff_relay(user, "supplier.po_ack_relayed", deal_id, reason=body.reason, evidence=_stamp(body.evidence))
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
    market_rules: list[str] = []
    try:  # destination quality rules (catalogue) join the inspector's checks
        from shared import catalogue
        deal = await service.get_deal(deal_id)
        market_rules = catalogue.quality_requirements_for(deal.hs_code, getattr(deal, "market", None))
    except Exception:  # noqa: BLE001
        market_rules = []
    return {"deal_id": deal_id, "packing": spec, "description": packing.describe(spec) or None,
            "special": packing.special(spec), "surcharge_usd_per_mt": extra, "surcharge_parts": parts,
            "in_supplier_price": bool(getattr(session, "packing_in_supplier_price", False)),
            "qc_checks": packing.qc_checklist(spec) + [f"destination rule: {r}" for r in market_rules],
            "market_quality_requirements": market_rules}


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
        raise HTTPException(status_code=422, detail="give the packing as text or fields")
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
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    if offer.source == "operator" or offer.relay_evidence:
        from shared.audit_log import staff_relay
        await staff_relay(user, "supplier.quote_relayed", deal_id, supplier_id=offer.supplier_id,
                          fob_price_usd_per_mt=offer.fob_price_usd_per_mt, evidence=offer.relay_evidence)
    # The same check as a scanned supplier (reused when fresh), then the board is re-scored.
    from shared.orchestration import offer_checks
    offer = await offer_checks.check_offer(offer)
    updated = await service.upsert_supplier_offer(deal_id, offer)
    if offer.fob_price_usd_per_mt:
        from shared import pricebook
        await pricebook.record(pricebook.PriceObservation(
            hs_code=deal.hs_code, side="supplier", kind="supplier_offer",
            price_usd_per_mt=offer.fob_price_usd_per_mt, grade=offer.grade, deal_id=deal_id,
            counterparty_id=offer.supplier_id, note="entered by operator",
        ))
    try:
        from shared.orchestration import matchmaking
        await matchmaking.evaluate(deal_id)
    except Exception as exc:  # noqa: BLE001 - the offer is saved; Re-score on the board retries
        logger.warning("Deal %s: board not re-scored after the operator offer: %s", deal_id, exc)
    return await service.get_deal(deal_id)


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


@router.post("/{deal_id}/match/best-and-final", response_model=dict)
async def best_and_final(deal_id: str, user: AuthUser = Depends(get_current_user)) -> dict:
    """Open the SEND_BEST_AND_FINAL gate: the other quoted suppliers are asked to match or
    beat the best quote (on their RFQ threads; the competitor is never named)."""
    from shared.orchestration import best_and_final as bf
    await _org_deal(deal_id, user)
    try:
        return {"approval_id": await bf.open_gate(deal_id)}
    except bf.NothingToAsk as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{deal_id}/match/indicative-offer", response_model=dict)
async def request_indicative_offer(deal_id: str, ref: _PairRef, user: AuthUser = Depends(get_current_user)) -> dict:
    """Operator asks for an indicative offer to this buyer from this supplier's quote (SEND_INDICATIVE_OFFER gate)."""
    deal = await _org_deal(deal_id, user)
    board, pair = await _pair(deal, ref)
    if not pair.landed or pair.supplier_price.basis != "quoted":
        raise HTTPException(status_code=422,
                            detail="an indicative offer needs this supplier's quoted price")
    if pair.blockers:
        raise HTTPException(status_code=422, detail=pair.blockers[0])
    from shared.orchestration import matchmaking
    lead = next((o.get("lead_time_days") for o in board.offers if o["offer_id"] == ref.offer_id), None)
    gid = await matchmaking.propose_indicative_offer(deal, pair, board, lead)
    if gid is None:
        raise HTTPException(status_code=422, detail="buyer has no e-mail address")
    return {"deal_id": deal_id, "approval_id": gid}


@router.post("/{deal_id}/match/confirm", response_model=dict)
async def request_match(deal_id: str, ref: _PairRef, user: AuthUser = Depends(get_current_user)) -> dict:
    """Operator proposes this pair as the deal (CONFIRM_MATCH gate) — even below the
    probability threshold, as long as nothing blocks it."""
    deal = await _org_deal(deal_id, user)
    _, pair = await _pair(deal, ref)
    if pair.blockers:
        raise HTTPException(status_code=422, detail=pair.blockers[0])
    if not pair.buyer_price.value or not pair.supplier_price.value or not pair.firm:
        raise HTTPException(
            status_code=422,
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


# ---------------------------------------------------------------------------
# Samples before the first order, and the buyer's import permits (staff).
# ---------------------------------------------------------------------------

class SampleRequest(BaseModel):
    note: str | None = None
    buyer: str | None = None
    """Candidate id when several buyers are courted."""


class SampleCourier(BaseModel):
    courier: str
    awb: str = Field(min_length=1)
    cost_usd: float | None = None


class SampleVerdict(BaseModel):
    verdict: str
    """approved · rejected"""
    note: str | None = None


def _staff_name(user: AuthUser) -> str:
    return getattr(user, "email", None) or getattr(user, "user_id", None) or "staff"


@router.get("/{deal_id}/samples")
async def list_samples(deal_id: str, user: AuthUser = Depends(require_portless)) -> dict:
    deal = await _org_deal(deal_id, user)
    return {"samples": deal.samples, "approved_sample_ref": deal.approved_sample_ref}


@router.post("/{deal_id}/samples")
async def request_sample(deal_id: str, body: SampleRequest, user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import samples
    try:
        row = await samples.request(deal_id, by=_staff_name(user), note=body.note, buyer=body.buyer)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    from shared.audit_log import staff_relay
    await staff_relay(user, "sample.requested", deal_id, sample_id=row["sample_id"])
    return row


@router.post("/{deal_id}/samples/{sample_id}/courier")
async def sample_courier(deal_id: str, sample_id: str, body: SampleCourier,
                         user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import samples
    try:
        row = await samples.record_courier(deal_id, sample_id, courier=body.courier, awb=body.awb,
                                           cost_usd=body.cost_usd, by=_staff_name(user))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from shared.audit_log import staff_relay
    await staff_relay(user, "sample.shipped", deal_id, sample_id=sample_id, awb=row["awb"])
    return row


@router.post("/{deal_id}/samples/{sample_id}/verdict")
async def sample_verdict(deal_id: str, sample_id: str, body: SampleVerdict,
                         user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import samples
    try:
        row = await samples.record_verdict(deal_id, sample_id, verdict=body.verdict, note=body.note,
                                           by=_staff_name(user))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from shared.audit_log import staff_relay
    await staff_relay(user, "sample.verdict", deal_id, sample_id=sample_id, verdict=row["status"])
    return row


class BuyerPermit(BaseModel):
    name: str = Field(min_length=2)
    """As the catalogue names it after ``buyer:`` (e.g. "SFDA importer registration")."""
    number: str = Field(min_length=1)
    valid_until: str | None = None


@router.put("/{deal_id}/buyer-permits")
async def put_buyer_permit(deal_id: str, body: BuyerPermit, user: AuthUser = Depends(require_portless)) -> dict:
    """Record an import permit the buyer holds (number + validity) — the PI waits for the ones its
    product × market needs."""
    deal = await _org_deal(deal_id, user)
    if body.valid_until:
        try:
            datetime.fromisoformat(body.valid_until)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="valid_until must be an ISO date") from exc
    permits = dict(deal.buyer_permits or {})
    permits[body.name.strip()] = {"number": body.number.strip(), "valid_until": body.valid_until,
                                  "recorded_by": _staff_name(user)}
    from services.deals.repository import get_repository
    await get_repository().set_fields(deal_id, {"buyer_permits": permits})
    from shared.audit_log import staff_relay
    await staff_relay(user, "buyer_permit.recorded", deal_id, permit=body.name.strip())
    from shared.orchestration import compliance
    missing = [f.data.get("missing") for f in compliance.check_buyer_permits(deal.model_copy(update={"buyer_permits": permits}))]
    return {"buyer_permits": permits, "still_missing": (missing[0] if missing else [])}


class IncentiveCredit(BaseModel):
    shipping_bill: str = Field(min_length=1)
    kind: str
    """rodtep · drawback"""
    amount_usd: float
    credited_on: str | None = None


@router.post("/{deal_id}/incentive-credits")
async def record_incentive_credit(deal_id: str, body: IncentiveCredit, user: AuthUser = Depends(require_portless)) -> dict:
    """Record a RoDTEP scrip / drawback credit from the ICEGATE ledger (staff, audited) and compare."""
    await _org_deal(deal_id, user)
    from shared.orchestration import incentive_credits
    try:
        row = await incentive_credits.record(deal_id, shipping_bill=body.shipping_bill, kind=body.kind,
                                             amount_usd=body.amount_usd, credited_on=body.credited_on,
                                             by=_staff_name(user))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from shared.audit_log import staff_relay
    await staff_relay(user, "incentive.credited", deal_id, shipping_bill=row["shipping_bill"], amount_usd=row["amount_usd"])
    return await incentive_credits.check(deal_id)


@router.get("/{deal_id}/incentive-credits")
async def incentive_credit_check(deal_id: str, user: AuthUser = Depends(require_portless)) -> dict:
    await _org_deal(deal_id, user)
    from shared.orchestration import incentive_credits
    return await incentive_credits.check(deal_id)


# ---------------------------------------------------------------------------
# CHA hand-off, cargo insurance, letter of intent (todo 2026-11-19 batch)
# ---------------------------------------------------------------------------

class ShippingBillBody(BaseModel):
    number: str
    date: str | None = None
    port_code: str | None = None
    leo_date: str | None = None
    declared: list[str] = Field(default_factory=list)


class ChaInvoiceBody(BaseModel):
    amount_usd: float
    items: dict[str, float] | None = None


class InsuranceDeclarationBody(BaseModel):
    certificate_no: str
    insurer: str | None = None
    sum_insured_usd: float | None = None


class InsuranceClaimBody(BaseModel):
    reason: str
    amount_usd: float
    delivered_on: str | None = None


class InsuranceClaimUpdate(BaseModel):
    status: str
    survey_ref: str | None = None
    recovered_usd: float | None = None
    note: str | None = None


class LoiBody(BaseModel):
    text: str | None = None
    """The buyer's LOI text (read like an e-mail); or the facts below."""
    quantity_mt: float | None = None
    delivery_window: str | None = None
    port: str | None = None
    spec: str | None = None
    reference: str | None = None
    candidate_id: str | None = None


class FreightRequestBody(BaseModel):
    ready_date: str | None = None
    port_of_loading: str | None = None
    port_of_discharge: str | None = None


@router.get("/{deal_id}/freight/quotes")
async def deal_freight_quotes(deal_id: str, user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import freight_procurement
    deal = await _org_deal(deal_id, user)
    rfq = deal.freight_rfq or {}
    return {**rfq, "best": freight_procurement.best(rfq)}


@router.post("/{deal_id}/freight/request")
async def deal_freight_request(deal_id: str, body: FreightRequestBody, user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import freight_procurement
    await _org_deal(deal_id, user)
    try:
        return await freight_procurement.request_quotes(deal_id, by=_staff_name(user), ready_date=body.ready_date,
                                                        pol=body.port_of_loading, pod=body.port_of_discharge)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{deal_id}/freight/counter")
async def deal_freight_counter(deal_id: str, user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import freight_procurement
    await _org_deal(deal_id, user)
    try:
        return await freight_procurement.counter(deal_id, by=_staff_name(user))
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{deal_id}/cha")
async def deal_cha(deal_id: str, user: AuthUser = Depends(require_portless)) -> dict:
    deal = await _org_deal(deal_id, user)
    return deal.cha or {}


@router.post("/{deal_id}/cha/pack")
async def deal_cha_pack(deal_id: str, port: str | None = None, user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import cha
    await _org_deal(deal_id, user)
    try:
        return await cha.send_pack(deal_id, by=_staff_name(user), port=port)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{deal_id}/cha/shipping-bill")
async def deal_shipping_bill(deal_id: str, body: ShippingBillBody, user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import cha
    await _org_deal(deal_id, user)
    try:
        row = await cha.record_shipping_bill(deal_id, body.model_dump(), by=_staff_name(user))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from shared.audit_log import staff_relay
    await staff_relay(user, "cha.shipping_bill", deal_id, number=row["number"])
    return row


@router.post("/{deal_id}/cha/invoice")
async def deal_cha_invoice(deal_id: str, body: ChaInvoiceBody, user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import cha
    await _org_deal(deal_id, user)
    try:
        return await cha.record_invoice(deal_id, amount_usd=body.amount_usd, items=body.items, by=_staff_name(user))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{deal_id}/insurance")
async def deal_insurance(deal_id: str, user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import cargo_insurance
    deal = await _org_deal(deal_id, user)
    return {**(deal.insurance or {}), "overdue_claims": cargo_insurance.overdue(deal)}


@router.post("/{deal_id}/insurance/declaration")
async def insurance_declaration(deal_id: str, body: InsuranceDeclarationBody,
                                user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import cargo_insurance
    await _org_deal(deal_id, user)
    try:
        return await cargo_insurance.record_declaration(deal_id, certificate_no=body.certificate_no, insurer=body.insurer,
                                                        sum_insured_usd=body.sum_insured_usd, by=_staff_name(user))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{deal_id}/insurance/claims")
async def insurance_claim(deal_id: str, body: InsuranceClaimBody, user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import cargo_insurance
    await _org_deal(deal_id, user)
    try:
        return await cargo_insurance.open_claim(deal_id, reason=body.reason, amount_usd=body.amount_usd,
                                                delivered_on=body.delivered_on, by=_staff_name(user))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{deal_id}/insurance/claims/{claim_id}")
async def insurance_claim_update(deal_id: str, claim_id: str, body: InsuranceClaimUpdate,
                                 user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import cargo_insurance
    await _org_deal(deal_id, user)
    try:
        row = await cargo_insurance.update_claim(deal_id, claim_id, status=body.status, by=_staff_name(user),
                                                 survey_ref=body.survey_ref, recovered_usd=body.recovered_usd, note=body.note)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from shared.audit_log import staff_relay
    await staff_relay(user, "insurance.claim", deal_id, claim_id=claim_id, status=row["status"])
    return row


@router.post("/{deal_id}/loi")
async def record_loi(deal_id: str, body: LoiBody, user: AuthUser = Depends(require_portless)) -> dict:
    """Record a buyer's letter of intent (pasted text or its facts)."""
    from shared.orchestration import loi
    await _org_deal(deal_id, user)
    if body.text and loi.broker_chain_pattern(body.text):
        raise HTTPException(status_code=422, detail="the text carries the LOI / ICPO / FCO broker-chain pattern — "
                                                    "confirm the buyer directly before recording it")
    facts = loi.parse(body.text) if body.text else {"is_loi": True}
    if facts is None:
        raise HTTPException(status_code=422, detail="the text is not a letter of intent")
    facts.update({k: v for k, v in body.model_dump(exclude={"text", "candidate_id"}).items() if v is not None})
    return await loi.record(deal_id, facts, source="staff", by=_staff_name(user), candidate_id=body.candidate_id)


@router.get("/{deal_id}/loi/template")
async def loi_template(deal_id: str, incoterm: str = "CIF", place: str = "", delivery_window: str = "within 45 days",
                       user: AuthUser = Depends(require_portless)) -> dict:
    from shared.orchestration import loi
    from shared.product_label import product_label
    deal = await _org_deal(deal_id, user)
    price = deal.buyer_target_price_usd
    return {"text": loi.template(product=product_label(deal.hs_code or "") or f"HS {deal.hs_code}", spec=None,
                                 quantity_mt=float(deal.quantity_mt or 0), price_low=round(price * 0.97) if price else None,
                                 price_high=round(price * 1.03) if price else None, incoterm=incoterm,
                                 place=place or (deal.market or ""), delivery_window=delivery_window)}
