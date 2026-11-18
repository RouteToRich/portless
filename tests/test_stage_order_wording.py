"""Negotiation drafts that put the order steps in the wrong order are flagged and held at L0
(agents/negotiation_agent/service.py stage_order_errors)."""
from __future__ import annotations

import pytest

from agents.negotiation_agent.service import _FORCE_L0_FLAGS, draft_risk_flags, stage_order_errors


@pytest.mark.parametrize("text", [
    "As soon as we have the inspector selected, we will send the proforma invoice.",
    "We will issue the PI once the inspection is booked.",
    "We can release the purchase order before the escrow deposit to save time.",
    "The QC inspection will happen before escrow funding.",
])
def test_wrong_order_flagged(text):
    assert stage_order_errors(text)
    flags = draft_risk_flags(text)
    assert "stage_order_wrong" in flags and "stage_order_wrong" in _FORCE_L0_FLAGS


@pytest.mark.parametrize("text", [
    "Once you confirm, we will send the Proforma Invoice. After the escrow is funded we issue the Purchase "
    "Order and book the inspection.",
    "The inspector you choose checks the goods before shipment.",
    "Payment is released to the supplier only after the inspection passes.",
    "",
])
def test_right_order_not_flagged(text):
    assert stage_order_errors(text) == []
    assert "stage_order_wrong" not in draft_risk_flags(text)
