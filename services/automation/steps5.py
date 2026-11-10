"""Steps for the supplier commitment: the firm order request before the PI, the
standby supplier on a decline or silence, nobody left (TC-SUP-*)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import seed as fx
from .engine import Ctx, step
from .steps import _deal, _deal_id, _exceptions, _gates, _reply, _value

# Supplier "A" is the fixture's own supplier; every other key is a standby added by the scenario.


def _supplier(ctx: Ctx, key: str) -> dict[str, Any]:
    if key == "A":
        return {"supplier_id": f"SUP-{ctx.ids['uid']}", "mail": ctx.ids["supplier_mail"], "price": float(ctx.ids["supplier_price"])}
    standby = ctx.extra.get("standbys", {}).get(key)
    assert standby is not None, f"no standby supplier {key!r} in this scenario"
    return standby


async def _commitment(ctx: Ctx) -> Any:
    from shared.orchestration import supplier_commitment
    return await supplier_commitment.get(_deal_id(ctx))


def _key_of(ctx: Ctx, supplier_id: str | None) -> str | None:
    if supplier_id == f"SUP-{ctx.ids['uid']}":
        return "A"
    return next((k for k, s in ctx.extra.get("standbys", {}).items() if s["supplier_id"] == supplier_id), supplier_id)


def _order_requests(ctx: Ctx) -> list[dict[str, str]]:
    return [m for m in ctx.emails if "order confirmation" in m.get("subject", "").lower()]


# ---------------------------------------------------------------------------
# Setup and actions
# ---------------------------------------------------------------------------

@step(r'standby supplier "([^"]+)" quotes (\d+(?:\.\d+)?) with score (\d+)( and no e-mail address)?')
async def standby(ctx: Ctx, key: str, price: str, score: str, no_mail: str | None) -> None:
    """A Match Engine SOURCE result for another supplier, plus its received quote when it has an address."""
    from services.deals.repository import get_repository
    from services.deals.schemas import MatchResult, SupplierOffer

    uid = ctx.ids["uid"]
    sid = f"SUP-{uid}-{key}"
    mail = None if no_mail else f"export.{key.lower()}@standby-{key.lower()}-{uid}.invalid"
    company = f"Standby Mill {key} {uid}"
    ctx.extra.setdefault("standbys", {})[key] = {"supplier_id": sid, "mail": mail, "price": float(price)}
    deal = await _deal(ctx)
    match = MatchResult(match_id=f"M-{sid}", supplier_id=sid, company_name=company, composite_score=float(score),
                        gatekeeper_passed=True, arbitrage_score=80.0, proximity_score=80.0, reliability_score=80.0,
                        decision="SOURCE", fob_price_usd=float(price))
    offers = list(deal.supplier_offers or [])
    if mail:
        offers.append(SupplierOffer(offer_id=f"OFF-{sid}", supplier_id=sid, company_name=company, email=mail,
                                    fob_price_usd_per_mt=float(price), gst_verified=True, iec_active=True, source="quote",
                                    received_at=datetime.now(timezone.utc)))
    await get_repository().save(deal.model_copy(update={"match_results": [*deal.match_results, match], "supplier_offers": offers}))


@step(r'the PI leg runs for deal "([^"]+)"')
async def pi_leg(ctx: Ctx, alias: str) -> None:
    """Every path to a PI goes through ``issue_pi_for_deal`` (negotiation agreed, compliance / risk approved)."""
    from shared.orchestration.continuations import issue_pi_for_deal
    ctx.extra["acted"] = True
    await issue_pi_for_deal(str(ctx.ids[alias]), agreed_price=float(ctx.ids["buyer_price"]),
                            quantity_mt=float(ctx.ids["qty"]), market=fx.MARKET)
    await ctx.settle()


@step(r'supplier "([^"]+)" replies "([^"]+)" from its own address')
async def supplier_replies(ctx: Ctx, key: str, text: str) -> None:
    ctx.extra["acted"] = True
    supplier = _supplier(ctx, key)
    assert supplier["mail"], f"supplier {key} has no address to write from"
    await _reply(ctx, supplier["mail"], f"Re: Order confirmation requested [NEG:{ctx.ids['N']}]", ctx.fill(text))


@step(r"the supplier order sweep runs (\d+) hours? later")
async def order_sweep(ctx: Ctx, hours: str) -> None:
    from shared.orchestration import supplier_commitment
    ctx.extra["acted"] = True
    await supplier_commitment.sweep(datetime.now(timezone.utc) + timedelta(hours=int(hours)))
    await ctx.settle()


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@step(r'the order request is "([^"]+)"')
async def request_status(ctx: Ctx, status: str) -> None:
    rec = await _commitment(ctx)
    assert rec is not None, "no order request was made"
    assert rec.status == status, f"order request is {rec.status}; attempts {[(a.supplier_id, a.outcome) for a in rec.attempts]}"


@step(r'there is no order request')
async def no_request(ctx: Ctx) -> None:
    rec = await _commitment(ctx)
    assert rec is None, f"order request {rec.status if rec else None} exists"


@step(r'supplier "([^"]+)" holds the order request')
async def holds(ctx: Ctx, key: str) -> None:
    rec = await _commitment(ctx)
    cur = rec.current if rec is not None and rec.status == "requested" else None
    assert cur is not None and cur.outcome == "requested" and cur.supplier_id == _supplier(ctx, key)["supplier_id"], (
        f"holder is {_key_of(ctx, cur.supplier_id) if cur else None}; status {rec.status if rec else None}")


@step(r'the order request to supplier "([^"]+)" is "([^"]+)"')
async def attempt_outcome(ctx: Ctx, key: str, outcome: str) -> None:
    rec = await _commitment(ctx)
    sid = _supplier(ctx, key)["supplier_id"]
    found = [a.outcome for a in (rec.attempts if rec else []) if a.supplier_id == sid]
    assert found and found[-1] == outcome, f"supplier {key}: {found or 'never asked'}"


@step(r'supplier "([^"]+)" is skipped with a reason containing "([^"]+)"')
async def skipped(ctx: Ctx, key: str, text: str) -> None:
    rec = await _commitment(ctx)
    sid = _supplier(ctx, key)["supplier_id"]
    reasons = [s.get("reason", "") for s in (rec.skipped if rec else []) if s.get("supplier_id") == sid]
    assert any(text.lower() in r.lower() for r in reasons), f"supplier {key} skipped for: {reasons or 'not skipped'}"


@step(r'an order request is composed for supplier "([^"]+)" at its own price')
async def request_composed(ctx: Ctx, key: str) -> None:
    supplier = _supplier(ctx, key)
    mails = [m for m in _order_requests(ctx) if m["to"] == supplier["mail"]]
    assert mails, f"no order request to {key}; requests went to {[m['to'] for m in _order_requests(ctx)]}"
    price = f"{supplier['price']:,.0f}"
    assert price in mails[-1]["subject"] and price in mails[-1]["body"], f"request to {key} does not quote {price}: {mails[-1]['subject']}"


@step(r'no order request is composed for supplier "([^"]+)"')
async def no_request_for(ctx: Ctx, key: str) -> None:
    supplier = _supplier(ctx, key)
    hit = [m for m in _order_requests(ctx) if supplier["mail"] and m["to"] == supplier["mail"]]
    assert not hit, f"an order request went to supplier {key}"


@step(r"no order request names the buyer or the buyer's price")
async def requests_clean(ctx: Ctx) -> None:
    words = (ctx.ids["buyer_company"], ctx.ids["buyer_mail"], f"{float(ctx.ids['buyer_price']):,.0f}")
    leaks = [m["to"] for m in _order_requests(ctx) if any(w.lower() in (m["subject"] + m["body"]).lower() for w in words)]
    assert _order_requests(ctx), "no order request was composed"
    assert not leaks, f"order request to {leaks} names the buyer or its price"


@step(r'the deal\'s supplier is supplier "([^"]+)"')
async def deal_supplier(ctx: Ctx, key: str) -> None:
    from agents.negotiation_agent.repository import get_repository
    supplier = _supplier(ctx, key)
    deal = await _deal(ctx)
    party = next((p for p in deal.parties if _value(p.role).lower() == "supplier"), None)
    session = await get_repository().get_session(ctx.ids["N"])
    assert party is not None and party.party_id == supplier["supplier_id"], f"deal supplier is {party.party_id if party else None}"
    assert session.seller_id == supplier["supplier_id"] and session.seller_email == supplier["mail"], (
        f"negotiation seller is {session.seller_id} <{session.seller_email}>")


@step(r'the gate "([^"]+)" is for supplier "([^"]+)"')
async def gate_for(ctx: Ctx, action: str, key: str) -> None:
    gates = await _gates(ctx, action)
    sid = _supplier(ctx, key)["supplier_id"]
    assert gates and gates[-1].callback_payload.get("supplier_id") == sid, (
        f"{action} is for {[_key_of(ctx, g.callback_payload.get('supplier_id')) for g in gates]}")


@step(r'the committed supplier is "([^"]+)" at its own price')
async def committed(ctx: Ctx, key: str) -> None:
    from agents.negotiation_agent.repository import get_repository
    supplier = _supplier(ctx, key)
    rec = await _commitment(ctx)
    session = await get_repository().get_session(ctx.ids["N"])
    assert rec is not None and rec.status == "confirmed" and rec.confirmed_supplier_id == supplier["supplier_id"], (
        f"committed: {rec.status if rec else None} {rec.confirmed_supplier_id if rec else None}")
    assert abs((rec.confirmed_price or 0) - supplier["price"]) < 0.01, f"committed price {rec.confirmed_price}"
    assert abs((session.supplier_last_quote or 0) - supplier["price"]) < 0.01, f"PO price basis {session.supplier_last_quote}"


@step(r'exactly (\d+) exceptions? (?:is|are) recorded with "([^"]+)" equal to "([^"]+)"')
async def exceptions_count(ctx: Ctx, n: str, field: str, value: str) -> None:
    def field_of(row: Any) -> Any:
        return getattr(row, field, None) if getattr(row, field, None) is not None else (row.context or {}).get(field)
    rows = [r for r in await _exceptions(ctx) if _value(field_of(r)) == ctx.fill(value)]
    assert len(rows) == int(n), f"{len(rows)} exception(s) with {field}={value}"
