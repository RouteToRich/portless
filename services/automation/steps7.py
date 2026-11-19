"""Steps for the 2026-11-19 scenarios: generic response checks, actor rows, the exception playbook
variants, LLM failures, and the trade-operations / radar-source features (catalogue phases 14–15)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .engine import Ctx, NotAutomated, step
from .steps import _deal_id, _path, _same


# ── generic response checks ────────────────────────────────────────────────

@step(r'I remember the response field "([^"]+)" as "([^"]+)"')
async def remember(ctx: Ctx, dotted: str, alias: str) -> None:
    assert ctx.response is not None, "no response yet"
    ctx.ids[alias] = _path(ctx.response.json(), dotted)


@step(r'the response list "([^"]+)" has (\d+) items?')
async def list_size(ctx: Ctx, dotted: str, n: str) -> None:
    assert ctx.response is not None
    data = ctx.response.json()
    items = data if dotted == "." else _path(data, dotted)
    assert isinstance(items, list) and len(items) == int(n), f"{dotted}: {items!r}"


@step(r'the response list "([^"]+)" has at least (\d+) items?')
async def list_at_least(ctx: Ctx, dotted: str, n: str) -> None:
    assert ctx.response is not None
    data = ctx.response.json()
    items = data if dotted == "." else _path(data, dotted)
    assert isinstance(items, list) and len(items) >= int(n), f"{dotted}: {items!r}"


@step(r'the response field "([^"]+)" contains "([^"]*)"')
async def field_contains(ctx: Ctx, dotted: str, text: str) -> None:
    assert ctx.response is not None
    value = _path(ctx.response.json(), dotted)
    wanted = ctx.fill(text)
    if isinstance(value, list):
        assert any(wanted.lower() in json.dumps(v, default=str).lower() for v in value), f"{dotted}: {value!r}"
    else:
        assert wanted.lower() in str(value).lower(), f"{dotted}: {value!r}"


@step(r'the response field "([^"]+)" does not contain "([^"]*)"')
async def field_not_contains(ctx: Ctx, dotted: str, text: str) -> None:
    assert ctx.response is not None
    value = _path(ctx.response.json(), dotted)
    assert ctx.fill(text).lower() not in json.dumps(value, default=str).lower(), f"{dotted}: {value!r}"


@step(r'the response field "([^"]+)" is at least "([\d.]+)"')
async def field_at_least(ctx: Ctx, dotted: str, n: str) -> None:
    assert ctx.response is not None
    value = _path(ctx.response.json(), dotted)
    assert value is not None and float(value) >= float(n), f"{dotted}: {value!r}"


@step(r'the response field "([^"]+)" is empty')
async def field_empty(ctx: Ctx, dotted: str) -> None:
    assert ctx.response is not None
    value = _path(ctx.response.json(), dotted)
    assert value in (None, "", [], {}), f"{dotted}: {value!r}"


@step(r'the deal field "([^"]+)" is "([^"]*)"')
async def deal_field(ctx: Ctx, dotted: str, expected: str) -> None:
    from services.deals.repository import get_repository
    deal = await get_repository().get(_deal_id(ctx))
    value = _path(deal.model_dump(mode="json"), dotted)
    assert _same(value, ctx.fill(expected)), f"{dotted}: {value!r}"


# ── actor rows and connectors ──────────────────────────────────────────────

@step(r'the actor "([^"]+)" returns:')
async def actor_returns(ctx: Ctx, name: str) -> None:
    """The named connector answers the doc string (JSON) — every other connector is untouched."""
    from shared.connectors import registry
    answer = json.loads(ctx.fill(ctx.doc or "{}"))
    real = ctx.extra.setdefault("real_get_connector", registry.get_connector)
    fakes: dict[str, Any] = ctx.extra.setdefault("fake_connectors", {})

    class _Fake:
        configured = True

        async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
            fakes.setdefault(f"{name}:calls", []).append(request)
            return answer

    fakes[name] = _Fake()
    ctx.patch(registry, "get_connector", lambda n: fakes[n] if n in fakes else real(n))


# ── exceptions: playbook variants and LLM failures ─────────────────────────

_VARIANTS: dict[str, dict[str, Any]] = {
    "first failure": {},
    "failures beyond the limit": {"_failed": 5},
    "shortfall just now": {"shortfall": 500, "action_required": "fund_shortfall"},
    "unfunded past grace": {"_since_days": 30},
    "candidates remaining": {},
    "funnel exhausted": {"action_required": "find_new_buyers_or_cancel"},
    "a render failure": {"error": "template render failed"},
    "a provider timeout": {"error": "provider timeout"},
}


@step(r'an exception of category "([^"]+)" with (.+) is raised')
async def exception_variant(ctx: Ctx, category: str, variant: str) -> None:
    spec = dict(_VARIANTS.get(variant.strip(), {}))
    if variant.strip() not in _VARIANTS:
        raise NotAutomated(f"Exception context not automated: {variant}")
    if "_failed" in spec:
        from shared.orchestration import playbooks
        count = int(spec.pop("_failed"))

        async def failed(_deal_id: str) -> int:
            return count
        ctx.patch(playbooks, "failed_inspections", failed)
    if "_since_days" in spec:
        spec["since"] = (datetime.now(timezone.utc) - timedelta(days=spec.pop("_since_days"))).isoformat()
    await ctx.call("POST", "/exception-resolver/exceptions", json_body={
        "deal_id": _deal_id(ctx), "source_event": "automation", "source_service": "automation",
        "detail": spec.get("error", "automation"), "context": {"category": category, **spec}})
    if ctx.response is not None and ctx.response.status_code < 300:
        ctx.ids["EXC"] = ctx.response.json().get("exception_id", "")


@step(r"the LLM gateway has no model left")
async def llm_exhausted(ctx: Ctx) -> None:
    from shared.llm_gateway import LLMClient, LLMExhaustedException

    async def exhausted(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise LLMExhaustedException("automation: every model failed")
    ctx.patch(LLMClient, "chat", exhausted)


@step(r"the LLM gateway returns invalid JSON")
async def llm_bad_json(ctx: Ctx) -> None:
    from shared.llm_gateway import LLMClient
    from shared.llm_gateway.client import LLMResponse

    async def garbage(self: Any, *args: Any, **kwargs: Any) -> Any:
        return LLMResponse(content="this is not json {", model="qa", prompt_tokens=1, completion_tokens=1,
                           finish_reason="stop", raw={})
    ctx.patch(LLMClient, "chat", garbage)
    from services.exception_resolver import agent
    ctx.patch(agent, "_playbooks_enabled", lambda: False)


@step(r"the resolver agent will crash")
async def resolver_crash(ctx: Ctx) -> None:
    from services.exception_resolver import service

    async def crash(_record: Any) -> Any:
        raise RuntimeError("automation: resolver crashed")
    ctx.patch(service, "run_agent", crash)


# ── data jobs (turned on for one scenario) ─────────────────────────────────

@step(r'the data job "([^"]+)" runs')
async def data_job(ctx: Ctx, name: str) -> None:
    """Run one daily / weekly / monthly data job now (its switch on for this scenario)."""
    import importlib
    jobs = {
        "country feeds": ("shared.country_feeds", "run", "COUNTRY_FEEDS_ENABLED"),
        "destination rules": ("shared.orchestration.destination_rules", "monthly", "DESTINATION_RULES_ENABLED"),
        "refusals": ("shared.orchestration.refusals", "daily", "REFUSALS_ENABLED"),
        "registration checks": ("shared.orchestration.registration_checks", "check_own", "REGISTRATION_CHECKS_ENABLED"),
        "demand leads": ("shared.orchestration.demand_leads", "run_actors", "DEMAND_LEADS_ENABLED"),
        "india exports": ("services.trade_min.india_exports", "monthly", "INDIA_EXPORTS_ENABLED"),
        "directory sources": ("services.trade_min.directory_sources", "weekly", "DIRECTORY_SOURCES_ENABLED"),
        "freight procurement": ("shared.orchestration.freight_procurement", "sweep", "FREIGHT_PROCUREMENT_ENABLED"),
        "regulation watch": ("shared.orchestration.regulatory_watch", "run_actor", "REGULATORY_WATCH_ENABLED"),
    }
    if name not in jobs:
        raise NotAutomated(f"Data job not automated: {name}")
    module, fn, switch = jobs[name]
    ctx.set_env(switch, "true")
    mod = importlib.import_module(module)
    if hasattr(mod, "_last"):
        mod._last.clear()  # the job's once-a-day / week / month memory
    result = await getattr(mod, fn)()
    ctx.extra["job_result"] = result


@step(r'the data job result field "([^"]+)" is "([^"]*)"')
async def job_field(ctx: Ctx, dotted: str, expected: str) -> None:
    value = _path(ctx.extra.get("job_result") or {}, dotted)
    assert _same(value if not isinstance(value, list) else len(value), ctx.fill(expected)), f"{dotted}: {value!r}"


# ── free-form inbound e-mail ───────────────────────────────────────────────

@step(r'an e-mail from "([^"]+)" arrives with subject "([^"]+)" and body:')
async def mail_with_body(ctx: Ctx, sender: str, subject: str) -> None:
    from .steps import _reply
    await _reply(ctx, ctx.fill(sender), ctx.fill(subject), ctx.fill(ctx.doc or ""))


@step(r'the e-mail was processed as "([^"]+)"')
async def processed_as(ctx: Ctx, status: str) -> None:
    result = ctx.extra.get("inbound")
    assert result is not None, "no inbound e-mail"
    got = str(getattr(getattr(result, "status", None), "value", getattr(result, "status", "")))
    assert got == status, f"{got}: {getattr(result, 'message', '')}"


@step(r'the e-mail processing message contains "([^"]+)"')
async def processed_message(ctx: Ctx, text: str) -> None:
    result = ctx.extra.get("inbound")
    assert result is not None and text.lower() in str(getattr(result, "message", "")).lower(), getattr(result, "message", None)


@step(r'the connector "counterparty_check" answers verdict "([^"]+)"')
async def screening_verdict(ctx: Ctx, verdict: str) -> None:
    """The sanctions screening actor answers this verdict for every party (no paid run)."""
    from shared.connectors import apify

    async def answer(self: Any, request: dict[str, Any]) -> dict[str, Any]:
        rows = [{"name": p.get("name"), "country": p.get("country"), "verdict": verdict,
                 "reason": f"automation: screening {verdict}", "hits": [], "lists_checked": {}}
                for p in request.get("counterparties") or []]
        return {"results": rows, "total": len(rows)}
    ctx.patch(apify.ApifyCounterpartyCheckConnector, "quote", answer)
    ctx.patch(apify.ApifyCounterpartyCheckConnector, "configured", property(lambda self: True))


@step(r'the stage gate "([^"]+)" is open')
async def stage_gate_open(ctx: Ctx, action: str) -> None:
    """The gate the seeded stage opens, created through the real continuation (with its real payload)."""
    from .steps import _gates, natural_gate
    await natural_gate(ctx, action)
    assert await _gates(ctx, action, "PENDING"), f"no pending {action} after the stage's continuation ran"
