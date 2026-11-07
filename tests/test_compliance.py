"""Trade compliance (shared/orchestration/compliance.py) and where it stops a deal.

  checks        → HS export policy (seed + operator confirmation), destination,
                  unscreened party, related parties, DGFT denied / new supplier,
                  off-market price / spread, payer, loading + seal
  continuations → deal.created block stops outreach + RFQ; PI withheld on block,
                  CONFIRM_TRADE_COMPLIANCE on review; payer hold → PO gate L0;
                  QC passed waits for the seal; seal mismatch → exception
  callbacks     → CONFIRM_TRADE_COMPLIANCE accepts findings and issues the PI;
                  approving a held gate accepts its review flags
  API           → loading record endpoint, HS-policy confirmation
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared import pricebook
from shared.domain.events import DomainEvent, EventTopic
from shared.orchestration import autonomy_policy as ap
from shared.orchestration import compliance as cp
from shared.orchestration import continuations as c

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EVENT_CONTINUATION_ENABLED", "true")
    monkeypatch.setenv("ENRICHMENT_ON_DEAL_CREATED", "false")
    monkeypatch.setenv("COMPLIANCE_ENABLED", "true")
    monkeypatch.setenv("COMPLIANCE_LOADING_CHECK_REQUIRED", "true")
    for v in ("COMPLIANCE_UNCHECKED_SCREENING", "COMPLIANCE_BLOCKED_COUNTRIES", "COMPLIANCE_REVIEW_COUNTRIES"):
        monkeypatch.delenv(v, raising=False)
    ap.reset_for_tests()
    monkeypatch.setattr(ap, "_mongo_collection", lambda: None)
    for cls in ap.ACTION_CLASSES:
        monkeypatch.delenv(f"AUTONOMY_{cls.action}", raising=False)
    cp.reset_for_tests()
    pricebook.reset_for_tests()
    monkeypatch.setattr(pricebook, "public_priors", AsyncMock(return_value={}))
    monkeypatch.setattr("services.sourcing.service.list_quotes", AsyncMock(return_value=[]))
    yield
    cp.reset_for_tests()
    pricebook.reset_for_tests()
    ap.reset_for_tests()


@pytest.fixture
def published(monkeypatch):
    events: list[DomainEvent] = []

    class _Pub:
        async def publish(self, event):
            events.append(event)

    monkeypatch.setattr("shared.bus.get_publisher", lambda: _Pub())
    return events


def _party(role, **kw):
    base = dict(
        role=role, legal_name="Glaconchemie GmbH" if role == "buyer" else "Swadesh India Chemical Pvt Ltd",
        country="DE" if role == "buyer" else "IN",
        email="buy@glacon.de" if role == "buyer" else "sales@swadesh.in",
        website=None, phone=None, address=None,
        sanctions_status="clear", sanctions_reason=None, sanctions_checked_at=NOW,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _deal(status="NEGOTIATION_AGREED", **kw):
    base = dict(
        deal_id="DEAL-C", status=SimpleNamespace(value=status), market="DE", hs_code="152000",
        autonomy_profile=SimpleNamespace(value="L0"), winning_supplier_id="SUP-1", opportunity_id=None,
        buyer_target_price_usd=900.0, match_results=[], parties=[_party("buyer"), _party("supplier")],
        buyer_candidates=[], quantity_mt=20.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


async def _confirm_free(hs="1520"):
    await cp.set_hs_policy(cp.HsPolicyEntry(hs_code=hs, status="free", confirmed_by="ops"))


def _neg_repo(ask=700.0, qty=20.0):
    repo = AsyncMock()
    repo.get_session_by_deal = AsyncMock(return_value=SimpleNamespace(current_ask=ask, quantity_mt=qty))
    return repo


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

class TestChecks:
    @pytest.mark.asyncio
    async def test_hs_policy_seed_and_confirmation(self):
        assert (await cp.check_hs("930190")).severity == "block"          # arms
        assert (await cp.check_hs("2844.10")).code == "hs_scomet"          # radioactive
        assert (await cp.check_hs("440310")).code == "hs_prohibited"       # logs
        assert (await cp.check_hs("100630")).severity == "review"          # rice
        assert (await cp.check_hs("152000")).code == "hs_unverified"
        await _confirm_free("1520")                                         # parent covers 6-digit
        assert (await cp.check_hs("152000")).severity == "info"

    def test_destination(self):
        assert cp.check_destination("IR").severity == "block"
        assert cp.check_destination("North Korea").code == "destination_embargoed"
        assert cp.check_destination("RU").severity == "review"
        assert cp.check_destination("DE") is None

    def test_unchecked_screening_is_not_clear(self, monkeypatch):
        deal = _deal(parties=[_party("buyer", sanctions_status="unchecked"), _party("supplier", sanctions_status=None)])
        codes = {f.code for f in cp.check_screening(deal)}
        assert codes == {"buyer_screening_unchecked", "supplier_screening_unchecked"}
        monkeypatch.setenv("COMPLIANCE_UNCHECKED_SCREENING", "allow")
        assert cp.check_screening(deal) == []

    def test_related_parties(self):
        assert cp.check_related_parties(_deal()) is None
        same = _deal(parties=[_party("buyer", email="a@acme-group.com"),
                              _party("supplier", email="b@acme-group.com")])
        assert "same company domain" in cp.check_related_parties(same).message
        gmail = _deal(parties=[_party("buyer", email="a@gmail.com"), _party("supplier", email="b@gmail.com")])
        assert cp.check_related_parties(gmail) is None
        phone = _deal(parties=[_party("buyer", phone="+49 151 2345 6789"), _party("supplier", phone="0151-23456789")])
        assert "same phone number" in cp.check_related_parties(phone).message

    @pytest.mark.asyncio
    async def test_supplier_denied_and_new(self, monkeypatch):
        v = SimpleNamespace(dgft_denied=True, business_age_years=0.4)
        monkeypatch.setattr("services.sourcing.service.list_quotes", AsyncMock(
            return_value=[SimpleNamespace(supplier_id="SUP-1", verification=v)]))
        codes = {f.code: f.severity for f in await cp.check_supplier_record(_deal())}
        assert codes == {"supplier_dgft_denied": "block", "supplier_new_company": "review"}

    @pytest.mark.asyncio
    async def test_prices_off_market_and_spread(self):
        for p in (880.0, 900.0, 920.0):
            await pricebook.record(pricebook.PriceObservation(
                hs_code="152000", side="buyer", kind="buyer_agreed", market="DE", price_usd_per_mt=p, deal_id="OTHER"))
        # this deal's own observation is ignored
        await pricebook.record(pricebook.PriceObservation(
            hs_code="152000", side="buyer", kind="buyer_agreed", market="DE", price_usd_per_mt=2000.0, deal_id="DEAL-C"))
        found = {f.code for f in await cp.check_prices(_deal(), buyer_price=1900.0, supplier_price=700.0)}
        assert found == {"buyer_price_off_market", "spread_too_wide"}
        assert await cp.check_prices(_deal(), buyer_price=905.0, supplier_price=700.0) == []

    @pytest.mark.asyncio
    async def test_payer(self):
        deal = _deal()
        assert (await cp.check_payer(deal, {"payer_name": "Glaconchemie GmbH", "payer_country": "DE"})).status == "clear"
        r = await cp.check_payer(deal, {"payer_name": "Offshore Holdings Ltd", "payer_country": "AE"})
        assert {f.code for f in r.findings} == {"payer_not_buyer", "payer_country_mismatch"}
        assert "compliance:payer_not_buyer" in await cp.hold_flags("DEAL-C")
        assert (await cp.check_payer(deal, {})).findings[0].code == "payer_unknown"

    @pytest.mark.asyncio
    async def test_shipment_and_seal(self):
        deal = _deal("QC_PASSED")
        assert (await cp.check_shipment(deal)).blocks[0].code == "loading_not_verified"
        po = SimpleNamespace(quantity_mt=20.0)
        with patch.object(cp, "_pi_po", AsyncMock(return_value=(po, po))):
            await cp.record_loading("DEAL-C", cp.LoadingRecord(
                container_numbers=["msku1234565"], seal_numbers=["sgs-778899"], loaded_quantity_mt=20.1,
                hs_code="152000", photos=["https://x/seal.jpg"]))
            assert (await cp.check_shipment(deal)).status == "clear"
            await cp.record_loading("DEAL-C", cp.LoadingRecord(
                container_numbers=["MSKU1234565"], seal_numbers=["SGS-778899"], loaded_quantity_mt=14.0,
                hs_code="930190", photos=["p"]))
            codes = {f.code: f.severity for f in (await cp.check_shipment(deal)).findings}
        assert codes == {"shipment_hs_mismatch": "block", "shipment_qty_mismatch": "review"}
        assert await cp.check_seal("DEAL-C", ["sgs-778899"]) is None
        assert (await cp.check_seal("DEAL-C", ["OTHER-1"])).code == "seal_mismatch"
        assert "compliance:seal_mismatch" in await cp.hold_flags("DEAL-C")


# ---------------------------------------------------------------------------
# Continuations
# ---------------------------------------------------------------------------

class TestDealCreated:
    @pytest.mark.asyncio
    async def test_prohibited_product_stops_outreach_and_rfq(self, published):
        deal = _deal("MATCHING", hs_code="930190")
        gate, outreach = AsyncMock(), AsyncMock()
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch("shared.orchestration.outreach.start_outreach", new=outreach),
        ):
            await c.on_deal_created(DomainEvent(topic=EventTopic.DEAL_CREATED, deal_id="DEAL-C",
                                                payload={"status": "MATCHING"}))
        gate.assert_not_awaited()
        outreach.assert_not_awaited()
        exc = [e for e in published if e.topic == EventTopic.DEAL_EXCEPTION]
        assert exc and exc[0].payload["compliance"] == "block"


class TestBeforePi:
    async def _agreed(self, deal, issue, gate):
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),
            patch("services.dealmaker.service.issue_pi", new=issue),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch("shared.orchestration.enrichment.enrich_deal_parties", new=AsyncMock(return_value=None)),
            patch("agents.negotiation_agent.repository.get_repository", return_value=_neg_repo()),
        ):
            await c.on_negotiation_agreed(DomainEvent(
                topic=EventTopic.NEGOTIATION_AGREED, deal_id="DEAL-C", payload={"agreed_price": 900.0}))

    @pytest.mark.asyncio
    async def test_clear_issues_pi(self):
        await _confirm_free()
        issue, gate = AsyncMock(return_value=SimpleNamespace(pi_id="PI-1")), AsyncMock()
        await self._agreed(_deal(), issue, gate)
        issue.assert_awaited_once()
        gate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unverified_hs_asks_and_approve_issues_pi(self):
        issue, gate = AsyncMock(return_value=SimpleNamespace(pi_id="PI-1")), AsyncMock()
        deal = _deal()
        await self._agreed(deal, issue, gate)
        issue.assert_not_awaited()
        req = gate.await_args.args[0]
        assert req.action == "CONFIRM_TRADE_COMPLIANCE" and req.autonomy_level == "L0"
        assert req.callback_payload["codes"] == ["hs_unverified"]

        from services.hitl_approval.callbacks import execute_callback
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),
            patch("services.dealmaker.service.issue_pi", new=issue),
            patch("agents.negotiation_agent.repository.get_repository", return_value=_neg_repo()),
        ):
            out = await execute_callback("CONFIRM_TRADE_COMPLIANCE", "DEAL-C",
                                         {**req.callback_payload, "resolved_by": "ops"}, "APR-1")
        assert out["status"] == "pi_issued"
        issue.assert_awaited_once()
        assert (await cp.hs_policy_status("152000"))[0] == "free"   # remembered for the next deal

    @pytest.mark.asyncio
    async def test_embargoed_buyer_country_withholds_pi(self, published):
        await _confirm_free()
        issue, gate = AsyncMock(), AsyncMock()
        await self._agreed(_deal(parties=[_party("buyer", country="IR"), _party("supplier")]), issue, gate)
        issue.assert_not_awaited()
        gate.assert_not_awaited()
        assert any(e.payload.get("compliance") == "block" for e in published if e.topic == EventTopic.DEAL_EXCEPTION)


class TestHolds:
    @pytest.mark.asyncio
    async def test_payer_hold_forces_po_gate_to_l0_and_approve_accepts(self, monkeypatch):
        monkeypatch.setenv("AUTONOMY_ISSUE_PO", "L2")
        deal = _deal("ESCROW_FUNDED")
        gate = AsyncMock()
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch("agents.negotiation_agent.repository.get_repository", return_value=_neg_repo()),
        ):
            await c.on_escrow_funded(DomainEvent(topic=EventTopic.ESCROW_FUNDED, deal_id="DEAL-C",
                                                 payload={"payer_name": "Someone Else FZE"}))
        req = gate.await_args.args[0]
        assert req.action == "ISSUE_PO" and req.autonomy_level == "L0"
        assert "compliance:payer_not_buyer" in req.risk_flags
        assert req.callback_payload["compliance_accept"] == ["payer_not_buyer"]

        from services.hitl_approval.callbacks import execute_callback
        with patch("services.dealmaker.service.issue_po", new=AsyncMock(return_value=SimpleNamespace(po_id="PO-1"))):
            await execute_callback("ISSUE_PO", "DEAL-C", req.callback_payload, "APR-2")
        assert await cp.hold_flags("DEAL-C") == []

    @pytest.mark.asyncio
    async def test_qc_passed_waits_for_seal_then_loading_gates_freight(self):
        deal = _deal("QC_PASSED")
        gate = AsyncMock()
        po = SimpleNamespace(quantity_mt=20.0)
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch.object(c, "_generate_documents", AsyncMock(return_value=[])),
            patch.object(c, "_tranche_gate", AsyncMock()),
            patch.object(cp, "_pi_po", AsyncMock(return_value=(po, po))),
        ):
            await c.on_qc_passed(DomainEvent(topic=EventTopic.QC_PASSED, deal_id="DEAL-C", payload={}))
            assert not [a for a in gate.await_args_list if a.args[0].action == "BOOK_FREIGHT"]
            await cp.record_loading("DEAL-C", cp.LoadingRecord(
                container_numbers=["MSKU1234565"], seal_numbers=["SGS-1"], loaded_quantity_mt=20.0,
                hs_code="152000", photos=["p"]))
            await c.on_loading_recorded(DomainEvent(topic=EventTopic.LOADING_RECORDED, deal_id="DEAL-C", payload={}))
        freight = [a.args[0] for a in gate.await_args_list if a.args[0].action == "BOOK_FREIGHT"]
        assert freight and freight[0].callback_payload["seal_numbers"] == ["SGS-1"]

    @pytest.mark.asyncio
    async def test_qc_webhook_with_wrong_hs_raises_exception(self, published):
        deal = _deal("QC_PASSED")
        gate = AsyncMock()
        with (
            patch("services.deals.service.get_deal", new=AsyncMock(return_value=deal)),
            patch("services.hitl_approval.service.create_approval", new=gate),
            patch.object(c, "_generate_documents", AsyncMock(return_value=[])),
            patch.object(cp, "_pi_po", AsyncMock(return_value=(None, None))),
        ):
            await c.on_qc_passed(DomainEvent(topic=EventTopic.QC_PASSED, deal_id="DEAL-C", payload={
                "container_numbers": ["C1"], "seal_numbers": ["S1"], "loaded_hs_code": "2844", "photos": ["p"],
                "loaded_quantity_mt": 20.0}))
        gate.assert_not_awaited()
        assert any(e.payload.get("action_required") == "stop_shipment_and_reinspect"
                   for e in published if e.topic == EventTopic.DEAL_EXCEPTION)

    @pytest.mark.asyncio
    async def test_bl_seal_mismatch_raises_exception(self, published):
        await cp.record_loading("DEAL-C", cp.LoadingRecord(container_numbers=["C1"], seal_numbers=["S1"]))
        await c.on_freight_milestone(DomainEvent(topic=EventTopic.FREIGHT_MILESTONE, deal_id="DEAL-C",
                                                 payload={"milestone": "DEPARTED", "seal_numbers": ["S9"]}))
        assert any(e.payload.get("stage") == "FREIGHT_MILESTONE→SEAL_MISMATCH"
                   for e in published if e.topic == EventTopic.DEAL_EXCEPTION)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class TestApi:
    @pytest.mark.asyncio
    async def test_loading_endpoint_records_and_emits(self, published):
        from services.shield_link.schemas import LoadingRecordRequest
        from services.shield_link.service import record_loading
        with patch("services.shield_link.service._fetch_deal", new=AsyncMock(return_value=_deal("QC_PASSED"))), \
             patch("services.shield_link.service.get_publisher", return_value=SimpleNamespace(
                 publish=AsyncMock(side_effect=lambda e: published.append(e)))):
            out = await record_loading("DEAL-C", LoadingRecordRequest(
                container_numbers=["msku1"], seal_numbers=["s-1"], loaded_quantity_mt=20.0))
        assert out["loading"]["seal_numbers"] == ["S-1"]
        assert (await cp.get_record("DEAL-C")).loading.container_numbers == ["MSKU1"]
        assert any(e.topic == EventTopic.LOADING_RECORDED for e in published)
