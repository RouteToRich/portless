"""HTTP routes for the Trade-Min service (FR-1, LLD 4.5, §10)."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from shared import catalogue as _catalogue
from shared.catalogue import ProductLine

from . import accuracy, directory, freight_lanes, incentives, relationships, scan_reports, service, validation
from .config import DEFAULT_CONFIG
from .schemas import CounterpartyRecord, EnrichedOpportunity, MatchEngineSeed, ScanReport, ScanRequest

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


def _staff():
    from shared.auth.dependencies import require_portless
    return require_portless


@router.get("/freight-lanes/check")
async def freight_lane_check(market: str, usd_per_container: float = Query(gt=0),
                             container_type: str = "dry") -> dict:
    """Is a freight quote in line with what the lane has cost (median of the last 180 days)?"""
    from . import freight_lanes as fl
    return await fl.check_quote(market, usd_per_container, container_type)


@router.get("/scan/plan")
async def scan_plan() -> dict:
    """Which corridors to scan, rescan or skip today, ranked by expected profit per Apify dollar
    and cut at the day's Apify budget; ``scan_request`` is the body for POST /trade-min/scan."""
    from . import scan_plan as plan
    return await plan.build_plan()


@router.get("/catalogue/proposed-markets")
async def catalogue_proposed_markets() -> list[dict]:
    """Countries where buyers of a catalogue product were found (mostly by the worldwide search)
    at least WORLDWIDE_MARKET_MIN_BUYERS times, not yet one of the line's markets."""
    from . import markets
    return await markets.proposed_markets()


class AcceptMarket(BaseModel):
    product_id: str
    market: str


@router.post("/catalogue/proposed-markets/accept")
async def catalogue_accept_market(body: AcceptMarket, user=Depends(_staff())) -> dict:
    """Add the market to the catalogue line, so the next scan covers it."""
    from . import markets
    try:
        return await markets.accept_market(body.product_id, body.market, by=getattr(user, "email", None) or "staff")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class SupplierDocument(BaseModel):
    kind: str
    filename: str = Field(min_length=1)
    content_b64: str | None = None
    url: str | None = None
    mime_type: str = "application/pdf"
    number: str | None = None
    valid_until: str | None = None
    note: str | None = None


@router.post("/suppliers/{record_id:path}/documents")
async def add_supplier_document(record_id: str, body: SupplierDocument, user=Depends(_staff())) -> dict:
    """Attach an onboarding paper (GST certificate, cancelled cheque, bank letter, FSSAI, APEDA …)."""
    from . import supplier_onboarding
    try:
        return await supplier_onboarding.add_document(record_id, by=getattr(user, "email", None) or "staff",
                                                      **body.model_dump())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class BankVerified(BaseModel):
    doc_id: str
    note: str | None = None


@router.post("/suppliers/{record_id:path}/bank-verified")
async def supplier_bank_verified(record_id: str, body: BankVerified, user=Depends(_staff())) -> dict:
    """A person checked the bank account against the cancelled cheque / bank letter on file."""
    from . import supplier_onboarding
    try:
        return await supplier_onboarding.verify_bank(record_id, doc_id=body.doc_id, note=body.note,
                                                     by=getattr(user, "email", None) or "staff")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/suppliers/{record_id:path}/markets")
async def supplier_markets(record_id: str) -> dict:
    """Every market whose directory buyers want this supplier's products (supplier-led
    opportunities beyond the scanned corridors); start one with …/start-prospect."""
    from . import markets
    try:
        return await markets.markets_for_supplier(record_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
        raise HTTPException(status_code=422, detail=str(exc))
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


class IncentiveUpdate(BaseModel):
    rodtep_pct: float = Field(default=0.0, ge=0.0, le=20.0)
    rodtep_cap_usd_per_mt: float | None = Field(default=None, ge=0.0)
    drawback_pct: float = Field(default=0.0, ge=0.0, le=20.0)
    drawback_cap_usd_per_mt: float | None = Field(default=None, ge=0.0)
    eligible_confirmed: bool = False
    source: str | None = None
    note: str | None = None


@router.get("/incentives", response_model=list[incentives.ExportIncentive])
async def list_incentives() -> list[incentives.ExportIncentive]:
    """RoDTEP / duty drawback rates per HS code (income in the radar's profit once confirmed)."""
    return await incentives.list_entries()


@router.put("/incentives/{hs_code}", response_model=incentives.ExportIncentive)
async def put_incentive(hs_code: str, body: IncentiveUpdate, user=Depends(_staff())) -> incentives.ExportIncentive:
    """Enter the rates from the DGFT RoDTEP schedule (Appendix 4R) / CBIC drawback schedule.
    ``eligible_confirmed`` (after the CA's check) makes the radar count them as income."""
    try:
        entry = incentives.ExportIncentive(
            hs_code=hs_code, **body.model_dump(),
            confirmed_by=(getattr(user, "email", None) or getattr(user, "user_id", None)) if body.eligible_confirmed else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await incentives.set_entry(entry)


class LaneUpdate(BaseModel):
    market: str = Field(min_length=2, max_length=2)
    container_type: Literal["dry", "dry40", "hc40", "reefer", "reefer40", "lcl"] = "dry"
    """dry / reefer = a 20' box; dry40 = 40' dry, hc40 = 40' high cube, reefer40 = 40' reefer;
    lcl = part-container load, ``usd_per_container`` then means USD per freight tonne (W/M)."""
    usd_per_container: float = Field(gt=0)
    container_mt: float = Field(default=20.0, gt=0)
    note: str | None = None


@router.get("/freight-lanes")
async def list_freight_lanes() -> list[dict]:
    """Sea freight rates per lane the radar uses (forwarder quotes and staff entries)."""
    return [{**r.model_dump(mode="json"), "usd_per_mt": r.usd_per_mt} for r in await freight_lanes.list_rates()]


@router.put("/freight-lanes")
async def put_freight_lane(body: LaneUpdate, user=Depends(_staff())) -> dict:
    """Record a freight quote received for a lane (India → market); used until it is older
    than LANE_QUOTE_MAX_AGE_DAYS."""
    rate = await freight_lanes.save(freight_lanes.LaneRate(
        market=body.market, container_type=body.container_type, usd_per_container=body.usd_per_container,
        container_mt=body.container_mt,
        provider=f"staff:{getattr(user, 'email', None) or getattr(user, 'user_id', 'operator')}", note=body.note,
    ))
    return {**rate.model_dump(mode="json"), "usd_per_mt": rate.usd_per_mt}


@router.get("/profit/accuracy")
async def profit_accuracy() -> dict:
    """The radar's estimates against what settled deals really earned, and the correction per product."""
    return await accuracy.summary()


@router.get("/scan/report", response_model=ScanReport | None)
async def last_scan_report() -> ScanReport | None:
    """The last scan, corridor by corridor: opportunity found, or why it was dropped
    (no live demand data, no measured gap, net gap below the floor, score too low)."""
    return await scan_reports.latest()


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


# Sort keys for the directory. Each returns a value where larger = earlier in "desc".
_STAGE_RANK = {"traded": 8, "in_deal": 7, "quoted": 6, "interested": 6, "replied": 5, "contacted": 4,
               "new": 3, "declined": 1, "do_not_contact": 0}


def _last_activity(r: CounterpartyRecord) -> str:
    rel = r.relationship or {}
    return max(filter(None, [rel.get("last_reply_at"), rel.get("last_contact_at"), rel.get("last_quote_at")]),
               default="")


def _price(r: CounterpartyRecord) -> float | None:
    rel = r.relationship or {}
    if r.role == "supplier":
        return rel.get("last_quote_usd_per_mt") or r.estimated_fob_price_usd
    return rel.get("target_price_usd_per_mt")


SORTS: dict[str, tuple[Any, str]] = {
    # key: (value function, default order)
    "score": (lambda r: r.score or 0.0, "desc"),
    "recent": (lambda r: r.last_seen_at.isoformat() if r.last_seen_at else "", "desc"),
    "added": (lambda r: r.first_seen_at.isoformat() if r.first_seen_at else "", "desc"),
    "activity": (_last_activity, "desc"),
    "stage": (lambda r: _STAGE_RANK.get((r.relationship or {}).get("stage", "new"), 3), "desc"),
    "name": (lambda r: (r.name or "").lower(), "asc"),
    "country": (lambda r: (r.country or "~").upper(), "asc"),
    "fit": (lambda r: r.fit_score if r.fit_score is not None else -1, "desc"),
    "price": (_price, "asc"),
    "seen": (lambda r: r.times_seen or 0, "desc"),
}


def _book(rows: list[CounterpartyRecord], *, stage: str | None, contactable: bool, sort: str,
          order: str | None = None) -> list[CounterpartyRecord]:
    """Filter / order the annotated directory like a trading book. Rows without a value
    for the sort key (no price, no activity) always go last, whatever the order."""
    if stage:
        rows = [r for r in rows if (r.relationship or {}).get("stage", "new") == stage]
    if contactable:
        rows = [r for r in rows if not r.do_not_contact
                and (r.validation is None or r.validation.status != "failed")
                and (r.relationship or {}).get("stage") != "declined"
                and (r.email or r.phone)]
    fn, default_order = SORTS.get(sort, SORTS["recent"])
    desc = (order or default_order) == "desc"
    have = [r for r in rows if fn(r) not in (None, "")]
    missing = [r for r in rows if fn(r) in (None, "")]
    have.sort(key=lambda r: (fn(r), (r.name or "").lower()), reverse=desc)
    return have + missing


@router.post("/directory/clean")
async def clean_directory(user=Depends(_staff())) -> dict:
    """Store the current clean-up on every directory record: names cut out of marketing
    sentences and split from run-together domains, usable e-mails, the company's own
    country, and buyers that look like Indian exporters flagged ``role_suspect``. Then the
    role check: an Indian ``role_suspect`` "buyer" that makes the product becomes a supplier,
    the rest stay out of outreach; and operator notes stored as a company's description are
    dropped (``notes_scrubbed``)."""
    changed = await directory.clean_existing()
    roles = await directory.clean_roles()
    return {"changed": changed, **roles, "notes_scrubbed": await directory.scrub_notes()}


@router.get("/personal-data/retention")
async def personal_data_retention_preview() -> dict:
    """Directory companies whose personal contact data the retention sweep would remove
    (never contacted, in no prospect or deal, not seen for PERSONAL_DATA_RETENTION_DAYS)."""
    from . import personal_data
    rows = await personal_data.retention_candidates()
    return {"count": len(rows), "ids": [r.id for r in rows[:200]], "retention_days": personal_data.personal_retention_days()}


@router.post("/personal-data/retention")
async def personal_data_retention_run(user=Depends(_staff())) -> dict:
    from . import personal_data
    return await personal_data.run_retention(by=getattr(user, "email", None) or "staff")


class EraseRequest(BaseModel):
    reason: str = Field(min_length=5)


@router.post("/{role}/{record_id:path}/erase")
async def erase_personal_data(role: str, record_id: str, body: EraseRequest, user=Depends(_staff())) -> dict:
    """Erase a person's contact data on request (DPDP): contacts removed, company do-not-contact;
    signed deals keep what their statutory records need until the retention date."""
    from . import personal_data
    r = _role_of(role)
    try:
        return await personal_data.erase(r, record_id, by=getattr(user, "email", None) or "staff", reason=body.reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{role}/{record_id:path}/products")
async def counterparty_products(role: str, record_id: str) -> dict:
    """Per product: what the buyer wants / the supplier supplies, each fact with its source and
    date, how complete it is against the catalogue template, and the questions still open."""
    from . import product_facts
    r = _role_of(role)
    try:
        return await product_facts.view(r, _full_id(r, record_id))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class ProductFactsEdit(BaseModel):
    facts: dict[str, Any]
    reason: str | None = None


@router.put("/{role}/{record_id:path}/products/{hs_code}")
async def edit_counterparty_products(role: str, record_id: str, hs_code: str, body: ProductFactsEdit,
                                     user=Depends(_staff())) -> dict:
    """Staff enter or correct a company's requirement / offer for one product (audited). A staff
    entry is never overwritten by a reply or a scan."""
    from . import product_facts
    r = _role_of(role)
    try:
        return await product_facts.staff_edit(r, _full_id(r, record_id), hs_code, body.facts,
                                              by=getattr(user, "email", None) or "staff", reason=body.reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _role_of(role: str) -> str:
    r = {"buyers": "buyer", "suppliers": "supplier", "buyer": "buyer", "supplier": "supplier"}.get(role)
    if r is None:
        raise HTTPException(status_code=404, detail="role must be buyers or suppliers")
    return r


def _full_id(role: str, record_id: str) -> str:
    return record_id if record_id.startswith(f"{role}:") else f"{role}:{record_id}"


@router.post("/mandi/{hs_code}/backfill")
async def mandi_backfill(hs_code: str, days: int = 90, user=Depends(_staff())) -> dict:
    """Fill the mandi price history of the product's commodity from Agmarknet's historical
    dataset, so the radar's trend works before a week of daily points exists."""
    from shared.connectors.mandi import commodity_for
    from . import mandi_history
    commodity = commodity_for(hs_code)
    if not commodity:
        raise HTTPException(status_code=404, detail=f"no Agmarknet commodity for HS {hs_code}")
    return await mandi_history.backfill(commodity, days=max(1, min(int(days), 365)))


@router.get("/directory/yield")
async def directory_yield(role: str | None = None, days: int = 90) -> dict:
    """Per source: companies found, verified, contacted, replied, engaged, in a deal and traded,
    with the Apify spend and the cost per verified / replied / traded company."""
    from . import source_yield
    if role not in (None, "buyer", "supplier"):
        raise HTTPException(status_code=422, detail="role must be buyer or supplier")
    return await source_yield.report(role, days)


@router.get("/directory/summary")
async def directory_summary() -> dict:
    """The book at a glance per role: companies per relationship stage, new this week (and for
    which products / countries), verified, contactable, the hottest companies and the top scores."""
    from . import counterparty_profile
    return await counterparty_profile.summary()


@router.get("/directory/{role}/page")
async def directory_page(
    role: Literal["buyers", "suppliers"],
    hs_code: str | None = Query(default=None),
    market: str | None = Query(default=None),
    q: str | None = Query(default=None),
    country: str | None = Query(default=None),
    validation_status: Literal["verified", "unverified", "failed"] | None = Query(default=None, alias="validation"),
    stage: str | None = Query(default=None),
    contactable: bool = Query(default=False),
    sort: str = Query(default="score"),
    order: Literal["asc", "desc"] | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=60, ge=1, le=500),
) -> dict:
    """One page of the directory with the total: every matching company is cleaned, scored,
    filtered and sorted first, then the page is cut — so the order and the count are right
    for the whole directory, not only for the first 200 rows."""
    rows = await directory.list_counterparties(role[:-1], hs_code=hs_code, market=market, q=q,
                                               country=country, limit=100_000, validation=validation_status)
    rows = _book(await relationships.annotate(rows), stage=stage, contactable=contactable, sort=sort, order=order)
    page = rows[offset:offset + limit]
    return {"items": [r.model_dump(mode="json") for r in page], "total": len(rows), "offset": offset,
            "limit": limit, "sort": sort if sort in SORTS else "recent",
            "order": order or SORTS.get(sort, SORTS["recent"])[1]}


class MergeRequest(BaseModel):
    keep: str
    merge: list[str] = Field(min_length=1, max_length=20)


@router.get("/directory/{role}/duplicates", response_model=list[dict])
async def directory_duplicates(role: Literal["buyers", "suppliers"], user=Depends(_staff())) -> list[dict]:
    """Probable duplicates (same company e-mail domain, phone or name in one country) to merge."""
    from services.trade_min import dedupe
    return await dedupe.duplicates(role[:-1])


@router.post("/directory/{role}/merge", response_model=CounterpartyRecord)
async def directory_merge(role: Literal["buyers", "suppliers"], body: MergeRequest,
                          user=Depends(_staff())) -> CounterpartyRecord:
    """Fold the ``merge`` records into ``keep`` (history kept on the record, the others deleted)."""
    from services.trade_min import dedupe
    who = getattr(user, "email", None) or getattr(user, "user_id", "staff")
    try:
        return await dedupe.merge(role[:-1], body.keep, body.merge, who)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class BulkRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    action: Literal["check", "do_not_contact", "add_to_prospect"]
    reason: str | None = Field(default=None, max_length=300)
    deal_id: str | None = None
    confirm: bool = False


@router.post("/directory/{role}/bulk", response_model=dict)
async def directory_bulk(role: Literal["buyers", "suppliers"], body: BulkRequest, user=Depends(_staff())) -> dict:
    """Check / do-not-contact / add-to-prospect the selected companies (a check first answers its Apify cost)."""
    from services.trade_min import directory_bulk as db
    who = getattr(user, "email", None) or getattr(user, "user_id", "staff")
    try:
        return await db.bulk(role[:-1], body.ids, body.action, by=who, reason=body.reason,
                             deal_id=body.deal_id, confirm=body.confirm)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/directory/{role}/export.csv")
async def directory_export(
    role: Literal["buyers", "suppliers"],
    hs_code: str | None = Query(default=None),
    market: str | None = Query(default=None),
    q: str | None = Query(default=None),
    country: str | None = Query(default=None),
    validation_status: Literal["verified", "unverified", "failed"] | None = Query(default=None, alias="validation"),
    stage: str | None = Query(default=None),
    contactable: bool = Query(default=False),
    sort: str = Query(default="score"),
    order: Literal["asc", "desc"] | None = Query(default=None),
    user=Depends(_staff()),
):
    """The directory as CSV, filtered and sorted like the page."""
    from fastapi.responses import Response

    from services.trade_min import directory_bulk as db
    rows = await directory.list_counterparties(role[:-1], hs_code=hs_code, market=market, q=q,
                                               country=country, limit=100_000, validation=validation_status)
    rows = _book(await relationships.annotate(rows), stage=stage, contactable=contactable, sort=sort, order=order)
    stamp = datetime.now().strftime("%Y%m%d")
    return Response(db.to_csv(rows), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="portless-{role}-{stamp}.csv"'})


@router.get("/suppliers/{record_id:path}/performance")
async def supplier_performance(record_id: str) -> dict:
    """How this supplier did on real deals: QC pass rate, goods ready on time, disputes, price kept."""
    from shared.orchestration import supplier_performance as sp
    store = await directory.get_directory()
    rec = await store.get(record_id)
    if rec is None or rec.role != "supplier":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{record_id!r} not found")
    perf = await sp.for_record(rec)
    return {"supplier_id": record_id, "name": rec.name, "performance": perf, "has_history": bool(perf)}


@router.get("/{role}/{record_id:path}/profile")
async def counterparty_profile_view(role: Literal["buyers", "suppliers"], record_id: str) -> dict:
    """One company with everything needed to act on it: score, relationship, every prospect /
    deal it is in, a dated timeline, the open prospects of its products and the next actions."""
    from . import counterparty_profile
    try:
        return await counterparty_profile.profile(role[:-1], record_id)
    except counterparty_profile.ProfileError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


class AddToProspect(BaseModel):
    deal_id: str


@router.post("/{role}/{record_id:path}/add-to-prospect")
async def add_counterparty_to_prospect(role: Literal["buyers", "suppliers"], record_id: str,
                                       body: AddToProspect) -> dict:
    """A buyer joins the prospect's outreach funnel; a supplier joins its match board with the
    quote pending. Refused for do-not-contact, a failed check or an exporter posing as a buyer."""
    from . import counterparty_profile
    try:
        return await counterparty_profile.add_to_prospect(role[:-1], record_id, body.deal_id)
    except counterparty_profile.ProfileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - deal not found / duplicate candidate
        detail = str(exc)
        code = status.HTTP_404_NOT_FOUND if "not found" in detail.lower() or exc.__class__.__name__ == "DealNotFoundError"             else 422
        raise HTTPException(status_code=code, detail=detail) from exc


class ContactPreference(BaseModel):
    do_not_contact: bool
    reason: str | None = Field(default=None, max_length=300)


@router.put("/{role}/{record_id:path}/contact-preference", response_model=CounterpartyRecord)
async def put_contact_preference(role: Literal["buyers", "suppliers"], record_id: str, body: ContactPreference,
                                 user=Depends(_staff())) -> CounterpartyRecord:
    """Put a company on (or take it off) the do-not-contact list — outreach skips it in
    every prospect. A reason is required to put it on."""
    if body.do_not_contact and not (body.reason or "").strip():
        raise HTTPException(status_code=422, detail="a reason is required")
    from datetime import datetime, timezone
    who = getattr(user, "email", None) or getattr(user, "user_id", "staff")
    store = await directory.get_directory()
    rec = await store.set_contact_preference(record_id, {
        "do_not_contact": body.do_not_contact,
        "dnc_reason": f"{body.reason.strip()} (by {who})" if body.do_not_contact else None,
        "dnc_at": datetime.now(timezone.utc) if body.do_not_contact else None,
    })
    if rec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such company in the directory")
    return (await relationships.annotate([rec]))[0]


class NoteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    remind_at: datetime | None = None


@router.post("/{role}/{record_id:path}/notes/{note_id}/done", response_model=CounterpartyRecord)
async def post_note_done(role: Literal["buyers", "suppliers"], record_id: str, note_id: str,
                         user=Depends(_staff())) -> CounterpartyRecord:
    """Mark a note's follow-up done (it leaves the digest)."""
    from services.trade_min import company_notes
    who = getattr(user, "email", None) or getattr(user, "user_id", "staff")
    try:
        rec = await company_notes.done(record_id, note_id, who)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if rec is None or rec.role != role.rstrip("s"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such company in the directory")
    return rec


@router.post("/{role}/{record_id:path}/notes", response_model=CounterpartyRecord)
async def post_note(role: Literal["buyers", "suppliers"], record_id: str, body: NoteRequest,
                    user=Depends(_staff())) -> CounterpartyRecord:
    """A staff note on a company (also sent to knowledge memory), optionally with a reminder date."""
    from services.trade_min import company_notes
    who = getattr(user, "email", None) or getattr(user, "user_id", "staff")
    existing = await (await directory.get_directory()).get(record_id)
    if existing is None or existing.role != role.rstrip("s"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such company in the directory")
    try:
        rec = await company_notes.add(record_id, body.text, who, body.remind_at)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return rec  # type: ignore[return-value]


@router.get("/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
async def get_unsubscribe(d: str = Query(default=""), c: str = Query(default=""),
                          sig: str = Query(default="")) -> HTMLResponse:
    """The unsubscribe link of an outreach e-mail: a confirm page (no sign-in; ``sig`` checked).
    Nothing is recorded on GET — mail scanners open every link."""
    from shared.orchestration import unsubscribe
    if not unsubscribe.verify(d, c, sig):
        return HTMLResponse(unsubscribe.invalid_page(), status_code=403)
    return HTMLResponse(unsubscribe.confirm_page(d, c, sig))


@router.post("/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
async def post_unsubscribe(d: str = Query(default=""), c: str = Query(default=""),
                           sig: str = Query(default="")) -> HTMLResponse:
    """Record the opt-out: do-not-contact in every prospect; a courted candidate is declined."""
    from shared.orchestration import unsubscribe
    if not unsubscribe.verify(d, c, sig):
        return HTMLResponse(unsubscribe.invalid_page(), status_code=403)
    try:
        await unsubscribe.apply(d, c)
    except Exception as exc:  # noqa: BLE001 - never show a stack to the recipient
        logging.getLogger(__name__).error("Unsubscribe %s/%s failed: %s", d, c, exc)
        return HTMLResponse(unsubscribe.invalid_page(), status_code=404)
    return HTMLResponse(unsubscribe.done_page())


class ScreeningDecision(BaseModel):
    decision: Literal["clear", "fail"]
    reason: str = Field(min_length=3, max_length=500)


@router.get("/screening/review", response_model=list[CounterpartyRecord])
async def list_screening_review(role: Literal["buyer", "supplier"] | None = Query(default=None),
                                user=Depends(_staff())) -> list[CounterpartyRecord]:
    """Companies whose sanctions screening returned ``review`` (a possible list match or a list
    that was unavailable), oldest check first — they stay unverified until staff decide."""
    from services.trade_min import screening_review
    return await relationships.annotate(await screening_review.queue(role))


@router.post("/screening/rescreen", response_model=dict)
async def post_rescreen(user=Depends(_staff())) -> dict:
    """Re-check now every verified company of an open prospect / deal whose result is older
    than TRADE_MIN_VALIDATION_MAX_AGE_DAYS (the daily job, run by hand). Paid Apify runs."""
    from services.trade_min import rescreen
    return await rescreen.run()


@router.post("/{role}/{record_id:path}/screening-review", response_model=CounterpartyRecord)
async def post_screening_review(role: Literal["buyers", "suppliers"], record_id: str, body: ScreeningDecision,
                                user=Depends(_staff())) -> CounterpartyRecord:
    """Clear or fail a ``review`` screening with a reason; kept on the record (``screening_reviews``)."""
    from services.trade_min import screening_review
    from shared.auth.roles import RoleRequired, require_compliance_officer
    if body.decision == "clear":  # failing a company is never restricted
        try:
            require_compliance_officer(user, "Clearing a possible sanctions match")
        except RoleRequired as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    who = getattr(user, "email", None) or getattr(user, "user_id", "staff")
    existing = await (await directory.get_directory()).get(record_id)
    if existing is None or existing.role != role.rstrip("s"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such company in the directory")
    try:
        rec = await screening_review.resolve(record_id, body.decision, body.reason, who)
    except (screening_review.NotInReview, screening_review.SamePerson) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if rec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such company in the directory")
    return (await relationships.annotate([rec]))[0]


@router.get("/buyers", response_model=list[CounterpartyRecord])
async def list_buyers(
    hs_code: str | None = Query(default=None),
    market: str | None = Query(default=None, description="ISO-2 market, e.g. AE"),
    q: str | None = Query(default=None, description="Search name / e-mail / website / country"),
    country: str | None = Query(default=None, description="ISO-2 country of the buyer itself, e.g. DE"),
    validation_status: Literal["verified", "unverified", "failed"] | None = Query(
        default=None, alias="validation", description="Scan-time check result; omit for all"),
    stage: str | None = Query(default=None, description="Relationship stage: new, contacted, replied, interested, quoted, in_deal, traded, declined, do_not_contact"),
    contactable: bool = Query(default=False, description="Only companies outreach may write to (not failed, not do-not-contact, not declined)"),
    sort: str = Query(default="recent", description="score, recent, added, activity, stage, name, country, fit, price, seen"),
    order: Literal["asc", "desc"] | None = Query(default=None, description="Default per sort (score desc, name asc …)"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[CounterpartyRecord]:
    """Every buyer a scan found, in any country — with or without an opportunity
    or deal — newest first, merged per company across scans."""
    rows = await directory.list_counterparties("buyer", hs_code=hs_code, market=market, q=q,
                                               country=country, limit=limit,
                                               validation=validation_status)
    return _book(await relationships.annotate(rows), stage=stage, contactable=contactable, sort=sort, order=order)


@router.post("/suppliers/import", response_model=dict)
async def import_suppliers(request: Request, dry_run: bool = Query(default=True), check: bool = Query(default=True),
                           user=Depends(_staff())) -> dict:
    """CSV of local suppliers (body = the CSV text). Dry run (default) reports what would be
    accepted, duplicated or refused, without saving or running paid checks."""
    from services.trade_min import manual_suppliers as ms
    who = getattr(user, "email", None) or getattr(user, "user_id", "staff")
    text = (await request.body()).decode("utf-8", errors="replace")
    if not text.strip():
        raise HTTPException(status_code=422, detail="empty CSV")
    return await ms.import_csv(text, who, dry_run=dry_run, check=check)


@router.post("/suppliers", response_model=CounterpartyRecord, status_code=status.HTTP_201_CREATED)
async def add_supplier(body: dict, force: bool = Query(default=False), user=Depends(_staff())) -> CounterpartyRecord:
    """Add a local supplier by hand; the same checks as a scanned supplier run at once."""
    from pydantic import ValidationError

    from services.trade_min import manual_suppliers as ms
    who = getattr(user, "email", None) or getattr(user, "user_id", "staff")
    try:
        data = ms.ManualSupplierIn(**body)
        return await ms.add(data, who, force=force)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False, include_context=False)) from exc
    except ms.DuplicateSupplier as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"message": str(exc), "existing_id": exc.existing_id}) from exc


class StartProspectRequest(BaseModel):
    market: str = Field(min_length=2, max_length=2)
    hs_code: str | None = None
    quantity_mt: float | None = Field(default=None, gt=0)


@router.post("/suppliers/{record_id:path}/start-prospect", response_model=dict)
async def start_prospect_from_supplier(record_id: str, body: StartProspectRequest, user=Depends(_staff())) -> dict:
    """Supply first: a prospect for this supplier's product in a market, with the directory's best
    contactable buyers as candidates and the outreach started."""
    from services.trade_min import supply_first
    who = getattr(user, "email", None) or getattr(user, "user_id", "staff")
    org = getattr(user, "org_id", None) or "portless"
    try:
        return await supply_first.start(record_id, body.market, by=who, org_id=org, hs_code=body.hs_code,
                                        quantity_mt=body.quantity_mt)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except supply_first.CannotStart as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.put("/suppliers/{record_id:path}", response_model=CounterpartyRecord)
async def edit_supplier(record_id: str, body: dict, user=Depends(_staff())) -> CounterpartyRecord:
    from pydantic import ValidationError

    from services.trade_min import manual_suppliers as ms
    who = getattr(user, "email", None) or getattr(user, "user_id", "staff")
    try:
        rec = await ms.edit(record_id, ms.ManualSupplierIn(**body), who)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False, include_context=False)) from exc
    except ms.DuplicateSupplier as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"message": str(exc), "existing_id": exc.existing_id}) from exc
    if rec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such supplier")
    return rec


@router.delete("/suppliers/{record_id:path}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_supplier(record_id: str, user=Depends(_staff())) -> None:
    from services.trade_min import manual_suppliers as ms
    try:
        ok = await ms.delete(record_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such supplier")


@router.get("/suppliers", response_model=list[CounterpartyRecord])
async def list_suppliers(
    hs_code: str | None = Query(default=None),
    market: str | None = Query(default=None, description="Market the supplier was scanned for"),
    q: str | None = Query(default=None, description="Search name / e-mail / website"),
    country: str | None = Query(default=None, description="ISO-2 country of the supplier, e.g. IN for India only"),
    validation_status: Literal["verified", "unverified", "failed"] | None = Query(
        default=None, alias="validation", description="Scan-time check result; omit for all"),
    stage: str | None = Query(default=None, description="Relationship stage: new, contacted, replied, interested, quoted, in_deal, traded, declined, do_not_contact"),
    contactable: bool = Query(default=False, description="Only companies outreach may write to (not failed, not do-not-contact, not declined)"),
    sort: str = Query(default="recent", description="score, recent, added, activity, stage, name, country, fit, price, seen"),
    order: Literal["asc", "desc"] | None = Query(default=None, description="Default per sort (score desc, name asc …)"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[CounterpartyRecord]:
    """Every supplier a scan found, in any country (``country=IN`` → India only) —
    with or without an opportunity or deal — newest first, merged per company."""
    rows = await directory.list_counterparties("supplier", hs_code=hs_code, market=market, q=q,
                                               country=country, limit=limit,
                                               validation=validation_status)
    return _book(await relationships.annotate(rows), stage=stage, contactable=contactable, sort=sort, order=order)


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
    return (await directory.with_open_opportunities([rec]))[0]


class PhoneCheckIn(BaseModel):
    whatsapp: Literal["confirmed", "not_reachable", "unknown"]
    evidence: str | None = None


@router.post("/suppliers/{record_id:path}/phone-check", response_model=CounterpartyRecord)
async def phone_check_supplier(record_id: str, body: PhoneCheckIn,
                               user=Depends(_staff())) -> CounterpartyRecord:
    """Staff: the supplier answered (or not) on WhatsApp — recorded on its check (audited); the
    number itself is checked against the numbering plan again."""
    from shared import phone_check
    from shared.audit_log import staff_relay
    from .schemas import CounterpartyValidation
    store = await directory.get_directory()
    rec = await store.get(record_id)
    if rec is None or rec.role != "supplier":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{record_id!r} not found")
    pc = phone_check.check(rec.phone, rec.country)
    if pc["status"] != "valid" and body.whatsapp == "confirmed":
        raise HTTPException(status_code=422, detail=f"the number on file is not valid: {pc['reason']}")
    v = (rec.validation or CounterpartyValidation()).model_copy(
        update={"phone": pc["status"], "phone_e164": pc["e164"], "whatsapp": body.whatsapp})
    rec.validation = validation.decide(v, role="supplier", country=rec.country, has_phone=bool(rec.phone))
    await store.set_validation(rec)
    await staff_relay(user, "supplier.phone_check", record_id, whatsapp=body.whatsapp, evidence=body.evidence)
    return (await directory.with_open_opportunities([rec]))[0]


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
    found_by: str | None = Query(default=None, description="source chip: market_statistics, national_statistics, …"),
) -> list[EnrichedOpportunity]:
    """List active enriched opportunities.

    Results are sorted by urgency tier (4 = emergency first) then arbitrage rank.
    Use ``urgency_tier`` to filter for a specific urgency level only, ``found_by`` for how
    the opportunity was found.
    """
    rows = await service.list_opportunities(
        limit=limit,
        min_rank=min_rank,
        urgency_tier=urgency_tier,
        counterparties=counterparties,
    )
    if found_by:
        rows = [o for o in rows if (getattr(o, "found_by", None) or "market_statistics") == found_by]
    return rows


@router.get("/opportunities/company-led")
async def company_led_preview() -> list[dict]:
    """Product × market groups where the directory alone supports an opportunity (checked,
    contactable buyers + reachable Indian suppliers), even without a measured Comtrade gap."""
    from . import company_led
    return await company_led.preview()


@router.post("/opportunities/company-led")
async def company_led_create(user=Depends(_staff())) -> dict:
    """Create those opportunities (found_by=directory); corridors already listed are skipped."""
    from . import company_led
    return await company_led.create()


@router.get("/opportunities/found-by")
async def opportunities_found_by() -> dict:
    """How many listed opportunities each source found (compare which source turns into deals)."""
    rows = await service.list_opportunities(limit=500, min_rank=0.0, urgency_tier=None, counterparties="all")
    return {"counts": service.count_found_by(rows), "sources": list(service.FOUND_BY)}


@router.post("/opportunities/{opportunity_id}/rescore", response_model=EnrichedOpportunity)
async def rescore_opportunity(
    opportunity_id: str,
    pairs: int = Query(default=3, ge=1, le=5, description="Best pairs to improve (each costs Apify runs)"),
    counterparties: Literal["verified", "all"] = Query(default="all"),
) -> EnrichedOpportunity:
    """Before starting a deal: fill what can be filled without contacting anyone for the
    best buyer × seller pairs (counterparty check, supplier address / GSTIN from its
    website, plant-to-port distance, an estimated supplier price), score the pairs
    again and save. ``rescore`` on the response lists each pair before → after, what
    was filled and what still needs the deal (RFQ / outreach)."""
    from . import rescore as _rescore
    opp = await _rescore.rescore(opportunity_id, pairs=pairs)
    if opp is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Opportunity {opportunity_id!r} not found")
    return service._with_counterparties(opp, counterparties)


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
