"""Deal composition agent — choose the best deal after negotiation (todo 13.1 / 13.3).

With ``DEAL_COMPOSITION_ENABLED`` a prospect is not pinned to one buyer × one supplier before
negotiating. Every interested verified buyer and every quoting verified supplier stays a
candidate on its own thread (firm_offers.py). When firm offers exist this module decides which
deal to make: **one buyer × 1..N suppliers**. Example: the buyer needs 1000 MT, supplier A
offers 800 MT and supplier B 200 MT → one deal, one buyer, two supplier legs.

Code decides; nothing here asks a model, and no money decision depends on one.

**Objective** — the highest expected margin after landed cost: each leg's FOB, freight, one
inspection per facility (the per-shipment inspection is spread over that leg's quantity),
insurance and escrow fee (``landed_cost``), × the chance the legs all perform.

**Hard rules for every combination** (any one broken → the combination is not offered):

- compatible grade / spec (``matchmaking.grade_fit``);
- every supplier's MOQ respected, no leg above its offered quantity;
- total within ``COMPOSITION_QTY_TOLERANCE_PCT`` (10) of the buyer's quantity;
- every leg ready before the buyer's shipment date less the transit time;
- ports of loading the same, or consolidatable (``COMPOSITION_ALLOW_MULTI_PORT``, default on —
  each leg then stuffs at its own port and the plan says so);
- every party verified (``COMPOSITION_REQUIRE_VERIFIED``, default on) and not related to
  another party of the combination (same company domain / name);
- at most ``COMPOSITION_MAX_SUPPLIERS`` (3) legs;
- the margin floor on the total **and** on each leg.

**When** — never the moment a negotiation ends. ``run()`` re-runs whenever an offer arrives,
changes, is withdrawn or expires (firm_offers), and from the deal watcher when an offer nears
expiry or the buyer's deadline gets close. It answers **close now** (the earliest validity in
the best combination ends within ``COMPOSITION_CLOSE_HOURS`` (48), the buyer's deadline is
within ``COMPOSITION_DEADLINE_DAYS`` (7), or waiting is not worth it) or **wait** (quotes still
pending that could beat it, with the expected gain). The best single-supplier deal is always
shown next to the composed one.

**Gate** — ``CONFIRM_DEAL_COMPOSITION`` (L0 always, never auto-approved by a value threshold)
with the combination, per-leg quantity / price / port / ready date, margin per leg and in
total, the freight plan, risks and the runner-ups. APPROVE (``approve``) sets the buyer party
and the supplier legs, sends each supplier its own firm order request on a ``[LEG:<deal>:<leg>]``
thread, and once every leg has confirmed (``CONFIRM_SUPPLIER_ORDER`` per leg) the buyer's
agreement is confirmed and the PI is issued at the combined quantity.

**A leg that declines or stays silent** (``SUPPLIER_CONFIRMATION_HOURS``) → a new composition
from the remaining offers for that leg's quantity only (generalises the single standby
supplier); nothing left → ``deal.exception`` ``COMPOSITION:no_cover``.

Candidates not chosen stay in the pool with their offers for the next composition or a second
deal; they get no "closed" e-mail while their offer is valid. Every approved composition is
recorded in ``outcomes`` (predicted margin, later the realised one) for calibration.
"""
from __future__ import annotations

import hashlib
import itertools
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ACTION = "CONFIRM_DEAL_COMPOSITION"
LEG_TOKEN = re.compile(r"\[LEG:(DEAL-[A-Z0-9-]+):(LEG-[A-Z0-9]+)\]", re.IGNORECASE)
_MAX_POOL = 10


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def enabled() -> bool:
    from shared.orchestration.outreach import composition_enabled
    return composition_enabled()


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _on(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off")


def min_score() -> float:
    from shared.orchestration import matchmaking
    raw = os.getenv("COMPOSITION_MIN_SCORE")
    try:
        return float(raw) if raw not in (None, "") else matchmaking.min_probability()
    except ValueError:
        return matchmaking.min_probability()


def qty_tolerance_pct() -> float:
    return max(0.0, _f("COMPOSITION_QTY_TOLERANCE_PCT", 10.0))


def max_suppliers() -> int:
    return max(1, int(_f("COMPOSITION_MAX_SUPPLIERS", 3)))


def close_hours() -> float:
    return _f("COMPOSITION_CLOSE_HOURS", 48.0)


def deadline_days() -> float:
    return _f("COMPOSITION_DEADLINE_DAYS", 7.0)


def wait_min_gain_pct() -> float:
    return _f("COMPOSITION_WAIT_MIN_GAIN_PCT", 3.0)


def require_verified() -> bool:
    return _on("COMPOSITION_REQUIRE_VERIFIED")


def allow_multi_port() -> bool:
    return _on("COMPOSITION_ALLOW_MULTI_PORT")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class LegPlan(BaseModel):
    supplier_id: str
    company_name: str = ""
    email: str | None = None
    offer_id: str
    quantity_mt: float
    offered_mt: float | None = None
    moq_mt: float | None = None
    price_usd_per_mt: float
    """The supplier's FOB."""
    incoterm: str = "FOB"
    port: str | None = None
    port_km: float | None = None
    ready_by: datetime | None = None
    valid_until: datetime | None = None
    verified: bool | None = None
    cost_usd_per_mt: float
    """Landed cost of this leg in the buyer's incoterm at the buyer's price (incl. its own inspection)."""
    margin_usd: float
    margin_pct: float


class Combination(BaseModel):
    buyer_id: str
    buyer_name: str = ""
    buyer_email: str | None = None
    buyer_offer_id: str
    buyer_price_usd_per_mt: float
    incoterm: str = "CIF"
    destination_port: str | None = None
    buyer_quantity_mt: float
    buyer_valid_until: datetime | None = None
    shipment_by: datetime | None = None
    quantity_mt: float
    legs: list[LegPlan]
    revenue_usd: float
    cost_usd: float
    margin_usd: float
    margin_pct: float
    probability: float
    expected_margin_usd: float
    earliest_validity: datetime | None = None
    freight_plan: str = ""
    risks: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def single(self) -> bool:
        return len(self.legs) == 1

    def keys(self) -> set[tuple[str, str]]:
        return {("buyer", self.buyer_id)} | {("supplier", leg.supplier_id) for leg in self.legs}

    def signature(self) -> str:
        legs = ",".join(f"{leg.supplier_id}:{leg.quantity_mt:g}@{leg.price_usd_per_mt:g}" for leg in
                        sorted(self.legs, key=lambda x: x.supplier_id))
        return f"{self.buyer_id}@{self.buyer_price_usd_per_mt:g}|{legs}"


class CompositionResult(BaseModel):
    deal_id: str
    computed_at: datetime
    reason: str | None = None
    decision: str = "none"
    """close_now · wait · none"""
    decision_reason: str = ""
    best: Combination | None = None
    best_single: Combination | None = None
    alternatives: list[Combination] = Field(default_factory=list)
    offers: dict[str, int] = Field(default_factory=dict)
    rejected: list[str] = Field(default_factory=list)
    """Why combinations were not offered (a sample, one line each)."""
    pending_quotes: int = 0
    expected_gain_usd: float = 0.0
    what_would_change: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Building combinations (pure)
# ---------------------------------------------------------------------------

def _transit_days(market: str | None) -> int:
    from shared.orchestration import matchmaking
    return matchmaking._region_transit(market)


def _name_core(name: str | None) -> str:
    from agents.negotiation_agent.email_safety import _name_core as core  # type: ignore[import]
    return core(name)


def related(a: Any, b: Any) -> str | None:
    """Two parties of one combination that look like the same company (domain / name)."""
    from agents.negotiation_agent.email_safety import FREEMAIL, domain  # type: ignore[import]
    da, db = domain(getattr(a, "email", None)), domain(getattr(b, "email", None))
    if da and da == db and da not in FREEMAIL:
        return f"{a.company_name or a.counterparty_id} and {b.company_name or b.counterparty_id} share the domain {da}"
    ca, cb = _name_core(a.company_name), _name_core(b.company_name)
    if len(ca) >= 4 and ca == cb:
        return f"{a.company_name} and {b.company_name} look like one company"
    return None


def leg_economics(fob: float, qty: float, buyer_price: float, incoterm: str, market: str | None, *,
                  freight_usd_per_mt: float | None = None) -> tuple[float, float, float]:
    """(landed cost per MT, margin USD, margin %) of one leg at the buyer's price. Escrow fee and
    insurance are a share of the buyer's price; freight, inspection (one per facility) and the
    destination costs come from the landed-cost bridge."""
    from shared.orchestration import landed_cost as lc
    land = lc.landed(fob, market, qty, incoterm=incoterm, margin_floor_pct=0.0, freight_usd_per_mt=freight_usd_per_mt)
    base = (land.fob_usd_per_mt + land.freight_usd_per_mt + land.inspection_usd_per_mt
            + land.destination_usd_per_mt - land.loading_saved_usd_per_mt)
    cost = base + buyer_price * (land.escrow_fee_pct + land.insurance_pct) / 100.0
    margin = (buyer_price - cost) * qty
    pct = (buyer_price - cost) / buyer_price * 100.0 if buyer_price else -100.0
    return round(cost, 2), round(margin, 2), round(pct, 2)


def _allocate(buyer_qty: float, suppliers: list[Any], tol: float) -> tuple[list[float] | None, str | None]:
    """Quantities per supplier (cheapest first, each up to its offered quantity, at least its
    MOQ) whose total lands within the tolerance band; every supplier must get a share."""
    low, high = buyer_qty * (1 - tol / 100.0), buyer_qty * (1 + tol / 100.0)
    left = buyer_qty
    alloc: list[float] = []
    for s in suppliers:
        cap = s.quantity_mt or left  # no stated quantity: it can cover the rest
        take = min(left, cap)
        moq = s.min_quantity_mt or 0.0
        if take < moq:
            if moq > cap:
                return None, f"{s.company_name or s.counterparty_id}: MOQ {moq:g} MT above its offer"
            take = moq
        if take <= 0:
            return None, f"{s.company_name or s.counterparty_id} would get no share"
        alloc.append(round(take, 3))
        left -= take
    total = sum(alloc)
    if total < low - 1e-6:
        return None, f"together {total:g} MT — below the buyer's {buyer_qty:g} MT less {tol:g} %"
    if total > high + 1e-6:
        return None, f"together {total:g} MT — above the buyer's {buyer_qty:g} MT plus {tol:g} % (MOQ)"
    return alloc, None


def _leg_risk(s: Any) -> float:
    return 1.0 if s.verified else 0.85


def combinations(deal: Any, offers: list[Any], *, now: datetime | None = None,
                 freight_usd_per_mt: float | None = None, buyer_ids: set[str] | None = None,
                 exclude_suppliers: set[str] | None = None, max_legs: int | None = None
                 ) -> tuple[list[Combination], list[str]]:
    """Every feasible combination (buyer × 1..N suppliers), best expected margin first, and a
    sample of why the others were refused."""
    from shared.orchestration import landed_cost as lc
    from shared.orchestration import matchmaking
    now = now or _now()
    market = getattr(deal, "market", None)
    floor = getattr(deal, "margin_floor_pct", None) or lc.default_margin_floor_pct()
    tol = qty_tolerance_pct()
    max_legs = max_legs or max_suppliers()
    buyers = [o for o in offers if o.side == "buyer" and (buyer_ids is None or o.counterparty_id in buyer_ids)]
    sellers = [o for o in offers if o.side == "supplier" and o.counterparty_id not in (exclude_suppliers or set())]
    rejected: list[str] = []
    out: list[Combination] = []
    transit = _transit_days(market)

    for b in buyers:
        if not b.quantity_mt or not b.price_usd_per_mt:
            rejected.append(f"{b.company_name}: no quantity or price in the offer")
            continue
        if require_verified() and b.verified is not True:
            rejected.append(f"{b.company_name}: buyer not verified")
            continue
        inc = b.incoterm or "CIF"
        pool = []
        for s in sellers:
            _fit, blocker, _ = matchmaking.grade_fit(b.grade, s.grade)
            if blocker:
                rejected.append(f"{s.company_name}: {blocker}")
                continue
            if require_verified() and s.verified is not True:
                rejected.append(f"{s.company_name}: supplier not verified")
                continue
            rel = related(b, s)
            if rel:
                rejected.append(f"related parties: {rel}")
                continue
            if b.date_by and s.date_by and _aware(s.date_by) + timedelta(days=transit) > _aware(b.date_by):
                rejected.append(f"{s.company_name}: ready {s.date_by:%d %b} — too late for shipment by {b.date_by:%d %b}")
                continue
            cost, _, _ = leg_economics(float(s.price_usd_per_mt), min(b.quantity_mt, s.quantity_mt or b.quantity_mt),
                                       float(b.price_usd_per_mt), inc, market, freight_usd_per_mt=freight_usd_per_mt)
            pool.append((cost, s))
        pool.sort(key=lambda x: x[0])
        pool_s = [s for _, s in pool[:_MAX_POOL]]
        for n in range(1, min(max_legs, len(pool_s)) + 1):
            for subset in itertools.combinations(pool_s, n):
                subset = sorted(subset, key=lambda s: next(c for c, x in pool if x is s))
                why = None
                for a, c in itertools.combinations(subset, 2):
                    why = related(a, c)
                    if why:
                        break
                if why:
                    rejected.append(f"related parties: {why}")
                    continue
                ports = {(s.port or "").strip().lower() for s in subset if s.port}
                if len(ports) > 1 and not allow_multi_port():
                    rejected.append("ports of loading differ: " + ", ".join(sorted(ports)))
                    continue
                alloc, why = _allocate(float(b.quantity_mt), subset, tol)
                if alloc is None:
                    if n == 1 or "share" not in (why or ""):
                        rejected.append(f"{' + '.join(s.company_name or s.counterparty_id for s in subset)}: {why}")
                    continue
                legs: list[LegPlan] = []
                bad = None
                for s, q in zip(subset, alloc):
                    cost, margin, pct = leg_economics(float(s.price_usd_per_mt), q, float(b.price_usd_per_mt), inc,
                                                      market, freight_usd_per_mt=freight_usd_per_mt)
                    if pct < floor:
                        bad = f"{s.company_name or s.counterparty_id}: leg margin {pct:.1f} % below the {floor:g} % floor"
                        break
                    legs.append(LegPlan(
                        supplier_id=s.counterparty_id, company_name=s.company_name, email=s.email, offer_id=s.offer_id,
                        quantity_mt=q, offered_mt=s.quantity_mt, moq_mt=s.min_quantity_mt,
                        price_usd_per_mt=float(s.price_usd_per_mt), incoterm=s.incoterm or "FOB", port=s.port,
                        port_km=s.port_km, ready_by=s.date_by, valid_until=s.valid_until, verified=s.verified,
                        cost_usd_per_mt=cost, margin_usd=margin, margin_pct=pct,
                    ))
                if bad:
                    rejected.append(bad)
                    continue
                qty = sum(leg.quantity_mt for leg in legs)
                revenue = round(float(b.price_usd_per_mt) * qty, 2)
                margin = round(sum(leg.margin_usd for leg in legs), 2)
                cost = round(revenue - margin, 2)
                pct = round(margin / revenue * 100.0, 2) if revenue else -100.0
                if pct < floor:
                    rejected.append(f"total margin {pct:.1f} % below the {floor:g} % floor")
                    continue
                prob = 1.0 if b.verified else 0.85
                for s in subset:
                    prob *= _leg_risk(s)
                prob *= 0.97 ** (len(subset) - 1)  # every extra leg is one more party that can fail
                validities = [_aware(x) for x in [b.valid_until] + [s.valid_until for s in subset] if x]
                risks: list[str] = []
                notes: list[str] = []
                if len(ports) > 1:
                    risks.append("separate stuffing at " + str(len(ports)) + " ports of loading")
                if any(s.date_by is None for s in subset):
                    notes.append("ready date not stated by every supplier")
                if any(not s.verified for s in subset) or not b.verified:
                    risks.append("unverified party")
                if abs(qty - b.quantity_mt) > 1e-6:
                    notes.append(f"{qty:g} MT against the buyer's {b.quantity_mt:g} MT (within {tol:g} %)")
                plan = " + ".join(f"{leg.quantity_mt:g} MT from {leg.port or 'its port'}" for leg in legs)
                out.append(Combination(
                    buyer_id=b.counterparty_id, buyer_name=b.company_name, buyer_email=b.email, buyer_offer_id=b.offer_id,
                    buyer_price_usd_per_mt=float(b.price_usd_per_mt), incoterm=inc, destination_port=b.port,
                    buyer_quantity_mt=float(b.quantity_mt), buyer_valid_until=b.valid_until, shipment_by=b.date_by,
                    quantity_mt=qty, legs=legs, revenue_usd=revenue, cost_usd=cost, margin_usd=margin, margin_pct=pct,
                    probability=round(prob, 4), expected_margin_usd=round(margin * prob, 2),
                    earliest_validity=min(validities) if validities else None,
                    freight_plan=("each supplier stuffs its own containers: " if len(legs) > 1 else "") + plan,
                    risks=risks, notes=notes,
                ))
    out.sort(key=lambda c: (c.expected_margin_usd, c.margin_pct), reverse=True)
    return out, list(dict.fromkeys(rejected))[:20]


# ---------------------------------------------------------------------------
# Close now or wait
# ---------------------------------------------------------------------------

def _pending_suppliers(deal: Any, offers: list[Any]) -> list[str]:
    """Suppliers asked (RFQ / SOURCE match) who have not given a firm price yet."""
    quoted = {o.counterparty_id for o in offers if o.side == "supplier"}
    asked = {m.supplier_id for m in getattr(deal, "match_results", None) or [] if getattr(m, "decision", "") == "SOURCE"}
    asked |= {o.supplier_id for o in getattr(deal, "supplier_offers", None) or [] if o.price_basis != "quoted"}
    return sorted(asked - quoted)


async def _expected_gain(deal: Any, best: Combination, pending: list[str]) -> float:
    """What waiting for the pending quotes could add: the price book's lower supplier estimate
    against the best combination's weighted FOB, × 30 % chance per pending quote (max 90 %)."""
    if not pending:
        return 0.0
    try:
        from shared import pricebook
        est = await pricebook.estimate(deal.hs_code, "supplier", market=getattr(deal, "market", None))
        low = getattr(est, "low", None) or (est.median * 0.95 if getattr(est, "median", None) else None)
    except Exception:  # noqa: BLE001
        low = None
    if not low:
        return 0.0
    fob = sum(leg.price_usd_per_mt * leg.quantity_mt for leg in best.legs) / max(best.quantity_mt, 1e-9)
    gain = max(0.0, fob - float(low)) * best.quantity_mt
    return round(gain * min(0.9, 0.3 * len(pending)), 2)


def decide(best: Combination | None, *, now: datetime, pending: int, expected_gain: float) -> tuple[str, str]:
    if best is None:
        return "none", "no combination satisfies every rule yet"
    if best.earliest_validity and best.earliest_validity - now <= timedelta(hours=close_hours()):
        return "close_now", f"an offer in it expires {best.earliest_validity:%d %b %H:%M} UTC"
    if best.shipment_by and _aware(best.shipment_by) - now <= timedelta(days=deadline_days()):
        return "close_now", f"the buyer ships by {best.shipment_by:%d %b}"
    if pending and expected_gain >= best.margin_usd * wait_min_gain_pct() / 100.0:
        return "wait", (f"{pending} quote(s) still pending could add about USD {expected_gain:,.0f} "
                        f"(≥ {wait_min_gain_pct():g} % of the margin)")
    if best.probability < min_score():
        return "wait", f"chance {best.probability:.0%} below {min_score():.0%}"
    return "close_now", "best combination clears every rule; waiting is not worth more"


def _what_would_change(best: Combination | None, alternatives: list[Combination], rejected: list[str]) -> list[str]:
    out: list[str] = []
    if best is not None and alternatives:
        gap = best.expected_margin_usd - alternatives[0].expected_margin_usd
        out.append(f"the runner-up ({alternatives[0].buyer_name}, {len(alternatives[0].legs)} leg(s)) wins if it gains "
                   f"USD {gap:,.0f} of expected margin")
    if best is not None:
        for leg in best.legs:
            out.append(f"{leg.company_name or leg.supplier_id} dropping {leg.quantity_mt:g} MT re-runs the composition for that quantity")
    out.extend(f"blocked: {r}" for r in rejected[:3])
    return out


# ---------------------------------------------------------------------------
# State on the deal
# ---------------------------------------------------------------------------

async def note(deal_id: str, fields: dict[str, Any]) -> None:
    """Merge fields into ``DealRecord.composition`` (atomic field write)."""
    from services.deals.repository import get_repository  # type: ignore[import]
    repo = get_repository()
    deal = await repo.get(deal_id)
    if deal is None:
        return
    cur = dict(getattr(deal, "composition", None) or {})
    cur.update(fields)
    await repo.save(deal.model_copy(update={"composition": cur, "updated_at": _now()}))


async def compose(deal: Any, *, reason: str | None = None, now: datetime | None = None) -> CompositionResult:
    from shared.orchestration import firm_offers, matchmaking
    now = now or _now()
    offers = await firm_offers.live_offers(deal, now)
    freight, _basis, _inc = await matchmaking._costs(deal)
    combos, rejected = combinations(deal, offers, now=now, freight_usd_per_mt=freight)
    best = combos[0] if combos else None
    single = next((c for c in combos if c.single), None)
    alternatives = [c for c in combos[1:6] if c is not single][:3]
    pending = _pending_suppliers(deal, offers)
    gain = await _expected_gain(deal, best, pending) if best else 0.0
    decision, why = decide(best, now=now, pending=len(pending), expected_gain=gain)
    return CompositionResult(
        deal_id=deal.deal_id, computed_at=now, reason=reason, decision=decision, decision_reason=why,
        best=best, best_single=single, alternatives=alternatives,
        offers={"buyers": sum(1 for o in offers if o.side == "buyer"),
                "suppliers": sum(1 for o in offers if o.side == "supplier")},
        rejected=rejected, pending_quotes=len(pending), expected_gain_usd=gain,
        what_would_change=_what_would_change(best, alternatives, rejected),
    )


def _status(deal: Any) -> str:
    return str(getattr(deal.status, "value", deal.status))


async def _pending_gate(deal_id: str) -> Any | None:
    try:
        from services.hitl_approval.service import list_approvals  # type: ignore[import]
        return next((g for g in await list_approvals(status="PENDING", deal_id=deal_id) if g.action == ACTION), None)
    except Exception:  # noqa: BLE001
        return None


async def run(deal_id: str, *, reason: str | None = None, now: datetime | None = None,
              propose: bool = True) -> dict[str, Any]:
    """Recompute and store the composition; on ``close_now`` open the gate (one at a time)."""
    if not enabled():
        return {"enabled": False}
    from services.deals.service import get_deal  # type: ignore[import]
    deal = await get_deal(deal_id)
    if _status(deal) not in ("DRAFT", "MATCHING"):
        return {"skipped": f"deal is {_status(deal)}"}
    result = await compose(deal, reason=reason, now=now)
    state = dict(getattr(deal, "composition", None) or {})
    state["latest"] = result.model_dump(mode="json")
    out: dict[str, Any] = {"decision": result.decision, "reason": result.decision_reason}
    if propose and result.decision == "close_now" and result.best is not None:
        gate = await _pending_gate(deal_id)
        sig = result.best.signature()
        if gate is not None and (gate.callback_payload or {}).get("signature") != sig:
            # a better combination than the one waiting for a person: replace it
            from services.hitl_approval.service import withdraw_pending  # type: ignore[import]
            await withdraw_pending(deal_id, {ACTION}, "superseded by a better combination")
            gate = None
        if gate is None and sig not in (state.get("rejected_signatures") or []):
            gid = await propose_gate(deal, result)
            state["proposed"] = {"approval_id": gid, "signature": sig, "at": (now or _now()).isoformat()}
            out["approval_id"] = gid
    await note(deal_id, state)
    await _ask_missing(deal, result)
    return out


async def _ask_missing(deal: Any, result: CompositionResult) -> None:
    """Interested buyers without a firm offer, and quoted suppliers whose quote states no
    validity, are asked on their own thread (once each)."""
    from shared.orchestration import firm_offers
    have = {(o.side, o.counterparty_id) for o in (getattr(deal, "firm_offers", None) or []) if o.status == "live"}
    try:
        for c in getattr(deal, "buyer_candidates", None) or []:
            req = getattr(c, "requirement", None)
            firm = req is not None and req.target_price_usd_per_mt and req.target_price_basis in ("stated", "counter", "accepted_offer") \
                and req.quantity_mt
            if str(c.status) in ("interested", "selected") and not firm and ("buyer", c.candidate_id) not in have:
                await firm_offers.request_firm_offer(deal, "buyer", c.candidate_id)
        for o in getattr(deal, "supplier_offers", None) or []:
            if (o.price_basis == "quoted" and o.fob_price_usd_per_mt and not o.validity_days
                    and ("supplier", o.supplier_id) not in have
                    and (o.origin_deal_id in (None, deal.deal_id))):
                await firm_offers.request_firm_offer(deal, "supplier", o.supplier_id, reason="validity")
    except Exception as exc:  # noqa: BLE001 — asking never fails the composition
        logger.warning("Composition: firm-offer requests for %s failed: %s", deal.deal_id, exc)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

def _leg_line(leg: LegPlan) -> str:
    ready = f", ready {leg.ready_by:%d %b}" if leg.ready_by else ""
    return (f"{leg.company_name or leg.supplier_id}: {leg.quantity_mt:g} MT at {leg.price_usd_per_mt:,.0f} FOB "
            f"{leg.port or '(port tbc)'}{ready} — margin {leg.margin_usd:,.0f} ({leg.margin_pct:.1f} %)")


async def propose_gate(deal: Any, result: CompositionResult) -> str | None:
    from services.hitl_approval.schemas import CreateApprovalRequest  # type: ignore[import]
    from services.hitl_approval.service import create_approval  # type: ignore[import]
    best = result.best
    assert best is not None
    summary = (f"Deal composition: {best.buyer_name} {best.quantity_mt:g} MT at {best.buyer_price_usd_per_mt:,.0f} "
               f"{best.incoterm} ← {len(best.legs)} supplier(s): " + "; ".join(_leg_line(x) for x in best.legs)
               + f" · total margin {best.margin_usd:,.0f} ({best.margin_pct:.1f} %), {best.probability:.0%} likely")
    if result.best_single is not None and result.best_single.signature() != best.signature():
        summary += (f" · best single supplier: {result.best_single.legs[0].company_name} "
                    f"margin {result.best_single.margin_usd:,.0f}")
    flags = ["money_related_composition", f"legs_{len(best.legs)}"] + [r.replace(" ", "_")[:40] for r in best.risks]
    approval = await create_approval(CreateApprovalRequest(
        deal_id=deal.deal_id, action=ACTION, summary=summary, risk_flags=flags,
        autonomy_level="L0",  # never auto-approved, whatever the policy or value threshold says
        financial_impact_usd=best.revenue_usd,
        callback_payload={
            "deal_id": deal.deal_id, "signature": best.signature(),
            "combination": best.model_dump(mode="json"),
            "best_single": result.best_single.model_dump(mode="json") if result.best_single else None,
            "alternatives": [c.model_dump(mode="json") for c in result.alternatives],
            "decision_reason": result.decision_reason,
        },
        idempotency_key=f"composition-{deal.deal_id}-{hashlib.sha1(best.signature().encode()).hexdigest()[:12]}",
    ))
    logger.info("Composition: gate %s for deal %s — %s", approval.approval_id, deal.deal_id, best.signature())
    return approval.approval_id


async def reject(deal_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Soft REJECT: the prospect stays open; this exact combination is not proposed again."""
    from services.deals.service import get_deal  # type: ignore[import]
    deal = await get_deal(deal_id)
    state = dict(getattr(deal, "composition", None) or {})
    sigs = list(state.get("rejected_signatures") or [])
    if payload.get("signature") and payload["signature"] not in sigs:
        sigs.append(payload["signature"])
    await note(deal_id, {"rejected_signatures": sigs[-50:], "proposed": None})
    return {"deal_id": deal_id, "status": "declined"}


# ---------------------------------------------------------------------------
# APPROVE: the combination becomes the deal
# ---------------------------------------------------------------------------

def leg_token(deal_id: str, leg_id: str) -> str:
    return f"[LEG:{deal_id}:{leg_id}]"


def parse_leg_token(subject: str | None) -> tuple[str, str] | None:
    m = LEG_TOKEN.search(subject or "")
    return (m.group(1).upper(), m.group(2).upper()) if m else None


def _new_leg_id() -> str:
    return f"LEG-{uuid4().hex[:8].upper()}"


async def approve(deal_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """CONFIRM_DEAL_COMPOSITION APPROVE callback."""
    from services.deals.repository import get_repository as deals_repo  # type: ignore[import]
    from services.deals.schemas import SupplierLeg, UpdateFinancialsRequest, UpdateStatusRequest  # type: ignore[import]
    from services.deals.service import (  # type: ignore[import]
        get_deal, record_match_result, set_supplier_party, update_financials, update_party, update_status,
    )
    from shared.domain.enums import DealStatus
    from shared.orchestration import firm_offers

    deal = await get_deal(deal_id)
    if _status(deal) != "MATCHING":
        return {"skipped": f"deal is {_status(deal)}, not MATCHING"}
    combo = Combination(**payload["combination"])
    live = {(o.side, o.counterparty_id): o for o in await firm_offers.live_offers(deal)}
    stale = []
    b = live.get(("buyer", combo.buyer_id))
    if b is None or abs(float(b.price_usd_per_mt) - combo.buyer_price_usd_per_mt) > 0.5:
        stale.append(f"buyer {combo.buyer_name}")
    for leg in combo.legs:
        s = live.get(("supplier", leg.supplier_id))
        if s is None or abs(float(s.price_usd_per_mt) - leg.price_usd_per_mt) > 0.5:
            stale.append(f"supplier {leg.company_name or leg.supplier_id}")
    if stale:
        logger.warning("Composition: approved combination of %s is stale (%s) — recomposing", deal_id, stale)
        await run(deal_id, reason="approved combination no longer live: " + ", ".join(stale))
        return {"stale": stale}

    cand = next((c for c in deal.buyer_candidates if c.candidate_id == combo.buyer_id), None)
    await update_party(deal_id, "buyer", {
        "party_id": combo.buyer_id, "legal_name": combo.buyer_name or (cand.legal_name if cand else ""),
        "country": (cand.country if cand else None) or deal.market, "email": combo.buyer_email,
        "website": getattr(cand, "website", None), "phone": getattr(cand, "phone", None),
        "contact_source": "composition", "email_status": "ok" if getattr(cand, "reply_address", None) else "unverified",
        "sanctions_status": "unchecked",
    }, source=None, replace=True)
    now = _now()
    legs = [SupplierLeg(
        leg_id=_new_leg_id(), supplier_id=lp.supplier_id, company_name=lp.company_name, email=lp.email,
        quantity_mt=lp.quantity_mt, price_usd_per_mt=lp.price_usd_per_mt, incoterm=lp.incoterm,
        port_of_loading=lp.port, ready_date=lp.ready_by.date().isoformat() if lp.ready_by else None,
        offer_id=lp.offer_id, status="requested", requested_at=now,
    ) for lp in combo.legs]
    main = max(legs, key=lambda x: x.quantity_mt)
    await set_supplier_party(deal_id, main.supplier_id, company_name=main.company_name or None, email=main.email)
    for leg in legs:
        await record_match_result(deal_id, leg.supplier_id, company_name=leg.company_name,
                                  fob_price_usd=leg.price_usd_per_mt,
                                  composite_score=round(combo.probability * 100, 2), make_winner=leg is main)
    await update_financials(deal_id, UpdateFinancialsRequest(buyer_target_price_usd=combo.buyer_price_usd_per_mt))
    fresh = await get_deal(deal_id)
    state = dict(fresh.composition or {})
    state.update({"approved": {"signature": combo.signature(), "at": now.isoformat(),
                               "combination": combo.model_dump(mode="json")}, "proposed": None})
    await deals_repo().save(fresh.model_copy(update={
        "supplier_legs": legs, "quantity_mt": combo.quantity_mt, "composition": state,
        "selected_pair": {"candidate_id": combo.buyer_id, "composition": True, "legs": len(legs),
                          "confirmed_at": now.isoformat(), "quantity_mt": combo.quantity_mt},
        "buyer_price_source": "composition:firm_offer", "updated_at": now,
    }))
    await firm_offers.mark_used(deal_id, combo.keys())
    await _open_session(fresh, combo, main)
    await update_status(deal_id, UpdateStatusRequest(
        target_status=DealStatus.NEGOTIATING,
        reason=f"Deal composition approved: {combo.buyer_name} × {len(legs)} supplier(s)"))
    try:
        from services.hitl_approval.service import withdraw_pending  # type: ignore[import]
        await withdraw_pending(deal_id, {"CONFIRM_MATCH", "SEND_INDICATIVE_OFFER", ACTION},
                               "deal composition approved")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Composition: other proposals of %s not withdrawn: %s", deal_id, exc)
    from shared.orchestration import outcomes
    await outcomes.record_composition(await get_deal(deal_id), combo, payload.get("approval_id"))
    for leg in legs:
        await send_leg_request(deal_id, leg.leg_id)
    logger.info("Composition: deal %s composed — %s", deal_id, combo.signature())
    return {"composed": True, "legs": [x.leg_id for x in legs], "quantity_mt": combo.quantity_mt}


async def _open_session(deal: Any, combo: Combination, main: Any) -> None:
    """The prices are firm: the deal's negotiation session is opened already agreed at the
    buyer's price (no LLM rounds), so every notice and the PI find their thread. The deal
    moves to NEGOTIATION_AGREED only once every leg has confirmed (``_all_legs_confirmed``)."""
    from agents.negotiation_agent.repository import get_repository as neg_repo  # type: ignore[import]
    from agents.negotiation_agent.schemas import NegotiationPolicy, NegotiationSession  # type: ignore[import]
    repo = neg_repo()
    existing = await repo.get_session_by_deal(deal.deal_id)
    if existing is not None and existing.status not in ("FAILED", "ESCALATED"):
        return
    now = _now()
    fob = round(sum(x.price_usd_per_mt * x.quantity_mt for x in combo.legs) / max(combo.quantity_mt, 1e-9), 2)
    await repo.save_session(NegotiationSession(
        negotiation_id=str(uuid4()), deal_id=deal.deal_id, trade_match_id=f"COMP-{deal.deal_id}",
        buyer_id=combo.buyer_id, seller_id=main.supplier_id, product_code=deal.hs_code,
        buyer_country_code=deal.market, buyer_target_price=combo.buyer_price_usd_per_mt,
        supplier_quote_price=fob, policy=NegotiationPolicy(), buyer_email=combo.buyer_email,
        seller_email=main.email, quantity_mt=combo.quantity_mt, status="AGREED",
        buyer_last_offer=combo.buyer_price_usd_per_mt, supplier_last_quote=fob,
        agreed_price=combo.buyer_price_usd_per_mt, trade_context="composed deal (firm offers)",
        created_at=now, updated_at=now,
    ))


# ---------------------------------------------------------------------------
# Legs: firm order request, reply, confirmation, replacement
# ---------------------------------------------------------------------------

def leg_request_body(leg: Any, *, product: str, buyer_incoterm: str | None = None) -> str:
    from shared import company_profile as cp
    from shared.orchestration.supplier_commitment import confirm_hours
    from shared.orchestration.unsubscribe import supplier_opt_out_line
    greeting = f"Dear {leg.company_name} team," if leg.company_name else "Dear Sir / Madam,"
    ready = f" ready by {leg.ready_date}" if leg.ready_date else ""
    return (f"{greeting}\n\nThank you for your offer. We would like to place a firm order for {product}: "
            f"{leg.quantity_mt:g} MT at USD {leg.price_usd_per_mt:,.2f} per MT {leg.incoterm} "
            f"{leg.port_of_loading or ''}{ready}.\n\nPlease confirm within {confirm_hours():g} hours that you accept "
            "this order at this price and quantity (\"we confirm\" is enough), and tell us the full address of the "
            "facility where the goods will be inspected. Our purchase order follows once the order is confirmed.\n\n"
            + supplier_opt_out_line(leg.email) + cp.signature())


async def send_leg_request(deal_id: str, leg_id: str) -> bool:
    from services.deals.service import get_deal  # type: ignore[import]
    from shared.orchestration import supplier_legs
    from shared.orchestration.outreach import _product_label
    deal = await get_deal(deal_id)
    leg = supplier_legs.leg(deal, leg_id)
    if leg is None or not leg.email:
        logger.warning("Composition: leg %s of %s has no address — the request waits for staff", leg_id, deal_id)
        return False
    body = leg_request_body(leg, product=_product_label(deal))
    subject = f"{leg_token(deal_id, leg_id)} Firm order — {_product_label(deal)} {leg.quantity_mt:g} MT"
    from agents.negotiation_agent.email_safety import prospect_leak_findings  # type: ignore[import]
    leaks = prospect_leak_findings(body, leg.email, "supplier", deal)
    if leaks:
        logger.error("Composition: leg request to %s refused — %s", leg.email, leaks)
        return False
    from shared.orchestration import kill_switch
    if await kill_switch.is_paused():
        logger.warning("Composition: platform paused — leg request %s not sent", leg_id)
        return False
    from services.email_gateway.schemas import SendEmailRequest  # type: ignore[import]
    from services.email_gateway.sender import send_email  # type: ignore[import]
    result = await send_email(SendEmailRequest(
        to_address=leg.email, subject=subject, body=body, negotiation_id="", round_no=0,
        email_id=f"LEGREQ{deal_id.replace('-', '')[-8:]}{leg_id.replace('-', '')[-8:]}",
    ))
    outcome = str(getattr(getattr(result, "outcome", None), "value", getattr(result, "outcome", "sent")))
    from agents.negotiation_agent.email_history import record_deal_email  # type: ignore[import]
    await record_deal_email(deal_id=deal_id, direction="outbound", party="supplier", to_address=leg.email,
                            from_address="PortLess", subject=subject, body=body, kind="leg_order_request",
                            delivered=outcome != "logged")
    await supplier_legs.set_leg(deal_id, leg_id, {"status": "requested", "requested_at": _now()})
    return outcome != "logged"


async def handle_leg_reply(deal_id: str, leg_id: str, from_address: str, body: str, inbound_id: str = "") -> dict[str, Any]:
    """A supplier's reply on its ``[LEG:…]`` thread: confirm → CONFIRM_SUPPLIER_ORDER gate for
    that leg; decline → re-compose the leg; anything else → a person reads it."""
    from agents.negotiation_agent.email_safety import same_owner  # type: ignore[import]
    from services.deals.service import get_deal  # type: ignore[import]
    from shared.orchestration import supplier_address, supplier_commitment, supplier_legs
    deal = await get_deal(deal_id)
    leg = supplier_legs.leg(deal, leg_id)
    if leg is None:
        return {"handled": False, "reason": "unknown leg"}
    if not same_owner(from_address, leg.email):
        await _operator(deal, f"Reply on leg {leg_id} from {from_address}, not the leg's supplier {leg.email}",
                        ["sender_not_verified", "composition_leg"], inbound_id, body)
        return {"handled": False, "reason": "sender_not_verified"}
    try:
        address = supplier_address.parse(body)
    except Exception:  # noqa: BLE001
        address = None
    if address and not leg.facility_address:
        await supplier_legs.set_leg(deal_id, leg_id, {"facility_address": address})
    if leg.status == "confirmed" and leg.po_id:
        # after the PO: an acknowledgement / goods-ready date on the leg's own thread
        from shared.orchestration import qc_schedule
        ready = qc_schedule.parse_ready_date(body)
        if leg.po_ack != "acknowledged" and supplier_commitment._PO_ACK.search(body or ""):
            await supplier_legs.acknowledge_po(deal_id, leg_id, by="supplier:email",
                                               ready_date=ready.isoformat() if ready else None)
            return {"handled": True, "verdict": "po_acknowledged"}
        if ready is not None:
            await supplier_legs.record_ready_date(deal_id, leg_id, ready.isoformat(), by="supplier:email")
            return {"handled": True, "verdict": "ready_date"}
    if leg.status != "requested":
        await _operator(deal, f"Leg {leg_id} ({leg.company_name}) wrote again after it was {leg.status}",
                        ["composition_leg"], inbound_id, body)
        return {"handled": False, "reason": f"leg is {leg.status}"}
    verdict, price = await supplier_commitment.read_order_reply(body, leg.price_usd_per_mt)
    if verdict == "confirmed":
        gid = await _gate_leg_confirmation(deal, leg, inbound_id, body)
        return {"handled": True, "verdict": "confirmed", "approval_id": gid}
    if verdict == "declined":
        return {"handled": True, "verdict": "declined", **(await replace_leg(deal_id, leg_id, "declined"))}
    await _operator(deal, f"Leg {leg_id} ({leg.company_name}) answered with something else"
                    + (f" (price {price:g})" if price else ""), ["composition_leg"], inbound_id, body)
    return {"handled": False, "reason": "unclear"}


async def _operator(deal: Any, summary: str, flags: list[str], inbound_id: str, body: str) -> None:
    try:
        from services.hitl_approval.schemas import CreateApprovalRequest  # type: ignore[import]
        from services.hitl_approval.service import create_approval  # type: ignore[import]
        await create_approval(CreateApprovalRequest(
            deal_id=deal.deal_id, action="PARSE_INBOUND_EMAIL", summary=summary + f". Preview: {(body or '')[:200]}",
            risk_flags=flags, autonomy_level="L0",
            callback_payload={"deal_id": deal.deal_id, "inbound_id": inbound_id},
            idempotency_key=f"leg-parse-{deal.deal_id}-{inbound_id or uuid4().hex[:8]}",
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Composition: operator gate for %s not created: %s", deal.deal_id, exc)


async def _gate_leg_confirmation(deal: Any, leg: Any, inbound_id: str, body: str) -> str | None:
    from services.hitl_approval.schemas import CreateApprovalRequest  # type: ignore[import]
    from services.hitl_approval.service import create_approval  # type: ignore[import]
    from shared.orchestration.autonomy_policy import resolve_gate
    spec = resolve_gate("CONFIRM_SUPPLIER_ORDER", deal)
    approval = await create_approval(CreateApprovalRequest(
        deal_id=deal.deal_id, action="CONFIRM_SUPPLIER_ORDER",
        summary=(f"Leg {leg.leg_id}: {leg.company_name} confirms {leg.quantity_mt:g} MT at "
                 f"{leg.price_usd_per_mt:,.2f} {leg.incoterm}. Reply: {(body or '')[:200]}"),
        risk_flags=["composition_leg"], autonomy_level=spec.level, expires_at=spec.expires_at_iso(),
        default_on_timeout=spec.default_on_timeout, financial_impact_usd=round(leg.quantity_mt * leg.price_usd_per_mt, 2),
        callback_payload={"deal_id": deal.deal_id, "leg_id": leg.leg_id, "supplier_id": leg.supplier_id,
                          "price": leg.price_usd_per_mt, "inbound_id": inbound_id},
        idempotency_key=f"leg-confirm-{deal.deal_id}-{leg.leg_id}",
    ))
    return approval.approval_id


async def confirm_leg(deal_id: str, leg_id: str, *, by: str = "supplier") -> dict[str, Any]:
    """CONFIRM_SUPPLIER_ORDER (leg) APPROVE: the leg is confirmed; once every live leg is,
    the buyer's agreement is confirmed and the PI goes out at the combined quantity."""
    from shared.orchestration import supplier_legs
    await supplier_legs.set_leg(deal_id, leg_id, {"status": "confirmed", "confirmed_at": _now(), "note": f"confirmed by {by}"})
    return {"leg_id": leg_id, "confirmed": True, **(await _all_legs_confirmed(deal_id))}


async def _all_legs_confirmed(deal_id: str) -> dict[str, Any]:
    from services.deals.service import get_deal  # type: ignore[import]
    from shared.orchestration import supplier_legs
    deal = await get_deal(deal_id)
    live = supplier_legs.active_legs(deal)
    if not live or any(x.status != "confirmed" for x in live):
        return {"all_confirmed": False, "waiting": [x.leg_id for x in live if x.status != "confirmed"]}
    if _status(deal) != "NEGOTIATING":
        return {"all_confirmed": True, "skipped": _status(deal)}
    from agents.negotiation_agent.repository import get_repository as neg_repo  # type: ignore[import]
    session = await neg_repo().get_session_by_deal(deal_id)
    if session is None:
        return {"all_confirmed": True, "skipped": "no session"}
    qty = round(sum(x.quantity_mt for x in live), 3)
    fob = round(sum(x.price_usd_per_mt * x.quantity_mt for x in live) / max(qty, 1e-9), 2)
    session = session.model_copy(update={"quantity_mt": qty, "supplier_last_quote": fob, "supplier_quote_price": fob,
                                         "status": "AGREED", "updated_at": _now()})
    await neg_repo().save_session(session)
    from services.deals.schemas import UpdateStatusRequest  # type: ignore[import]
    from services.deals.service import update_status  # type: ignore[import]
    from shared.domain.enums import DealStatus
    await update_status(deal_id, UpdateStatusRequest(target_status=DealStatus.NEGOTIATION_AGREED,
                                                     reason=f"every supplier leg confirmed ({len(live)})"))
    from shared.bus import get_publisher
    from shared.domain.events import DomainEvent, EventTopic
    await get_publisher().publish(DomainEvent(
        topic=EventTopic.NEGOTIATION_AGREED, deal_id=deal_id,
        payload={"negotiation_id": session.negotiation_id, "agreed_price": session.agreed_price,
                 "round_no": 0, "composition": True, "quantity_mt": qty},
    ))
    return {"all_confirmed": True, "quantity_mt": qty}


async def replace_leg(deal_id: str, leg_id: str, outcome: str) -> dict[str, Any]:
    """A leg declined / stayed silent: compose its quantity only from the remaining offers."""
    from services.deals.schemas import SupplierLeg  # type: ignore[import]
    from services.deals.service import get_deal, set_supplier_party  # type: ignore[import]
    from shared.orchestration import firm_offers, matchmaking, supplier_legs
    deal = await get_deal(deal_id)
    old = supplier_legs.leg(deal, leg_id)
    if old is None:
        return {"replaced": False, "reason": "unknown leg"}
    await supplier_legs.set_leg(deal_id, leg_id, {"status": outcome, "note": f"{outcome} the firm order request"})
    deal = await get_deal(deal_id)
    approved = Combination(**(deal.composition or {})["approved"]["combination"]) if (deal.composition or {}).get("approved") else None
    if approved is None:
        return {"replaced": False, "reason": "no approved composition"}
    used = {x.supplier_id for x in deal.supplier_legs}
    offers = await firm_offers.live_offers(deal)
    from services.deals.schemas import FirmOffer  # type: ignore[import]
    buyer = FirmOffer(offer_id=approved.buyer_offer_id, side="buyer", counterparty_id=approved.buyer_id,
                      company_name=approved.buyer_name, email=approved.buyer_email,
                      quantity_mt=old.quantity_mt, price_usd_per_mt=approved.buyer_price_usd_per_mt,
                      incoterm=approved.incoterm, port=approved.destination_port, date_by=approved.shipment_by,
                      verified=True, source="derived")
    supply = [o for o in offers if o.side == "supplier"]
    freight, _b, _i = await matchmaking._costs(deal)
    room = max(1, max_suppliers() - len(supplier_legs.active_legs(deal)))
    combos, rejected = combinations(deal, [buyer] + supply, freight_usd_per_mt=freight,
                                    exclude_suppliers=used, max_legs=room)
    if not combos:
        from shared.bus import get_publisher
        from shared.domain.events import DomainEvent, EventTopic
        await get_publisher().publish(DomainEvent(
            topic=EventTopic.DEAL_EXCEPTION, deal_id=deal_id,
            payload={"source": "composition", "stage": "COMPOSITION:no_cover", "category": "NEGOTIATION_FAILED",
                     "error": f"leg {leg_id} ({old.company_name}) {outcome}; no remaining offer covers its "
                              f"{old.quantity_mt:g} MT", "rejected": rejected[:5],
                     "action_required": "find_a_supplier_or_reduce_the_quantity_with_the_buyer"},
        ))
        return {"replaced": False, "exception": "COMPOSITION:no_cover"}
    best = combos[0]
    now = _now()
    new_legs = [SupplierLeg(
        leg_id=_new_leg_id(), supplier_id=lp.supplier_id, company_name=lp.company_name, email=lp.email,
        quantity_mt=lp.quantity_mt, price_usd_per_mt=lp.price_usd_per_mt, incoterm=lp.incoterm,
        port_of_loading=lp.port, ready_date=lp.ready_by.date().isoformat() if lp.ready_by else None,
        offer_id=lp.offer_id, status="requested", requested_at=now, replaced_leg_id=leg_id,
    ) for lp in best.legs]
    await supplier_legs.add_legs(deal_id, new_legs)
    await firm_offers.mark_used(deal_id, {("supplier", x.supplier_id) for x in new_legs})
    if supplier_legs.leg(deal, leg_id) is not None and old.supplier_id == getattr(deal, "winning_supplier_id", None):
        main = max(new_legs + [x for x in supplier_legs.active_legs(deal) if x.leg_id != leg_id],
                   key=lambda x: x.quantity_mt)
        await set_supplier_party(deal_id, main.supplier_id, company_name=main.company_name or None, email=main.email)
    for x in new_legs:
        await send_leg_request(deal_id, x.leg_id)
    logger.info("Composition: leg %s of %s %s → replaced by %s", leg_id, deal_id, outcome, [x.leg_id for x in new_legs])
    return {"replaced": True, "new_legs": [x.leg_id for x in new_legs]}


async def sweep(now: datetime | None = None) -> dict[str, int]:
    """Deal watcher: silent legs → replaced; prospects whose offers near expiry or whose buyer
    deadline is close → composition re-run."""
    counts = {"legs_replaced": 0, "rerun": 0}
    if not enabled():
        return counts
    now = now or _now()
    from shared.orchestration import supplier_legs
    from shared.orchestration.supplier_commitment import confirm_hours
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deals = await get_repository().list_active(limit=1000)
    except Exception as exc:  # noqa: BLE001
        logger.error("Composition: could not list deals: %s", exc)
        return counts
    for deal in deals:
        try:
            status = _status(deal)
            if status == "NEGOTIATING" and deal.supplier_legs:
                for x in supplier_legs.active_legs(deal):
                    asked = _aware(x.requested_at)
                    if x.status == "requested" and asked and now - asked >= timedelta(hours=confirm_hours()):
                        await replace_leg(deal.deal_id, x.leg_id, "no_reply")
                        counts["legs_replaced"] += 1
            elif status == "MATCHING":
                latest = ((deal.composition or {}).get("latest")) or {}
                best = latest.get("best") or {}
                end = best.get("earliest_validity")
                ship = best.get("shipment_by")
                soon = False
                for raw, window in ((end, timedelta(hours=close_hours())), (ship, timedelta(days=deadline_days()))):
                    if raw:
                        when = _aware(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
                        soon = soon or (when - now <= window)
                if soon and latest.get("decision") != "close_now":
                    await run(deal.deal_id, reason="offer near expiry / buyer deadline close", now=now)
                    counts["rerun"] += 1
        except Exception as exc:  # noqa: BLE001 — one deal never stops the sweep
            logger.error("Composition: sweep of %s failed: %s", deal.deal_id, exc)
    return counts


# ---------------------------------------------------------------------------
# Keeping a qualifying prospect open (todo 13.1)
# ---------------------------------------------------------------------------

def keeps_open(deal: Any) -> bool:
    """With composition on, a prospect whose best combination (or best pair) scores at or above
    ``COMPOSITION_MIN_SCORE`` is never dropped while one qualifying buyer and one qualifying
    supplier are still in play: a decline or one funnel running out does not end it, and the
    deal watcher does not count it as a stall."""
    if not enabled():
        return False
    latest = ((getattr(deal, "composition", None) or {}).get("latest")) or {}
    best = latest.get("best") or {}
    if best and float(best.get("probability") or 0) >= min_score():
        return True
    offers = latest.get("offers") or {}
    if offers.get("buyers") and offers.get("suppliers"):
        return True
    has_buyer = any(str(c.status) in ("interested", "selected") for c in getattr(deal, "buyer_candidates", None) or [])
    has_supplier = any(o.price_basis == "quoted" and o.fob_price_usd_per_mt and not o.verification_failed
                       for o in getattr(deal, "supplier_offers", None) or [])
    return has_buyer and has_supplier
