"""DocuGen storage and party data, Shield-Link failure paths, remediation playbooks, and Sourcing helpers."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.docugen import service as dg
from services.shield_link import repository as sl_repo
from services.shield_link import service as sl
from services.shield_link.schemas import FreightMilestonePayload, ThirdPartyOrderRecord
from services.sourcing import service as sc
from shared.connectors.google_drive import GoogleDriveError
from shared.domain.enums import PartyRole
from shared.domain.models import Party
from shared.orchestration import playbooks as pl
from tests.factories import make


def _party(role=PartyRole.BUYER, **kw):
    return Party(**{"role": role, "legal_name": "Acme Oils Ltd", "country": "ES", **kw})


class TestDocuGenParties:
    def test_legal_name_is_used(self):
        assert dg._party_name(SimpleNamespace(parties=[_party()]), "buyer") == "Acme Oils Ltd"

    def test_duck_typed(self):
        deal = SimpleNamespace(parties=[SimpleNamespace(role="BUYER", name=None, company_name="C", address="A", vat_number="V")])
        assert (dg._party_name(deal, "buyer"), dg._party_address(deal, "buyer"), dg._party_vat(deal, "buyer")) == ("C", "A", "V")

    def test_no_party(self):
        deal = SimpleNamespace(parties=[])
        assert dg._party_name(deal, "buyer") is None and dg._party_address(deal, "buyer") is None and dg._party_vat(deal, "buyer") is None

    def test_other_role(self):
        deal = SimpleNamespace(parties=[SimpleNamespace(role="supplier", name="S", address="A", vat_number="V")])
        assert dg._party_name(deal, "buyer") is None and dg._party_address(deal, "buyer") is None and dg._party_vat(deal, "buyer") is None

    @pytest.mark.parametrize(("value", "expected"), [("IEC1", "IEC1"), ("", None)])
    def test_iec_comes_from_settings(self, monkeypatch, value, expected):
        monkeypatch.setattr(dg.settings, "portless_entity_iec", value)
        assert dg._party_iec(None) == expected


class TestDocuGenStorage:
    async def test_fetch_not_found(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(side_effect=RuntimeError("Deal Not Found"))), pytest.raises(dg.DealNotFoundError):
            await dg._fetch_deal("D")

    async def test_fetch_other_error(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(side_effect=RuntimeError("mongo"))), pytest.raises(RuntimeError):
            await dg._fetch_deal("D")

    def test_storage_fallback(self, monkeypatch, tmp_path):
        monkeypatch.setattr(dg.settings, "docugen_storage_dir", str(tmp_path / "x"))
        real, calls = Path.mkdir, {"n": 0}

        def flaky(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError
            return real(self, *a, **k)

        with patch.object(Path, "mkdir", flaky):
            assert dg._storage_dir().name == "docugen"

    @pytest.mark.parametrize(("result", "uri"), [({"web_view_link": "L", "file_id": "F"}, "L"), ({"web_view_link": "", "file_id": "F"}, "gdrive://F")])
    async def test_drive(self, result, uri):
        drive = MagicMock(is_configured=MagicMock(return_value=True), upload_document=MagicMock(return_value=result))
        with patch.object(dg, "get_drive_connector", return_value=drive):
            assert await dg._write_document("D", "a.html", "x") == uri

    async def test_drive_failure_goes_to_disk(self, monkeypatch, tmp_path):
        monkeypatch.setattr(dg.settings, "docugen_storage_dir", str(tmp_path))
        drive = MagicMock(is_configured=MagicMock(return_value=True), upload_document=MagicMock(side_effect=GoogleDriveError("x")))
        with patch.object(dg, "get_drive_connector", return_value=drive):
            assert (await dg._write_document("D", "a.html", "x")).startswith("file:")

    async def test_emit_failure_is_contained(self):
        with patch.object(dg, "get_publisher", side_effect=RuntimeError):
            await dg._emit(next(iter(dg.EventTopic)), "D", {})

    async def test_no_uri(self):
        with pytest.raises(FileNotFoundError, match="not rendered"):
            await dg.load_stored_document(None)

    async def test_drive_document(self):
        drive = MagicMock(is_configured=MagicMock(return_value=True), download_file=MagicMock(return_value=(b"x", "text/html")))
        with patch.object(dg, "get_drive_connector", return_value=drive):
            assert await dg.load_stored_document("gdrive://F") == (b"x", "text/html")

    async def test_drive_document_without_credentials(self):
        with patch.object(dg, "get_drive_connector", return_value=MagicMock(is_configured=MagicMock(return_value=False))), pytest.raises(FileNotFoundError, match="credentials"):
            await dg.load_stored_document("gdrive://F")

    async def test_local_file(self, tmp_path):
        path = tmp_path / "a.html"
        path.write_text("x")
        assert await dg.load_stored_document(path.as_uri()) == (b"x", "text/html")

    async def test_local_file_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="missing"):
            await dg.load_stored_document((tmp_path / "nope.html").as_uri())

    async def test_unknown_scheme(self):
        with pytest.raises(FileNotFoundError, match="Unrecognized"):
            await dg.load_stored_document("s3://bucket/key")


# ---------------------------------------------------------------------------
# Shield-Link
# ---------------------------------------------------------------------------

@pytest.fixture
def orders(monkeypatch):
    repo = sl_repo.InMemoryShieldLinkRepository()
    monkeypatch.setattr(sl, "get_repository", lambda: repo)
    monkeypatch.setattr(sl, "_emit", AsyncMock())
    return repo


async def _order(repo, **kw):
    record = make(ThirdPartyOrderRecord, **{"tpo_id": "T1", "deal_id": "D1", "order_type": "FREIGHT", "ext_ref": "F1", "provider": "cogoport", "status": "BOOKED", **kw})
    await repo.save(record)
    return record


class TestShieldHelpers:
    async def test_fetch(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(return_value="d")):
            assert await sl._fetch_deal("D") == "d"

    async def test_fetch_not_found(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(side_effect=RuntimeError("not found"))), pytest.raises(sl.DealNotFoundError):
            await sl._fetch_deal("D")

    async def test_fetch_other(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(side_effect=RuntimeError("mongo"))), pytest.raises(RuntimeError):
            await sl._fetch_deal("D")

    async def test_transition(self):
        with patch("services.deals.service.update_status", new=AsyncMock()) as update:
            await sl._transition_deal("D", "CLEARED", "r")
        update.assert_awaited_once()

    async def test_transition_failure(self):
        with patch("services.deals.service.update_status", new=AsyncMock(side_effect=RuntimeError)), pytest.raises(RuntimeError):
            await sl._transition_deal("D", "CLEARED", "r")

    async def test_emit_failure(self):
        with patch.object(sl, "get_publisher", side_effect=RuntimeError):
            await sl._emit(sl.EventTopic.QC_PASSED, "D", {})

    async def test_buyer_choice_lookup_failure(self):
        with patch("agents.negotiation_agent.repository.get_repository", side_effect=RuntimeError):
            assert await sl._buyer_opted_qc_provider("D") is None


class TestShieldFreight:
    async def test_unknown_reference(self, orders):
        with pytest.raises(sl.TpoNotFoundError):
            await sl.handle_freight_milestone("x", make(FreightMilestonePayload, ext_ref="x"), {}, b"")

    @pytest.mark.parametrize("verify", [AsyncMock(return_value=False), AsyncMock(side_effect=RuntimeError)])
    async def test_bad_signature(self, orders, verify):
        await _order(orders)
        with patch.object(sl, "get_freight_connector", return_value=MagicMock(webhook_verify=verify)), pytest.raises(sl.WebhookVerifyError):
            await sl.handle_freight_milestone("F1", make(FreightMilestonePayload, ext_ref="F1"), {}, b"")

    async def test_record_milestone_unknown(self, orders):
        with pytest.raises(sl.TpoNotFoundError):
            await sl.record_milestone("x", MagicMock())

    async def test_record_milestone_on_a_qc_order(self, orders):
        await _order(orders, order_type="QC")
        with pytest.raises(sl.TpoNotFoundError):
            await sl.record_milestone("T1", MagicMock())

    async def test_get_tpo(self, orders):
        await _order(orders)
        assert (await sl.get_tpo("T1")).tpo_id == "T1"
        with pytest.raises(sl.TpoNotFoundError):
            await sl.get_tpo("x")


class TestShieldTracking:
    async def test_no_reference(self, orders):
        await _order(orders, ext_ref=None, container_number=None)
        assert (await sl.poll_tracking("D1"))["status"] == "no_reference"

    async def test_actor_failure(self, orders):
        await _order(orders, container_number="C1")
        with patch("shared.connectors.registry.get_connector", return_value=MagicMock(quote=AsyncMock(side_effect=RuntimeError("x")))):
            assert (await sl.poll_tracking("D1"))["status"] == "error"

    async def test_no_event_yet(self, orders):
        await _order(orders, container_number="C1")
        with patch("shared.connectors.registry.get_connector", return_value=MagicMock(quote=AsyncMock(return_value={"shipments": [{"milestone": None}]}))):
            assert (await sl.poll_tracking("D1"))["status"] == "no_event"


# ---------------------------------------------------------------------------
# Playbooks
# ---------------------------------------------------------------------------

class TestPlaybooks:
    def test_bad_env_number(self, monkeypatch):
        monkeypatch.setenv("X_N", "abc")
        assert pl._int("X_N", 4) == 4

    async def test_failed_inspections_are_counted(self):
        rows = [SimpleNamespace(order_type="QC", qc_result="FAIL"), SimpleNamespace(order_type="QC", qc_result="PASS"), SimpleNamespace(order_type="FREIGHT", qc_result="FAIL")]
        with patch("services.shield_link.repository.get_repository", return_value=MagicMock(list_by_deal=AsyncMock(return_value=rows))):
            assert await pl.failed_inspections("D") == 1

    async def test_failed_inspections_unavailable(self):
        with patch("services.shield_link.repository.get_repository", side_effect=RuntimeError):
            assert await pl.failed_inspections("D") == 0

    @staticmethod
    def _deal(status):
        return SimpleNamespace(deal_id="D", status=SimpleNamespace(value=status), parties=[])

    async def test_reinspection_wrong_state(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(return_value=self._deal("PAID"))), patch.object(pl, "reinspection_allowed", new=AsyncMock(return_value=(True, 1))), pytest.raises(ValueError, match="re-inspection needs"):
            await pl.book_reinspection("D")

    async def test_shortfall_reminder(self):
        hold = SimpleNamespace(status="PARTIALLY_FUNDED", shortfall=100.0, escrow_id="E")
        with patch("services.escrow_guard.repository.get_repository", return_value=MagicMock(get_hold_by_deal=AsyncMock(return_value=hold))), patch("services.escrow_guard.service._notify_funding_shortfall", new=AsyncMock()) as notify:
            result = await pl.remind_funding_shortfall("D")
        assert result["action"] == "shortfall_reminder_sent" and result["shortfall"] == 100.0
        notify.assert_awaited_once()

    @pytest.mark.parametrize("hold", [None, SimpleNamespace(status="FUNDED", shortfall=None), SimpleNamespace(status="PARTIALLY_FUNDED", shortfall=0)])
    async def test_nothing_to_remind(self, hold):
        with patch("services.escrow_guard.repository.get_repository", return_value=MagicMock(get_hold_by_deal=AsyncMock(return_value=hold))):
            assert (await pl.remind_funding_shortfall("D"))["success"] is False

    async def test_already_cancelled(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(return_value=self._deal("CANCELLED"))):
            assert await pl.cancel_unfunded_deal("D") == {"action": "already_cancelled"}

    async def test_refund_failure_still_cancels(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(return_value=self._deal("ESCROW_PENDING"))), \
             patch("services.escrow_guard.repository.get_repository", side_effect=RuntimeError), \
             patch.object(pl, "_transition", new=AsyncMock()) as transition, \
             patch("shared.orchestration.notifications.notify_deal_cancelled", new=AsyncMock(return_value=[])):
            await pl.cancel_unfunded_deal("D")
        assert transition.await_args.args[1] == "CANCELLED"

    @pytest.mark.parametrize(("reply", "success", "text"), [({"advanced": True}, True, None), ({"exhausted": True}, False, "no further buyer"), ({}, False, "idle")])
    async def test_next_buyer(self, reply, success, text):
        with patch("shared.orchestration.outreach.advance", new=AsyncMock(return_value=reply)):
            result = await pl.next_buyer("D")
        assert result["success"] is success and (text is None or text in result["error"])


# ---------------------------------------------------------------------------
# Sourcing
# ---------------------------------------------------------------------------

class TestSourcingHelpers:
    async def test_emit_failure(self):
        with patch.object(sc, "get_publisher", side_effect=RuntimeError):
            await sc._emit_event(sc.EventTopic.QUOTE_RECEIVED, "D", {})

    @pytest.mark.parametrize("error", [KeyError("K"), RuntimeError("x")])
    def test_connector_build_failure(self, error):
        with patch.object(sc, "get_connector", side_effect=error):
            assert sc._build_connector("x") is None

    def test_connector_built(self):
        with patch.object(sc, "get_connector", return_value="c"):
            assert sc._build_connector("x") == "c"

    async def test_safe_call(self):
        with patch.object(sc, "get_connector", return_value=MagicMock(quote=AsyncMock(return_value={"a": 1}))):
            assert await sc._safe_call("x", "quote", {}) == {"a": 1}

    async def test_safe_call_failure(self):
        with patch.object(sc, "get_connector", side_effect=RuntimeError):
            assert await sc._safe_call("x", "quote", {}) is None

    def test_pattern(self):
        assert sc._find_pattern("gst 33abc", r"33[a-z]+") == "33ABC" and sc._find_pattern("nothing", r"33[a-z]+") is None

    def test_sender_blank(self):
        assert sc.rfq_sender_supplier(SimpleNamespace(dispatch_results=[]), None) is None

    async def test_unknown_sender_is_never_guessed(self):
        rfq = SimpleNamespace(deal_id="D", dispatch_results=[SimpleNamespace(supplier_id="S", email="s@y.co", company_name="C")])
        with patch.object(sc, "get_rfq", new=AsyncMock(return_value=rfq)), pytest.raises(sc.QuoteNotFoundError):
            await sc.receive_quote_by_email("R1", from_address="stranger@z.co", unit_price_usd=1.0, body_text="b")

    async def test_fetch_deal_failure(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(side_effect=RuntimeError)):
            assert await sc._fetch_deal("D") is None

    async def test_transition_failure_is_contained(self):
        with patch("services.deals.service.update_status", new=AsyncMock(side_effect=RuntimeError)):
            await sc._transition_to_negotiating("D")

    async def test_transition(self):
        with patch("services.deals.service.update_status", new=AsyncMock()) as update:
            await sc._transition_to_negotiating("D")
        update.assert_awaited_once()

    def test_port_distance(self):
        deal = {"match_results": [{"supplier_id": "A", "supplier_to_port_km": 50}, {"supplier_id": "B"}]}
        assert sc._get_supplier_port_km(deal, "A") == 50.0
        assert sc._get_supplier_port_km(deal, "B") == 300.0
        assert sc._get_supplier_port_km({}, "A") == 300.0

    async def test_update_financials_failure_is_contained(self):
        with patch("services.deals.service.get_deal", new=AsyncMock(side_effect=RuntimeError)):
            await sc._update_deal_financials("D", "S", 70.0)

    async def test_get_quote_found(self):
        repo = MagicMock(get_quote=AsyncMock(return_value="q"))
        with patch.object(sc, "get_repository", return_value=repo):
            assert await sc.get_quote("Q") == "q"
