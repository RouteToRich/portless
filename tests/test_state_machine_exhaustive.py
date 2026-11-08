"""Every (from, to) pair of the deal lifecycle, plus the guard boundaries.

``EXPECTED`` is written out by hand on purpose: it is an independent statement
of the lifecycle, so an accidental edit to ``DEAL_TRANSITIONS`` fails here.
"""
from __future__ import annotations

from collections import deque
from itertools import product

import pytest

from shared.domain.enums import DealStatus as S
from shared.domain.state_machine import (
    DEAL_TRANSITIONS,
    can_transition,
    dual_doc_match_ok,
    margin_floor_ok,
)

EXPECTED: dict[S, set[S]] = {
    S.DRAFT: {S.MATCHING},
    S.MATCHING: {S.NEGOTIATING},
    S.NEGOTIATING: {S.NEGOTIATING, S.NEGOTIATION_AGREED, S.NEGOTIATION_FAILED},
    S.NEGOTIATION_FAILED: {S.MATCHING, S.CANCELLED},
    S.NEGOTIATION_AGREED: {S.PI_SENT},
    S.PI_SENT: {S.PI_ACCEPTED, S.CANCELLED},
    S.PI_ACCEPTED: {S.ESCROW_PENDING, S.CANCELLED},
    S.ESCROW_PENDING: {S.ESCROW_FUNDED, S.CANCELLED},
    S.ESCROW_FUNDED: {S.PO_ISSUED},
    S.PO_ISSUED: {S.SUPPLIER_DISPATCH_READY},
    S.SUPPLIER_DISPATCH_READY: {S.QC_PASSED, S.QC_FAILED},
    S.QC_PASSED: {S.IN_TRANSIT},
    S.QC_FAILED: {S.DISPUTE},
    S.IN_TRANSIT: {S.CLEARED},
    S.CLEARED: {S.PAID},
    S.DISPUTE: {S.CANCELLED, S.REFUNDED, S.SUPPLIER_DISPATCH_READY},
    S.PAID: set(),
    S.CANCELLED: set(),
    S.REFUNDED: set(),
}

ALL_PAIRS = list(product(S, S))
FUNDED_OR_LATER = {
    S.ESCROW_FUNDED, S.PO_ISSUED, S.SUPPLIER_DISPATCH_READY, S.QC_PASSED, S.QC_FAILED,
    S.IN_TRANSIT, S.CLEARED, S.DISPUTE,
}


def _reachable(start: S) -> set[S]:
    seen = {start}
    queue = deque([start])
    while queue:
        for nxt in DEAL_TRANSITIONS.get(queue.popleft(), set()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


@pytest.mark.parametrize(("current", "target"), ALL_PAIRS, ids=lambda s: s.value)
def test_every_pair_matches_the_lifecycle(current: S, target: S) -> None:
    assert can_transition(current, target) is (target in EXPECTED[current])


def test_every_status_has_an_entry() -> None:
    assert set(DEAL_TRANSITIONS) == set(S)


def test_every_status_is_reachable_from_draft() -> None:
    assert _reachable(S.DRAFT) == set(S)


@pytest.mark.parametrize("status", [s for s in S if s not in {S.PAID, S.CANCELLED, S.REFUNDED}], ids=lambda s: s.value)
def test_no_open_status_is_a_dead_end(status: S) -> None:
    assert _reachable(status) & {S.PAID, S.CANCELLED, S.REFUNDED}


@pytest.mark.parametrize("terminal", [S.PAID, S.CANCELLED, S.REFUNDED], ids=lambda s: s.value)
def test_terminal_status_goes_nowhere(terminal: S) -> None:
    assert not any(can_transition(terminal, target) for target in S)


@pytest.mark.parametrize("status", sorted(FUNDED_OR_LATER - {S.DISPUTE}, key=lambda s: s.value), ids=lambda s: s.value)
def test_funded_deal_cannot_be_cancelled_without_a_dispute(status: S) -> None:
    """Cancelling would strand the buyer's money: it has to go through DISPUTE."""
    assert not can_transition(status, S.CANCELLED)


def test_paid_is_only_reachable_from_cleared() -> None:
    assert [s for s in S if can_transition(s, S.PAID)] == [S.CLEARED]


def test_refund_is_only_reachable_from_dispute() -> None:
    assert [s for s in S if can_transition(s, S.REFUNDED)] == [S.DISPUTE]


def test_po_is_only_reachable_from_funded_escrow() -> None:
    assert [s for s in S if can_transition(s, S.PO_ISSUED)] == [S.ESCROW_FUNDED]


def test_qc_cannot_be_skipped_on_the_way_to_transit() -> None:
    assert [s for s in S if can_transition(s, S.IN_TRANSIT)] == [S.QC_PASSED]


def test_failed_negotiation_can_rematch() -> None:
    assert can_transition(S.NEGOTIATION_FAILED, S.MATCHING)


def test_unknown_current_status_is_refused() -> None:
    """A raw string misses the dict: the documented reason callers coerce first."""
    assert can_transition("NOT_A_STATUS", S.MATCHING) is False  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("margin", "floor", "ok"),
    [
        (1000.01, 1000.0, True),
        (1000.0, 1000.0, True),
        (999.99, 1000.0, False),
        (0.0, 0.0, True),
        (-0.01, 0.0, False),
        (5.0, -10.0, True),
        (-5.0, -10.0, True),
        (-10.01, -10.0, False),
    ],
)
def test_margin_floor_boundaries(margin: float, floor: float, ok: bool) -> None:
    assert margin_floor_ok(margin, floor) is ok


@pytest.mark.parametrize(
    "values",
    [(0.0, 1.0, 1.0), (1.0, 0.0, 1.0), (1.0, 1.0, 0.0), (-1.0, 1.0, 1.0), (1.0, -1.0, 1.0), (1.0, 1.0, -1.0), (0.0, 0.0, 0.0)],
)
def test_dual_doc_refuses_any_non_positive_value(values: tuple[float, float, float]) -> None:
    assert dual_doc_match_ok(*values, tolerance_pct=100.0) is False


@pytest.mark.parametrize(
    "values",
    [(999.0, 1000.0, 1000.0), (1000.0, 999.0, 1000.0), (1000.0, 1000.0, 999.0)],
)
def test_dual_doc_spread_counts_whichever_document_differs(values: tuple[float, float, float]) -> None:
    assert dual_doc_match_ok(*values) is True


@pytest.mark.parametrize(
    "values",
    [(998.9, 1000.0, 1000.0), (1000.0, 998.9, 1000.0), (1000.0, 1000.0, 998.9)],
)
def test_dual_doc_refuses_past_tolerance_on_any_document(values: tuple[float, float, float]) -> None:
    assert dual_doc_match_ok(*values) is False


def test_dual_doc_zero_tolerance_needs_identical_values() -> None:
    assert dual_doc_match_ok(10.0, 10.0, 10.0, tolerance_pct=0.0) is True
    assert dual_doc_match_ok(10.0, 10.0, 9.9999, tolerance_pct=0.0) is False
