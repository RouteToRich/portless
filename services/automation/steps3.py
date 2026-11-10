"""Fixture add-ons that need more than one record, and the remaining money steps."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from . import seed as fx
from . import steps2
from .engine import Ctx, NotAutomated, step
from .steps import _deal, _deal_id, _gate_exists, _hold


async def _add_hold(ctx: Ctx, code: str) -> None:
    from shared.orchestration import compliance
    record = await compliance.get_record(_deal_id(ctx))
    if code not in record.holds:
        record.holds.append(code)
    await compliance.save_record(record)


async def apply_after(ctx: Ctx, after: tuple[Any, ...]) -> None:
    kind = after[0]
    if kind == "candidates":
        await steps2.add_candidates(ctx, after[1], email=after[2])
    elif kind == "board":
        await steps2.add_board(ctx)
    elif kind == "offer":
        await steps2.make_session(ctx, "ROUND", last_offer_to_buyer=after[1], current_bid=after[1])
    elif kind == "hold":
        await _add_hold(ctx, after[1])
    elif kind == "milestone":
        from services.shield_link.repository import get_repository
        repo = get_repository()
        order = await repo.get(ctx.ids["FRT"])
        await repo.save(order.model_copy(update={"last_milestone": after[1], "last_milestone_at": datetime.now(timezone.utc)}))
    elif kind == "screening":
        from services.deals.repository import get_repository
        deal = await _deal(ctx)
        for party in deal.parties:
            if str(getattr(party.role, "value", party.role)).lower() == "buyer":
                party.sanctions_status = after[1]
        await get_repository().save(deal)
    elif kind == "shared_domain":
        from services.deals.repository import get_repository
        deal = await _deal(ctx)
        for party in deal.parties:
            party.email = f"{str(getattr(party.role, 'value', party.role)).lower()}@shared-domain.invalid"
            party.website = "https://shared-domain.invalid"
        await get_repository().save(deal)
    elif kind == "review":
        await _gate_exists(ctx, "CONFIRM_TRADE_COMPLIANCE", amount=25000.0, payload={
            "deal_id": _deal_id(ctx), "agreed_price": ctx.ids["buyer_price"], "quantity_mt": ctx.ids["qty"], "market": "AE",
            "hs_code": "10063020", "codes": ["hs_unverified"], "findings": [],
        })
    elif kind == "aged_escrow":
        from services.escrow_guard.repository import get_repository
        hold = await _hold(ctx)
        await get_repository().save_hold(hold.model_copy(update={"created_at": datetime.now(timezone.utc) - timedelta(days=after[1])}))
    elif kind == "funded_event":
        from services.escrow_guard.repository import get_repository
        hold = await _hold(ctx)
        await get_repository().save_hold(hold.model_copy(update={
            "status": "PARTIALLY_FUNDED", "funded_amount": after[1], "shortfall": hold.amount - after[1], "processed_events": [after[2]],
        }))
        await _record_shortfall(ctx)
    elif kind == "paid":
        await fx.add_payout(ctx.ids, after[1], after[2])
    elif kind == "reinspect":
        await _gate_exists(ctx, "RESOLVE_EXCEPTION", payload={"deal_id": _deal_id(ctx), "playbook": "reinspect", "exception_id": f"EXC-{uuid4().hex[:6]}", "category": "QC_FAILED"})
    elif kind == "payout_gate":
        from shared.orchestration import continuations
        await continuations._supplier_payout_gate(await _deal(ctx), "automation", first=True)
    elif kind == "approved_tranche":
        from services.hitl_approval.repository import get_repository
        hold = await _hold(ctx)
        gate = await _gate_exists(ctx, "RELEASE_ESCROW_TRANCHE", amount=5000.0, payload={"deal_id": _deal_id(ctx), "escrow_id": hold.escrow_id, "tranche_id": "T3"})
        repo = get_repository()
        record = await repo.get(gate.approval_id)
        await repo.save(record.model_copy(update={"status": "APPROVED", "decision": "APPROVE", "resolved_by": "qa"}))
    else:
        raise NotAutomated(f"Fixture add-on not automated: {kind}")


async def _record_shortfall(ctx: Ctx) -> None:
    from shared.bus import get_publisher
    from shared.domain.events import DomainEvent, EventTopic
    await get_publisher().publish(DomainEvent(topic=EventTopic.DEAL_EXCEPTION, deal_id=_deal_id(ctx), payload={
        "source": "escrow_guard", "error": "funding shortfall", "action_required": "funding_shortfall",
    }))
    await ctx.settle()


@step(r"exactly (\d+) exception is recorded for the shortfall")
async def one_shortfall(ctx: Ctx, n: str) -> None:
    from .steps import _exceptions
    rows = await _exceptions(ctx)
    assert len(rows) == int(n), f"{len(rows)} exceptions"
