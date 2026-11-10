"""Catalogue steps for payouts, approvals, sign-in, webhooks and the exception resolver."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx

from . import seed as fx
from .engine import PERSONAS, Ctx, step
from .steps import _approve, _deal, _deal_id, _exceptions, _gate_exists, _gates, _hold, _signed, _value


def _now() -> datetime:
    return datetime.now(timezone.utc)


@step(r'I call "([A-Z]+) ([^"]+)" with bearer token "([^"]+)"')
async def bearer(ctx: Ctx, method: str, path: str, token: str) -> None:
    from shared.auth import identity
    identity.clear_cache()
    if token == "valid" and ctx.extra.get("auth_down"):
        ctx.patch(identity, "ask_gateway", AsyncMock(side_effect=identity.IdentityError(503, "The sign-in service is unreachable")))
    elif token != "valid":
        ctx.patch(identity, "ask_gateway", AsyncMock(return_value=None))
    ctx.persona = "anonymous"
    await ctx.call(method, path, headers={"Authorization": f"Bearer {token}"})


@step(r'the connector "auth_gateway" fails with "timeout"')
async def auth_down(ctx: Ctx) -> None:
    ctx.extra["auth_down"] = True


@step(r'the provider "stripe" sends "([^"]+)" with a valid signature timestamped (\d+) minutes ago')
async def stripe_old(ctx: Ctx, path: str, minutes: str) -> None:
    raw = json.dumps({"ext_ref": ctx.ids["ESC_REF"], "status": "FUNDED", "amount": 2500000, "event_id": "ev-old"}).encode()
    await ctx.call("POST", path, content=raw, persona="anonymous", headers={"content-type": "application/json", **_signed("stripe", raw, "valid", int(minutes) * 60)})


@step(r'I post the form "([^"]+)" with field "from" set to "([^"]+)"')
async def post_form(ctx: Ctx, path: str, sender: str) -> None:
    await ctx.call("POST", path, data={"from": sender, "sender": sender, "recipient": "trade@portless-qa.invalid", "subject": "x", "text": "y", "body-plain": "y", "to": "trade@portless-qa.invalid", "timestamp": str(int(time.time())), "token": uuid4().hex, "signature": "0" * 64}, persona="anonymous")


@step(r"I post a Mailgun message (.+)")
async def mailgun(ctx: Ctx, variant: str) -> None:
    stamp, token = str(int(time.time())), uuid4().hex
    key = b"qa-mailgun"
    if variant == "signed 1 hour ago":
        stamp = str(int(time.time()) - 3600)
    if variant == "with a non-numeric time":
        stamp = "abc"
    if variant == "with a wrong signature":
        key = b"wrong"
    form = {"sender": "a@buyer.invalid", "recipient": "trade@portless-qa.invalid", "subject": "x", "body-plain": "y", "timestamp": stamp, "token": token,
            "signature": hmac.new(key, f"{stamp}{token}".encode(), hashlib.sha256).hexdigest()}
    if variant == "missing the token field":
        form.pop("token")
    await ctx.call("POST", "/email-processor/inbound/mailgun", data=form, persona="anonymous")


async def _publish(ctx: Ctx, topic: str, payload: dict[str, Any]) -> None:
    from shared.bus import get_publisher
    from shared.domain.events import DomainEvent, EventTopic
    await get_publisher().publish(DomainEvent(topic=EventTopic(topic), deal_id=_deal_id(ctx), payload=payload))
    await ctx.settle()


def _error_log(ctx: Ctx) -> list[str]:
    import logging
    if "log" not in ctx.extra:
        lines: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if record.levelno >= logging.ERROR:
                    lines.append(record.getMessage())

        handler = Capture()
        logging.getLogger().addHandler(handler)
        ctx.undo.append(lambda: logging.getLogger().removeHandler(handler))
        ctx.extra["log"] = lines
    return ctx.extra["log"]


@step(r'the event "negotiation.agreed" without a price and without a negotiation id is published')
async def agreed_echo(ctx: Ctx) -> None:
    _error_log(ctx)
    await _publish(ctx, "negotiation.agreed", {})


@step(r'the event "negotiation.agreed" with negotiation id "([^"]+)" and no price is published')
async def agreed_no_price(ctx: Ctx, neg: str) -> None:
    _error_log(ctx)
    await _publish(ctx, "negotiation.agreed", {"negotiation_id": neg})


@step(r"no error is logged for the deal")
async def no_error(ctx: Ctx) -> None:
    hits = [m for m in _error_log(ctx) if _deal_id(ctx) in m]
    assert not hits, hits[:2]


@step(r'an error containing "([^"]+)" is logged')
async def error_logged(ctx: Ctx, text: str) -> None:
    assert any(text in m for m in _error_log(ctx)), _error_log(ctx)[:3]


@step(r'the event "deal.exception" carrying an exception id is published')
async def resolver_echo(ctx: Ctx) -> None:
    await _publish(ctx, "deal.exception", {"exception_id": "EXC-echo", "source": "exception_resolver"})


@step(r"no new exception is recorded")
async def no_new_exception(ctx: Ctx) -> None:
    assert not await _exceptions(ctx), "a resolver run started"


async def _payouts(ctx: Ctx) -> list[Any]:
    from services.escrow_guard.repository import get_repository
    return await get_repository().list_payouts_by_deal(_deal_id(ctx))


@step(r'the payout has status "([^"]+)"')
async def payout_status(ctx: Ctx, status: str) -> None:
    rows = await _payouts(ctx)
    assert rows and rows[-1].status == status, [p.status for p in rows]


@step(r"exactly (\d+) payout exists for the deal")
async def exactly_payouts(ctx: Ctx, n: str) -> None:
    rows = await _payouts(ctx)
    assert len(rows) == int(n), f"{len(rows)} payouts"


async def _approved_payout_call(ctx: Ctx, amount: float, key: str) -> None:
    from services.hitl_approval.repository import get_repository
    gate = await _gate_exists(ctx, "RELEASE_SUPPLIER_PAYOUT", amount=amount, payload={"deal_id": _deal_id(ctx), "amount": amount, "idempotency_key": key})
    repo = get_repository()
    record = await repo.get(gate.approval_id)
    await repo.save(record.model_copy(update={"status": "APPROVED", "decision": "APPROVE", "resolved_by": "qa"}))
    await ctx.call("POST", "/escrow-guard/payouts", json_body={"deal_id": _deal_id(ctx), "payee_id": f"SUP-{ctx.ids['uid']}", "amount": amount, "idempotency_key": key, "approval_id": gate.approval_id})


@step(r"the same payout request is sent twice with one idempotency key")
async def same_payout(ctx: Ctx) -> None:
    key = f"pay-{ctx.ids['uid']}"
    await _approved_payout_call(ctx, 1000.0, key)
    await _approved_payout_call(ctx, 1000.0, key)


async def _complete_payouts(ctx: Ctx) -> None:
    for payout in await _payouts(ctx):
        if payout.status in ("REQUESTED", "PROCESSING") and payout.ext_ref:
            raw = json.dumps({"ext_ref": payout.ext_ref, "status": "COMPLETED", "event_id": f"p-{uuid4().hex[:8]}"}).encode()
            await ctx.call("POST", "/escrow-guard/webhooks/payout", content=raw, persona="anonymous", headers={"content-type": "application/json", **_signed("payoneer", raw, "valid")})


@step(r"every (?:open )?payout gate is approved and completed")
async def payouts_done(ctx: Ctx) -> None:
    for _ in range(6):
        if not await _gates(ctx, "RELEASE_SUPPLIER_PAYOUT", "PENDING"):
            await _complete_payouts(ctx)
            if not await _gates(ctx, "RELEASE_SUPPLIER_PAYOUT", "PENDING"):
                return
        await _approve(ctx, "staff", "RELEASE_SUPPLIER_PAYOUT")
        await _complete_payouts(ctx)


@step(r"the deal's realized_margin_usd is ([\d.]+)")
async def realized(ctx: Ctx, value: str) -> None:
    deal = await _deal(ctx)
    assert abs(float(deal.realized_margin_usd or 0) - float(value)) < 0.01, f"realized margin {deal.realized_margin_usd}"


@step(r'an approval with idempotency key "([^"]+)" exists for the deal')
async def approval_key(ctx: Ctx, key: str) -> None:
    from services.hitl_approval.schemas import CreateApprovalRequest
    from services.hitl_approval.service import create_approval
    await create_approval(CreateApprovalRequest(deal_id=_deal_id(ctx), action="ISSUE_PO", summary="seed", autonomy_level="L0", callback_payload={}, idempotency_key=f"{key}-{ctx.ids['uid']}"))


@step(r'a resolved gate "([^"]+)" exists for the deal')
async def resolved_gate(ctx: Ctx, action: str) -> None:
    await _gate_exists(ctx, action)
    await _approve(ctx, "staff", action, decision="REJECT")
    ctx.extra.pop("acted", None)


@step(r'the collection "([^"]+)" has no duplicate key "([^"]+)"')
async def no_duplicate_key(ctx: Ctx, _name: str, _key: str) -> None:
    gates = await _gates(ctx, "ISSUE_PO")
    assert len(gates) <= 1, f"{len(gates)} gates"


@step(r"a money gate of ([\d.]+) is created")
async def money_gate(ctx: Ctx, amount: str) -> None:
    if "D" not in ctx.ids:
        ctx.ids.update(await fx.seed_deal("CLEARED"))
    ctx.extra["money_gate"] = await _gate_exists(ctx, "RELEASE_ESCROW_TRANCHE", amount=float(amount))


@step(r"required approvals is (\d+)")
async def required(ctx: Ctx, n: str) -> None:
    assert ctx.extra["money_gate"].required_approvals == int(n), ctx.extra["money_gate"].required_approvals


@step(r'an L1 gate "([^"]+)" with default "([^"]+)" expired (\d+) minutes? ago')
async def expired_l1(ctx: Ctx, action: str, default: str, minutes: str) -> None:
    from services.hitl_approval.schemas import CreateApprovalRequest
    from services.hitl_approval.service import create_approval
    await create_approval(CreateApprovalRequest(
        deal_id=_deal_id(ctx), action=action, summary="seed", autonomy_level="L1", callback_payload={"email_id": "none"},
        default_on_timeout=default, expires_at=(_now() - timedelta(minutes=int(minutes))).isoformat(), idempotency_key=f"qa-{uuid4().hex[:8]}",
    ))


@step(r'a pending gate "([^"]+)" whose callback will fail')
async def failing_callback(ctx: Ctx, action: str) -> None:
    from services.hitl_approval import callbacks
    ctx.patch(callbacks, "_CALLBACK_MAP", {**callbacks._CALLBACK_MAP, action: AsyncMock(side_effect=RuntimeError("automation: callback failed"))})
    await _gate_exists(ctx, action)


@step(r"the standalone approval service is running without sign-in middleware")
async def standalone(ctx: Ctx) -> None:
    from fastapi import FastAPI
    from services.hitl_approval.router import router
    app = FastAPI()
    app.include_router(router)
    ctx.extra["standalone"] = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://standalone")
    ctx.undo.append(lambda: ctx.extra["standalone"].aclose())


@step(r'a pending "([^"]+)" gate of ([\d.]+) USD exists')
async def gate_of(ctx: Ctx, action: str, amount: str) -> None:
    if "D" not in ctx.ids:
        ctx.ids.update(await fx.seed_deal("CLEARED"))
    ctx.extra["gate"] = await _gate_exists(ctx, action, amount=float(amount))


@step(r'an anonymous caller approves it signing as "([^"]+)"')
async def anonymous_approves(ctx: Ctx, name: str) -> None:
    ctx.response = await ctx.extra["standalone"].post(f"/hitl-approval/approvals/{ctx.extra['gate'].approval_id}/resolve", json={"decision": "APPROVE", "resolved_by": name})


@step(r"the gate has no signatures")
async def no_signatures(ctx: Ctx) -> None:
    from services.hitl_approval.service import get_approval
    assert not (await get_approval(ctx.extra["gate"].approval_id)).approvals


@step(r'an exception "([^"]+)" exists for the deal')
async def exception_exists(ctx: Ctx, alias: str) -> None:
    await _publish(ctx, "deal.exception", {"source": "automation", "error": "automation"})
    rows = await _exceptions(ctx)
    assert rows, "no exception"
    ctx.ids[alias] = rows[0].exception_id


@step(r'a pending gate "RESOLVE_EXCEPTION" without a playbook')
async def resolve_no_playbook(ctx: Ctx) -> None:
    if "D" not in ctx.ids:
        ctx.ids.update(await fx.seed_deal("DISPUTE"))
    await _gate_exists(ctx, "RESOLVE_EXCEPTION", payload={"deal_id": _deal_id(ctx), "exception_id": f"EXC-{uuid4().hex[:6]}"})


@step(r"the buyer is screened again")
async def screened(ctx: Ctx) -> None:
    deal = await _deal(ctx)
    party = next(p for p in deal.parties if _value(p.role).lower() == "buyer")
    age = _now() - datetime.fromisoformat(str(party.sanctions_checked_at).replace("Z", "+00:00"))
    if age >= timedelta(days=30):
        from .engine import NotAutomated
        raise NotAutomated("A repeated screening needs the counterparty-check actor, which is stubbed here")


@step(r'the gate check "([^"]+)" holds')
async def gate_check(ctx: Ctx, expected: str) -> None:
    if expected == "none":
        assert not await _gates(ctx, "CONFIRM_COUNTERPARTY_RISK")
        return
    if expected.startswith("pending "):
        assert await _gates(ctx, expected.split(" ", 1)[1], "PENDING"), expected
        return
    if expected.startswith("exception"):
        assert await _exceptions(ctx), expected
        return
    from .engine import NotAutomated
    raise NotAutomated(f"Gate check not automated: {expected}")


@step(r'the deal reaches "([^"]+)"')
async def deal_reaches(ctx: Ctx, status: str) -> None:
    assert _value((await _deal(ctx)).status) == status, _value((await _deal(ctx)).status)


@step(r'a pending gate "CONFIRM_COUNTERPARTY_RISK" exists for a confirmed-HS deal')
async def risk_gate(ctx: Ctx) -> None:
    from .steps2 import hs_confirmed, make_session
    ctx.ids.update(await fx.seed_deal("NEGOTIATING"))
    await hs_confirmed(ctx, fx.HS_CODE, "free")
    from services.deals.repository import get_repository
    from shared.domain.enums import DealStatus
    await make_session(ctx, "AGREED", agreed_price=ctx.ids["buyer_price"])
    await get_repository().save((await _deal(ctx)).model_copy(update={"status": DealStatus.NEGOTIATION_AGREED}))
    await _gate_exists(ctx, "CONFIRM_COUNTERPARTY_RISK", amount=25000.0, payload={"deal_id": _deal_id(ctx), "agreed_price": ctx.ids["buyer_price"], "quantity_mt": ctx.ids["qty"], "market": fx.MARKET})


@step(r'I call the tranche release directly for "([^"]+)"')
async def direct_release(ctx: Ctx, tranche: str) -> None:
    from services.escrow_guard.schemas import ReleaseTrancheRequest
    from services.escrow_guard.service import InvalidDealState, release_tranche
    try:
        await release_tranche(ctx.ids["ESC"], ReleaseTrancheRequest(tranche_id=tranche, proof_refs=["qa"]))
        ctx.response = httpx.Response(200, json={})
    except InvalidDealState as exc:
        ctx.response = httpx.Response(422, json={"detail": str(exc)})


@step(r'the tranche callback for "([^"]+)" runs again')
async def tranche_again(ctx: Ctx, tranche: str) -> None:
    from services.hitl_approval.callbacks import execute_callback
    hold = await _hold(ctx)
    ctx.extra["acted"] = True
    await execute_callback("RELEASE_ESCROW_TRANCHE", _deal_id(ctx), {"escrow_id": hold.escrow_id, "tranche_id": tranche, "proof_refs": ["qa"]}, "APPR-repeat")
    await ctx.settle()


@step(r'the document "CI" is requested twice with the same key')
async def doc_twice(ctx: Ctx) -> None:
    body = {"deal_id": _deal_id(ctx), "doc_type": "CI", "idempotency_key": f"doc-{ctx.ids['uid']}"}
    await ctx.call("POST", "/docugen/documents", json_body=body)
    await ctx.call("POST", "/docugen/documents", json_body=body)


@step(r'the collection "documents" has no duplicate "CI"')
async def no_duplicate_ci(ctx: Ctx) -> None:
    from services.docugen.repository import get_repository
    rows = [d for d in await get_repository().list_by_deal(_deal_id(ctx)) if _value(d.doc_type) == "CI"]
    assert len(rows) == 1, f"{len(rows)} CI documents"


__all__ = ["PERSONAS"]
