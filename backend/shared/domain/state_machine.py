"""Deal state machine (LLD 4.2). Transitions are enforced in code, never by an LLM.

Guard conditions (margin floor, credit check, dual-doc match, verified escrow)
are intentionally left as TODO pure-function stubs — they must be deterministic.
"""
from __future__ import annotations

from .enums import DealStatus

# Allowed forward/branch transitions of the deal lifecycle.
DEAL_TRANSITIONS: dict[DealStatus, set[DealStatus]] = {
    DealStatus.DRAFT: {DealStatus.MATCHING},
    DealStatus.MATCHING: {DealStatus.NEGOTIATING},
    DealStatus.NEGOTIATING: {
        DealStatus.NEGOTIATING,  # counter-round
        DealStatus.NEGOTIATION_AGREED,
        DealStatus.NEGOTIATION_FAILED,
    },
    DealStatus.NEGOTIATION_FAILED: {DealStatus.MATCHING, DealStatus.CANCELLED},
    DealStatus.NEGOTIATION_AGREED: {DealStatus.PI_SENT},
    DealStatus.PI_SENT: {DealStatus.PI_ACCEPTED},
    DealStatus.PI_ACCEPTED: {DealStatus.ESCROW_PENDING},
    DealStatus.ESCROW_PENDING: {DealStatus.ESCROW_FUNDED},
    DealStatus.ESCROW_FUNDED: {DealStatus.PO_ISSUED},
    DealStatus.PO_ISSUED: {DealStatus.SUPPLIER_DISPATCH_READY},
    DealStatus.SUPPLIER_DISPATCH_READY: {DealStatus.QC_PASSED, DealStatus.QC_FAILED},
    DealStatus.QC_PASSED: {DealStatus.IN_TRANSIT},
    DealStatus.IN_TRANSIT: {DealStatus.CLEARED},
    DealStatus.CLEARED: {DealStatus.PAID},
    DealStatus.QC_FAILED: {DealStatus.DISPUTE},
    DealStatus.DISPUTE: {DealStatus.CANCELLED, DealStatus.REFUNDED},
    DealStatus.PAID: set(),
    DealStatus.CANCELLED: set(),
    DealStatus.REFUNDED: set(),
}


def can_transition(current: DealStatus, target: DealStatus) -> bool:
    """Return True if ``target`` is a permitted next state from ``current``.

    NOTE: This only checks topology. Business guards (net_margin >= margin_floor,
    verified escrow webhook, credit check, dual-doc match) MUST be evaluated
    separately as deterministic pure functions before committing a transition.
    """
    return target in DEAL_TRANSITIONS.get(current, set())


# --- Deterministic guard stubs (pure functions; never overridden by an LLM) ---

def margin_floor_ok(net_margin_usd: float, margin_floor_usd: float) -> bool:
    """TODO: implement net-margin floor guard."""
    raise NotImplementedError


def dual_doc_match_ok(crf_hash: str, bol_hash: str, invoice_hash: str) -> bool:
    """TODO: implement >0.1% mismatch guard across CRF / BoL / Commercial Invoice."""
    raise NotImplementedError
