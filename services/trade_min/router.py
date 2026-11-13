"""HTTP routes for the Trade-Min service (FR-1, LLD 4.5, §10)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from shared import catalogue as _catalogue
from shared.catalogue import ProductLine

from . import directory, service, validation
from .config import DEFAULT_CONFIG
from .schemas import CounterpartyRecord, EnrichedOpportunity, MatchEngineSeed, ScanRequest

router = APIRouter(prefix="/trade-min", tags=["trade-min"])


class CatalogueUpdate(BaseModel):
    products: list[ProductLine] = Field(default_factory=list)
    updated_by: str = "operator"


def _catalogue_view() -> dict:
    cat = _catalogue.get_catalogue()
    return {
        **cat.model_dump(mode="json"),
        "corridors": _catalogue.corridors(),
        "defaults": {
            "hs_codes": list(DEFAULT_CONFIG.default_hs_codes),
            "markets": list(DEFAULT_CONFIG.default_markets),
        },
        "active": bool(_catalogue.active_lines()),
    }


@router.get("/catalogue")
async def get_catalogue() -> dict:
    """What PortLess sells and where: product lines (HS code, search words,
    markets, seller profile) that drive the scanner, the Apify actor inputs
    and the outreach e-mails. Empty → the built-in defaults are used."""
    return _catalogue_view()


@router.get("/hs-search")
async def hs_search(q: str = Query("", description="HS code prefix or product words"),
                    limit: int = Query(20, ge=1, le=50)) -> dict:
    """The console's HS code picker: matching codes with their product name and official description."""
    from shared import hs_names
    return {"results": hs_names.search(q, limit)}


@router.get("/hs-names")
async def hs_names(codes: str = Query("", description="Comma-separated HS codes, at most 100")) -> dict:
    """The product name the console shows next to each HS code (catalogue name, curated short name,
    else the official HS description — shared/hs_names.py) and the full official description for the
    tooltip; null only for a code no source knows. Readable by every signed-in user."""
    from shared import hs_names
    wanted = [c.strip() for c in codes.split(",") if c.strip()][:100]
    return {"names": {code: hs_names.display_name(code) for code in wanted},
            "descriptions": {code: hs_names.official(code) for code in wanted}}


@router.put("/catalogue")
async def put_catalogue(payload: CatalogueUpdate) -> dict:
    """Replace the catalogue (Mongo-persisted; in-memory without MONGO_URI)."""
    try:
        await _catalogue.set_catalogue(payload.products, updated_by=payload.updated_by)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    from shared.orchestration.memory import remember_product
    for line in payload.products:
        remember_product(line)
    return _catalogue_view()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "trade-min"}


@router.get("/sources")
async def list_sources() -> dict:
    """Which demand / buyer / seller / enrichment sources the scanner can reach
    right now (live, stub, mock, open) — including the PortLess-owned Apify
    actors and whether an APIFY_TOKEN is configured."""
    return service.describe_sources()


@router.post("/scan", response_model=list[EnrichedOpportunity])
async def scan(payload: ScanRequest) -> list[EnrichedOpportunity]:
    """Trigger a 5-phase opportunity discovery scan.

    Runs all configured connectors concurrently and returns enriched
    opportunities sorted by urgency tier then arbitrage rank.
    """
    return await service.scan_opportunities(payload)


class KnowledgeNote(BaseModel):
    subject_type: str = Field(description="buyer | supplier | product | market | deal")
    subject_id: str = Field(description="company domain / candidate id, HS code, ISO-2 market or deal id")
    text: str = Field(min_length=3, max_length=8000)
    author: str | None = None


@router.post("/knowledge/notes", response_model=dict)
async def add_knowledge_note(note: KnowledgeNote) -> dict:
    """Remember an operator note in knowledge-service (RAG) — e.g. "supplier X failed
    QC on moisture in March", "buyer Y only buys with LC at sight". The negotiation
    agent and the reply reader read it back; money decisions never do."""
    from shared import knowledge
    from shared.orchestration.memory import remember_note
    if not knowledge.enabled():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="knowledge-service not configured (LLM_GATEWAY_URL + service account)")
    queued = remember_note(subject_type=note.subject_type, subject_id=note.subject_id, text=note.text,
                           author=note.author)
    return {"queued": queued, "subject_type": note.subject_type, "subject_id": note.subject_id}


@router.get("/knowledge/search", response_model=dict)
async def search_knowledge(q: str = Query(..., min_length=2), top_k: int = Query(8, ge=1, le=20)) -> dict:
    """What PortLess remembers that matches ``q`` (e-mails, notes, products, deal summaries)."""
    from shared import knowledge
    if not knowledge.enabled():
        return {"enabled": False, "results": []}
    results = await knowledge.recall(q, top_k=top_k)
    return {"enabled": True, "results": [
        {"text": (r.get("text") or r.get("content") or "")[:1200], "metadata": r.get("metadata") or {},
         "graph_fact": knowledge.is_graph_fact(r), "score": r.get("score")} for r in results]}


@router.get("/price-book/{hs_code}", response_model=dict)
async def price_book(hs_code: str, market: str | None = Query(None, description="ISO-2 destination market"),
                     grade: str | None = Query(None)) -> dict:
    """Collected price information for an HS code: the public Comtrade anchors, the
    buyer-side (CIF) and supplier-side (FOB India) estimates with their range and
    confidence, and the observations behind them (quotes, targets, counters, agreed)."""
    from shared import pricebook
    priors = await pricebook.public_priors(hs_code, market.upper() if market else None)
    buyer = await pricebook.estimate(hs_code, "buyer", market=market, grade=grade, priors=priors)
    supplier = await pricebook.estimate(hs_code, "supplier", market=market, grade=grade, priors=priors)
    obs = await pricebook.observations(hs_code, limit=200)
    return {"hs_code": hs_code, "market": market, "priors": priors,
            "buyer": buyer.model_dump(mode="json"), "supplier": supplier.model_dump(mode="json"),
            "observations": [o.model_dump(mode="json") for o in obs]}


@router.get("/buyers", response_model=list[CounterpartyRecord])
async def list_buyers(
    hs_code: str | None = Query(default=None),
    market: str | None = Query(default=None, description="ISO-2 market, e.g. AE"),
    q: str | None = Query(default=None, description="Search name / e-mail / website / country"),
    country: str | None = Query(default=None, description="ISO-2 country of the buyer itself, e.g. DE"),
    validation_status: Literal["verified", "unverified", "failed"] | None = Query(
        default=None, alias="validation", description="Scan-time check result; omit for all"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[CounterpartyRecord]:
    """Every buyer a scan found, in any country — with or without an opportunity
    or deal — newest first, merged per company across scans."""
    return await directory.list_counterparties("buyer", hs_code=hs_code, market=market, q=q,
                                               country=country, limit=limit,
                                               validation=validation_status)


@router.get("/suppliers", response_model=list[CounterpartyRecord])
async def list_suppliers(
    hs_code: str | None = Query(default=None),
    market: str | None = Query(default=None, description="Market the supplier was scanned for"),
    q: str | None = Query(default=None, description="Search name / e-mail / website"),
    country: str | None = Query(default=None, description="ISO-2 country of the supplier, e.g. IN for India only"),
    validation_status: Literal["verified", "unverified", "failed"] | None = Query(
        default=None, alias="validation", description="Scan-time check result; omit for all"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[CounterpartyRecord]:
    """Every supplier a scan found, in any country (``country=IN`` → India only) —
    with or without an opportunity or deal — newest first, merged per company."""
    return await directory.list_counterparties("supplier", hs_code=hs_code, market=market, q=q,
                                               country=country, limit=limit,
                                               validation=validation_status)


@router.post("/{role}/{record_id:path}/validate", response_model=CounterpartyRecord)
async def validate_counterparty(role: Literal["buyers", "suppliers"], record_id: str) -> CounterpartyRecord:
    """Run the scan-time check now for one directory record (sanctions, e-mail,
    GST / IEC for Indian suppliers) — e.g. a worldwide-search company or one
    outside an opportunity's top N. ``record_id`` is the record's ``id``."""
    store = await directory.get_directory()
    rec = await store.get(record_id)
    if rec is None or rec.role != role.removesuffix("s"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{record_id!r} not found")
    rec = await validation.validate_record(rec)
    await store.set_validation(rec)
    return rec


@router.get("/opportunities/sourcing-status")
async def opportunities_sourcing_status(ids: str = Query("", description="Comma-separated opportunity ids, at most 200")) -> dict:
    """For the Opportunity Radar: per opportunity with a deal — RFQs sent, quotes and the best
    price, and whether a supplier has confirmed the order at its price (stage
    confirmed / awaiting_confirmation / quoted / rfq_sent / no_rfq)."""
    from .sourcing_status import sourcing_status
    wanted = [i.strip() for i in ids.split(",") if i.strip()][:200]
    return {"status": await sourcing_status(wanted)}


@router.get("/opportunities", response_model=list[EnrichedOpportunity])
async def list_opportunities(
    limit: int = Query(default=100, ge=1, le=500),
    min_rank: float = Query(default=0.0, ge=0.0, le=100.0),
    urgency_tier: int | None = Query(default=None, ge=1, le=4),
    counterparties: Literal["verified", "all"] = Query(
        default="all", description="verified → only buyers / suppliers that passed the scan-time check"),
) -> list[EnrichedOpportunity]:
    """List active enriched opportunities.

    Results are sorted by urgency tier (4 = emergency first) then arbitrage rank.
    Use ``urgency_tier`` to filter for a specific urgency level only.
    """
    return await service.list_opportunities(
        limit=limit,
        min_rank=min_rank,
        urgency_tier=urgency_tier,
        counterparties=counterparties,
    )


@router.get("/opportunities/{opportunity_id}", response_model=EnrichedOpportunity)
async def get_opportunity(
    opportunity_id: str,
    counterparties: Literal["verified", "all"] = Query(default="all"),
) -> EnrichedOpportunity:
    """Fetch a single enriched opportunity by ID."""
    result = await service.get_opportunity(opportunity_id, counterparties)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Opportunity {opportunity_id!r} not found",
        )
    return result


@router.get(
    "/opportunities/{opportunity_id}/match-seed",
    response_model=MatchEngineSeed,
    summary="Get Match Engine seed values for an opportunity",
)
async def get_match_seed(opportunity_id: str) -> MatchEngineSeed:
    """Return pre-computed Match Engine input values derived from the opportunity.

    The Deals service / DealMaker agent calls this when creating a deal from an
    opportunity to seed ``ScoreRequest`` without redundant connector calls:

    - ``buyer_target_price_usd``  → ``ScoreRequest.buyer_target_price``
    - ``top_sellers[i].fob_price_usd`` → ``ScoreRequest.supplier_quote_price``
    - ``top_sellers[i].supplier_to_port_km`` → ``ScoreRequest.supplier_to_port_km``

    Fields not pre-seeded here (resolved in the Sourcing phase):
    ``deal_id``, ``verification``, ``business_age_years``,
    ``udyam_registered``, ``annual_shipment_count``.
    """
    seed = await service.get_match_seed(opportunity_id)
    if seed is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Opportunity {opportunity_id!r} not found",
        )
    return seed


@router.post(
    "/opportunities/{opportunity_id}/convert",
    status_code=status.HTTP_200_OK,
    summary="Mark an opportunity as CONVERTED",
)
async def mark_converted(opportunity_id: str) -> dict[str, bool]:
    """Mark an opportunity as CONVERTED once a deal has been created from it.

    Called by the Deals service after ``deal.created`` is emitted to prevent
    the opportunity from being surfaced to other deal flows.
    """
    updated = await service.mark_opportunity_converted(opportunity_id)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Opportunity {opportunity_id!r} not found",
        )
    return {"updated": True}
