"""Unit tests for the shared deal state machine."""
from __future__ import annotations

from shared.domain import DealStatus, can_transition


def test_valid_forward_transition() -> None:
    assert can_transition(DealStatus.DRAFT, DealStatus.MATCHING)


def test_negotiation_counter_round_self_loop() -> None:
    assert can_transition(DealStatus.NEGOTIATING, DealStatus.NEGOTIATING)


def test_invalid_transition_rejected() -> None:
    assert not can_transition(DealStatus.DRAFT, DealStatus.PAID)


def test_terminal_states_have_no_exits() -> None:
    for terminal in (DealStatus.PAID, DealStatus.CANCELLED, DealStatus.REFUNDED):
        assert not can_transition(terminal, DealStatus.MATCHING)
