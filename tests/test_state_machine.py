"""Unit tests for the shared deal state machine and its deterministic guards."""
from __future__ import annotations

from shared.domain import DealStatus, can_transition
from shared.domain.state_machine import dual_doc_match_ok, margin_floor_ok


def test_valid_forward_transition() -> None:
    assert can_transition(DealStatus.DRAFT, DealStatus.MATCHING)


def test_negotiation_counter_round_self_loop() -> None:
    assert can_transition(DealStatus.NEGOTIATING, DealStatus.NEGOTIATING)


def test_invalid_transition_rejected() -> None:
    assert not can_transition(DealStatus.DRAFT, DealStatus.PAID)


def test_terminal_states_have_no_exits() -> None:
    for terminal in (DealStatus.PAID, DealStatus.CANCELLED, DealStatus.REFUNDED):
        assert not can_transition(terminal, DealStatus.MATCHING)


# ---------------------------------------------------------------------------
# margin_floor_ok
# ---------------------------------------------------------------------------

def test_margin_floor_passes_above_floor() -> None:
    assert margin_floor_ok(1500.0, 1000.0)


def test_margin_floor_passes_exactly_at_floor() -> None:
    assert margin_floor_ok(1000.0, 1000.0)   # inclusive


def test_margin_floor_fails_below_floor() -> None:
    assert not margin_floor_ok(999.99, 1000.0)


def test_margin_floor_negative_margin_fails() -> None:
    assert not margin_floor_ok(-50.0, 0.0)


# ---------------------------------------------------------------------------
# dual_doc_match_ok
# ---------------------------------------------------------------------------

def test_dual_doc_identical_values_pass() -> None:
    assert dual_doc_match_ok(100.0, 100.0, 100.0)


def test_dual_doc_within_tolerance_passes() -> None:
    # 0.05% spread on the largest value — under the 0.1% default tolerance.
    assert dual_doc_match_ok(100.0, 100.0, 99.95)


def test_dual_doc_at_tolerance_boundary_passes() -> None:
    # Exactly 0.1% spread — inclusive boundary passes.
    assert dual_doc_match_ok(1000.0, 1000.0, 999.0)


def test_dual_doc_exceeding_tolerance_fails() -> None:
    # 1% spread — well over 0.1%.
    assert not dual_doc_match_ok(100.0, 100.0, 99.0)


def test_dual_doc_custom_tolerance() -> None:
    assert dual_doc_match_ok(100.0, 100.0, 99.0, tolerance_pct=2.0)
    assert not dual_doc_match_ok(100.0, 100.0, 99.0, tolerance_pct=0.5)


def test_dual_doc_non_positive_value_fails() -> None:
    assert not dual_doc_match_ok(100.0, 0.0, 100.0)
    assert not dual_doc_match_ok(100.0, -100.0, 100.0)
