"""Deal state machine (LLD 4.2). Transitions are enforced in code, never by an LLM.

Guard conditions (margin floor, dual-doc match) are deterministic pure functions
so their outcome is reproducible and never influenced by an LLM. ``can_transition``
checks only the lifecycle topology; callers evaluate the relevant guard(s) before
committing a state change.
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


# --- Deterministic guards (pure functions; never overridden by an LLM) ---

def margin_floor_ok(net_margin_usd: float, margin_floor_usd: float) -> bool:
    """Return True when the deal's net margin meets or exceeds the floor.

    A non-positive floor imposes no constraint (always passes). The comparison is
    inclusive: a net margin exactly equal to the floor is acceptable.
    """
    return net_margin_usd >= margin_floor_usd


def dual_doc_match_ok(
    crf_value: float,
    bol_value: float,
    invoice_value: float,
    tolerance_pct: float = 0.1,
) -> bool:
    """Three-way trade-finance document match on declared quantity/value.

    The quantity (or value) stated on the Clean Report of Findings (CRF), the
    carrier's Bill of Lading (BoL), and the Commercial Invoice must be consistent
    before funds are released. The guard fails when the spread across the three
    documents exceeds ``tolerance_pct`` (default 0.1%), measured relative to the
    largest of the three values.

    Any non-positive value is treated as unverifiable and fails the guard — a
    valid shipment cannot declare a zero or negative quantity/value.
    """
    values = (crf_value, bol_value, invoice_value)
    if any(v <= 0 for v in values):
        return False
    hi, lo = max(values), min(values)
    divergence_pct = (hi - lo) / hi * 100.0
    return divergence_pct <= tolerance_pct
