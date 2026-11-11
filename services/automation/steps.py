"""The catalogue's step vocabulary, implemented against the real application."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

from . import seed as fx
from .engine import PERSONAS, Ctx, NotAutomated, step

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _deal_id(ctx: Ctx) -> str:
    return str(ctx.ids["D"])


async def _deal(ctx: Ctx, deal_id: str | None = None) -> Any:
    from services.deals.service import get_deal
    return await get_deal(deal_id or _deal_id(ctx))


def _value(obj: Any) -> str:
    return str(getattr(obj, "value", obj))


async def _gates(ctx: Ctx, action: str | None = None, status: str | None = None) -> list[Any]:
    from services.hitl_approval.service import list_approvals
    rows = await list_approvals(status=status, deal_id=_deal_id(ctx))
    return [r for r in rows if action is None or r.action == action]


async def _hold(ctx: Ctx) -> Any:
    from services.escrow_guard.repository import get_repository
    return await get_repository().get_hold_by_deal(_deal_id(ctx))


async def _ledger(ctx: Ctx) -> Any:
    from services.escrow_guard.service import get_deal_ledger
    return await get_deal_ledger(_deal_id(ctx))


async def take_snapshot(ctx: Ctx) -> None:
    deal = await _deal(ctx)
    ctx.snapshot["status"] = _value(deal.status)
    ctx.snapshot["updated_at"] = deal.updated_at
    if "ESC" in ctx.ids:
        ctx.snapshot["ledger"] = (await _ledger(ctx)).model_dump(mode="json")


def _path(data: Any, dotted: str) -> Any:
    current = data
    for part in dotted.replace("[", ".").replace("]", "").split("."):
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(dotted)
    return current


def _same(actual: Any, expected: str) -> bool:
    if isinstance(actual, bool):
        return str(actual).lower() == expected.lower()
    if actual is None:
        return expected in ("", "null", "None")
    if isinstance(actual, (int, float)):
        try:
            return abs(float(actual) - float(expected)) < 0.01
        except ValueError:
            return False
    return str(actual).lower() == expected.lower()


async def natural_gate(ctx: Ctx, action: str) -> None:
    """A deal seeded at a stage has the gates that stage opens. Created through
    the real continuation, and only before the scenario has done anything, so a
    gate an action should have opened can never be supplied here."""
    if ctx.extra.get("acted") or "D" not in ctx.ids or await _gates(ctx, action):
        return
    from shared.domain.events import DomainEvent, EventTopic
    from shared.orchestration import continuations as c
    deal = await _deal(ctx)
    status = _value(deal.status)
    if action == "ISSUE_PO" and status == "ESCROW_FUNDED":
        await c.on_escrow_funded(DomainEvent(topic=EventTopic.ESCROW_FUNDED, deal_id=_deal_id(ctx), payload={"payer_name": ctx.ids["buyer_company"], "payer_country": fx.MARKET}))
    elif action == "RELEASE_ESCROW_TRANCHE" and status in ("PO_ISSUED", "SUPPLIER_DISPATCH_READY", "QC_PASSED", "IN_TRANSIT", "CLEARED"):
        hold = await _hold(ctx)
        due = next((t for t in hold.tranches if t.status == "PENDING" and t.trigger != "ACCEPTED"), None)
        if due is not None:
            await c._tranche_gate(deal, due.trigger, ["proof-qa"], "automation")
    elif action == "BOOK_FREIGHT" and status == "QC_PASSED":
        await _gate_exists(ctx, action, payload={"deal_id": _deal_id(ctx), "provider": "cogoport", "incoterm": "FOB"})
    elif action == "FILE_CUSTOMS" and status == "IN_TRANSIT":
        await _gate_exists(ctx, action, payload={"deal_id": _deal_id(ctx), "provider": "icegate", "documents": []})
    elif action == "RELEASE_SUPPLIER_PAYOUT" and status == "CLEARED":
        await c._supplier_payout_gate(deal, "automation", first=True)
    await ctx.settle()


async def _approve(ctx: Ctx, persona: str, action: str, *, decision: str = "APPROVE", signing_as: str | None = None) -> None:
    await natural_gate(ctx, action)
    gates = await _gates(ctx, action, "PENDING")
    assert gates, f"no pending {action} gate; pending: {[g.action for g in await _gates(ctx, None, 'PENDING')]}"
    ctx.ids["APPR"] = gates[0].approval_id
    await ctx.call("POST", f"/hitl-approval/approvals/{gates[0].approval_id}/resolve", persona=persona,
                   json_body={"decision": decision, "resolved_by": signing_as or persona})


async def _gate_exists(ctx: Ctx, action: str, *, amount: float | None = None, payload: dict[str, Any] | None = None, level: str = "L0") -> Any:
    from services.hitl_approval.schemas import CreateApprovalRequest
    from services.hitl_approval.service import create_approval

    gate = await create_approval(CreateApprovalRequest(
        deal_id=_deal_id(ctx), action=action, summary=f"automation fixture for {action}", autonomy_level=level,
        financial_impact_usd=amount, callback_payload=payload or {}, idempotency_key=f"qa-{uuid4().hex[:10]}",
    ))
    ctx.ids["APPR"] = gate.approval_id
    return gate


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@step(r'I am "([^"]+)"')
async def am(ctx: Ctx, persona: str) -> None:
    if persona == "org":
        persona = "org_user"
    ctx.persona = persona


@step(r'the setting "([^"]+)" is "([^"]*)"')
async def setting(ctx: Ctx, name: str, value: str) -> None:
    ctx.set_env(name, value)
    if name == "ENV_PROFILE":
        raise NotAutomated("Needs a backend restarted under this profile")
    if name == "INSPECTION_WINDOW_TRIGGER":
        from services.escrow_guard.config import settings as escrow_settings
        ctx.patch(escrow_settings, "inspection_window_trigger", value)
    if name == "AUTH_TRUST_GATEWAY_HEADERS":
        from services.auth.config import settings
        ctx.patch(settings, "trust_gateway_headers", value.lower() == "true")
    if name.endswith("_WEBHOOK_SECRET"):
        await _blank_provider_secret(ctx, name, value)


async def _blank_provider_secret(ctx: Ctx, name: str, value: str) -> None:
    from shared.connectors import payments, shields
    for module in (payments, shields):
        for attr in dir(module):
            cls = getattr(module, attr)
            if isinstance(cls, type) and hasattr(cls, "webhook_verify") and hasattr(cls, "_secret_env") and cls._secret_env == name:
                ctx.patch(cls, "_secret_override", value)


@step(r'the autonomy level of "([^"]+)" is "([^"]+)"')
async def autonomy(ctx: Ctx, action: str, level: str) -> None:
    ctx.set_env(f"AUTONOMY_{action}", level)


_RELEASED = re.compile(r"with (T[1-4](?:(?:, | and )T[1-4])*) released")


def _options(suffix: str) -> dict[str, Any]:
    """Fixture wording after 'from fixture "X"'. Anything unrecognised is reported, not ignored."""
    s, opts = suffix.strip(), {}  # type: str, dict[str, Any]
    if not s:
        return opts
    if (m := _RELEASED.search(s)):
        opts["released"] = re.findall(r"T[1-4]", m.group(1))
        s = (s[:m.start()] + s[m.end():]).strip()
    if not s:
        return opts
    if s.startswith("and "):
        s = "with " + s[4:]
    rules: list[tuple[str, Any]] = [
        (r"with T1 unreleased", lambda m: {"released": []}),
        (r"waiting for loading", lambda m: {"loading": False}),
        (r"with loading recorded", lambda m: {"loading": True}),
        (r"with the window open", lambda m: {"window_open": True, "released": ["T1", "T2", "T3"]}),
        (r"fully settled", lambda m: {"released": ["T1", "T2", "T3", "T4"], "settled": True}),
        (r"owned by \"([^\"]+)\"", lambda m: {"org_id": m.group(1)}),
        (r"funded with ([\d.]+)", lambda m: {"funded": float(m.group(1))}),
        (r"using escrow provider \"([^\"]+)\"", lambda m: {"provider": m.group(1)}),
        (r"with a disputed escrow", lambda m: {"disputed": True}),
        (r"with market \"([^\"]+)\"", lambda m: {"market": m.group(1)}),
        (r"with no buyer target price and no PI", lambda m: {"target_price": None, "po": None}),
        (r"with no buyer target price", lambda m: {"target_price": None}),
        (r"whose buyer opted for \"([^\"]+)\"", lambda m: {"qc_provider": m.group(1)}),
        (r"with no PO on record", lambda m: {"po": False}),
        (r"with delivery recorded", lambda m: {"delivery": True}),
        (r"with no hold", lambda m: {"with_hold": False}),
        (r"with ([\d.]+) paid and the window open", lambda m: {"window_open": True, "released": ["T1", "T2", "T3"], "_after": ("paid", float(m.group(1)), "COMPLETED")}),
        (r"with ([\d.]+) paid and a disputed escrow", lambda m: {"disputed": True, "released": ["T1", "T2", "T3"], "_after": ("paid", float(m.group(1)), "COMPLETED")}),
        (r"with a processing payout of ([\d.]+)", lambda m: {"_after": ("paid", float(m.group(1)), "PROCESSING")}),
        (r"with ([\d.]+) paid", lambda m: {"_after": ("paid", float(m.group(1)), "COMPLETED")}),
        (r"fully released with a processing balance payout", lambda m: {"released": ["T1", "T2", "T3", "T4"], "_after": ("paid", 20000.0, "PROCESSING")}),
        (r"with the final tranche out", lambda m: {"released": ["T1", "T2", "T3", "T4"]}),
        (r"with PI idempotency key \"([^\"]+)\"", lambda m: {"pi_key": m.group(1)}),
        (r"with escrow idempotency key \"([^\"]+)\"", lambda m: {"escrow_key": m.group(1)}),
        (r"with inspector seal \"([^\"]+)\"", lambda m: {"loading": True}),
        (r"at milestone \"([^\"]+)\"", lambda m: {"_after": ("milestone", m.group(1))}),
        (r"with (\d+) buyer candidates?", lambda m: {"_after": ("candidates", int(m.group(1)), True)}),
        (r"with (\d+) buyer candidate without an e-mail", lambda m: {"_after": ("candidates", int(m.group(1)), False)}),
        (r"with buyer \"C1\" and supplier offer \"OF1\"", lambda m: {"_after": ("board",)}),
        (r"with no buyer and no offer", lambda m: {}),
        (r"with an offer of ([\d.]+) sent to the buyer", lambda m: {"_after": ("offer", float(m.group(1)))}),
        # The PO margin guard measures against the PI's agreed price, so the
        # buyer price must be the PI's (and the session's), not only the target.
        (r"with buyer price ([\d.]+)", lambda m: {"buyer_price": float(m.group(1))}),
        (r"with an open \"([^\"]+)\" hold", lambda m: {"_after": ("hold", m.group(1))}),
        (r"with market \"([^\"]+)\"", lambda m: {"market": m.group(1)}),
        (r"with a fresh buyer screening of \"([^\"]+)\"", lambda m: {"_after": ("screening", m.group(1))}),
        (r"with buyer and supplier sharing a domain", lambda m: {"_after": ("shared_domain",)}),
        (r"with a pending compliance review", lambda m: {"_after": ("review",)}),
        (r"unfunded past the grace period", lambda m: {"_after": ("aged_escrow", 15)}),
        (r"funded with ([\d.]+) by event \"([^\"]+)\"", lambda m: {"_after": ("funded_event", float(m.group(1)), m.group(2))}),
        (r"with the re-inspection gate pending", lambda m: {"_after": ("reinspect",)}),
        (r"with the payout gate open", lambda m: {"_after": ("payout_gate",)}),
        (r"with an approved tranche gate \"\{APPR\}\"", lambda m: {"_after": ("approved_tranche",)}),
    ]
    for pattern, build in rules:
        m = re.fullmatch(pattern, s)
        if m:
            opts.update(build(m))
            return opts
    raise NotAutomated(f"Fixture variant not automated: {suffix.strip()}")


@step(r'a deal "([^"]+)" at status "([^"]+)" from fixture "([^"]+)"(.*)')
async def deal_at_status(ctx: Ctx, alias: str, status: str, fixture: str, suffix: str) -> None:
    opts = _options(suffix)
    hold_payout = opts.pop("delivery", False)
    after = opts.pop("_after", None)
    ctx.ids.update(await fx.seed_deal(status, fixture, **opts))
    ctx.ids[alias] = ctx.ids["D"]
    if after:
        from .steps3 import apply_after
        await apply_after(ctx, after)
    if hold_payout:
        from services.escrow_guard.repository import get_repository
        hold = await _hold(ctx)
        await get_repository().save_hold(hold.model_copy(update={"delivery_confirmed_at": datetime.now(timezone.utc)}))


@step(r'a deal "([^"]+)" built from fixture "([^"]+)"(?: with market "([^"]+)")?')
async def deal_built(ctx: Ctx, alias: str, fixture: str, market: str | None) -> None:
    ctx.ids.update(await fx.seed_deal("MATCHING", fixture, market=market or fx.MARKET))
    ctx.ids[alias] = ctx.ids["D"]


async def _aged(ctx: Ctx, repo_get: Any, repo_save: Any, record: Any, field: str, n: str, unit: str) -> None:
    when = datetime.now(timezone.utc) - timedelta(**{unit if unit.endswith("s") else unit + "s": int(n)})
    await repo_save(record.model_copy(update={field: when}))


@step(r'"([^"]+)" of the deal\'s PI was set (\d+) (hours?|days?) ago')
async def pi_aged(ctx: Ctx, field: str, n: str, unit: str) -> None:
    from services.dealmaker.repository import get_repository
    repo = get_repository()
    await _aged(ctx, None, repo.save_pi, await repo.get_pi(ctx.ids["PI"]), field, n, unit)


@step(r'"([^"]+)" of the deal\'s escrow was set (\d+) (hours?|days?) ago')
async def escrow_aged(ctx: Ctx, field: str, n: str, unit: str) -> None:
    from services.escrow_guard.repository import get_repository
    await _aged(ctx, None, get_repository().save_hold, await _hold(ctx), field, n, unit)


@step(r'"([^"]+)" of the deal was set (\d+) (hours?|days?) ago')
async def deal_aged(ctx: Ctx, field: str, n: str, unit: str) -> None:
    from services.deals.repository import get_repository
    deal = await _deal(ctx)
    when = datetime.now(timezone.utc) - timedelta(**{unit if unit.endswith("s") else unit + "s": int(n)})
    if field == "updated_at":  # the watcher dates a wait from the record that started it
        await _age_wait(ctx, when)
    if field == "sanctions_checked_at":
        for party in deal.parties:
            party.sanctions_checked_at = when.isoformat()
        await get_repository().save(deal)
        return
    await get_repository().save(deal.model_copy(update={field: when}))


async def _age_wait(ctx: Ctx, when: datetime) -> None:
    from services.dealmaker.repository import get_repository as dealmaker
    from services.escrow_guard.repository import get_repository as escrow
    if "PI" in ctx.ids:
        pi = await dealmaker().get_pi(ctx.ids["PI"])
        await dealmaker().save_pi(pi.model_copy(update={"issued_at": when}))
    if "PO" in ctx.ids:
        po = await dealmaker().get_po(ctx.ids["PO"])
        await dealmaker().save_po(po.model_copy(update={"issued_at": when}))
    if "ESC" in ctx.ids:
        await escrow().save_hold((await _hold(ctx)).model_copy(update={"created_at": when}))


@step(r'a pending gate "([^"]+)" exists for the deal')
async def pending_gate_for_deal(ctx: Ctx, action: str) -> None:
    if ctx.keyword != "Given":
        assert await _gates(ctx, action, "PENDING"), f"no pending {action}"
        return
    if "D" not in ctx.ids:
        ctx.ids.update(await fx.seed_deal("ESCROW_FUNDED"))
    payload: dict[str, Any] = {}
    if action == "CONFIRM_NEGOTIATION_AGREEMENT":
        from .steps2 import make_session
        neg = await make_session(ctx, "HITL_PENDING", agreed_price=ctx.ids["buyer_price"])
        payload = {"negotiation_id": neg, "agreed_price": ctx.ids["buyer_price"]}
    await _gate_exists(ctx, action, payload=payload)


@step(r'the connector "([^"]+)" fails with "([^"]+)"')
async def connector_fails(ctx: Ctx, name: str, error: str) -> None:
    from shared.connectors import escrow as escrow_connectors, payments, qc_providers, shields

    if name == "smtp":
        from services.email_gateway import sender
        from services.email_gateway.sender import EmailSendError
        ctx.patch(sender, "send_email", AsyncMock(side_effect=EmailSendError(error)))
        return
    if name == "google_drive":
        from shared.connectors import google_drive
        from services.docugen import service as docugen
        ctx.patch(docugen, "load_stored_document", AsyncMock(side_effect=google_drive.GoogleDriveError(error)))
        return
    targets = {"tazapay": (escrow_connectors, "TazapayEscrowConnector"),
               "escrow_com": (escrow_connectors, "EscrowComConnector"), "payoneer": (payments, "PayoneerConnector"),
               "cogoport": (shields, "CogoportConnector"), "sgs": (qc_providers, "SgsConnector"),
               "qima": (qc_providers, "QimaConnector")}
    if name not in targets:
        raise NotAutomated(f"Connector failure not automated: {name}")
    module, cls_name = targets[name]
    cls = getattr(module, cls_name)
    boom = AsyncMock(side_effect=module.ConnectorError(error))
    for method in ("quote", "book", "release", "refund"):
        if hasattr(cls, method):
            ctx.patch(cls, method, boom)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _split(call: str) -> tuple[str, str]:
    method, path = call.split(" ", 1)
    return method, path


@step(r'I call "([A-Z]+ [^"]+)"')
async def call_plain(ctx: Ctx, call: str) -> None:
    method, path = _split(call)
    await ctx.call(method, path)


@step(r'I call "([A-Z]+ [^"]+)" with body:')
async def call_body(ctx: Ctx, call: str) -> None:
    method, path = _split(call)
    await ctx.call(method, path, json_body=ctx.body())


@step(r'I call "([A-Z]+ [^"]+)" with header "([^"]+)" set to "([^"]*)"')
async def call_header(ctx: Ctx, call: str, name: str, value: str) -> None:
    method, path = _split(call)
    await ctx.call(method, path, headers={name: value})


@step(r'I call with its approved gate "([A-Z]+ [^"]+)" with body:')
async def call_with_gate(ctx: Ctx, call: str) -> None:
    from services.hitl_approval.repository import get_repository

    method, path = _split(call)
    path = ctx.fill(path)
    body = ctx.body() or {}
    actions = {"/release": "RELEASE_ESCROW_TRANCHE", "/accept": "CONFIRM_BUYER_ACCEPTANCE", "/refund": "REFUND_ESCROW", "/payouts": "RELEASE_SUPPLIER_PAYOUT"}
    action = next((a for suffix, a in actions.items() if path.endswith(suffix)), None)
    if action is None:
        await ctx.call(method, path, json_body=body)
        return
    escrow = path.split("/escrows/")[1].split("/")[0] if "/escrows/" in path else None
    payload: dict[str, Any] = {k: body.get(k) for k in ("tranche_id", "deal_id", "amount", "idempotency_key") if k in body}
    if escrow:
        payload["escrow_id"] = escrow
    gate = await _gate_exists(ctx, action, payload=payload, level="L0")
    repo = get_repository()
    record = await repo.get(gate.approval_id)
    await repo.save(record.model_copy(update={"status": "APPROVED", "decision": "APPROVE", "resolved_by": "qa"}))
    await ctx.call(method, path, json_body={**body, "approval_id": gate.approval_id})


@step(r'"([^"]+)" approves the pending "([^"]+)" gate')
async def approves(ctx: Ctx, persona: str, action: str) -> None:
    await _approve(ctx, persona, action)


@step(r'"([^"]+)" has approved the pending "([^"]+)" gate')
async def has_approved(ctx: Ctx, persona: str, action: str) -> None:
    await _approve(ctx, persona, action)


@step(r'"([^"]+)" rejects the pending "([^"]+)" gate')
async def rejects(ctx: Ctx, persona: str, action: str) -> None:
    await _approve(ctx, persona, action, decision="REJECT")


@step(r'"([^"]+)" approves the pending "([^"]+)" gate signing as "([^"]+)"')
async def approves_signing(ctx: Ctx, persona: str, action: str, name: str) -> None:
    await _approve(ctx, persona, action, signing_as=name)


@step(r'the sweep "([^"]+)" runs')
async def sweep(ctx: Ctx, name: str) -> None:
    now = datetime.now(timezone.utc)
    if name == "deal_watcher":
        from shared.orchestration import deal_watcher
        ctx.set_env("DEAL_WATCHER_ENABLED", "true")
        await deal_watcher.sweep_stalled_deals(now)
    elif name == "escrow_watcher":
        from services.escrow_guard.service import check_inspection_windows, check_reconciliation
        await check_inspection_windows(now)
        await check_reconciliation()
    elif name == "hitl_timeout":
        from services.hitl_approval import service as hitl
        from services.hitl_approval.repository import get_repository
        for record in await get_repository().list_pending_expired(now.isoformat()):
            await hitl._auto_resolve(record, record.default_on_timeout, "automation sweep")
    elif name == "negotiation_stale":
        from agents.negotiation_agent.service import sweep_stale_negotiations
        await sweep_stale_negotiations()
    else:
        raise NotAutomated(f"Sweep not automated: {name}")
    await ctx.settle()


_SIGNATURES: dict[str, tuple[str, str]] = {
    "tazapay": ("x-tazapay-signature", "qa-tazapay"), "escrow_com": ("x-escrow-signature", "qa-escrowcom"),
    "escrow.com": ("x-escrow-signature", "qa-escrowcom"), "payoneer": ("x-payoneer-signature", "qa-payoneer"),
    "sgs": ("x-sgs-signature", "qa-sgs"), "qima": ("x-qima-signature", "qa-qima"),
    "bureau_veritas": ("x-bv-signature", "qa-bv"), "intertek": ("x-intertek-signature", "qa-intertek"),
    "tuv_sud": ("x-tuvsud-signature", "qa-tuv"), "cogoport": ("x-cogoport-signature", "qa-cogoport"),
    "freightos": ("x-freightos-signature", "qa-freightos"),
}


def _signed(provider: str, raw: bytes, kind: str, age_seconds: int = 0) -> dict[str, str]:
    if kind == "none":
        return {}
    if provider == "stripe":
        stamp = str(int(time.time()) - age_seconds)
        key = b"wrong" if kind == "wrong" else b"qa-stripe"
        digest = hmac.new(key, f"{stamp}.".encode() + raw, hashlib.sha256).hexdigest()
        return {"stripe-signature": f"t={stamp},v1={digest}"}
    if provider == "tazapay":  # Tazapay's own scheme: base64 HMAC over event id + body + created_at
        from shared.connectors.escrow import tazapay_signature
        return {"signature": tazapay_signature("wrong" if kind == "wrong" else "qa-tazapay", raw)}
    if provider not in _SIGNATURES:
        raise NotAutomated(f"Signature for provider not automated: {provider}")
    header, secret = _SIGNATURES[provider]
    key = b"wrong" if kind == "wrong" else secret.encode()
    return {header: hmac.new(key, raw, hashlib.sha256).hexdigest()}


@step(r'the provider "([^"]+)" sends "([^"]+)" with signature "([^"]+)" and body:')
async def provider_sends(ctx: Ctx, provider: str, path: str, kind: str) -> None:
    if ctx.doc and "{PAY_REF}" in ctx.doc and "PAY_REF" not in ctx.ids:
        await fx.add_payout(ctx.ids, 1000.0, "PROCESSING")
    try:
        raw = json.dumps(ctx.body()).encode("utf-8")
    except ValueError:
        raw = ctx.fill(ctx.doc or "").encode("utf-8")
    await ctx.call("POST", path, content=raw, persona="anonymous",
                   headers={"content-type": "application/json", **_signed(provider, raw, kind)})


@step(r'the event "([^"]+)" for the deal is published( twice)?')
async def event_published(ctx: Ctx, topic: str, twice: str | None) -> None:
    from shared.bus import get_publisher
    from shared.domain.events import DomainEvent, EventTopic
    deal = await _deal(ctx)
    payload = {"status": _value(deal.status)} if topic == "deal.created" else {}
    for _ in range(2 if twice else 1):
        await get_publisher().publish(DomainEvent(topic=EventTopic(topic), deal_id=_deal_id(ctx), payload=payload))
        await ctx.settle()


@step(r'the carrier reports "([^"]+)"(?: with seal "([^"]+)")?')
async def carrier(ctx: Ctx, milestone: str, seal: str | None) -> None:
    body: dict[str, Any] = {"ext_ref": ctx.ids["FRT_REF"], "milestone": milestone}
    if seal:
        body["seal_numbers"] = [seal]
    raw = json.dumps(body).encode("utf-8")
    await ctx.call("POST", "/shield-link/freight/webhook", content=raw, persona="anonymous",
                   headers={"content-type": "application/json", **_signed("cogoport", raw, "valid")})
    assert ctx.response is not None and ctx.response.status_code < 300, ctx.response.text


async def _qc_webhook(ctx: Ctx, result: str, **extra: Any) -> None:
    body: dict[str, Any] = {"ext_ref": ctx.ids["QC_REF"], "result": result, "crf_hash": "crf-qa", "report_url": "https://reports.invalid/qc.pdf", **extra}
    raw = json.dumps(body).encode("utf-8")
    await ctx.call("POST", "/shield-link/qc/webhook", content=raw, persona="anonymous",
                   headers={"content-type": "application/json", **_signed("sgs", raw, "valid")})
    assert ctx.response is not None and ctx.response.status_code < 300, ctx.response.text


@step(r"the inspector reports FAIL")
async def inspector_fail(ctx: Ctx) -> None:
    await _qc_webhook(ctx, "FAIL")


@step(r"the inspector reports PASS with a sealed container")
async def inspector_sealed(ctx: Ctx) -> None:
    await _qc_webhook(ctx, "PASS", container_numbers=["CONT-1"], seal_numbers=["SEAL-1"], loaded_quantity_mt=ctx.ids["qty"],
                      loaded_hs_code=fx.HS_CODE, photos=["https://photos.invalid/1.jpg"])


@step(r"the inspector reports PASS with no loading details")
async def inspector_bare(ctx: Ctx) -> None:
    await _qc_webhook(ctx, "PASS")


async def _reply(ctx: Ctx, sender: str, subject: str, text: str, in_reply_to: str = "") -> Any:
    from services.email_processor.schemas import InboundEmailSource, InboundRawEmail
    from services.email_processor.service import process_inbound

    result = await process_inbound(InboundRawEmail(
        from_address=sender, to_address="trade@portless-qa.invalid", subject=subject, body_text=text, in_reply_to=in_reply_to,
        message_id=f"<{uuid4().hex}@qa.invalid>", source=InboundEmailSource.IMAP, received_at=datetime.now(timezone.utc),
    ))
    await ctx.settle()
    ctx.extra["inbound"] = result
    return result


@step(r'the buyer replies "([^"]+)" from the known address')
async def buyer_replies(ctx: Ctx, text: str) -> None:
    await _reply(ctx, ctx.ids["buyer_mail"], f"Re: your offer [NEG:{ctx.ids['N']}]", text)


@step(r"the buyer's acceptance is recorded")
async def buyer_acceptance(ctx: Ctx) -> None:
    from services.escrow_guard.schemas import AcceptEscrowRequest
    from services.escrow_guard.service import accept_escrow
    await accept_escrow(ctx.ids["ESC"], AcceptEscrowRequest(accepted_by="qa-buyer"))
    await ctx.settle()


@step(r'the platform is paused by "([^"]+)" with reason "([^"]+)"')
async def paused(ctx: Ctx, persona: str, reason: str) -> None:
    from shared.orchestration import kill_switch
    await kill_switch.pause(persona, reason)
    ctx.undo.append(lambda: kill_switch.resume("automation", "scenario finished"))


# ---------------------------------------------------------------------------
# API results
# ---------------------------------------------------------------------------

@step(r"the response status is (\d+)")
async def status_is(ctx: Ctx, code: str) -> None:
    assert ctx.response is not None, "no call was made"
    assert ctx.response.status_code == int(code), f"expected {code}, got {ctx.response.status_code}: {ctx.response.text[:400]}"


@step(r'the response field "([^"]+)" is "([^"]*)"')
async def field_is(ctx: Ctx, path: str, expected: str) -> None:
    assert ctx.response is not None
    actual = _path(ctx.response.json(), path)
    if expected == "the caller's user id":
        expected = PERSONAS[ctx.persona]["X-User-ID"]  # type: ignore[index]
    assert _same(actual, ctx.fill(expected)), f"{path}: expected {expected!r}, got {actual!r}"


@step(r'the response field "([^"]+)" is not "([^"]*)"')
async def field_is_not(ctx: Ctx, path: str, expected: str) -> None:
    assert ctx.response is not None
    actual = _path(ctx.response.json(), path)
    assert not _same(actual, ctx.fill(expected)), f"{path} should not be {expected!r}"


@step(r'the response field "([^"]+)" is present')
async def field_present(ctx: Ctx, path: str) -> None:
    assert ctx.response is not None
    try:
        _path(ctx.response.json(), path)
    except (KeyError, IndexError, ValueError) as exc:
        raise AssertionError(f"{path} missing in {ctx.response.text[:300]}") from exc


@step(r'the response detail contains "([^"]+)"')
async def detail_contains(ctx: Ctx, text: str) -> None:
    assert ctx.response is not None
    detail = str(ctx.response.json().get("detail"))
    assert ctx.fill(text).lower() in detail.lower(), f"detail was: {detail[:300]}"


# ---------------------------------------------------------------------------
# State results
# ---------------------------------------------------------------------------

@step(r'deal "([^"]+)" has status "([^"]+)"')
async def deal_status(ctx: Ctx, alias: str, status: str) -> None:
    deal = await _deal(ctx, str(ctx.ids.get(alias, alias)))
    assert _value(deal.status) == status, f"deal is {_value(deal.status)}, expected {status}"


@step(r'deal "([^"]+)" is unchanged')
async def deal_unchanged(ctx: Ctx, alias: str) -> None:
    deal = await _deal(ctx, str(ctx.ids.get(alias, alias)))
    assert _value(deal.status) == ctx.snapshot.get("status"), f"status moved to {_value(deal.status)}"


@step(r'a pending gate "([^"]+)" exists at level "([^"]+)"')
async def pending_at_level(ctx: Ctx, action: str, level: str) -> None:
    await natural_gate(ctx, action)
    gates = await _gates(ctx, action, "PENDING")
    assert gates, f"no pending {action}; gates: {[(g.action, g.status) for g in await _gates(ctx)]}"
    assert any(g.autonomy_level == level for g in gates), f"{action} is at {[g.autonomy_level for g in gates]}, expected {level}"


@step(r'a gate "([^"]+)" exists with status "([^"]+)"')
async def gate_with_status(ctx: Ctx, action: str, status: str) -> None:
    assert await _gates(ctx, action, status), f"no {action} at {status}; gates: {[(g.action, g.status) for g in await _gates(ctx)]}"


@step(r'no gate "([^"]+)" exists')
async def no_gate(ctx: Ctx, action: str) -> None:
    assert not await _gates(ctx, action), f"unexpected {action}"


@step(r'no gate "([^"]+)" exists with status "([^"]+)"')
async def no_gate_status(ctx: Ctx, action: str, status: str) -> None:
    assert not await _gates(ctx, action, status), f"unexpected {action} at {status}"


@step(r'exactly (\d+) gate "([^"]+)" exists')
async def exactly_gates(ctx: Ctx, n: str, action: str) -> None:
    found = await _gates(ctx, action)
    assert len(found) == int(n), f"{len(found)} {action} gates"


async def _one_gate(ctx: Ctx, action: str) -> Any:
    await natural_gate(ctx, action)
    gates = await _gates(ctx, action)
    assert gates, f"no {action}; gates: {[g.action for g in await _gates(ctx)]}"
    return sorted(gates, key=lambda g: g.created_at)[-1]


@step(r'the gate "([^"]+)" has risk flag "([^"]+)"')
async def gate_flag(ctx: Ctx, action: str, flag: str) -> None:
    gate = await _one_gate(ctx, action)
    every = await _gates(ctx, action)
    assert any(flag in g.risk_flags for g in every), f"flags: {[g.risk_flags for g in every]}, latest {gate.approval_id}"


@step(r'the gate "([^"]+)" does not have risk flag "([^"]+)"')
async def gate_no_flag(ctx: Ctx, action: str, flag: str) -> None:
    gate = await _one_gate(ctx, action)
    assert flag not in gate.risk_flags, f"flags: {gate.risk_flags}"


@step(r'the gate "([^"]+)" amount is ([\d.]+)')
async def gate_amount(ctx: Ctx, action: str, amount: str) -> None:
    gate = await _one_gate(ctx, action)
    assert abs(float(gate.financial_impact_usd or 0) - float(amount)) < 0.01, f"amount is {gate.financial_impact_usd}"


async def _all_mail(ctx: Ctx) -> list[dict[str, str]]:
    from agents.negotiation_agent.email_history import get_email_repository
    drafts = await get_email_repository().list_by_deal(_deal_id(ctx))
    return ctx.emails + [{"to": e.to_address, "subject": e.subject, "body": e.body} for e in drafts if _value(e.direction) == "outbound"]


@step(r'an e-mail containing "([^"]+)" is composed for the "([^"]+)"')
async def mail_composed(ctx: Ctx, text: str, party: str) -> None:
    other = ctx.ids["supplier_mail" if party == "buyer" else "buyer_mail"]
    mails = await _all_mail(ctx)
    hit = [m for m in mails if m["to"] != other and (text.lower() in m["subject"].lower() or text.lower() in m["body"].lower())]
    assert hit, f"no e-mail with {text!r} to the {party}; subjects: {[m['subject'] for m in mails][:8]}"


@step(r'no e-mail containing "([^"]+)" is composed')
async def no_mail(ctx: Ctx, text: str) -> None:
    hit = [m for m in await _all_mail(ctx) if text.lower() in m["subject"].lower() or text.lower() in m["body"].lower()]
    assert not hit, f"unexpected e-mail: {hit[0]['subject']}"


async def _exceptions(ctx: Ctx) -> list[Any]:
    from services.exception_resolver.repository import get_repository
    return await get_repository().list_by_deal(_deal_id(ctx))


@step(r'an exception is recorded with "([^"]+)" equal to "([^"]+)"')
async def exception_with(ctx: Ctx, field: str, value: str) -> None:
    rows = await _exceptions(ctx)
    def field_of(row: Any) -> Any:
        return getattr(row, field, None) if getattr(row, field, None) is not None else (row.context or {}).get(field)
    assert any(_value(field_of(r)) == ctx.fill(value) for r in rows), f"exceptions: {[(_value(r.category), r.source_event) for r in rows]}"


@step(r"no exception is recorded")
async def no_exception(ctx: Ctx) -> None:
    rows = await _exceptions(ctx)
    assert not rows, f"unexpected exceptions: {[r.source_event for r in rows]}"


@step(r'the ledger field "([^"]+)" is "?([^"]*)"?')
async def ledger_field(ctx: Ctx, field: str, value: str) -> None:
    ledger = (await _ledger(ctx)).model_dump(mode="json")
    assert field in ledger, f"ledger has no {field}"
    assert _same(ledger[field], value), f"{field} is {ledger[field]}, expected {value}"


@step(r"the ledger is unchanged")
async def ledger_unchanged(ctx: Ctx) -> None:
    ledger = (await _ledger(ctx)).model_dump(mode="json")
    assert ledger == ctx.snapshot.get("ledger"), "the ledger changed"


@step(r'the escrow status is "([^"]+)"')
async def escrow_status(ctx: Ctx, status: str) -> None:
    hold = await _hold(ctx)
    assert hold.status == status, f"escrow is {hold.status}"


@step(r'tranche "([^"]+)" has status "([^"]+)"')
async def tranche_status(ctx: Ctx, tranche: str, status: str) -> None:
    hold = await _hold(ctx)
    found = next(t for t in hold.tranches if t.tranche_id == tranche)
    assert found.status == status, f"{tranche} is {found.status}"


@step(r"the escrow has an inspection deadline")
async def has_deadline(ctx: Ctx) -> None:
    hold = await _hold(ctx)
    assert hold.inspection_deadline is not None, "no inspection deadline"


async def _compliance(ctx: Ctx) -> Any:
    from shared.orchestration import compliance
    return await compliance.get_record(_deal_id(ctx))


@step(r'the compliance hold "([^"]+)" is open')
async def hold_open(ctx: Ctx, code: str) -> None:
    record = await _compliance(ctx)
    assert any(code in h for h in record.holds), f"holds: {record.holds}"


@step(r'the compliance hold "([^"]+)" is not open')
async def hold_closed(ctx: Ctx, code: str) -> None:
    record = await _compliance(ctx)
    assert not any(code in h for h in record.holds), f"holds: {record.holds}"


_COLLECTIONS = {
    "approvals": lambda ctx: _gates(ctx),
    "third_party_orders": lambda ctx: _orders(ctx),
    "exceptions": lambda ctx: _exceptions(ctx),
}


async def _orders(ctx: Ctx) -> list[Any]:
    from services.shield_link.repository import get_repository
    return await get_repository().list_by_deal(_deal_id(ctx))


@step(r'the collection "([^"]+)" has (\d+) records for the deal')
async def collection_count(ctx: Ctx, name: str, n: str) -> None:
    if name in _COLLECTIONS:
        rows = await _COLLECTIONS[name](ctx)
    elif name == "quotes":
        from services.sourcing.repository import get_repository
        rows = await get_repository().list_quotes(_deal_id(ctx))
    elif name == "inbound_emails":
        from services.email_processor.repository import get_repository
        rows = [r for r in await get_repository().list_all() if r.deal_id == _deal_id(ctx)]
    elif name == "price_observations":
        from shared import pricebook
        rows = [o for o in await pricebook.observations(fx.HS_CODE) if getattr(o, "deal_id", None) == _deal_id(ctx)]
    elif name == "escrow_holds":
        rows = [h for h in [await _hold(ctx)] if h is not None]
    elif name == "payouts":
        from services.escrow_guard.repository import get_repository
        rows = await get_repository().list_payouts_by_deal(_deal_id(ctx))
    elif name == "proforma_invoices":
        from services.dealmaker.repository import get_repository
        rows = [p for p in [await get_repository().get_pi_by_deal(_deal_id(ctx))] if p]
    elif name == "purchase_orders":
        from services.dealmaker.repository import get_repository
        rows = [p for p in [await get_repository().get_po_by_deal(_deal_id(ctx))] if p]
    elif name == "documents":
        from services.docugen.repository import get_repository
        rows = await get_repository().list_by_deal(_deal_id(ctx))
    else:
        raise NotAutomated(f"Collection not automated: {name}")
    assert len(rows) == int(n), f"{name}: {len(rows)} records, expected {n}"


@step(r'a "([^"]+)" order exists for the deal')
async def order_exists(ctx: Ctx, kind: str) -> None:
    orders = await _orders(ctx)
    assert any(kind in (o.order_type, o.status) for o in orders), f"orders: {[(o.order_type, o.status) for o in orders]}"


@step(r'the journal balance of "([^"]+)" is ([\d.-]+)')
async def journal_balance(ctx: Ctx, account: str, value: str) -> None:
    from services.escrow_guard.journal import build_journal
    journal = (await build_journal(_deal_id(ctx))).model_dump(mode="json")
    balances = journal.get("balances") or journal.get("trial_balance") or {}
    assert _same(balances.get(account), value), f"{account}: {balances.get(account)}; all: {balances}"


@step(r"the journal debits equal its credits")
async def journal_balanced(ctx: Ctx) -> None:
    from services.escrow_guard.journal import build_journal
    journal = (await build_journal(_deal_id(ctx))).model_dump(mode="json")
    assert journal.get("balanced", True), f"journal not balanced: {journal.get('issues')}"


from . import steps2, steps3, steps4, steps5, steps6  # noqa: E402,F401 — registers the remaining steps


def _assign_test_organisations() -> None:
    """The organisation personas belong to; PortLess records it when an admin assigns them."""
    from services.auth import service as auth
    real = auth.user_for_identity
    orgs = {"user@qa-org-a.invalid": "qa-org-a", "user@qa-org-b.invalid": "qa-org-b"}

    async def with_org(identity: Any) -> Any:
        record = await real(identity)
        org = orgs.get(identity.email)
        return record.model_copy(update={"org_id": org}) if org else record

    auth.user_for_identity = with_org  # type: ignore[assignment]


_assign_test_organisations()


async def reset_shared_settings() -> None:
    """Settings live in the database, so a scenario that changes them must not leave them for the next."""
    import os
    from shared import catalogue as product_catalogue
    from shared.orchestration import autonomy_policy, compliance
    from .store import database
    if os.environ.get("MONGO_URI"):
        db = database(os.environ["MONGO_URI"])
        for name in ("autonomy_policy", "compliance_hs_policy", "catalogue"):
            await db[name].delete_many({})
    autonomy_policy.reset_for_tests()
    compliance._mem_policy.clear()
    product_catalogue.reset_for_tests()
