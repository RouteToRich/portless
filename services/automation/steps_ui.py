"""Steps of the ``@ui`` cases — the console driven through Playwright (see browser.py).

Locators follow the catalogue's rule (TEST_CASES.md 3.5): a visible label, role or text — never a CSS
class. A step that needs the browser raises NotAutomated when the run has no browser runner.
"""
from __future__ import annotations

import re
from typing import Any

from .engine import Ctx, NotAutomated, step

_TIMEOUT = 8000


def _console(ctx: Ctx) -> Any:
    console = ctx.extra.get("ui")
    if console is None:
        raise NotAutomated("Runs in a browser: start the run with the browser runner (console bundle + Chromium)")
    return console


def ui(pattern: str) -> Any:
    """A step whose body drives the page: it runs on the browser's own thread / loop."""
    def deco(fn: Any) -> Any:
        @step(pattern)
        async def runner(ctx: Ctx, *args: str) -> None:
            console = _console(ctx)
            await console.run(fn(ctx, console, *args))
        runner.__name__ = fn.__name__
        return fn
    return deco


def _path(page: Any) -> str:
    from .browser import BASE
    url = page.url
    return url[len(BASE):].split("?")[0] or "/" if url.startswith(BASE) else url


async def _wait_path(page: Any, want: str, negate: bool = False) -> None:
    import asyncio
    want = want.rstrip("/") or "/"
    for _ in range(int(_TIMEOUT / 100)):
        now = _path(page).rstrip("/") or "/"
        if (now == want) != negate:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"on {_path(page)!r}, expected {'not ' if negate else ''}{want!r}")


async def _visible(locator: Any) -> bool:
    try:
        await locator.first.wait_for(state="visible", timeout=_TIMEOUT)
        return True
    except Exception:  # noqa: BLE001
        return False


def _text(ctx: Ctx, text: str) -> str:
    return ctx.fill(text)


async def _clickable(page: Any, name: str) -> Any:
    """The control a person would click for this label: inside an open dialog first, then link, button,
    tab, menu item, then plain text; of several equal buttons, the visible submit button."""
    dialog = page.get_by_role("dialog")
    if await dialog.count() and await dialog.first.is_visible():
        inside = dialog.first.get_by_role("button", name=name, exact=True)
        if await inside.count():
            return inside.first
    for role in ("link", "button", "tab", "menuitem", "checkbox", "switch"):
        loc = page.get_by_role(role, name=name, exact=True)
        n = await loc.count()
        if n > 1 and role == "button":
            for i in range(n):
                if await loc.nth(i).get_attribute("type") == "submit" and await loc.nth(i).is_visible():
                    return loc.nth(i)
        if n:
            return loc.first
    for role in ("link", "button", "tab"):
        loc = page.get_by_role(role, name=re.compile(re.escape(name), re.I))
        if await loc.count():
            return loc.first
    loc = page.get_by_text(name, exact=True)
    if await loc.count():
        return loc.first
    return page.get_by_text(name).first


# ── session ─────────────────────────────────────────────────────────────────

@step(r'the browser is signed in as "([^"]+)"')
async def signed_in(ctx: Ctx, persona: str) -> None:
    console = _console(ctx)
    ctx.persona = persona.replace("-", "_")
    await console.sign_in(ctx.persona)


@step(r"the token refresh is refused")
async def refresh_refused(ctx: Ctx) -> None:
    _console(ctx).auth["refresh"] = "refused"


@step(r"the token refresh fails with a network error")
async def refresh_network(ctx: Ctx) -> None:
    _console(ctx).auth["refresh"] = "network"


@step(r"the sign-in service refuses the password")
async def login_refused(ctx: Ctx) -> None:
    _console(ctx).auth["login"] = "refused"


# ── API answers replaced for one scenario ──────────────────────────────────

def _route_pattern(ctx: Ctx, call: str) -> tuple[str, str]:
    method, _, path = ctx.fill(call).partition(" ")
    pattern = re.escape(path.split("?")[0])
    pattern = re.sub(r"\\\{[^}]+\\\}", r"[^/]+", pattern)  # {id} placeholders
    return method.upper(), pattern


@step(r'the API answers (\d+) for "([^"]+)"')
async def api_answers(ctx: Ctx, status: str, call: str) -> None:
    method, pattern = _route_pattern(ctx, call)
    _console(ctx).mocks.append({"method": method, "path": pattern, "status": int(status),
                                "detail": "Service unavailable" if int(status) >= 500 else "automation"})


@step(r'the API answers (\d+) with detail "([^"]+)" for "([^"]+)"')
async def api_answers_detail(ctx: Ctx, status: str, detail: str, call: str) -> None:
    method, pattern = _route_pattern(ctx, call)
    _console(ctx).mocks.append({"method": method, "path": pattern, "status": int(status), "detail": detail})


@step(r'the API answers 200 with an empty list for "([^"]+)"')
async def api_empty(ctx: Ctx, call: str) -> None:
    method, pattern = _route_pattern(ctx, call)
    _console(ctx).mocks.append({"method": method, "path": pattern, "status": 200, "body": []})


@step(r'the API is slow for "([^"]+)"')
async def api_slow(ctx: Ctx, call: str) -> None:
    method, pattern = _route_pattern(ctx, call)
    _console(ctx).mocks.append({"method": method, "path": pattern, "delay": 4.0})


# ── navigation and reading ──────────────────────────────────────────────────

@ui(r'I open "([^"]+)"')
async def open_page(ctx: Ctx, console: Any, route: str) -> None:
    # A later "the API answers …" replaces an earlier one for the same call (TC-UI-020 changes it per step).
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for m in console.mocks:
        latest[(m["method"], m["path"])] = m
    console.mocks = list(latest.values())
    await console.page.goto(_text(ctx, route), wait_until="domcontentloaded")
    if any(m.get("delay") for m in console.mocks):
        return  # a slow API on purpose: the loading state is what the next step reads
    try:
        await console.page.wait_for_load_state("networkidle", timeout=4000)
    except Exception:  # noqa: BLE001
        pass


@ui(r'I am on "([^"]+)"')
async def am_on(ctx: Ctx, console: Any, route: str) -> None:
    await _wait_path(console.page, _text(ctx, route))


@ui(r'I am not on "([^"]+)"')
async def not_on(ctx: Ctx, console: Any, route: str) -> None:
    import asyncio
    await asyncio.sleep(1.5)
    await _wait_path(console.page, _text(ctx, route), negate=True)


@ui(r'I see "([^"]+)"')
async def see(ctx: Ctx, console: Any, text: str) -> None:
    page = console.page
    assert await _visible(page.get_by_text(_text(ctx, text))), f"{text!r} is not shown on {_path(page)}"


@ui(r'I do not see "([^"]+)"')
async def not_see(ctx: Ctx, console: Any, text: str) -> None:
    page = console.page
    loc = page.get_by_text(_text(ctx, text), exact=True)
    try:
        await page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:  # noqa: BLE001
        pass
    assert not await loc.count() or not await loc.first.is_visible(), f"{text!r} is shown on {_path(page)}"


@ui(r'I see deal "([^"]+)"')
async def see_deal(ctx: Ctx, console: Any, alias: str) -> None:
    await see(ctx, console, str(ctx.ids.get(alias, alias)))


@ui(r'I do not see deal "([^"]+)"')
async def not_see_deal(ctx: Ctx, console: Any, alias: str) -> None:
    await not_see(ctx, console, str(ctx.ids.get(alias, alias)))


# ── acting ──────────────────────────────────────────────────────────────────

@ui(r'I click "([^"]+)"')
async def click(ctx: Ctx, console: Any, name: str) -> None:
    if not console.page.url.startswith("http"):
        await open_page(ctx, console, "/")
    target = None
    scope = console.__dict__.get("scope_text")
    if scope:  # a gate card was opened: its own button, not another deal's
        box = console.page.locator("div").filter(has_text=scope).filter(
            has=console.page.get_by_role("button", name=_text(ctx, name), exact=True))
        if await box.count():
            target = box.last.get_by_role("button", name=_text(ctx, name), exact=True).first
    if target is None:
        target = await _clickable(console.page, _text(ctx, name))
    await target.click(timeout=_TIMEOUT)
    try:
        await console.page.wait_for_load_state("networkidle", timeout=4000)
    except Exception:  # noqa: BLE001
        pass


@ui(r'I open the tab "([^"]+)"')
async def open_tab(ctx: Ctx, console: Any, name: str) -> None:
    page = console.page
    tab = page.get_by_role("tab", name=re.compile(rf"^{re.escape(name)}", re.I))
    if not await tab.count():
        tab = await _clickable(page, name)
    await tab.first.click(timeout=_TIMEOUT)


@ui(r'the tab "([^"]+)" is selected')
async def tab_selected(ctx: Ctx, console: Any, name: str) -> None:
    page = console.page
    tab = page.get_by_role("tab", name=re.compile(rf"^{re.escape(name)}", re.I)).first
    assert await tab.get_attribute("aria-selected") == "true" or await tab.get_attribute("data-state") == "active", \
        f"tab {name!r} is not selected"


@ui(r'I fill "([^"]+)" with "([^"]*)"')
async def fill(ctx: Ctx, console: Any, label: str, value: str) -> None:
    page = console.page
    for loc in (page.get_by_label(label, exact=False), page.get_by_placeholder(label), page.get_by_role("textbox", name=label)):
        if await loc.count():
            await loc.first.fill(_text(ctx, value), timeout=_TIMEOUT)
            return
    raise AssertionError(f"no field labelled {label!r} on {_path(page)}")


@ui(r'I confirm "([^"]+)" with "([^"]*)"')
async def confirm_with(ctx: Ctx, console: Any, opener: str, confirm: str) -> None:
    """Open the action's dialog with its button, give a reason when the dialog asks for one, confirm."""
    page = console.page
    await (await _clickable(page, _text(ctx, opener))).click(timeout=_TIMEOUT)
    dialog = page.get_by_role("dialog")
    await dialog.first.wait_for(state="visible", timeout=_TIMEOUT)
    box = dialog.first.get_by_role("textbox")
    if await box.count():
        await box.first.fill("automation: relayed by the operator", timeout=_TIMEOUT)
    await dialog.first.get_by_role("button", name=_text(ctx, confirm), exact=True).first.click(timeout=_TIMEOUT)


@ui(r'the button "([^"]+)" is disabled')
async def button_disabled(ctx: Ctx, console: Any, name: str) -> None:
    btn = console.page.get_by_role("button", name=re.compile(re.escape(name), re.I)).first
    assert await _visible(btn), f"no button {name!r}"
    assert await btn.is_disabled(), f"button {name!r} is enabled"


@ui(r'the button "([^"]+)" is (enabled|disabled|hidden|shown)')
async def button_state(ctx: Ctx, console: Any, name: str, state: str) -> None:
    page = console.page
    btn = page.get_by_role("button", name=re.compile(re.escape(name), re.I))
    if state == "hidden":
        assert not await btn.count() or not await btn.first.is_visible(), f"button {name!r} is shown"
        return
    assert await _visible(btn), f"no button {name!r}"
    if state == "enabled":
        assert await btn.first.is_enabled(), f"button {name!r} is disabled"
    elif state == "disabled":
        assert await btn.first.is_disabled(), f"button {name!r} is enabled"


# ── feedback ────────────────────────────────────────────────────────────────

def _toasts(page: Any) -> Any:
    return page.locator("[data-sonner-toast]")


@ui(r'a toast says "([^"]+)"')
async def toast_says(ctx: Ctx, console: Any, text: str) -> None:
    page = console.page
    loc = _toasts(page).filter(has_text=re.compile(re.escape(_text(ctx, text)), re.I))
    assert await _visible(loc), f"no toast saying {text!r}; toasts: {await _toasts(page).all_inner_texts()}"


@ui(r"a toast is shown")
async def toast_shown(ctx: Ctx, console: Any) -> None:
    assert await _visible(_toasts(console.page)), "no toast"


@ui(r"a toast shows the backend detail")
async def toast_detail(ctx: Ctx, console: Any) -> None:
    details = [m.get("detail") for m in console.mocks if m.get("detail") and m.get("detail") != "automation"]
    texts = " ".join(await _toasts(console.page).all_inner_texts()) if await _visible(_toasts(console.page)) else ""
    assert any(d and d.lower() in texts.lower() for d in details) or (texts and not details), f"toasts: {texts!r}"


@ui(r"an error is shown")
async def error_shown(ctx: Ctx, console: Any) -> None:
    page = console.page
    candidates = [_toasts(page), page.get_by_role("alert"), page.get_by_text(re.compile(r"invalid|error|failed|refused|wrong", re.I))]
    for loc in candidates:
        if await _visible(loc):
            return
    raise AssertionError(f"no error shown on {_path(page)}")


@ui(r"an error state is shown")
async def error_state(ctx: Ctx, console: Any) -> None:
    page = console.page
    assert await _visible(page.get_by_text(re.compile(r"Service unavailable|error|failed|no longer exists|not found", re.I))), "no error state"


@ui(r"a CSV file is saved")
async def csv_saved(ctx: Ctx, console: Any) -> None:
    import asyncio
    for _ in range(50):
        if console.downloads:
            break
        await asyncio.sleep(0.1)
    assert console.downloads, "no file was downloaded"
    assert console.downloads[-1].suggested_filename.lower().endswith(".csv"), console.downloads[-1].suggested_filename


# ── fixtures and checks for the deal / radar pages ─────────────────────────

_ESCROW_STATES = {
    "funded, window not open": " with T1, T2 and T3 released",
    "funded, window open": " with the window open",
    "funded": " with T1, T2 and T3 released",
    "disputed": " with a disputed escrow",
    "not disputed": " with T1, T2 and T3 released",
    "fully released": " fully settled",
}


@step(r'a deal "([^"]+)" with an escrow in state "([^"]+)"')
async def deal_escrow_state(ctx: Ctx, alias: str, state: str) -> None:
    from .steps import deal_at_status
    if state not in _ESCROW_STATES:
        raise NotAutomated(f"Escrow state not automated: {state}")
    await deal_at_status(ctx, alias, "CLEARED", "FX-STD", _ESCROW_STATES[state])


@ui(r'the button "([^"]+)" is not shown')
async def button_not_shown(ctx: Ctx, console: Any, name: str) -> None:
    btn = console.page.get_by_role("button", name=re.compile(re.escape(name), re.I))
    assert not await btn.count() or not await btn.first.is_visible(), f"button {name!r} is shown"


@ui(r'I click "([^"]+)" with an empty reason')
async def click_empty_reason(ctx: Ctx, console: Any, name: str) -> None:
    await (await _clickable(console.page, _text(ctx, name))).click(timeout=_TIMEOUT)


@ui(r'I raise a dispute with reason "([^"]+)"')
async def raise_dispute(ctx: Ctx, console: Any, reason: str) -> None:
    page = console.page
    await (await _clickable(page, "Raise dispute")).click(timeout=_TIMEOUT)
    dialog = page.get_by_role("dialog")
    await dialog.get_by_role("textbox").first.fill(_text(ctx, reason), timeout=_TIMEOUT)
    await dialog.get_by_role("button", name="Raise dispute", exact=True).first.click(timeout=_TIMEOUT)
    try:
        await page.wait_for_load_state("networkidle", timeout=4000)
    except Exception:  # noqa: BLE001
        pass


@step(r"the request's actor is the signed-in user's e-mail")
async def actor_is_user(ctx: Ctx) -> None:
    import json as _json
    from .engine import PERSONAS
    console = _console(ctx)
    email = (PERSONAS[ctx.persona] or {}).get("X-User-Email")
    sent = [b for m, p, b in console.bodies if "/dispute" in p or "/request-" in p or "/accept" in p]
    assert sent, f"no escrow action was sent; calls: {console.calls[-6:]}"
    body = _json.loads(sent[-1] or "{}")
    actor = body.get("raised_by") or body.get("accepted_by") or body.get("requested_by") or body.get("actor")
    assert actor == email, f"actor {actor!r}, signed in as {email!r}"


@step(r"an opportunity exists")
async def opportunity_exists(ctx: Ctx) -> None:
    from datetime import datetime, timedelta, timezone
    from services.trade_min.repository import get_repository
    from services.trade_min.schemas import BuyerSignal, EnrichedOpportunity, SellerCandidate
    from services.trade_min.service import _new_opportunity_id
    from . import seed as fx
    now = datetime.now(timezone.utc)
    uid = ctx.ids.get("uid", "qa")
    opp = EnrichedOpportunity(
        opportunity_id=_new_opportunity_id(), hs_code=fx.HS_CODE, market=fx.MARKET, status="ACTIVE", scanned_at=now,
        demand_index=60.0, price_gap_pct=25.0, source="apify_company_search", net_price_gap_pct=18.0, tariff_viable=True,
        arbitrage_rank=70.0, confidence_score=60.0, expires_at=now + timedelta(days=3),
        buyer_signals=[BuyerSignal(buyer_name=f"Radar Buyer {uid}", buyer_country=fx.MARKET, signal_type="web_search",
                                   source="apify_company_search", urgency_tier=1, email=f"buy@radar-{uid}.invalid",
                                   unit_price_usd=1250.0, price_unit="USD/MT")],
        seller_candidates=[SellerCandidate(candidate_id=f"radar-sup-{uid}", company_name=f"Radar Mill {uid}", location_city="Kakinada",
                                           location_state="Andhra Pradesh", hs_code=fx.HS_CODE, source="apify_company_search",
                                           estimated_fob_price_usd=1000.0, price_unit="USD/MT", email=f"sales@radar-mill-{uid}.invalid")],
    )
    await (await get_repository()).save(opp)
    ctx.ids["OPP"] = opp.opportunity_id


@step(r'a deal exists at "([^"]+)" for the opportunity')
async def deal_for_opportunity(ctx: Ctx, status: str) -> None:
    from services.deals.repository import get_repository
    deals = await get_repository().list_active(limit=5000)
    hit = [d for d in deals if getattr(d, "opportunity_id", None) == ctx.ids.get("OPP")]
    assert hit and str(getattr(hit[-1].status, "value", hit[-1].status)) == status, \
        f"deals for {ctx.ids.get('OPP')}: {[(d.deal_id, str(d.status)) for d in hit]}"
    ctx.ids["D"] = hit[-1].deal_id


@step(r"no prospects exist")
async def no_prospects(ctx: Ctx) -> None:
    console = _console(ctx)
    for path in (r"/deals", r"/deals/prospects/desk"):
        console.mocks.append({"method": "GET", "path": re.escape(path), "status": 200, "body": []})


@ui(r'the deal "([^"]+)" is not a link')
async def deal_not_link(ctx: Ctx, console: Any, alias: str) -> None:
    deal_id = str(ctx.ids.get(alias, alias))
    cell = console.page.get_by_text(deal_id).first
    assert await _visible(cell), f"{deal_id} is not shown"
    inside_link = await cell.evaluate("el => !!el.closest('a, [role=link]')")
    assert not inside_link, f"{deal_id} is inside a link"


@ui(r"I download the evidence pack")
async def download_pack(ctx: Ctx, console: Any) -> None:
    await console.page.get_by_role("button", name=re.compile(r"^\s*JSON\s*$")).first.click(timeout=_TIMEOUT)


@ui(r'I open the gate of deal "([^"]+)"')
async def open_gate_of_deal(ctx: Ctx, console: Any, alias: str) -> None:
    """Approvals Inbox: expand the (first) card of this deal — the run has other deals' gates too."""
    deal_id = str(ctx.ids.get(alias, alias))
    card = console.page.get_by_text(deal_id).first
    assert await _visible(card), f"no gate of {deal_id} is listed"
    await card.click(timeout=_TIMEOUT)
    console.scope_text = deal_id
