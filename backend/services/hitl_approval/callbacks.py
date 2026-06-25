"""Saga-continuation callbacks for approved HITL actions.

When an operator approves a request, the HITL service calls the appropriate
downstream service to resume the deal flow.  The ``callback_payload`` stored
with the approval record is the full request body for the target service,
exactly as the caller submitted it when creating the approval.

On REJECT the service emits a ``deal.exception`` event instead so the
Exception Resolver (or a human operator) can decide the next step.

Each callback is:
  async def _cb_<action>(deal_id, payload) -> dict | None
  Returns a dict summary (logged) or None if nothing to report.

The dispatcher ``execute_callback`` maps ApprovalAction values to handlers.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Individual action handlers
# ---------------------------------------------------------------------------

async def _cb_book_freight(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Continue: book freight container after HITL approval."""
    from services.shield_link.service import book_freight  # type: ignore[import]
    from services.shield_link.schemas import FreightBookingRequest  # type: ignore[import]
    result = await book_freight(FreightBookingRequest(**payload))
    return {"tpo_id": result.tpo_id, "status": result.status}


async def _cb_issue_po(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Continue: issue Purchase Order after HITL approval."""
    from services.dealmaker.service import issue_po  # type: ignore[import]
    from services.dealmaker.schemas import IssuePoRequest  # type: ignore[import]
    result = await issue_po(IssuePoRequest(**payload))
    return {"po_id": result.po_id, "status": result.status}


async def _cb_release_supplier_payout(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Continue: release supplier payout after HITL approval."""
    from services.escrow_guard.service import request_payout  # type: ignore[import]
    from services.escrow_guard.schemas import PayoutRequest  # type: ignore[import]
    result = await request_payout(PayoutRequest(**payload))
    return {"payout_id": result.payout_id, "status": result.status}


async def _cb_file_customs(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Continue: file customs (CHA) after HITL approval."""
    from services.shield_link.service import file_cha  # type: ignore[import]
    from services.shield_link.schemas import ChaFilingRequest  # type: ignore[import]
    result = await file_cha(ChaFilingRequest(**payload))
    return {"tpo_id": result.tpo_id, "status": result.status}


async def _cb_resolve_exception(deal_id: str, payload: dict[str, Any]) -> dict | None:
    """Continue: resolve a deal exception after HITL approval."""
    # Exception resolver is still a stub; log and return.
    logger.info("HITL: RESOLVE_EXCEPTION approved for deal %s — payload: %s", deal_id, payload)
    return {"action": "RESOLVE_EXCEPTION", "deal_id": deal_id}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_CALLBACK_MAP = {
    "BOOK_FREIGHT": _cb_book_freight,
    "ISSUE_PO": _cb_issue_po,
    "RELEASE_SUPPLIER_PAYOUT": _cb_release_supplier_payout,
    "FILE_CUSTOMS": _cb_file_customs,
    "RESOLVE_EXCEPTION": _cb_resolve_exception,
}


async def execute_callback(
    action: str,
    deal_id: str,
    callback_payload: dict[str, Any],
    approval_id: str,
) -> dict | None:
    """Execute the saga-continuation callback for an approved action.

    Errors are caught and logged rather than re-raised so that a failed
    callback does not roll back the approval record (the approval itself
    succeeded — the downstream failure should surface as a deal.exception).
    """
    handler = _CALLBACK_MAP.get(action)
    if handler is None:
        logger.warning(
            "HITL: no callback registered for action %r (approval=%s)", action, approval_id
        )
        return None

    try:
        result = await handler(deal_id, callback_payload)
        logger.info(
            "HITL: callback %s executed for deal %s approval %s → %s",
            action, deal_id, approval_id, result,
        )
        return result
    except Exception as exc:
        logger.error(
            "HITL: callback %s failed for deal %s approval %s: %s",
            action, deal_id, approval_id, exc,
        )
        # Surface as domain event so Exception Resolver can pick it up
        try:
            from shared.bus import get_publisher
            from shared.domain.events import DomainEvent, EventTopic
            await get_publisher().publish(DomainEvent(
                topic=EventTopic.DEAL_EXCEPTION,
                deal_id=deal_id,
                payload={
                    "source": "hitl_approval.callback",
                    "approval_id": approval_id,
                    "action": action,
                    "error": str(exc),
                    "action_required": "manual_retry_or_intervention",
                },
            ))
        except Exception:
            pass
        return None
