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

    # ── Apify spend ────────────────────────────────────────────────────────
    try:
        from shared.connectors.apify import max_runs_per_day, runs_today
        out["apify"] = {"runs_today": runs_today(), "max_runs_per_day": max_runs_per_day()}
    except Exception:  # noqa: BLE001
        pass
    return out


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
        lines.append(f"APIFY: {apify.get('runs_today', 0)}/{cap} actor runs today")
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
            await send_daily_digest()
        except asyncio.CancelledError:
            logger.info("Digest: stopped")
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Digest: tick failed: %s", exc)
            await asyncio.sleep(60)
