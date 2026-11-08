"""Properties every automation leg must have, checked across all of them.

Events are delivered at least once and can arrive after an operator has already
moved the deal, so each ``on_*`` handler must be a no-op when the switch is off
or the deal has moved on. New handlers are picked up automatically.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.domain.events import DomainEvent, EventTopic
from shared.orchestration import autonomy_policy as ap
from shared.orchestration import compliance as cp
from shared.orchestration import continuations as c

HANDLERS = sorted(name for name, fn in inspect.getmembers(c, inspect.iscoroutinefunction) if name.startswith("on_"))
TERMINAL = ["PAID", "CANCELLED", "REFUNDED"]
# Legs that legitimately act after the deal has reached a terminal status.
ALLOWED_ON_TERMINAL = {
    "on_deal_exception",     # the exception is the point, whatever the status
}
RICH_PAYLOAD = {
    "status": "MATCHING", "supplier_id": "SUP-1", "opportunity_id": "OPP-1", "agreed_price": 900.0,
    "quantity_mt": 20.0, "market": "DE", "escrow_id": "ESC-1", "tranche_id": "T1", "milestone": "DELIVERED",
    "pi_id": "PI-1", "po_id": "PO-1", "payout_id": "PAY-1",
}


def _event(deal_id="DEAL-S", payload=None):
    return DomainEvent(topic=EventTopic.DEAL_EXCEPTION, deal_id=deal_id, payload=dict(RICH_PAYLOAD if payload is None else payload))


def _deal(status="PAID", **kw):
    base = {
        "deal_id": "DEAL-S", "status": SimpleNamespace(value=status), "market": "DE", "hs_code": "152000", "parties": [],
        "autonomy_profile": SimpleNamespace(value="L0"), "winning_supplier_id": "SUP-1", "opportunity_id": None,
        "buyer_target_price_usd": 900.0, "match_results": [], "buyer_candidates": [], "quantity_mt": 20.0,
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EVENT_CONTINUATION_ENABLED", "true")
    monkeypatch.setenv("ENRICHMENT_ON_DEAL_CREATED", "false")
    ap.reset_for_tests()
    monkeypatch.setattr(ap, "_mongo_collection", lambda: None)
    cp.reset_for_tests()
    yield
    ap.reset_for_tests()
    cp.reset_for_tests()


@pytest.fixture
def gate():
    with patch("services.hitl_approval.service.create_approval", new=AsyncMock()) as mock:
        yield mock


@pytest.fixture
def published(monkeypatch):
    events: list[DomainEvent] = []

    class _Pub:
        async def publish(self, event):
            events.append(event)

    monkeypatch.setattr("shared.bus.get_publisher", lambda: _Pub())
    return events


def test_handlers_were_discovered():
    assert len(HANDLERS) >= 20 and "on_escrow_funded" in HANDLERS


class TestEveryHandler:
    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "OFF"])
    @pytest.mark.parametrize("name", HANDLERS)
    async def test_kill_switch_stops_it(self, monkeypatch, gate, published, name, value):
        monkeypatch.setenv("EVENT_CONTINUATION_ENABLED", value)
        lookup = AsyncMock(return_value=_deal("MATCHING"))
        with patch("services.deals.service.get_deal", new=lookup):
            await getattr(c, name)(_event())
        lookup.assert_not_awaited()
        gate.assert_not_awaited()
        assert published == []

    @pytest.mark.parametrize("status", TERMINAL)
    @pytest.mark.parametrize("name", [h for h in HANDLERS if h not in ALLOWED_ON_TERMINAL])
    async def test_replay_on_a_finished_deal_opens_no_gate(self, gate, published, name, status):
        with patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal(status))), \
                patch("services.deals.service.list_deals", new=AsyncMock(return_value=[])):
            await getattr(c, name)(_event())
        gate.assert_not_awaited()


class TestStatusHelpers:
    @pytest.mark.parametrize(("status", "trigger", "expected"), [
        ("PO_ISSUED", "PO_ISSUED", True), ("QC_PASSED", "PO_ISSUED", True), ("ESCROW_FUNDED", "PO_ISSUED", False),
        ("PAID", "CLEARED", True), ("DISPUTE", "PO_ISSUED", False), ("PO_ISSUED", "NOT_A_STATUS", False),
    ])
    def test_status_reached(self, status, trigger, expected):
        assert c._status_reached(status, trigger) is expected

    async def test_event_without_a_deal(self):
        assert await c._get_deal_in_status(None, "MATCHING", "stage") is None

    async def test_deal_in_another_status(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(return_value=_deal("PI_SENT"))):
            assert await c._get_deal_in_status("D", "MATCHING", "stage") is None

    async def test_deal_in_required_status(self):
        deal = _deal("MATCHING")
        with patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)):
            assert await c._get_deal_in_status("D", "MATCHING", "stage") is deal


class TestDealHelpers:
    @pytest.mark.parametrize(("qty", "expected"), [
        (12.5, 12.5), ("7", 7.0), (0, c._DEFAULT_QUANTITY_MT), (None, c._DEFAULT_QUANTITY_MT),
        (-1, c._DEFAULT_QUANTITY_MT), ("abc", c._DEFAULT_QUANTITY_MT),
    ])
    def test_deal_quantity(self, qty, expected):
        assert c._deal_quantity(SimpleNamespace(quantity_mt=qty)) == expected

    def _match(self, supplier, score, decision="SOURCE", fob=700.0):
        return SimpleNamespace(supplier_id=supplier, composite_score=score, decision=decision, fob_price_usd=fob, match_id=f"M-{supplier}")

    def test_no_matches(self):
        assert c._winning_match(_deal(match_results=[])) is None

    def test_winner_by_id_beats_score(self):
        low, high = self._match("SUP-1", 0.1), self._match("SUP-2", 0.9)
        assert c._winning_match(_deal(match_results=[high, low])) is low

    def test_best_source_when_winner_absent(self):
        discard, source = self._match("A", 0.9, "DISCARD"), self._match("B", 0.4)
        assert c._winning_match(_deal(winning_supplier_id=None, match_results=[discard, source])) is source

    def test_best_of_all_when_nothing_sourced(self):
        a, b = self._match("A", 0.2, "DISCARD"), self._match("B", 0.5, "DISCARD")
        assert c._winning_match(_deal(winning_supplier_id="ZZZ", match_results=[a, b])) is b

    @pytest.mark.parametrize(("party", "expected"), [
        (SimpleNamespace(role="buyer", email="a@b.c"), "a@b.c"),
        (SimpleNamespace(role=SimpleNamespace(value="BUYER"), email="a@b.c"), "a@b.c"),
        (SimpleNamespace(role="buyer", email=None, contact_email="c@d.e"), "c@d.e"),
        (SimpleNamespace(role="supplier", email="a@b.c"), None),
    ])
    def test_party_email(self, party, expected):
        assert c._party_email(_deal(parties=[party]), "buyer") == expected

    def test_party_email_without_parties(self):
        assert c._party_email(_deal(parties=None), "buyer") is None

    def test_seed_needs_at_least_one_price(self):
        assert c._negotiation_seed(_deal(buyer_target_price_usd=None)) is None

    def test_seed_buyer_target_only_opens_at_parity(self):
        assert c._negotiation_seed(_deal(buyer_target_price_usd=900.0, match_results=[])) == (900.0, 900.0, "DE", "SUP-1")

    def test_seed_supplier_ask_only_marks_up(self):
        seed = c._negotiation_seed(_deal(buyer_target_price_usd=None, match_results=[self._match("SUP-1", 0.5, fob=800.0)]))
        assert seed[0] == pytest.approx(round(800.0 * c._BUYER_MARKUP_OVER_FOB, 2)) and seed[1] == 800.0

    def test_seed_without_any_supplier_id(self):
        assert c._negotiation_seed(_deal(winning_supplier_id=None))[3] == "UNKNOWN"

    def test_seed_both_prices(self):
        deal = _deal(match_results=[self._match("SUP-1", 0.5, fob=700.0)])
        assert c._negotiation_seed(deal) == (900.0, 700.0, "DE", "SUP-1")

    @pytest.mark.parametrize(("price", "target", "floor", "expected"), [
        (828.0, 900.0, 8.0, True), (828.01, 900.0, 8.0, False), (700.0, 900.0, None, True),
        (None, 900.0, 8.0, False), (0, 900.0, 8.0, False), (700.0, 0.0, 8.0, False), ("abc", 900.0, 8.0, False),
    ])
    def test_price_in_corridor(self, price, target, floor, expected):
        assert c._price_in_corridor(price, _deal(buyer_target_price_usd=target, margin_floor_pct=floor)) is expected

    def test_auto_flow_check_failure_means_not_owned(self):
        with patch("shared.orchestration.auto_flow.enabled", side_effect=RuntimeError("x")):
            assert c._auto_flow_owns(_deal()) is False

    async def test_negotiation_numbers_survive_a_repository_failure(self):
        with patch("agents.negotiation_agent.repository.get_repository", side_effect=RuntimeError("down")):
            ask, qty = await c._negotiation_numbers("D", _deal(quantity_mt=33.0))
        assert ask is None and qty == 33.0


class TestGateCreation:
    async def test_policy_failure_defaults_to_always_ask(self, gate):
        with patch("shared.orchestration.autonomy_policy.resolve_gate", side_effect=RuntimeError("store down")):
            await c._create_gate("D", "ISSUE_PO", "s", 100.0, {}, "idem-1", deal=_deal())
        assert gate.await_args.args[0].autonomy_level == "L0"

    def test_gate_level_failure_defaults_to_always_ask(self):
        with patch("shared.orchestration.autonomy_policy.resolve_level", side_effect=RuntimeError("x")):
            assert c._gate_level("ISSUE_PO") == "L0"

    def test_gate_level_uses_the_policy(self):
        with patch("shared.orchestration.autonomy_policy.resolve_level", return_value="L2"):
            assert c._gate_level("DISPATCH_RFQ") == "L2"

    async def test_compliance_hold_forces_a_human_whatever_the_policy(self, gate, monkeypatch):
        monkeypatch.setenv("COMPLIANCE_ENABLED", "true")
        monkeypatch.setenv("AUTONOMY_ISSUE_PO", "L3")
        record = await cp.get_record("D")
        record.holds = ["payer_not_buyer"]
        await cp.save_record(record)
        await c._create_gate("D", "ISSUE_PO", "s", 100.0, {"k": 1}, "idem-1", deal=_deal())
        request = gate.await_args.args[0]
        assert request.autonomy_level == "L0"
        assert "compliance:payer_not_buyer" in request.risk_flags
        assert request.callback_payload == {"k": 1, "compliance_accept": ["payer_not_buyer"]}

    async def test_no_hold_keeps_the_payload_clean(self, gate):
        await c._create_gate("D", "ISSUE_PO", "s", 100.0, {"k": 1}, "idem-1", deal=_deal())
        assert gate.await_args.args[0].callback_payload == {"k": 1}


class TestExceptionPublishing:
    async def test_publish(self, published):
        await c._publish_exception("D", "STAGE", "why", {"x": 1})
        assert published[0].topic == EventTopic.DEAL_EXCEPTION and published[0].deal_id == "D"

    async def test_bus_failure_is_swallowed(self, monkeypatch):
        monkeypatch.setattr("shared.bus.get_publisher", lambda: (_ for _ in ()).throw(RuntimeError("bus")))
        await c._publish_exception("D", "STAGE", "why", {})

    async def test_best_effort_returns_the_result(self):
        async def ok():
            return 7

        assert await c._best_effort("S", "D", ok()) == 7

    async def test_best_effort_swallows_and_reports(self, published):
        async def boom():
            raise RuntimeError("x")

        assert await c._best_effort("S", "D", boom()) is None
