"""Tazapay and Escrow.com — the buyer chooses, the provider pays out.

Runs the real connector code against the provider simulator
(``services/escrow_sandbox``: Tazapay API v3, Escrow.com API 2017-09-01):

  • the provider is resolved: request → PI → deal (buyer / operator choice) → session → default
  • Tazapay: escrow opened with the buyer as customer (minor units); payment read
    from the provider; a tranche is SUBMITTED (documents uploaded) and counts as
    released only once Tazapay verified them
  • Escrow.com: one milestone per tranche; PortLess ships a milestone, the buyer
    accepts it on Escrow.com; a rejected milestone is a dispute; the final
    milestone is delivered when the acceptance window opens
  • webhooks never move money on the payload alone — the escrow is re-read
  • staff can relay a payout only for a stage already with the provider
  • the deal's escrow status line names the provider and the stage
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.escrow_guard import service as svc
from services.escrow_guard.repository import InMemoryEscrowGuardRepository, get_repository
from services.escrow_guard.schemas import (
    AcceptEscrowRequest,
    CreateEscrowRequest,
    ReleaseTrancheRequest,
)
from services.escrow_sandbox import escrow_com as sim_escrow_com
from services.escrow_sandbox import main as sim
from services.escrow_sandbox import tazapay as sim_tazapay
from shared.connectors.escrow import detect_escrow_provider_mention, resolve_escrow_provider
from shared.domain.enums import EscrowStatus
from shared.domain.events import EventTopic
from shared.orchestration import escrow_proof, escrow_status

BUYER = SimpleNamespace(role=SimpleNamespace(value="buyer"), legal_name="Gulf Foods LLC",
                        email="buyer@gulf.example", country="AE", address="Dubai")
SUPPLIER = SimpleNamespace(role=SimpleNamespace(value="supplier"), legal_name="Kaveri Mills Pvt Ltd",
                           email="sales@kaveri.example", country="IN", address="Thanjavur")


def _deal(status="PI_ACCEPTED", escrow_provider=None, source=None):
    return SimpleNamespace(deal_id="DEAL-E1", status=SimpleNamespace(value=status), parties=[BUYER, SUPPLIER],
                           escrow_provider=escrow_provider, escrow_provider_source=source)


@pytest.fixture(autouse=True)
def world(monkeypatch):
    import services.escrow_guard.repository as repo_mod
    repo_mod._repository = InMemoryEscrowGuardRepository()
    repo_mod._motor_client = None
    monkeypatch.setenv("ESCROW_SANDBOX_PROVIDERS", "all")
    monkeypatch.setenv("PORTLESS_PUBLIC_BASE_URL", "https://api.portless.io")
    monkeypatch.setenv("ESCROW_PROOF_LINK_SECRET", "proof-secret")
    sim.reset()
    state = SimpleNamespace(deal=_deal(), deal_status="ESCROW_FUNDED", events=[])

    async def _emit(topic, deal_id, payload):
        state.events.append((topic, payload))

    with (
        patch.object(svc, "_fetch_deal", new=AsyncMock(side_effect=lambda _id: state.deal)),
        patch.object(svc, "_transition_deal", new=AsyncMock()),
        patch.object(svc, "_deal_status", new=AsyncMock(side_effect=lambda _id: state.deal_status)),
        patch.object(svc, "_emit", new=_emit),
        patch("shared.orchestration.notifications.notify_funding_shortfall", new=AsyncMock()),
    ):
        yield state
    repo_mod._repository = None
    sim.reset()


def _topics(world):
    return [t for t, _ in world.events]


async def _open(provider: str, amount: float = 10000.0):
    return await svc.create_escrow(CreateEscrowRequest(deal_id="DEAL-E1", provider=provider, amount=amount))


async def _hold(escrow_id):
    return await get_repository().get_hold(escrow_id)


async def _release(escrow_id, tranche_id, proofs=("PO-1",)):
    return await svc.release_tranche(escrow_id, ReleaseTrancheRequest(tranche_id=tranche_id, proof_refs=list(proofs)))


# ---------------------------------------------------------------------------
# Choosing the provider
# ---------------------------------------------------------------------------

class TestProviderChoice:
    async def test_order_request_pi_deal_session_default(self, world, monkeypatch):
        assert await svc.resolve_provider("DEAL-E1") == ("tazapay", "default")
        monkeypatch.setenv("ESCROW_DEFAULT_PROVIDER", "escrow_com")
        assert await svc.resolve_provider("DEAL-E1") == ("escrow_com", "default")
        session = SimpleNamespace(escrow_provider="tazapay")
        with patch("agents.negotiation_agent.repository.get_repository",
                   return_value=SimpleNamespace(get_session_by_deal=AsyncMock(return_value=session))):
            assert await svc.resolve_provider("DEAL-E1") == ("tazapay", "buyer")
            world.deal = _deal(escrow_provider="escrow_com", source="operator")
            assert await svc.resolve_provider("DEAL-E1") == ("escrow_com", "operator")
            pi = SimpleNamespace(escrow_provider="tazapay")
            with patch("services.dealmaker.repository.get_repository",
                       return_value=SimpleNamespace(get_pi_by_deal=AsyncMock(return_value=pi))):
                assert await svc.resolve_provider("DEAL-E1") == ("tazapay", "pi")
                assert await svc.resolve_provider("DEAL-E1", "Escrow.com") == ("escrow_com", "request")

    async def test_a_disabled_provider_is_never_used(self, world, monkeypatch):
        monkeypatch.setenv("ESCROW_PROVIDERS_ENABLED", "tazapay")
        world.deal = _deal(escrow_provider="escrow_com", source="buyer")
        assert await svc.resolve_provider("DEAL-E1") == ("tazapay", "default")

    def test_names_a_buyer_writes(self):
        assert resolve_escrow_provider("Escrow.com") == "escrow_com"
        assert resolve_escrow_provider("TAZAPAY") == "tazapay"
        assert resolve_escrow_provider("paypal") is None
        assert detect_escrow_provider_mention("We prefer Escrow.com for the payment.") == "escrow_com"
        assert detect_escrow_provider_mention("Not Tazapay, we want escrow.com") == "escrow_com"
        assert detect_escrow_provider_mention("Either Tazapay or Escrow.com is fine") is None
        assert detect_escrow_provider_mention("Fine.\n> You may choose Tazapay or Escrow.com") is None
        assert detect_escrow_provider_mention("We would rather not use Tazapay") is None

    async def test_choice_is_recorded_until_the_escrow_opens(self, world):
        saved = {}
        deals = SimpleNamespace(set_fields=AsyncMock(side_effect=lambda _id, f: saved.update(f) or True))
        with patch("services.deals.repository.get_repository", return_value=deals):
            assert await escrow_status.set_choice("DEAL-E1", "Escrow.com", "buyer") is True
            assert saved == {"escrow_provider": "escrow_com", "escrow_provider_source": "buyer"}
            assert await escrow_status.set_choice("DEAL-E1", "wire-me", "operator") is False
            await _open("tazapay")
            assert await escrow_status.set_choice("DEAL-E1", "escrow_com", "operator") is False


# ---------------------------------------------------------------------------
# Tazapay — documents verified by Tazapay
# ---------------------------------------------------------------------------

class TestTazapay:
    async def test_open_pay_submit_verify(self, world, monkeypatch):
        monkeypatch.setenv("TAZAPAY_WEBHOOK_SECRET", "tz-secret")
        sim_tazapay.STATE.auto_verify = False
        esc = await _open("tazapay")
        assert esc.ext_ref.startswith("esc_") and esc.checkout_url.startswith("https://checkout-sandbox.tazapay.com/")
        assert esc.provider_display == "Tazapay"
        sent = sim_tazapay.STATE.requests[0]["body"]
        assert sent["amount"] == 1000000 and sent["customer_details"]["email"] == "buyer@gulf.example"
        assert (await _hold(esc.escrow_id)).provider_payload["chosen_by"] == "request"

        # Nothing paid yet: a read changes nothing.
        assert (await svc.poll_provider(esc.escrow_id)).status == "PENDING"

        # The buyer pays on Tazapay's page; Tazapay's signed webhook → PortLess reads the escrow.
        await sim.buyer_pays("tazapay", esc.ext_ref)
        body, headers = sim_tazapay.webhook_event("checkout.paid", esc.ext_ref)
        out = await svc.handle_provider_webhook("tazapay", headers, body, require_signature=True)
        hold = await _hold(esc.escrow_id)
        assert out["escrow_status"] == "FUNDED" and hold.status == "FUNDED" and hold.funded_amount == 10000.0
        funded = next(p for t, p in world.events if t == EventTopic.ESCROW_FUNDED)
        assert funded["provider"] == "tazapay" and funded["payer_name"] is None  # Tazapay does not report the payer
        # The same event again is a replay.
        again = await svc.handle_provider_webhook("tazapay", headers, body, require_signature=True)
        assert again["replayed"] is True

        # T1: documents go to Tazapay; the money stays until Tazapay verifies them.
        released = await _release(esc.escrow_id, "T1", ["PO-77", "https://qima.example/r.pdf"])
        t1 = next(t for t in released.tranches if t.tranche_id == "T1")
        assert t1.status == "SUBMITTED" and released.released_total == 0.0 and released.submitted_total == 3000.0
        docs = list(sim_tazapay.STATE.documents.values())  # the signed proof sheet + the QC report URL
        assert len(docs) == 2 and docs[0]["url"].startswith("https://api.portless.io/escrow-guard/proof/")
        assert docs[1]["url"] == "https://qima.example/r.pdf"
        doc = docs[0]
        assert doc["reference"] == esc.ext_ref and doc["type"] == "purchase_order"
        assert EventTopic.ESCROW_TRANCHE_SUBMITTED in _topics(world)
        assert EventTopic.ESCROW_TRANCHE_RELEASED not in _topics(world)
        assert escrow_status.compute(await _hold(esc.escrow_id))[0] == EscrowStatus.WITH_PROVIDER
        # Asking again does not submit twice.
        await _release(esc.escrow_id, "T1")
        assert len(sim_tazapay.STATE.documents) == 2

        # Tazapay verifies → the next read books the release.
        await sim.control(f"/tazapay/_sandbox/document/{doc['id']}/verify")
        hold = await svc.poll_provider(esc.escrow_id)
        t1 = next(t for t in hold.tranches if t.tranche_id == "T1")
        assert t1.status == "RELEASED" and t1.release_source == "poll" and hold.released_total == 3000.0
        rel = next(p for t, p in world.events if t == EventTopic.ESCROW_TRANCHE_RELEASED)
        assert rel["tranche_id"] == "T1" and rel["release_source"] == "poll"
        assert escrow_status.compute(hold)[0] == EscrowStatus.RELEASING

    async def test_refused_document_stops_nothing_silently(self, world):
        sim_tazapay.STATE.auto_verify = False
        esc = await _open("tazapay")
        await sim.buyer_pays("tazapay", esc.ext_ref)
        await svc.poll_provider(esc.escrow_id)
        await _release(esc.escrow_id, "T1")
        doc = next(iter(sim_tazapay.STATE.documents.values()))
        await sim.control(f"/tazapay/_sandbox/document/{doc['id']}/verify", {"status": "failed"})
        hold = await svc.poll_provider(esc.escrow_id)
        assert hold.disputed is True and EventTopic.DEAL_EXCEPTION in _topics(world)

    async def test_partial_payment_is_a_shortfall_once(self, world):
        esc = await _open("tazapay")
        await sim.buyer_pays("tazapay", esc.ext_ref, 4000.0)
        hold = await svc.poll_provider(esc.escrow_id)
        assert hold.status == "PARTIALLY_FUNDED" and hold.shortfall == 6000.0
        await svc.poll_provider(esc.escrow_id)
        assert _topics(world).count(EventTopic.ESCROW_FUNDING_SHORTFALL) == 1
        await sim.buyer_pays("tazapay", esc.ext_ref, 6000.0)
        assert (await svc.poll_provider(esc.escrow_id)).status == "FUNDED"

    async def test_auto_verified_release_is_booked_at_once(self, world):
        esc = await _open("tazapay")
        await sim.buyer_pays("tazapay", esc.ext_ref)
        await svc.poll_provider(esc.escrow_id)
        out = await _release(esc.escrow_id, "T1")
        assert next(t for t in out.tranches if t.tranche_id == "T1").status == "RELEASED"

    async def test_refund_goes_to_tazapay(self, world):
        esc = await _open("tazapay")
        await sim.buyer_pays("tazapay", esc.ext_ref)
        await svc.poll_provider(esc.escrow_id)
        world.deal = _deal("DISPUTE")
        with (patch("shared.orchestration.notifications.notify_escrow_refunded", new=AsyncMock()),
              patch("shared.orchestration.continuations._issue_document", new=AsyncMock())):
            out = await svc.refund(esc.escrow_id, svc.RefundRequest(reason="QC failed"))
        assert out.status == "REFUNDED"
        rfd = next(iter(sim_tazapay.STATE.refunds.values()))
        assert rfd["payin"] == esc.ext_ref and rfd["amount"] == 1000000

    async def test_unsigned_or_unknown_webhook_moves_nothing(self, world, monkeypatch):
        monkeypatch.setenv("TAZAPAY_WEBHOOK_SECRET", "tz-secret")
        esc = await _open("tazapay")
        await sim.buyer_pays("tazapay", esc.ext_ref)
        body, _ = sim_tazapay.webhook_event("checkout.paid", esc.ext_ref)
        with pytest.raises(svc.WebhookVerifyError):
            await svc.handle_provider_webhook("tazapay", {"signature": "forged"}, body)
        assert (await _hold(esc.escrow_id)).status == "PENDING"
        body, headers = sim_tazapay.webhook_event("checkout.paid", "esc_nobody", data={"id": "esc_nobody"})
        with pytest.raises(svc.HoldNotFoundError):
            await svc.handle_provider_webhook("tazapay", headers, body)


# ---------------------------------------------------------------------------
# Escrow.com — the buyer accepts each milestone on Escrow.com
# ---------------------------------------------------------------------------

class TestEscrowCom:
    async def test_milestones_shipped_then_accepted_by_the_buyer(self, world, monkeypatch):
        monkeypatch.setenv("ESCROW_COM_WEBHOOK_SECRET", "ec-token")
        sim_escrow_com.STATE.auto_accept = False
        esc = await _open("escrow_com")
        hold = await _hold(esc.escrow_id)
        assert all(t.provider_item_id for t in hold.tranches)
        assert esc.checkout_url == f"https://www.escrow-sandbox.com/transaction/{esc.ext_ref}"
        txn = sim_escrow_com.STATE.transactions[esc.ext_ref]
        assert [i["reference"] for i in txn["items"]] == ["T1", "T2", "T3", "T4"]
        assert txn["items"][3]["inspection_period"] == 3 * 86400  # the buyer's acceptance window

        await sim.buyer_pays("escrow_com", esc.ext_ref)
        hook = sim_escrow_com.webhook_event("payment_approved", esc.ext_ref)
        await svc.handle_provider_webhook("escrow_com", {"x-portless-webhook-token": "ec-token"}, hook,
                                          require_signature=True)
        hold = await _hold(esc.escrow_id)
        assert hold.status == "FUNDED"
        funded = next(p for t, p in world.events if t == EventTopic.ESCROW_FUNDED)
        assert funded["payer_name"] == "Gulf Foods LLC"  # Escrow.com takes money only from the buyer's account

        out = await _release(esc.escrow_id, "T1")
        assert next(t for t in out.tranches if t.tranche_id == "T1").status == "SUBMITTED"
        item = sim_escrow_com.STATE.transactions[esc.ext_ref]["items"][0]
        assert item["status"]["shipped"] and not item["status"]["accepted"]
        assert "the buyer to accept on Escrow.com" in escrow_status.compute(await _hold(esc.escrow_id))[1]

        # The buyer accepts on Escrow.com; the webhook (a hint) makes PortLess read the transaction.
        await sim.control(f"/escrow-com/_sandbox/transaction/{esc.ext_ref}/item/{item['id']}/accept")
        await svc.handle_provider_webhook("escrow_com", {"x-portless-webhook-token": "ec-token"},
                                          sim_escrow_com.webhook_event("accept", esc.ext_ref), require_signature=True)
        hold = await _hold(esc.escrow_id)
        assert next(t for t in hold.tranches if t.tranche_id == "T1").status == "RELEASED"
        assert hold.released_total == 3000.0

    async def test_wrong_token_is_refused(self, world, monkeypatch):
        monkeypatch.setenv("ESCROW_COM_WEBHOOK_SECRET", "ec-token")
        esc = await _open("escrow_com")
        with pytest.raises(svc.WebhookVerifyError):
            await svc.handle_provider_webhook("escrow_com", {"x-portless-webhook-token": "guess"},
                                              sim_escrow_com.webhook_event("payment_approved", esc.ext_ref),
                                              require_signature=True)

    async def test_rejected_milestone_is_a_dispute(self, world):
        sim_escrow_com.STATE.auto_accept = False
        esc = await _open("escrow_com")
        await sim.buyer_pays("escrow_com", esc.ext_ref)
        await svc.poll_provider(esc.escrow_id)
        await _release(esc.escrow_id, "T1")
        item = sim_escrow_com.STATE.transactions[esc.ext_ref]["items"][0]
        await sim.control(f"/escrow-com/_sandbox/transaction/{esc.ext_ref}/item/{item['id']}/reject")
        hold = await svc.poll_provider(esc.escrow_id)
        assert hold.disputed is True
        assert escrow_status.compute(hold)[0] == EscrowStatus.DISPUTED

    async def test_final_milestone_goes_out_when_the_window_opens(self, world):
        esc = await _open("escrow_com")  # auto_accept on: each milestone accepted as soon as it is shipped
        await sim.buyer_pays("escrow_com", esc.ext_ref)
        await svc.poll_provider(esc.escrow_id)
        sim_escrow_com.STATE.auto_accept = True
        for tid in ("T1", "T2", "T3"):
            await _release(esc.escrow_id, tid)
        hold = await _hold(esc.escrow_id)
        assert hold.released_total == 9000.0
        sim_escrow_com.STATE.auto_accept = False
        await svc.open_inspection_window(esc.escrow_id)
        hold = await _hold(esc.escrow_id)
        t4 = next(t for t in hold.tranches if t.tranche_id == "T4")
        assert t4.status == "SUBMITTED" and hold.inspection_deadline is not None
        # PortLess relays the buyer's "we accept" — Escrow.com still waits for the buyer.
        out = await svc.accept_escrow(esc.escrow_id, AcceptEscrowRequest(accepted_by="buyer"))
        assert out.released_total == 9000.0
        item = sim_escrow_com.STATE.transactions[esc.ext_ref]["items"][3]
        await sim.control(f"/escrow-com/_sandbox/transaction/{esc.ext_ref}/item/{item['id']}/accept")
        hold = await svc.poll_provider(esc.escrow_id)
        assert hold.status == "RELEASED" and hold.settled_at is not None and hold.released_total == 10000.0
        final = [p for t, p in world.events if t == EventTopic.ESCROW_TRANCHE_RELEASED and p["tranche_id"] == "FINAL"]
        assert final and final[0]["event"] == "buyer_accepted_at_provider"
        assert escrow_status.compute(hold)[0] == EscrowStatus.SETTLED

    async def test_ship_before_funding_is_refused_by_the_provider(self, world):
        esc = await _open("escrow_com")
        hold = await _hold(esc.escrow_id)
        await get_repository().save_hold(hold.model_copy(update={"status": "FUNDED"}))  # our record says funded…
        with pytest.raises(svc.ConnectorCallError, match="not funded"):  # …Escrow.com knows better
            await _release(esc.escrow_id, "T1")

    async def test_buyer_email_is_required(self, world):
        world.deal = SimpleNamespace(**{**vars(_deal()), "parties": [SimpleNamespace(**{**vars(BUYER), "email": None})]})
        with pytest.raises(svc.ConnectorCallError, match="e-mail"):
            await _open("escrow_com")
        assert escrow_status.compute(await get_repository().get_hold_by_deal("DEAL-E1"))[0] == EscrowStatus.OPEN_FAILED


# ---------------------------------------------------------------------------
# Staff relay, watcher, proof sheet, notice
# ---------------------------------------------------------------------------

class TestControls:
    async def test_staff_confirm_only_relays_a_submitted_stage(self, world):
        sim_tazapay.STATE.auto_verify = False
        esc = await _open("tazapay")
        await sim.buyer_pays("tazapay", esc.ext_ref)
        await svc.poll_provider(esc.escrow_id)
        with pytest.raises(svc.InvalidDealState, match="SUBMITTED"):
            await svc.confirm_release_by_staff(esc.escrow_id, "T1", provider_ref="TZ-PAYOUT-1",
                                               reason="seen on dashboard", confirmed_by="ops@portless.io")
        await _release(esc.escrow_id, "T1")
        out = await svc.confirm_release_by_staff(esc.escrow_id, "T1", provider_ref="TZ-PAYOUT-1",
                                                 reason="seen on dashboard", confirmed_by="ops@portless.io")
        t1 = next(t for t in out.tranches if t.tranche_id == "T1")
        assert t1.status == "RELEASED" and t1.release_source == "staff" and t1.ext_ref == "TZ-PAYOUT-1"
        audit = (await _hold(esc.escrow_id)).provider_payload["staff_confirmations"]
        assert audit[0]["confirmed_by"] == "ops@portless.io" and audit[0]["provider_ref"] == "TZ-PAYOUT-1"

    async def test_watcher_reads_waiting_escrows_on_its_interval(self, world, monkeypatch):
        monkeypatch.setenv("ESCROW_POLL_MINUTES", "30")
        esc = await _open("tazapay")
        assert await svc.poll_open_escrows() == 1  # never read: read now (still unpaid)
        assert await svc.poll_open_escrows() == 0  # read less than 30 minutes ago
        await sim.buyer_pays("tazapay", esc.ext_ref)
        hold = await _hold(esc.escrow_id)
        await get_repository().save_hold(hold.model_copy(update={"provider_checked_at": None}))
        assert await svc.poll_open_escrows() == 1
        assert (await _hold(esc.escrow_id)).status == "FUNDED"
        assert await svc.poll_open_escrows() == 0  # funded and nothing submitted: nothing to wait for

    async def test_proof_link_is_signed_expiring_and_buyer_safe(self, world, monkeypatch):
        monkeypatch.delenv("ESCROW_PROOF_LINK_SECRET")
        assert escrow_proof.signed_url("ESC-1", "T1") is None  # not configured
        monkeypatch.setenv("PORTLESS_PUBLIC_BASE_URL", "https://api.portless.io/")
        monkeypatch.setenv("ESCROW_PROOF_LINK_SECRET", "proof-secret")
        url = escrow_proof.signed_url("ESC-1", "T1", now=1_000_000)
        assert url.startswith("https://api.portless.io/escrow-guard/proof/ESC-1/T1?exp=")
        exp = url.split("exp=")[1].split("&")[0]
        sig = url.split("sig=")[1]
        assert escrow_proof.verify("ESC-1", "T1", exp, sig, now=1_000_001)
        assert not escrow_proof.verify("ESC-1", "T2", exp, sig, now=1_000_001)
        assert not escrow_proof.verify("ESC-1", "T1", exp, sig, now=int(exp) + 1)
        assert not escrow_proof.verify("ESC-1", "T1", exp, "0" * 64, now=1_000_001)

        sim_tazapay.STATE.auto_verify = False
        esc = await _open("tazapay")
        await sim.buyer_pays("tazapay", esc.ext_ref)
        await svc.poll_provider(esc.escrow_id)
        await _release(esc.escrow_id, "T1", ["PO-77"])
        doc = next(iter(sim_tazapay.STATE.documents.values()))
        assert doc["url"].startswith(f"https://api.portless.io/escrow-guard/proof/{esc.escrow_id}/T1?exp=")
        page = await escrow_proof.render(esc.escrow_id, "T1")
        assert "PO-77" in page and "Tazapay" in page and "Kaveri" not in page

    async def test_proof_route_refuses_a_bad_signature(self, world, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from services.escrow_guard.router import router
        monkeypatch.setenv("ESCROW_PROOF_LINK_SECRET", "proof-secret")
        monkeypatch.setenv("PORTLESS_PUBLIC_BASE_URL", "https://api.portless.io")
        esc = await _open("tazapay")
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        assert client.get(f"/escrow-guard/proof/{esc.escrow_id}/T1?exp={int(time.time()) + 60}&sig=bad").status_code == 403
        good = escrow_proof.signed_url(esc.escrow_id, "T1").split("/escrow-guard", 1)[1]
        response = client.get(f"/escrow-guard{good}")
        assert response.status_code == 200 and "Release proof" in response.text

    def test_buyer_notice_names_the_provider_and_who_releases(self):
        from shared.orchestration.notifications import _tpl_escrow_opened
        tranche = SimpleNamespace(pct=0.3, trigger="PO_ISSUED")
        for provider, url, phrase in (
            ("tazapay", "https://checkout.tazapay.com/x", "after it has checked our documents"),
            ("escrow_com", None, "after you accept it on Escrow.com"),
        ):
            esc = SimpleNamespace(provider=provider, escrow_id="ESC-1", ext_ref="R-1", amount=100.0, currency="USD",
                                  checkout_url=url, tranches=[tranche])
            _subject, body = _tpl_escrow_opened({"escrow": esc, "deal_id": "D1", "pi_id": "PI-1", "buyer_name": "Gulf"})
            name = "Tazapay" if provider == "tazapay" else "Escrow.com"
            assert f"Escrow provider  : {name}" in body and phrase in body and "never by us" in body
        assert "e-mailed you an invitation" in body  # Escrow.com without a link

    def test_status_labels_cover_every_state(self):
        assert set(escrow_status.LABELS) == set(EscrowStatus)
        assert escrow_status.compute(None) == (None, None)


# ---------------------------------------------------------------------------
# Automation wiring
# ---------------------------------------------------------------------------

class TestAutomation:
    def test_the_worker_runs_escrow_on_the_simulator_without_real_credentials(self, monkeypatch):
        from services.automation import safety
        env = safety.worker_env()
        assert env["ESCROW_SANDBOX_PROVIDERS"] == "tazapay,escrow_com"
        assert env["TAZAPAY_API_SECRET"] == "" and env["ESCROW_COM_EMAIL"] == "" and env["ESCROW_SANDBOX_URL"] == ""
        monkeypatch.setenv("MONGO_DB_NAME", safety.AUTOMATION_DB)
        monkeypatch.setenv("AUTOMATION_WORKER", "1")
        for key in safety._MUST_BE_BLANK:
            monkeypatch.setenv(key, "")
        monkeypatch.setenv("TAZAPAY_API_SECRET", "real")
        with pytest.raises(SystemExit, match="TAZAPAY_API_SECRET"):
            safety.assert_isolated()

    def test_the_simulator_never_runs_in_live_mode(self, monkeypatch):
        from shared.connectors.escrow import in_simulator
        monkeypatch.setenv("INTEGRATION_MODE", "live")
        assert not in_simulator("tazapay") and not in_simulator("escrow_com")

    async def test_a_seeded_escrow_exists_on_the_simulator(self, world):
        from datetime import UTC, datetime

        from services.automation.seed import _on_provider_simulator
        from services.escrow_guard.schemas import EscrowHoldRecord, TrancheSpec
        now = datetime.now(UTC)
        hold = EscrowHoldRecord(
            escrow_id="ESC-S1", deal_id="DEAL-E1", provider="escrow_com", amount=1000.0, currency="USD",
            status="FUNDED", ext_ref="EXT-S1", created_at=now, updated_at=now,
            tranches=[TrancheSpec(tranche_id=t, trigger=trg, pct=p, amount=1000.0 * p)
                      for t, trg, p in svc.DEFAULT_TRANCHES],
        )
        who = {"buyer_company": "QA Buyer", "buyer_mail": "buyer@qa.invalid"}
        seeded = await _on_provider_simulator(hold, who, "AE", 1000.0)
        assert seeded.ext_ref in sim_escrow_com.STATE.transactions
        assert all(t.provider_item_id for t in seeded.tranches)
        await get_repository().save_hold(seeded)
        out = await _release(seeded.escrow_id, "T1")  # simulator auto-accepts → released on the read
        assert next(t for t in out.tranches if t.tranche_id == "T1").status == "RELEASED"
