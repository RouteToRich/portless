"""Tests for the Email Processor service (inbound matching + persistence).

Covers:
  - Counter-price extraction from body text
  - process_inbound: unmatched email → UNMATCHED, record + attachments persisted
  - process_inbound: mapped + priced → run_round() triggered (orchestration), PROCESSED
  - Receiving API: POST /email-processor/inbound accepts a structured fetched email
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.email_processor.config import settings
from services.email_processor.repository import InMemoryEmailProcessorRepository
from services.email_processor.service import _extract_price, process_inbound
from services.email_processor.schemas import (
    EmailAttachment,
    InboundEmailSource,
    InboundEmailStatus,
    InboundRawEmail,
)


@pytest.fixture(autouse=True)
def use_in_memory_repo():
    """Wire an in-memory repository for each test."""
    import services.email_processor.repository as repo_mod
    repo_mod._repository = InMemoryEmailProcessorRepository()
    yield
    repo_mod._repository = None


def _raw(
    *,
    subject: str = "Re: Quote",
    body: str = "Hello",
    in_reply_to: str = "",
    attachments: list[EmailAttachment] | None = None,
) -> InboundRawEmail:
    return InboundRawEmail(
        from_address="supplier@example.com",
        to_address=settings.portless_trade_address,
        subject=subject,
        body_text=body,
        in_reply_to=in_reply_to,
        attachments=attachments or [],
        source=InboundEmailSource.IMAP,
        received_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Price extraction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "body,expected",
    [
        ("We can do $1,250.50 / MT", 1250.50),
        ("Our offer is 980 USD", 980.0),
        ("counter at 1100 per unit", 1100.0),
        ("no numbers here", None),
    ],
)
async def test_extract_price(body, expected):
    assert await _extract_price(body) == expected


async def test_extract_price_inr_converted_to_usd(monkeypatch):
    """A rupee-quoted amount (₹/INR/Rs) is converted to USD, not read as USD.

    Regression: the supplier agent quotes in INR by design, so its reply echoes
    a rupee figure (e.g. ₹90,901.91 ≈ $946 at 96). Before this it was read as
    $90,901.91 and the negotiation price-plausibility guard vetoed every reply.
    """
    import services.email_processor.service as svc
    # Pin the rate so the assertion is deterministic.
    async def _fixed_rate() -> float:
        return 96.0
    monkeypatch.setattr(svc, "_usd_inr_rate", _fixed_rate)

    for body in (
        "the Ex-Works price of ₹90,901.91, we would",
        "Our quote is 90901.91 INR per MT",
        "We can offer Rs. 90,901.91",
    ):
        usd = await svc._extract_price(body)
        assert usd is not None
        assert abs(usd - (90901.91 / 96.0)) < 0.01, f"{body!r} -> {usd}"

    # A plain USD figure is unaffected by the INR path.
    assert await svc._extract_price("Our offer is $1,004.00 / MT") == 1004.0


# ---------------------------------------------------------------------------
# process_inbound — unmatched
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_inbound_unmatched_persists_record_and_attachments():
    att = EmailAttachment(filename="doc.pdf", content_type="application/pdf", size=3, content_b64="YWJj")
    result = await process_inbound(_raw(subject="Random subject", attachments=[att]))

    assert result.status == InboundEmailStatus.UNMATCHED
    assert result.negotiation_id is None

    import services.email_processor.repository as repo_mod
    stored = await repo_mod._repository.get(result.inbound_id)
    assert stored is not None
    assert stored.status == InboundEmailStatus.UNMATCHED
    assert [a.filename for a in stored.attachments] == ["doc.pdf"]


# ---------------------------------------------------------------------------
# process_inbound — mapped + priced → orchestration triggered
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_inbound_advances_negotiation(monkeypatch):
    neg_id = "11111111-1111-1111-1111-111111111111"

    monkeypatch.setattr(
        "services.email_processor.mapper._negotiation_is_active",
        AsyncMock(return_value=True),
    )
    run_round_mock = AsyncMock(return_value=SimpleNamespace(round_no=2, status="ROUND"))
    monkeypatch.setattr("agents.negotiation_agent.service.run_round", run_round_mock)

    att = EmailAttachment(filename="counter.pdf", size=3, content_b64="YWJj")
    result = await process_inbound(
        _raw(subject="Re: deal", body="We counter at $1150/MT", attachments=[att]),
        negotiation_id_override=neg_id,
        party_override="supplier",
    )

    assert result.status == InboundEmailStatus.PROCESSED
    assert result.negotiation_id == neg_id
    assert result.round_no == 2

    run_round_mock.assert_awaited_once()
    called_neg_id, round_req = run_round_mock.await_args.args
    assert called_neg_id == neg_id
    assert round_req.party == "supplier"
    assert round_req.channel == "email"
    assert round_req.counter_price == 1150.0
    assert round_req.attachment_refs == ["counter.pdf"]


# ---------------------------------------------------------------------------
# Receiving API endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inbound_api_endpoint_unmatched():
    from httpx import ASGITransport, AsyncClient
    from services.email_processor.main import create_app

    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        async with app.router.lifespan_context(app):
            body = {
                "email": {
                    "from_address": "supplier@example.com",
                    "to_address": settings.portless_trade_address,
                    "subject": "no negotiation token here",
                    "body_text": "hello $900/MT",
                    "source": "imap",
                    "received_at": datetime.now(timezone.utc).isoformat(),
                    "attachments": [
                        {"filename": "q.pdf", "content_type": "application/pdf",
                         "size": 3, "content_b64": "YWJj"}
                    ],
                },
            }
            resp = await client.post("/email-processor/inbound", json=body)

    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == InboundEmailStatus.UNMATCHED.value
