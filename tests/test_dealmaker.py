"""Tests for DealMaker service: PI issuance, PI acceptance, PO issuance, margin guard."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from services.dealmaker.repository import InMemoryDealMakerRepository, init_repository
from services.dealmaker.schemas import (
    AcceptPiRequest,
    IssuePiRequest,
    IssuePoRequest,
    PiRecord,
)
from services.dealmaker.service import (
    DealNotFoundError,
    InvalidDealState,
    MarginGuardError,
    MarginUnknownError,
    PiNotFoundError,
    PiStateError,
    accept_pi,
    get_deal_documents,
    get_pi,
    get_po,
    issue_pi,
    issue_po,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _mock_deal(
    deal_id: str = "DEAL-TEST",
    status: str = "NEGOTIATION_AGREED",
    buyer_target_price: float | None = 500.0,
) -> object:
    """Minimal deal-like object returned by _fetch_deal."""

    class FakeDealStatus:
        def __init__(self, v: str) -> None:
            self.value = v

        def __str__(self) -> str:
            return self.value

    class FakeDeal:
        hs_code = "1006"
        market = "Spain"
        parties = []
        port_of_loading = "Chennai, IN"

    d = FakeDeal()
    d.deal_id = deal_id  # type: ignore[attr-defined]
    d.status = FakeDealStatus(status)  # type: ignore[attr-defined]
    d.buyer_target_price_usd = buyer_target_price  # type: ignore[attr-defined]
    return d


class _UnconfiguredDrive:
    """Force the local-disk path: without this, a developer .env with real
    GOOGLE_DRIVE_* credentials makes tests upload to their actual Drive."""

    def is_configured(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _repo(tmp_path, monkeypatch) -> InMemoryDealMakerRepository:
    """Inject a fresh in-memory repo and redirect document storage to tmp_path."""
    repo = InMemoryDealMakerRepository()
    monkeypatch.setattr(
        "services.dealmaker.service.get_repository", lambda: repo
    )
    monkeypatch.setattr(
        "services.dealmaker.service.settings.dealmaker_storage_dir",
        str(tmp_path),
    )
    monkeypatch.setattr(
        "services.dealmaker.service.get_drive_connector", lambda: _UnconfiguredDrive()
    )
    return repo


@pytest.fixture()
def deal_negotiation_agreed():
    return _mock_deal(status="NEGOTIATION_AGREED")


@pytest.fixture()
def deal_escrow_funded():
    return _mock_deal(status="ESCROW_FUNDED")


def _pi_request(**overrides) -> IssuePiRequest:
    defaults = dict(
        deal_id="DEAL-TEST",
        agreed_price=480.0,
        currency="USD",
        quantity_mt=100.0,
        incoterm="CIF",
        port_of_destination="Valencia, ES",
        payment_terms="30% advance + 70% on BL",
        buyer_name="Acme Imports SA",
        buyer_address="1 Trade Plaza, Madrid, Spain",
    )
    defaults.update(overrides)
    return IssuePiRequest(**defaults)


def _po_request(**overrides) -> IssuePoRequest:
    defaults = dict(
        deal_id="DEAL-TEST",
        agreed_price=420.0,
        currency="USD",
        quantity_mt=100.0,
        incoterm="FOB",
        port_of_loading="Chennai, IN",
        delivery_deadline=date(2026, 9, 30),
        payment_terms="30% advance + 70% on BL",
        supplier_name="Sunrise Agro Pvt Ltd",
        supplier_address="5 Granary Road, Patna, Bihar, India",
        quality_spec="AGMARK Grade 1, moisture < 12%",
    )
    defaults.update(overrides)
    return IssuePoRequest(**defaults)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# PI — issue
# ---------------------------------------------------------------------------

class TestIssuePi:
    def test_issues_pi_when_deal_in_correct_state(self, deal_negotiation_agreed):
        with (
            patch("services.dealmaker.service._fetch_deal", AsyncMock(return_value=deal_negotiation_agreed)),
            patch("services.dealmaker.service._transition_deal", AsyncMock()),
            patch("services.dealmaker.service._emit", AsyncMock()),
        ):
            resp = run(issue_pi(_pi_request()))

        assert resp.pi_id.startswith("PI-")
        assert resp.deal_id == "DEAL-TEST"
        assert resp.status == "SENT"
        assert resp.total_value == pytest.approx(480.0 * 100.0)
        assert resp.content_hash is not None
        assert resp.storage_uri is not None

    def test_raises_invalid_deal_state_when_deal_not_agreed(self):
        deal = _mock_deal(status="DRAFT")
        with patch("services.dealmaker.service._fetch_deal", AsyncMock(return_value=deal)):
            with pytest.raises(InvalidDealState):
                run(issue_pi(_pi_request()))

    def test_raises_deal_not_found(self):
        with patch(
            "services.dealmaker.service._fetch_deal",
            AsyncMock(side_effect=DealNotFoundError("DEAL-GHOST")),
        ):
            with pytest.raises(DealNotFoundError):
                run(issue_pi(_pi_request(deal_id="DEAL-GHOST")))

    def test_idempotency_returns_existing_pi(self, deal_negotiation_agreed, _repo):
        with (
            patch("services.dealmaker.service._fetch_deal", AsyncMock(return_value=deal_negotiation_agreed)),
            patch("services.dealmaker.service._transition_deal", AsyncMock()),
            patch("services.dealmaker.service._emit", AsyncMock()),
        ):
            r1 = run(issue_pi(_pi_request(idempotency_key="KEY-123")))
            r2 = run(issue_pi(_pi_request(idempotency_key="KEY-123")))

        assert r1.pi_id == r2.pi_id

    def test_pi_has_correct_expiry(self, deal_negotiation_agreed):
        with (
            patch("services.dealmaker.service._fetch_deal", AsyncMock(return_value=deal_negotiation_agreed)),
            patch("services.dealmaker.service._transition_deal", AsyncMock()),
            patch("services.dealmaker.service._emit", AsyncMock()),
        ):
            resp = run(issue_pi(_pi_request(validity_days=14)))

        delta = resp.expires_at - resp.issued_at
        assert delta.days == 14

    def test_document_file_written_to_disk(self, deal_negotiation_agreed, tmp_path):
        with (
            patch("services.dealmaker.service._fetch_deal", AsyncMock(return_value=deal_negotiation_agreed)),
            patch("services.dealmaker.service._transition_deal", AsyncMock()),
            patch("services.dealmaker.service._emit", AsyncMock()),
        ):
            resp = run(issue_pi(_pi_request()))

        assert resp.storage_uri.startswith("file://")
        # File must exist on disk
        html_path = resp.storage_uri.replace("file:///", "/").replace("file://", "")
        from pathlib import Path
        assert Path(html_path).exists() or Path(resp.storage_uri.lstrip("file://")).exists()

    def test_pi_file_streams_stored_content(self, deal_negotiation_agreed):
        # View/Download endpoint: resolves the stored URI to raw bytes + MIME.
        with (
            patch("services.dealmaker.service._fetch_deal", AsyncMock(return_value=deal_negotiation_agreed)),
            patch("services.dealmaker.service._transition_deal", AsyncMock()),
            patch("services.dealmaker.service._emit", AsyncMock()),
        ):
            resp = run(issue_pi(_pi_request()))

        from services.dealmaker.service import get_pi_file

        content, mime, filename = run(get_pi_file(resp.pi_id))
        assert b"<" in content  # rendered HTML markup
        assert mime == "text/html"
        assert filename == f"DEAL-TEST-{resp.pi_id}.html"


# ---------------------------------------------------------------------------
# PI — accept
# ---------------------------------------------------------------------------

class TestAcceptPi:
    def _create_pi_record(self, _repo, status: str = "SENT") -> str:
        now = datetime.now(timezone.utc)
        pi = PiRecord(
            pi_id="PI-TESTONLY",
            deal_id="DEAL-TEST",
            status=status,
            agreed_price=480.0,
            currency="USD",
            quantity_mt=100.0,
            total_value=48000.0,
            incoterm="CIF",
            port_of_destination="Valencia, ES",
            payment_terms="30% advance",
            validity_days=7,
            buyer_name="Acme",
            buyer_address="Madrid",
            buyer_vat_number=None,
            issued_at=now,
            expires_at=now + timedelta(days=7),
        )
        run(_repo.save_pi(pi))
        return pi.pi_id

    def test_accepts_sent_pi(self, _repo):
        pi_id = self._create_pi_record(_repo, status="SENT")
        with (
            patch("services.dealmaker.service._transition_deal", AsyncMock()),
            patch("services.dealmaker.service._emit", AsyncMock()),
        ):
            resp = run(accept_pi(pi_id, AcceptPiRequest(accepted_by="buyer@example.com")))

        assert resp.status == "ACCEPTED"
        assert resp.accepted_at is not None

    def test_raises_pi_not_found(self):
        with pytest.raises(PiNotFoundError):
            run(accept_pi("PI-GHOST", AcceptPiRequest()))

    def test_raises_pi_state_error_when_already_accepted(self, _repo):
        pi_id = self._create_pi_record(_repo, status="ACCEPTED")
        with pytest.raises(PiStateError):
            run(accept_pi(pi_id, AcceptPiRequest()))

    def test_raises_pi_state_error_when_superseded(self, _repo):
        pi_id = self._create_pi_record(_repo, status="SUPERSEDED")
        with pytest.raises(PiStateError):
            run(accept_pi(pi_id, AcceptPiRequest()))

    def test_registers_buyer_organization_when_email_supplied(self, _repo):
        pi_id = self._create_pi_record(_repo, status="SENT")
        register = AsyncMock()
        with (
            patch("services.dealmaker.service._transition_deal", AsyncMock()),
            patch("services.dealmaker.service._emit", AsyncMock()),
            patch("services.dealmaker.service._register_buyer_organization", register),
        ):
            run(accept_pi(pi_id, AcceptPiRequest(buyer_email="buyer@acme.com")))

        register.assert_awaited_once_with("Acme", "buyer@acme.com")

    def test_skips_organization_registration_when_no_email(self, _repo):
        pi_id = self._create_pi_record(_repo, status="SENT")
        with (
            patch("services.dealmaker.service._transition_deal", AsyncMock()),
            patch("services.dealmaker.service._emit", AsyncMock()),
            patch(
                "services.auth.service.find_or_create_organization_for_deal",
                AsyncMock(),
            ) as find_or_create,
        ):
            resp = run(accept_pi(pi_id, AcceptPiRequest()))

        assert resp.status == "ACCEPTED"
        find_or_create.assert_not_called()

    def test_org_registration_failure_does_not_block_acceptance(self, _repo):
        pi_id = self._create_pi_record(_repo, status="SENT")
        with (
            patch("services.dealmaker.service._transition_deal", AsyncMock()),
            patch("services.dealmaker.service._emit", AsyncMock()),
            patch(
                "services.auth.service.find_or_create_organization_for_deal",
                AsyncMock(side_effect=RuntimeError("auth service down")),
            ),
        ):
            resp = run(accept_pi(pi_id, AcceptPiRequest(buyer_email="buyer@acme.com")))

        assert resp.status == "ACCEPTED"


# ---------------------------------------------------------------------------
# PO — issue
# ---------------------------------------------------------------------------

class TestIssuePo:
    def test_issues_po_when_margin_ok(self, deal_escrow_funded):
        with (
            patch("services.dealmaker.service._fetch_deal", AsyncMock(return_value=deal_escrow_funded)),
            patch("services.dealmaker.service._transition_deal", AsyncMock()),
            patch("services.dealmaker.service._emit", AsyncMock()),
        ):
            # buyer_price=500, supplier_price=420 → margin = (500-420)/500 * 100 = 16%
            resp = run(issue_po(_po_request(agreed_price=420.0)))

        assert resp.po_id.startswith("PO-")
        assert resp.status == "ISSUED"
        assert resp.net_margin_pct == pytest.approx(16.0)
        assert resp.total_value == pytest.approx(420.0 * 100.0)

    def test_raises_margin_guard_error_when_below_floor(self, deal_escrow_funded):
        with (
            patch("services.dealmaker.service._fetch_deal", AsyncMock(return_value=deal_escrow_funded)),
        ):
            # buyer_price=500, supplier_price=496 → margin = 0.8% < 8% floor
            with pytest.raises(MarginGuardError):
                run(issue_po(_po_request(agreed_price=496.0)))

    def test_raises_invalid_deal_state_when_not_escrow_funded(self):
        deal = _mock_deal(status="PI_ACCEPTED")
        with patch("services.dealmaker.service._fetch_deal", AsyncMock(return_value=deal)):
            with pytest.raises(InvalidDealState):
                run(issue_po(_po_request()))

    def test_idempotency_returns_existing_po(self, deal_escrow_funded):
        with (
            patch("services.dealmaker.service._fetch_deal", AsyncMock(return_value=deal_escrow_funded)),
            patch("services.dealmaker.service._transition_deal", AsyncMock()),
            patch("services.dealmaker.service._emit", AsyncMock()),
        ):
            r1 = run(issue_po(_po_request(idempotency_key="PO-KEY-456")))
            r2 = run(issue_po(_po_request(idempotency_key="PO-KEY-456")))

        assert r1.po_id == r2.po_id

    def test_po_refused_when_no_buyer_price_exists(self, tmp_path, _repo):
        """With no target price and no PI the margin cannot be checked."""
        deal = _mock_deal(status="ESCROW_FUNDED", buyer_target_price=None)
        with (
            patch("services.dealmaker.service._fetch_deal", AsyncMock(return_value=deal)),
            patch("services.dealmaker.service._transition_deal", AsyncMock()),
            patch("services.dealmaker.service._emit", AsyncMock()),
        ):
            with pytest.raises(MarginUnknownError):
                run(issue_po(_po_request()))


# ---------------------------------------------------------------------------
# Deal documents aggregate
# ---------------------------------------------------------------------------

class TestGetDealDocuments:
    def test_returns_none_when_no_documents(self):
        resp = run(get_deal_documents("DEAL-EMPTY"))
        assert resp.pi is None
        assert resp.po is None

    def test_returns_pi_when_issued(self, deal_negotiation_agreed):
        with (
            patch("services.dealmaker.service._fetch_deal", AsyncMock(return_value=deal_negotiation_agreed)),
            patch("services.dealmaker.service._transition_deal", AsyncMock()),
            patch("services.dealmaker.service._emit", AsyncMock()),
        ):
            run(issue_pi(_pi_request()))

        docs = run(get_deal_documents("DEAL-TEST"))
        assert docs.pi is not None
        assert docs.po is None
