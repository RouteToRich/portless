"""Daily operator digest — one message a day with everything that needs a
human: pending gates (and how old they are), deals per stage, stalls, open
exceptions, money in escrow / released / owed to suppliers, Apify spend.

Delivered on Telegram (``HITL_TELEGRAM_*``) and/or e-mail (``DIGEST_EMAIL_TO``
through the e-mail gateway) at ``DIGEST_HOUR_UTC`` (default 06:00 UTC) by
``run_digest_scheduler`` (app lifespan). ``GET /hitl-approval/digest`` previews
it; ``POST /hitl-approval/digest/send`` sends it now.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_OPEN_EXCEPTIONS = ("OPEN", "ANALYZING", "REMEDIATING", "ESCALATED", "UNRESOLVABLE")


def enabled() -> bool:
    return os.getenv("DAILY_DIGEST_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def digest_hour_utc() -> tuple[int, int]:
    raw = os.getenv("DIGEST_HOUR_UTC", "6").strip() or "6"
    try:
        if ":" in raw:
            h, m = raw.split(":", 1)
            return max(0, min(23, int(h))), max(0, min(59, int(m)))
        return max(0, min(23, int(raw))), 0
    except ValueError:
        return 6, 0


def email_recipients() -> list[str]:
    raw = os.getenv("DIGEST_EMAIL_TO", "")
    return [a.strip() for a in raw.split(",") if a.strip()]


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hours_since(dt: datetime | None, now: datetime) -> float | None:
    dt = _aware(dt)
    return round((now - dt).total_seconds() / 3600.0, 1) if dt else None


async def build_digest(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {"generated_at": now.isoformat(), "approvals": {}, "deals": {}, "exceptions": {},
                           "money": {}, "apify": {}, "warnings": []}

    # ── Pending gates ──────────────────────────────────────────────────────
    try:
        from services.hitl_approval.repository import get_repository as _hitl_repo  # type: ignore[import]
        pending = await _hitl_repo().list_by_status("PENDING")
        by_action: dict[str, dict[str, Any]] = {}
        for a in pending:
            row = by_action.setdefault(a.action, {"count": 0, "oldest_hours": 0.0, "money_usd": 0.0, "countersign": 0})
            row["count"] += 1
            row["oldest_hours"] = max(row["oldest_hours"], _hours_since(a.created_at, now) or 0.0)
            row["money_usd"] = round(row["money_usd"] + float(a.financial_impact_usd or 0.0), 2)
            if getattr(a, "required_approvals", 1) > 1 and getattr(a, "approvals", None):
                row["countersign"] += 1
        out["approvals"] = {
            "pending_total": len(pending),
            "by_action": dict(sorted(by_action.items(), key=lambda kv: -kv[1]["count"])),
            "oldest_hours": max([_hours_since(a.created_at, now) or 0.0 for a in pending], default=0.0),
            "items": [
                {"approval_id": a.approval_id, "deal_id": a.deal_id, "action": a.action,
                 "age_hours": _hours_since(a.created_at, now), "financial_impact_usd": a.financial_impact_usd,
                 "deep_link": a.deep_link}
                for a in sorted(pending, key=lambda a: _aware(a.created_at) or now)[:20]
            ],
        }
    except Exception as exc:  # noqa: BLE001
        out["warnings"].append(f"approvals unavailable: {exc}")

    # ── Deals ──────────────────────────────────────────────────────────────
    try:
        from services.deals.repository import get_repository as _deals_repo  # type: ignore[import]
        from shared.orchestration import outreach
        deals = await _deals_repo().list_active(limit=1000)
        sla = float(os.getenv("DEAL_STALL_SLA_HOURS", "168") or 168)
        by_status: dict[str, int] = {}
        stalled: list[dict[str, Any]] = []
        waiting_buyer = 0
        for d in deals:
            status = d.status.value if hasattr(d.status, "value") else str(d.status)
            by_status[status] = by_status.get(status, 0) + 1
            age = _hours_since(d.updated_at, now)
            if status not in ("PAID", "CANCELLED", "REFUNDED", "NEGOTIATION_FAILED", "DRAFT") and age and age >= sla:
                stalled.append({"deal_id": d.deal_id, "status": status, "hours": age})
            if status in ("MATCHING", "NEGOTIATING") and outreach.waiting_for_buyer(d):
                waiting_buyer += 1
        out["deals"] = {"active": len(deals), "by_status": dict(sorted(by_status.items())),
                        "stalled": sorted(stalled, key=lambda s: -s["hours"])[:20], "waiting_for_buyer": waiting_buyer}
    except Exception as exc:  # noqa: BLE001
        out["warnings"].append(f"deals unavailable: {exc}")

    # ── Prospect desk: our move and stalled, with the next move ────────────
    try:
        from services.deals.repository import get_repository as _deals_repo  # type: ignore[import]
        from shared.orchestration import prospect_desk
        rows = await prospect_desk.desk(await _deals_repo().list_active(limit=1000))
        out["prospects"] = _desk_digest(rows)
    except Exception as exc:  # noqa: BLE001
        out["warnings"].append(f"prospect desk unavailable: {exc}")

    # ── Supplier price moves (price book) ──────────────────────────────────
    try:
        from shared.orchestration import price_alerts
        out["price_alerts"] = await price_alerts.alerts(now)
    except Exception as exc:  # noqa: BLE001
        out["warnings"].append(f"price alerts unavailable: {exc}")

    # ── Export proceeds not yet realised (RBI time limit) ──────────────────
    try:
        from shared.orchestration import realisation
        out["realisation"] = [r for r in await realisation.open_items() if r["status"] in ("overdue", "due_soon")]
    except Exception as exc:  # noqa: BLE001
        out["warnings"].append(f"realisation unavailable: {exc}")

    # ── Buyers whose usual next order is near ──────────────────────────────
    try:
        from shared.orchestration import reorders
        out["reorders_due"] = await reorders.due(now)
    except Exception as exc:  # noqa: BLE001
        out["warnings"].append(f"reorders unavailable: {exc}")

    # ── Company follow-ups that are due ────────────────────────────────────
    try:
        from services.trade_min import company_notes  # type: ignore[import]
        out["followups"] = await company_notes.due(now)
    except Exception as exc:  # noqa: BLE001
        out["warnings"].append(f"company follow-ups unavailable: {exc}")

    # ── Exceptions ─────────────────────────────────────────────────────────
    try:
        from services.exception_resolver.repository import get_repository as _er_repo  # type: ignore[import]
        from services.exception_resolver.schemas import ExceptionStatus  # type: ignore[import]
        counts: dict[str, int] = {}
        items: list[dict[str, Any]] = []
        for st in _OPEN_EXCEPTIONS:
            try:
                recs = await _er_repo().list_by_status(ExceptionStatus(st))
            except Exception:  # noqa: BLE001
                recs = []
            if recs:
                counts[st] = len(recs)
            items += [{"exception_id": r.exception_id, "deal_id": r.deal_id, "status": st,
                       "category": r.category.value if r.category else None,
                       "age_hours": _hours_since(r.raised_at, now)} for r in recs]
        out["exceptions"] = {"open_total": sum(counts.values()), "by_status": counts,
                             "items": sorted(items, key=lambda i: -(i["age_hours"] or 0))[:20]}
    except Exception as exc:  # noqa: BLE001
        out["warnings"].append(f"exceptions unavailable: {exc}")

    # ── Money ──────────────────────────────────────────────────────────────
    try:
        from services.escrow_guard.service import list_deal_ledgers  # type: ignore[import]
        ledgers = await list_deal_ledgers()
        money = {"escrows": len(ledgers), "in_escrow": 0.0, "released_total": 0.0, "net_to_portless": 0.0,
                 "payouts_total": 0.0, "supplier_balance_due": 0.0, "unreconciled": [], "disputed": 0,
                 "windows_open": 0, "currency": ledgers[0].currency if ledgers else "USD"}
        for l in ledgers:
            money["in_escrow"] = round(money["in_escrow"] + l.outstanding, 2)
            money["released_total"] = round(money["released_total"] + l.released_total, 2)
            money["net_to_portless"] = round(money["net_to_portless"] + l.net_to_portless, 2)
            money["payouts_total"] = round(money["payouts_total"] + l.payouts_total, 2)
            money["supplier_balance_due"] = round(money["supplier_balance_due"] + (l.supplier_balance_due or 0.0), 2)
            if not l.reconciled:
                money["unreconciled"].append(l.deal_id)
            money["disputed"] += int(bool(l.disputed))
            money["windows_open"] += int(l.inspection_deadline is not None and not l.settled)
        out["money"] = money
    except Exception as exc:  # noqa: BLE001
        out["warnings"].append(f"money unavailable: {exc}")

    # ── Fees above what the price assumed ──────────────────────────────────
    try:
        from shared.orchestration import fee_check
        out["fees_over"] = await fee_check.over_all()
        from shared.orchestration import incentive_credits
        out["incentives_short"] = await incentive_credits.short_all()
    except Exception as exc:  # noqa: BLE001
        out["warnings"].append(f"fee check unavailable: {exc}")

    # ── Background watchers still beating? ─────────────────────────────────
    try:
        from shared import heartbeat
        out["watchers"] = await heartbeat.status(now)
    except Exception as exc:  # noqa: BLE001
        out["warnings"].append(f"watcher heartbeats unavailable: {exc}")

    # ── Apify spend ────────────────────────────────────────────────────────
    try:
        from shared.connectors.apify import usage_today
        out["apify"] = await usage_today()
        try:
            from services.trade_min import validation  # type: ignore[import]
            out["apify"]["validation_runs_today"] = validation.runs_today()
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass

    # ── Regulatory: registrations lapsing, HS codes to re-confirm, new notices ──
    try:
        from shared import registrations
        out["registrations_due"] = await registrations.expiring()
    except Exception as exc:  # noqa: BLE001
        out["warnings"].append(f"registrations unavailable: {exc}")
    try:
        from shared.orchestration import compliance
        out["hs_policy_due"] = await compliance.hs_policy_due()
    except Exception as exc:  # noqa: BLE001
        out["warnings"].append(f"HS policy list unavailable: {exc}")
    try:
        from shared.orchestration import regulatory_watch
        out["regulatory_hits"] = await regulatory_watch.recent_hits(now)
    except Exception as exc:  # noqa: BLE001
        out["warnings"].append(f"regulatory watch unavailable: {exc}")
    return out


DESK_ITEMS = 8


def _desk_row(r: Any) -> dict[str, Any]:
    first = r.actions[0] if r.actions else None
    return {
        "deal_id": r.deal_id, "hs_code": r.hs_code, "market": r.market, "idle_days": r.idle_days,
        "waiting_on": r.waiting_on, "waiting_detail": r.waiting_detail, "priority": r.priority,
        "expected_value_usd": r.expected_value_usd,
        "next_move": first.text if first else None, "next_kind": first.kind if first else None,
    }


def _desk_digest(rows: list[Any]) -> dict[str, Any]:
    """The prospect desk in the morning digest: every prospect where the move is ours (best
    first) and every stalled one, each with its next move."""
    ours = [r for r in rows if r.waiting_on == "us"]
    stalled = [r for r in rows if r.stalled and r.waiting_on != "us"]
    return {
        "open": len(rows), "our_move_total": len(ours), "stalled_total": len([r for r in rows if r.stalled]),
        "our_move": [_desk_row(r) for r in sorted(ours, key=lambda r: -r.priority)[:DESK_ITEMS]],
        "stalled": [_desk_row(r) for r in sorted(stalled, key=lambda r: -(r.idle_days or 0))[:DESK_ITEMS]],
    }


def _desk_line(row: dict[str, Any]) -> str:
    where = f"{row['hs_code']}→{row['market'] or '?'}"
    idle = f", idle {row['idle_days']}d" if row.get("idle_days") else ""
    ev = f", EV {row['expected_value_usd']:,.0f} USD" if row.get("expected_value_usd") else ""
    move = row.get("next_move") or row.get("waiting_detail") or "look at it"
    return f"  • {row['deal_id']} {where}{idle}{ev}: {move}"


def render_digest_text(d: dict[str, Any]) -> str:
    ap, de, ex, mo = d.get("approvals") or {}, d.get("deals") or {}, d.get("exceptions") or {}, d.get("money") or {}
    lines = [f"PortLess daily digest — {d.get('generated_at', '')[:16].replace('T', ' ')} UTC", ""]
    lines.append(f"GATES WAITING: {ap.get('pending_total', 0)} (oldest {ap.get('oldest_hours', 0):.0f}h)")
    for action, row in (ap.get("by_action") or {}).items():
        extra = f", {row['money_usd']:,.0f} USD" if row.get("money_usd") else ""
        cs = f", {row['countersign']} need a 2nd approver" if row.get("countersign") else ""
        lines.append(f"  • {action}: {row['count']} (oldest {row['oldest_hours']:.0f}h{extra}{cs})")
    lines.append("")
    lines.append(f"DEALS: {de.get('active', 0)} active" + (
        f", {de['waiting_for_buyer']} waiting for an interested buyer" if de.get("waiting_for_buyer") else ""))
    for status, n in (de.get("by_status") or {}).items():
        lines.append(f"  • {status}: {n}")
    if de.get("stalled"):
        lines.append(f"  STALLED ({len(de['stalled'])}): " + ", ".join(
            f"{s['deal_id']} {s['status']} {s['hours'] / 24:.0f}d" for s in de["stalled"][:8]))
    lines.append("")
    pr = d.get("prospects") or {}
    if pr:
        lines.append(f"PROSPECTS: {pr.get('open', 0)} open · our move on {pr.get('our_move_total', 0)} · "
                     f"{pr.get('stalled_total', 0)} stalled")
        if pr.get("our_move"):
            lines.append("  OUR MOVE:")
            lines += [_desk_line(r) for r in pr["our_move"]]
        if pr.get("stalled"):
            lines.append("  STALLED (waiting on them):")
            lines += [_desk_line(r) for r in pr["stalled"]]
        lines.append("")
    rz = d.get("realisation") or []
    if rz:
        lines.append(f"EXPORT PROCEEDS TO REALISE ({len(rz)}):")
        for r in rz[:DESK_ITEMS]:
            lines.append(f"  • {r['deal_id']} SB {r['shipping_bill_number']}: {r['outstanding']:,.2f} {r['currency']} "
                         f"outstanding — {r['status'].replace('_', ' ')}, deadline {r['deadline']}")
        lines.append("")
    ro = d.get("reorders_due") or []
    if ro:
        lines.append(f"REORDERS DUE ({len(ro)}):")
        for r in ro[:DESK_ITEMS]:
            lines.append(f"  • {r['buyer']} — HS {r['hs_code']} → {r['market']}: usual every {r['usual_gap_days']} d, "
                         f"next ~{r['next_order_due']}" + (f" ({r['overdue_days']} d overdue)" if r["overdue_days"] else "")
                         + f" · reorder from {r['deal_id']}")
        lines.append("")
    pa = d.get("price_alerts") or []
    if pa:
        lines.append(f"PRICE ALERTS ({len(pa)}):")
        for a in pa[:DESK_ITEMS]:
            lines.append(f"  • HS {a['hs_code']} supplier FOB {a['change_pct']:+.1f}% "
                         f"({a['base_median_usd_per_mt']:,.0f} → {a['recent_median_usd_per_mt']:,.0f} USD/MT): {a['advice']}")
        lines.append("")
    fu = d.get("followups") or []
    if fu:
        lines.append(f"FOLLOW-UPS DUE ({len(fu)}):")
        for f in fu[:DESK_ITEMS]:
            lines.append(f"  • {f['company']} ({f['role']}, {f['remind_at'][:10]}): {f['text'][:120]}")
        lines.append("")
    lines.append(f"EXCEPTIONS OPEN: {ex.get('open_total', 0)} " + " ".join(
        f"{k}={v}" for k, v in (ex.get("by_status") or {}).items()))
    for item in (ex.get("items") or [])[:6]:
        lines.append(f"  • {item['deal_id']} {item.get('category') or '?'} ({item['status']}, {item.get('age_hours') or 0:.0f}h)")
    lines.append("")
    if mo:
        cur = mo.get("currency", "USD")
        lines.append(f"MONEY ({cur}): in escrow {mo.get('in_escrow', 0):,.0f} · released {mo.get('released_total', 0):,.0f} "
                     f"· net to PortLess {mo.get('net_to_portless', 0):,.0f} · paid to suppliers {mo.get('payouts_total', 0):,.0f} "
                     f"· still owed to suppliers {mo.get('supplier_balance_due', 0):,.0f}")
        flags = []
        if mo.get("unreconciled"):
            flags.append(f"UNRECONCILED: {', '.join(mo['unreconciled'][:5])}")
        if mo.get("disputed"):
            flags.append(f"{mo['disputed']} disputed")
        if mo.get("windows_open"):
            flags.append(f"{mo['windows_open']} acceptance window(s) running")
        if flags:
            lines.append("  " + " · ".join(flags))
    apify = d.get("apify") or {}
    if apify:
        cap = apify.get("max_runs_per_day") or "∞"
        val = apify.get("validation_runs_today")
        lines.append(f"APIFY: {apify.get('runs_today', 0)}/{cap} actor runs today"
                     + (f" ({val} counterparty checks, {apify.get('runs_today', 0) - val} discovery and other)"
                        if val else ""))
        if isinstance(apify.get("max_runs_per_day"), int) and apify["max_runs_per_day"] > 0 \
                and apify.get("runs_today", 0) >= 0.9 * apify["max_runs_per_day"]:
            lines.append("  ! the daily run budget is nearly used — checks and scans stop when it is reached")
        usd_cap = float(apify.get("max_usd_per_day") or 0)
        usd_now = float(apify.get("usd_today") or 0)
        if usd_now or usd_cap:
            lines.append(f"  spend {usd_now:.2f}" + (f" / {usd_cap:.2f}" if usd_cap else "") + " USD today")
            if usd_cap and usd_now >= 0.9 * usd_cap:
                lines.append("  ! the daily spend cap is nearly used — actor runs stop when it is reached")
        per_actor = sorted((apify.get("by_actor") or {}).items(), key=lambda kv: -float(kv[1].get("usd") or 0))
        if per_actor:
            lines.append("  per actor: " + ", ".join(
                f"{name.lower()} {int(v.get('runs') or 0)} run(s) {float(v.get('usd') or 0):.2f} USD"
                for name, v in per_actor[:6]))
    regs = d.get("registrations_due") or []
    if regs:
        lines.append("REGISTRATIONS: " + ", ".join(
            f"{r['kind']} {'LAPSED' if r['status'] == 'expired' else 'lapses in ' + str(r.get('days_left')) + ' d'}"
            f" ({r.get('expires_on')})" for r in regs[:6]))
    hs_due = d.get("hs_policy_due") or []
    if hs_due:
        lines.append(f"HS CODES TO RE-CONFIRM ({len(hs_due)}): " + ", ".join(
            f"{h['hs_code']} ({h['reason']})" for h in hs_due[:6]))
    hits = d.get("regulatory_hits") or []
    if hits:
        lines.append(f"REGULATORY NOTICES ({len(hits)}): " + "; ".join(
            f"{h.get('source')}: {h.get('title', '')[:80]} → {', '.join(h.get('hs_codes') or h.get('products') or [])}"
            for h in hits[:5]))
    fo = d.get("fees_over") or []
    if fo:
        lines.append(f"FEES ABOVE THE PRICE ({len(fo)} deal(s)): " + ", ".join(
            f"{r['deal_id']} +{r['over_usd']:,.0f} USD ({'/'.join(r['kinds'])})" for r in fo[:6]))
    inc = d.get("incentives_short") or []
    if inc:
        lines.append(f"RODTEP / DRAWBACK SHORT ({len(inc)} deal(s)): " + ", ".join(
            f"{r['deal_id']} credited {r['credited_usd']:,.0f} of {r['counted_usd']:,.0f} USD" for r in inc[:6]))
    wa = d.get("watchers") or {}
    if wa.get("stale"):
        lines.append("! WATCHERS NOT RUNNING: " + ", ".join(wa["stale"])
                     + " — reminders, auto-releases or timeouts may be waiting")
    for w in d.get("warnings") or []:
        lines.append(f"! {w}")
    return "\n".join(lines)


async def send_daily_digest(now: datetime | None = None) -> dict[str, Any]:
    """Build + deliver on every configured channel. Returns what happened."""
    d = await build_digest(now)
    text = render_digest_text(d)
    result: dict[str, Any] = {"telegram": False, "email": [], "text": text}
    try:
        from shared import telegram
        if telegram.configured():
            result["telegram"] = await telegram.send_message(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Digest: telegram failed: %s", exc)
    for to in email_recipients():
        try:
            from services.email_gateway.schemas import SendEmailRequest  # type: ignore[import]
            from services.email_gateway.sender import send_email  # type: ignore[import]
            r = await send_email(SendEmailRequest(
                to_address=to, subject=f"PortLess daily digest — {d.get('generated_at', '')[:10]}", body=text,
                email_id=f"DIGEST{(now or datetime.now(timezone.utc)).strftime('%Y%m%d')}",
            ))
            result["email"].append({"to": to, "outcome": str(getattr(getattr(r, "outcome", None), "value", getattr(r, "outcome", "")))})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Digest: e-mail to %s failed: %s", to, exc)
            result["email"].append({"to": to, "outcome": f"error: {exc}"})
    if not result["telegram"] and not result["email"]:
        logger.info("Digest (no delivery channel configured):\n%s", text)
    return result


def seconds_until_next(now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    h, m = digest_hour_utc()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def run_digest_scheduler() -> None:
    """Background loop started from the app lifespan; cancelled on shutdown."""
    if not enabled():
        logger.info("Digest: disabled (DAILY_DIGEST_ENABLED=false)")
        return
    h, m = digest_hour_utc()
    logger.info("Digest: scheduled daily at %02d:%02d UTC", h, m)
    while True:
        try:
            await asyncio.sleep(seconds_until_next())
            from shared import leader_lock
            if not await leader_lock.acquire("daily_digest", ttl_seconds=3600):
                continue  # another instance sends today's digest
            await send_daily_digest()
        except asyncio.CancelledError:
            logger.info("Digest: stopped")
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Digest: tick failed: %s", exc)
            await asyncio.sleep(60)
