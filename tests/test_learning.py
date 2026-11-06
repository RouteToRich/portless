"""Knowledge-service memory (RAG via the gateway) and the outcome feedback loop."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import services.deals.service  # noqa: F401
from services.deals.repository import InMemoryFallbackRepository
from services.deals.schemas import BuyerCandidate, BuyerRequirement, DealRecord, DealStatus, SupplierOffer
from shared import knowledge, pricebook
from shared.orchestration import matchmaking as mm
from shared.orchestration import memory, outcomes

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("OUTCOMES_MONGO", "false")
    monkeypatch.setenv("PRICEBOOK_MONGO", "false")
    monkeypatch.setenv("ENRICHMENT_ON_DEAL_CREATED", "false")
    outcomes.reset_for_tests()
    pricebook.reset_for_tests()
    knowledge.reset_for_tests()
    yield
    outcomes.reset_for_tests()
    knowledge.reset_for_tests()


@pytest.fixture
def deals_repo(monkeypatch):
    repo = InMemoryFallbackRepository()
    monkeypatch.setattr("services.deals.repository._repository", repo)
    return repo


def _deal(**kw):
    base = dict(deal_id="DEAL-L1", operator_id="op", org_id="portless", hs_code="290543", market="ES",
                status=DealStatus.MATCHING, created_at=NOW, updated_at=NOW, quantity_mt=25.0)
    base.update(kw)
    return DealRecord(**base)


# ---------------------------------------------------------------------------
class TestKnowledgeClient:
    def test_off_without_gateway(self):
        assert knowledge.enabled() is False  # conftest pins LLM_GATEWAY_URL=""

    @pytest.mark.asyncio
    async def test_ingest_and_retrieve_go_through_the_gateway_route(self, monkeypatch):
        from shared.llm_gateway.config import llm_gateway_settings as s
        from shared.llm_gateway.remote import reset_remote_client
        claims = base64.urlsafe_b64encode(json.dumps({"sub": "svc-user-1"}).encode()).decode().rstrip("=")
        monkeypatch.setattr(s, "gateway_url", "https://gw.example")
        monkeypatch.setattr(s, "gateway_token", f"h.{claims}.sig")
        monkeypatch.setattr(s, "gateway_account_id", "acct-portless")
        reset_remote_client()
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path.endswith("/retrieval"):
                return httpx.Response(200, json={"results": [
                    {"text": "Supplier Almsgroup failed QC on moisture", "metadata": {"kind": "note"}},
                    {"text": "Almsgroup — supplies Spain", "source": "knowledge-graph"}]})
            return httpx.Response(200, json={"document_id": "d1"})

        monkeypatch.setattr(knowledge, "_client", httpx.AsyncClient(
            base_url=knowledge._base_url(), transport=httpx.MockTransport(handler),
            headers={"Authorization": f"Bearer {s.gateway_token}"}))
        try:
            assert knowledge.enabled() is True
            await knowledge.ingest_text("hello", path="notes/supplier/almsgroup.in/x.txt", metadata={"kind": "note"})
            up = seen[0]
            assert str(up.url) == "https://gw.example/api/knowledge/api/v1/ingestion/upload"
            body = up.content.decode("utf-8", "ignore")
            assert "portless-trade" in body and "svc-user-1" in body and '"app": "portless"' in body
            assert '"account_id": "acct-portless"' in body  # every document names PortLess's account
            results = await knowledge.recall("almsgroup quality")
            assert knowledge.is_graph_fact(results[0])  # graph facts first
            ctx = knowledge.as_context(results)
            assert "[note] Supplier Almsgroup failed QC" in ctx
        finally:
            reset_remote_client()

    @pytest.mark.asyncio
    async def test_recall_drops_other_accounts_and_apps(self, monkeypatch):
        from shared.llm_gateway.config import llm_gateway_settings as s
        monkeypatch.setattr(s, "gateway_account_id", "acct-portless")
        monkeypatch.setattr(knowledge, "enabled", lambda: True)
        monkeypatch.setattr(knowledge, "retrieve", AsyncMock(return_value=[
            {"text": "ours", "metadata": {"app": "portless", "account_id": "acct-portless"}},
            {"text": "other account", "metadata": {"app": "portless", "account_id": "acct-makemerich"}},
            {"text": "other app", "metadata": {"app": "makemerich"}},
            {"text": "graph fact", "source": "knowledge-graph"},
        ]))
        texts = [r["text"] for r in await knowledge.recall("glycerine")]
        assert texts == ["graph fact", "ours"]

    @pytest.mark.asyncio
    async def test_recall_never_raises(self, monkeypatch):
        monkeypatch.setattr(knowledge, "enabled", lambda: True)
        monkeypatch.setattr(knowledge, "retrieve", AsyncMock(side_effect=httpx.ConnectError("down")))
        assert await knowledge.recall("anything") == []


class TestMemory:
    @pytest.mark.asyncio
    async def test_email_is_tagged_with_the_deal_from_its_subject_token(self, deals_repo, monkeypatch):
        await deals_repo.save(_deal())
        monkeypatch.setattr(knowledge, "enabled", lambda: True)
        remembered = []
        monkeypatch.setattr(knowledge, "remember", lambda text, path, metadata, **kw: remembered.append((text, path, metadata)))
        await memory.remember_email(direction="inbound", from_address="maria@degrados.es", to_address="ops@portless.io",
                                    subject="Re: Glycerine [BUY:DEAL-L1:degrados.es]", body="We need 40 MT CIF Valencia.",
                                    message_id="<m1@x>")
        text, path, meta = remembered[0]
        assert path.startswith("emails/DEAL-L1/") and meta["kind"] == "email" and meta["direction"] == "inbound"
        assert meta["deal_id"] == "DEAL-L1" and meta["hs_code"] == "290543" and meta["counterparty_id"] == "degrados.es"
        assert "40 MT CIF Valencia" in text

    @pytest.mark.asyncio
    async def test_context_is_empty_when_off(self):
        assert await memory.context_for(purpose="negotiation", hs_code="290543") == ""


# ---------------------------------------------------------------------------
def _row(p, result, **kw):
    return outcomes.Outcome(deal_id=kw.pop("deal_id", "D"), candidate_id=kw.pop("candidate_id", "b"),
                            predicted_probability=p, decision="approved", result=result, **kw)


class TestCalibration:
    def test_table_and_calibration(self, monkeypatch):
        monkeypatch.setenv("MATCH_CALIBRATION_MIN", "20")
        rows = [_row(0.9, "signed" if i < 5 else "failed") for i in range(25)]  # predicted 90 %, 20 % signed
        table = outcomes.calibration_table(rows)
        assert table["active"] and table["n_resolved"] == 25
        top = table["bins"][-1]
        assert top["n"] == 25 and top["observed_rate"] == 0.2
        cal = outcomes.calibrate(0.9, table)
        assert 0.2 < cal < 0.5  # pulled hard toward reality
        assert outcomes.calibrate(0.3, table) == 0.3  # empty band: unchanged

    def test_inactive_below_minimum(self):
        assert outcomes.calibrate(0.9, outcomes.calibration_table([_row(0.9, "failed")])) == 0.9

    def test_track_records(self):
        rows = [_row(0.5, "signed", candidate_id="good", supplier_id="s1", completion="completed"),
                _row(0.5, "failed", candidate_id="flaky", supplier_id="s2"),
                _row(0.5, "failed", candidate_id="flaky", supplier_id="s2"),
                _row(0.5, "signed", candidate_id="x", supplier_id="s3", completion="disputed")]
        rec = outcomes.track_records(rows)
        assert rec["buyer:good"]["factor"] == 1.0 and rec["buyer:flaky"]["factor"] == 0.5
        assert rec["supplier:s1"]["factor"] == 1.0 and rec["supplier:s3"]["factor"] < 1.0

    def test_offer_pricing_nudge(self):
        rows = [outcomes.Outcome(kind="indicative_offer", deal_id="D", candidate_id=str(i), response="accepted")
                for i in range(6)]
        assert outcomes.offer_pricing(rows)["price_nudge"] == 1.03

    def test_autonomy_evidence(self):
        assert outcomes.autonomy_evidence([])["CONFIRM_MATCH"]["recommended_level"] == "L0"
        rows = [_row(0.7, "signed" if i % 5 else "failed") for i in range(35)]  # 80 % sign at p=0.7
        rows += [outcomes.Outcome(deal_id="D", candidate_id="r", predicted_probability=0.7, decision="rejected",
                                  result="not_pursued")]
        ev = outcomes.autonomy_evidence(rows, threshold=0.45)["CONFIRM_MATCH"]
        assert ev["decided"] == 36 and ev["resolved"] == 35 and ev["recommended_level"] == "L1"


# ---------------------------------------------------------------------------
class TestLoop:
    @pytest.mark.asyncio
    async def test_proposal_decision_signed_completed(self, deals_repo):
        pair = SimpleNamespace(candidate_id="a", offer_id="OFR-s1", buyer_name="A", supplier_id="s1",
                               supplier_name="S1", probability=0.62, probability_raw=0.62, calibrated=False,
                               p_margin=0.9, spec_fit=1.0, engagement=0.85, reliability=1.0, timing_fit=1.0,
                               margin_pct=11.0, buyer_price=SimpleNamespace(value=950.0, basis="stated"),
                               supplier_price=SimpleNamespace(value=650.0), quantity_mt=25)
        deal = _deal()
        await deals_repo.save(deal)
        await outcomes.record_proposal(deal, pair, "APPR-1")
        await outcomes.mark_decision("DEAL-L1", "a", "OFR-s1", "approved")
        await outcomes.on_status(deal, "PI_ACCEPTED")
        await outcomes.on_status(deal, "PAID")
        (o,) = await outcomes.all_outcomes(kind="match")
        assert (o.decision, o.result, o.completion, o.predicted_probability) == ("approved", "signed", "completed", 0.62)

    @pytest.mark.asyncio
    async def test_status_change_on_the_deal_feeds_the_tracker(self, deals_repo, monkeypatch):
        from services.deals.schemas import UpdateStatusRequest
        from services.deals.service import update_status
        monkeypatch.setattr("services.deals.service._emit_event", AsyncMock())
        await deals_repo.save(_deal(status=DealStatus.NEGOTIATING))
        await outcomes._save(outcomes.Outcome(deal_id="DEAL-L1", candidate_id="a", offer_id="o",
                                              predicted_probability=0.5, decision="approved"))
        await update_status("DEAL-L1", UpdateStatusRequest(target_status=DealStatus.NEGOTIATION_FAILED,
                                                           reason="buyer walked away"))
        (o,) = await outcomes.all_outcomes(kind="match")
        assert o.result == "failed" and o.result_reason == "buyer walked away"

    @pytest.mark.asyncio
    async def test_board_uses_calibration_and_track_records(self, deals_repo, monkeypatch):
        monkeypatch.setattr(pricebook, "public_priors", AsyncMock(return_value={"buyer": 900.0, "supplier": 700.0}))
        for i in range(25):  # history: the board said ~45 % but only 20 % signed; buyer "a" failed twice
            await outcomes._save(_row(0.45, "signed" if i < 5 else "failed",
                                      candidate_id="a" if i in (10, 11) else f"c{i}"))
        cand = BuyerCandidate(candidate_id="a", legal_name="A", country="ES", email="a@a.es", status="interested",
                              requirement=BuyerRequirement(target_price_usd_per_mt=1000.0, target_price_basis="stated"))
        offer = SupplierOffer(offer_id="OFR-s1", supplier_id="s1", fob_price_usd_per_mt=600.0, gst_verified=True,
                              iec_active=True, received_at=NOW)
        await deals_repo.save(_deal(buyer_candidates=[cand], supplier_offers=[offer]))
        board = await mm.build_board(await deals_repo.get("DEAL-L1"))
        p = board.pairs[0]
        assert p.calibrated and p.probability < p.probability_raw
        assert p.buyer_history and p.buyer_history["failed"] == 2
        assert board.learning["calibration_active"] is True
