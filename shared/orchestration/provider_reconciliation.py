"""Daily reconciliation: PortLess's books against what the providers themselves report.

The ledger's own check (``check_reconciliation``) proves the books add up. This proves they
agree with the outside world: once a day (``PROVIDER_RECON_HOUR_UTC``, default 02:00) every
open escrow is read from its provider (Tazapay / Escrow.com) and every payout from the payout
rail (Payoneer), read-only — nothing is applied — and compared:

* funded amount at the provider vs ``funded_amount`` on the hold;
* stages the provider paid out vs tranches booked RELEASED, both ways;
* payouts the rail completed or failed vs the payout records.

Each difference raises ``deal.exception`` (stage ``RECONCILIATION:<kind>``) once per day and
appears in ``GET /escrow-guard/reconciliation/providers`` and the digest. A provider that
cannot be read is reported as ``unreadable`` (not a difference). Bank receipts vs e-BRC
need the bank statement feed (not in the system yet).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_TOLERANCE = 0.01
_last_run: dict[str, Any] = {"date": None, "report": None}
_alerted: set[str] = set()


def recon_hour() -> int:
    try:
        return max(0, min(23, int(os.getenv("PROVIDER_RECON_HOUR_UTC", "2"))))
    except ValueError:
        return 2


def compare_escrow(hold: Any, view: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure: differences between a hold and the provider's interpreted status."""
    diffs: list[dict[str, Any]] = []
    if view.get("funded"):
        theirs = view.get("funded_amount")
        ours = getattr(hold, "funded_amount", None)
        if theirs is not None and (ours is None or abs(float(theirs) - float(ours)) > _TOLERANCE):
            diffs.append({"kind": "funded_amount", "provider": float(theirs), "portless": ours,
                          "detail": f"provider holds {float(theirs):,.2f}, PortLess shows {ours if ours is not None else 'nothing'}"})
    elif getattr(hold, "status", None) in ("FUNDED", "RELEASED") and view.get("funded") is False:
        diffs.append({"kind": "funded_amount", "provider": 0.0, "portless": getattr(hold, "funded_amount", None),
                      "detail": "PortLess shows the escrow funded; the provider does not"})
    paid_out = set(view.get("released") or [])
    booked = {t.tranche_id for t in (getattr(hold, "tranches", None) or []) if t.status == "RELEASED"}
    for tid in sorted(paid_out - booked):
        diffs.append({"kind": "tranche_not_booked", "tranche_id": tid,
                      "detail": f"provider paid out {tid}; PortLess has not booked it"})
    for tid in sorted(booked - paid_out):
        diffs.append({"kind": "tranche_not_paid_by_provider", "tranche_id": tid,
                      "detail": f"PortLess booked {tid} released; the provider shows no payout"})
    return diffs


def compare_payout(payout: Any, body: dict[str, Any]) -> list[dict[str, Any]]:
    theirs = str(body.get("status") or "").upper()
    ours = str(getattr(payout, "status", "") or "").upper()
    if not theirs or theirs == ours:
        return []
    if theirs in ("COMPLETED", "PAID", "DONE") and ours != "COMPLETED":
        return [{"kind": "payout_completed_not_booked", "payout_id": payout.payout_id,
                 "detail": f"payout rail completed {payout.payout_id}; PortLess shows {ours}"}]
    if theirs in ("FAILED", "CANCELLED", "RETURNED") and ours == "COMPLETED":
        return [{"kind": "payout_booked_but_failed", "payout_id": payout.payout_id,
                 "detail": f"PortLess booked {payout.payout_id} completed; the rail says {theirs}"}]
    return []


async def run(*, now: datetime | None = None, alert: bool = True) -> dict[str, Any]:
    """Read every open escrow and live payout from its provider and compare. Never raises."""
    from services.escrow_guard.repository import get_repository  # type: ignore[import]
    from services.escrow_guard.service import NEUTRAL_PROVIDERS, _payment_connector  # type: ignore[import]
    now = now or datetime.now(timezone.utc)
    repo = get_repository()
    report: dict[str, Any] = {"run_at": now.isoformat(), "escrows_checked": 0, "payouts_checked": 0,
                              "differences": [], "unreadable": []}
    try:
        holds = await repo.list_holds()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ProviderRecon: holds unreadable: %s", exc)
        holds = []
    for hold in holds:
        if hold.status in ("REFUNDED", "CANCELLED") or hold.provider not in NEUTRAL_PROVIDERS or not hold.ext_ref:
            continue
        try:
            connector = _payment_connector(hold.provider)
            view = connector.interpret_status(await connector.status(hold.ext_ref), hold.tranches, None)
        except Exception as exc:  # noqa: BLE001
            report["unreadable"].append({"deal_id": hold.deal_id, "what": f"{hold.provider} escrow", "error": str(exc)[:200]})
            continue
        report["escrows_checked"] += 1
        for d in compare_escrow(hold, view):
            report["differences"].append({"deal_id": hold.deal_id, "escrow_id": hold.escrow_id, **d})
        try:
            payouts = await repo.list_payouts_by_deal(hold.deal_id)
        except Exception:  # noqa: BLE001
            payouts = []
        for p in payouts:
            if p.status not in ("PROCESSING", "COMPLETED") or not p.ext_ref:
                continue
            try:
                body = await _payout_connector().status(p.ext_ref)
            except Exception as exc:  # noqa: BLE001
                report["unreadable"].append({"deal_id": p.deal_id, "what": f"payout {p.payout_id}", "error": str(exc)[:200]})
                continue
            report["payouts_checked"] += 1
            for d in compare_payout(p, body):
                report["differences"].append({"deal_id": p.deal_id, **d})
    if alert:
        await _alert(report, now)
    _last_run.update(date=now.date().isoformat(), report=report)
    logger.info("ProviderRecon: %d escrow(s), %d payout(s) read — %d difference(s), %d unreadable",
                report["escrows_checked"], report["payouts_checked"], len(report["differences"]), len(report["unreadable"]))
    return report


def _payout_connector() -> Any:
    from shared.connectors.payments import PayoneerConnector
    return PayoneerConnector()


async def _alert(report: dict[str, Any], now: datetime) -> None:
    from shared.bus import get_publisher
    from shared.domain.events import DomainEvent, EventTopic
    for d in report["differences"]:
        key = f"{now.date()}:{d['deal_id']}:{d['kind']}:{d.get('tranche_id') or d.get('payout_id') or ''}"
        if key in _alerted:
            continue
        _alerted.add(key)
        try:
            await get_publisher().publish(DomainEvent(topic=EventTopic.DEAL_EXCEPTION, deal_id=d["deal_id"], payload={
                "source": "provider_reconciliation", "stage": f"RECONCILIATION:{d['kind']}", "category": "PAYMENT_FAILED",
                "error": d["detail"], "action_required": "compare_with_provider_statement",
            }))
        except Exception as exc:  # noqa: BLE001
            logger.warning("ProviderRecon: exception for %s not raised: %s", d["deal_id"], exc)


async def due_daily(now: datetime | None = None) -> dict[str, Any] | None:
    """The escrow watcher calls this every tick: runs once a day at the reconciliation hour."""
    now = now or datetime.now(timezone.utc)
    if now.hour < recon_hour() or _last_run["date"] == now.date().isoformat():
        return None
    return await run(now=now)


def last_report() -> dict[str, Any] | None:
    return _last_run["report"]


def reset_for_tests() -> None:
    _last_run.update(date=None, report=None)
    _alerted.clear()
