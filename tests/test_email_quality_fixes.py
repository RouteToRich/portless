"""E-mail quality fixes found by reading a real LLM run (UC-01, 2026-11-10)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from agents.negotiation_agent import service as svc
from agents.negotiation_agent.email_history import EmailDirection, EmailStatus
from agents.negotiation_agent.graph import strip_false_target_claim
from shared.orchestration import notifications as nf


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# 1. "our price meets your target" when it does not ------------------------------

def test_false_target_sentence_is_removed_and_the_rest_kept():
    text = ("Dear team,\n\nThank you for your counter of USD 1,200 per MT. We have reviewed your budget and, "
            "at USD 1,316.43 per MT CIF, our price meets your target. Payment goes into escrow.\n\nPlease confirm the port.")
    out, claimed = strip_false_target_claim(text)
    assert claimed is True
    assert "meets your target" not in out
    assert "Thank you for your counter of USD 1,200 per MT." in out and "Payment goes into escrow." in out
    assert out.endswith("Please confirm the port.")


def test_other_target_phrasings_are_caught():
    for claim in ("This meets your target price.", "It is within your budget.", "That matches your stated target."):
        _out, claimed = strip_false_target_claim(f"Dear team,\n\nWe offer USD 1,293.25. {claim}")
        assert claimed, claim


def test_text_without_a_claim_is_untouched():
    text = "Dear team,\n\nWe offer USD 1,293.25 per MT CIF. What is your target price?"
    assert strip_false_target_claim(text) == (text, False)


def test_false_target_claim_forces_human_review():
    assert "false_target_claim" in svc._FORCE_L0_FLAGS


def test_market_benchmark_claims_are_removed():
    from agents.negotiation_agent.graph import strip_market_claim
    for claim in ("This price meets the standard benchmark for milled rice of this specification.",
                  "This price reflects the current market and our quality terms."):
        out, found = strip_market_claim(f"Dear team,\n\nWe can offer USD 1,293.25 per MT CIF. {claim} Please confirm.")
        assert found, claim
        assert claim not in out and "We can offer USD 1,293.25 per MT CIF." in out and "Please confirm." in out
    assert "unverified_market_claim" in svc._FORCE_L0_FLAGS


def test_meets_your_counter_offer_is_a_false_claim_too():
    text = ("Dear team,\n\nWe can offer 20 MT at USD 1287.81 per MT CIF your destination port. "
            "This meets your counter-offer of USD 1200 per MT. Please confirm the port.")
    out, claimed = strip_false_target_claim(text)
    assert claimed and "meets your counter-offer" not in out and "USD 1287.81" in out


def test_internal_pricing_words_are_removed():
    from agents.negotiation_agent.graph import strip_internal_terms
    for phrase in ("This is within our allowed range.", "It is above our floor price.",
                   "That leaves our margin intact.", "This is our recommended price."):
        out, found = strip_internal_terms(f"Dear team,\n\nWe can offer USD 1,287.81 per MT. {phrase} Kindly confirm.")
        assert found, phrase
        assert phrase not in out and "Kindly confirm." in out
    assert "internal_terms" in svc._FORCE_L0_FLAGS


def test_delivery_wording_is_not_mistaken_for_a_claim():
    from agents.negotiation_agent.graph import strip_internal_terms
    text = "Dear team,\n\nWe can deliver within your requested timeframe."
    assert strip_false_target_claim(text) == (text, False)
    assert strip_internal_terms(text) == (text, False)


# 1b. reading the buyer's counter, not our own echoed price ------------------------

def test_counter_wins_over_our_echoed_price():
    from services.email_processor.service import _extract_price
    body = ("Thank you for your offer. The price of USD 1,316.43 per MT is above our current market budget. "
            "We can counter at USD 1,200 per MT CIF AE for 20 MT.")
    assert run(_extract_price(body)) == 1200.0
    assert run(_extract_price(body, ignore_price=1316.43)) == 1200.0


def test_our_echoed_price_is_skipped_when_another_figure_exists():
    from services.email_processor.service import _extract_price
    body = "Your USD 1,316.43 per MT is too high for us; USD 1,240 per MT would work."
    assert run(_extract_price(body, ignore_price=1316.43)) == 1240.0


def test_single_price_and_supplier_offer_still_read():
    from services.email_processor.service import _extract_price
    assert run(_extract_price("We can proceed at USD 1,225 per MT CIF AE, valid for 7 days.")) == 1225.0
    assert run(_extract_price("I can improve slightly and offer USD 1,010 per MT FOB Chennai.")) == 1010.0


def test_quoted_history_is_ignored():
    from services.email_processor.service import _extract_price
    body = ("We can do USD 1,180 per MT.\n\nOn Mon, 10 Nov 2026 PortLess <trade@portless.io> wrote:\n"
            "> We can offer milled rice at USD 1,316.43 per MT CIF.")
    assert run(_extract_price(body)) == 1180.0


# 2. no second buyer e-mail in a row ---------------------------------------------

def _rec(party, direction, status, minutes):
    return SimpleNamespace(party=party, direction=direction, status=status,
                           sent_at=None, updated_at=None,
                           created_at=datetime(2026, 11, 10, tzinfo=timezone.utc) + timedelta(minutes=minutes))


class _Repo:
    def __init__(self, records):
        self.records = records

    async def list_by_negotiation(self, _nid):
        return self.records


def test_buyer_owes_a_reply_after_our_last_e_mail():
    records = [_rec("buyer", EmailDirection.INBOUND, EmailStatus.RECEIVED, 1),
               _rec("buyer", EmailDirection.OUTBOUND, EmailStatus.SENT, 2)]
    with patch.object(svc, "get_email_repository", lambda: _Repo(records)):
        assert run(svc._awaiting_reply_from("N1", "buyer")) is True


def test_buyer_who_answered_does_not_owe_a_reply():
    records = [_rec("buyer", EmailDirection.OUTBOUND, EmailStatus.SENT, 1),
               _rec("buyer", EmailDirection.INBOUND, EmailStatus.RECEIVED, 2),
               _rec("buyer", EmailDirection.OUTBOUND, EmailStatus.REJECTED, 3)]  # a rejected draft never went out
    with patch.object(svc, "get_email_repository", lambda: _Repo(records)):
        assert run(svc._awaiting_reply_from("N1", "buyer")) is False


def test_supplier_reply_while_buyer_owes_one_goes_back_to_the_supplier():
    from agents.negotiation_agent.graph import orchestrator_node
    from tests.test_negotiation_agent import _orch_chat, _session, _state

    session = _session(round_no=2, current_bid=500.0, current_ask=440.0)
    state = {**_state(session, is_first_round=False, party="supplier", current_bid=500.0, current_ask=440.0),
             "buyer_awaiting_reply": True}
    with patch("agents.negotiation_agent.graph._chat", _orch_chat("CALL_BUYER_AGENT", 12.0)):
        result = run(orchestrator_node(state))
    assert result["routing"] == "CALL_SUPPLIER_AGENT"


# 3-5. notice templates ---------------------------------------------------------

def test_cargo_cleared_never_prints_none():
    _s, body = nf._tpl_cargo_cleared({"deal_id": "D1", "buyer_name": "Gulf LLC", "milestone": None})
    assert "None" not in body and "cleared customs at destination" in body


def test_escrow_schedule_is_in_words_not_codes():
    tranches = [SimpleNamespace(tranche_id=f"T{i}", pct=p, trigger=t) for i, (p, t) in enumerate(
        [(0.3, "PO_ISSUED"), (0.4, "QC_PASSED"), (0.2, "CLEARED"), (0.1, "ACCEPTED")], 1)]
    esc = SimpleNamespace(escrow_id="ESC-1", amount=25865.0, currency="USD", provider="tazapay",
                          checkout_url=None, tranches=tranches)
    _s, body = nf._tpl_escrow_opened({"deal_id": "D1", "buyer_name": "Gulf LLC", "pi_id": "PI-1", "escrow": esc})
    for code in ("PO_ISSUED", "QC_PASSED", "CLEARED", "ACCEPTED", "T1:"):
        assert code not in body, code
    assert "30% when the purchase order is placed" in body and "10% when you accept the goods" in body


def test_quantities_have_no_trailing_zero():
    assert nf._qty(20.0) == "20" and nf._qty(20.5) == "20.5" and nf._qty(1250.0) == "1,250"
