"""Tests for the Email Gateway service (IMAP fetch + SMTP send transport).

Covers:
  - 15-minute IMAP poll interval default
  - Attachment extraction from a MIME message (inbound transport)
  - Outbound: log-only fallback → LOGGED; Message-ID + [NEG:] subject token
  - Outbound: configured send success → SENT; failure → EmailSendError
  - POST /email-gateway/send endpoint
"""
from __future__ import annotations

import base64
import sys
import types
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

from services.email_gateway import sender as svc
from services.email_gateway.config import settings as gw_settings
from services.email_gateway.imap_poller import _extract_attachments, _mime_to_raw_email
from services.email_gateway.schemas import SendEmailRequest, SendOutcome


def _req(**kw) -> SendEmailRequest:
    base = dict(
        to_address="supplier@example.com",
        subject="Re: Quote",
        body="We offer $460/MT",
        negotiation_id="NEG-1",
        round_no=2,
        email_id="EMAIL-ABC",
    )
    base.update(kw)
    return SendEmailRequest(**base)


@pytest.fixture
def log_only(monkeypatch):
    monkeypatch.setattr(gw_settings, "gmail_sender_address", "")
    monkeypatch.setattr(gw_settings, "gmail_app_password", "")


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(gw_settings, "gmail_sender_address", "bot@gmail.com")
    monkeypatch.setattr(gw_settings, "gmail_app_password", "app-pw")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_poll_interval_default_is_15_minutes():
    # Assert the code default independent of any local .env override.
    from services.email_gateway.config import EmailGatewaySettings
    assert EmailGatewaySettings(_env_file=None).imap_poll_interval_seconds == 900


# ---------------------------------------------------------------------------
# Inbound attachment extraction (IMAP / MIME)
# ---------------------------------------------------------------------------

def test_extract_attachments_from_mime():
    msg = MIMEMultipart()
    msg["From"] = "supplier@example.com"
    msg["To"] = "portless-trade@portless.io"
    msg["Subject"] = "Quote with PI"
    msg.attach(MIMEText("Please find the PI attached. $1200/MT", "plain"))
    pdf = MIMEApplication(b"%PDF-1.4 fake", _subtype="pdf")
    pdf.add_header("Content-Disposition", "attachment", filename="proforma.pdf")
    msg.attach(pdf)

    attachments = _extract_attachments(msg)
    assert len(attachments) == 1
    att = attachments[0]
    assert att.filename == "proforma.pdf"
    assert att.content_type == "application/pdf"
    assert att.size == len(b"%PDF-1.4 fake")
    assert base64.b64decode(att.content_b64) == b"%PDF-1.4 fake"


def test_mime_to_raw_email_carries_attachments():
    msg = MIMEMultipart()
    msg["From"] = "buyer@example.com"
    msg["To"] = "portless-trade@portless.io"
    msg["Subject"] = "Re: deal"
    msg.attach(MIMEText("body", "plain"))
    xlsx = MIMEApplication(b"xlsxbytes", _subtype="vnd.ms-excel")
    xlsx.add_header("Content-Disposition", "attachment", filename="prices.xlsx")
    msg.attach(xlsx)

    raw = _mime_to_raw_email(msg)
    assert raw.from_address == "buyer@example.com"
    assert [a.filename for a in raw.attachments] == ["prices.xlsx"]


# ---------------------------------------------------------------------------
# Outbound — log-only fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_log_only_returns_logged(log_only):
    result = await svc.send_email(_req())
    assert result.outcome == SendOutcome.LOGGED
    assert result.message_id == "<EMAIL-ABC.NEG-1@portless.io>"


def test_message_id_requires_both_ids():
    assert svc._build_message_id("", "NEG-1") == ""
    assert svc._build_message_id("EMAIL-ABC", "") == ""
    assert svc._build_message_id("EMAIL-ABC", "NEG-1") == "<EMAIL-ABC.NEG-1@portless.io>"


def test_build_mime_subject_token_and_message_id(enabled):
    mid = svc._build_message_id("EMAIL-ABC", "NEG-1")
    mime = svc._build_mime(_req(), mid)
    assert "[NEG:NEG-1]" in mime["Subject"]
    assert mime["Message-ID"] == "<EMAIL-ABC.NEG-1@portless.io>"
    assert mime["To"] == "supplier@example.com"


# ---------------------------------------------------------------------------
# Outbound — configured send (aiosmtplib mocked)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enabled_send_success(enabled, monkeypatch):
    captured = {}

    async def fake_send(mime, **kwargs):
        captured["kwargs"] = kwargs
        captured["subject"] = mime["Subject"]

    monkeypatch.setitem(sys.modules, "aiosmtplib", types.SimpleNamespace(send=fake_send))

    result = await svc.send_email(_req())
    assert result.outcome == SendOutcome.SENT
    assert result.message_id == "<EMAIL-ABC.NEG-1@portless.io>"
    assert captured["kwargs"]["hostname"] == "smtp.gmail.com"
    assert captured["kwargs"]["port"] == 587
    assert captured["kwargs"]["start_tls"] is True
    assert "[NEG:NEG-1]" in captured["subject"]


@pytest.mark.asyncio
async def test_enabled_send_failure_raises(enabled, monkeypatch):
    async def fake_send(mime, **kwargs):
        raise RuntimeError("smtp down")

    monkeypatch.setitem(sys.modules, "aiosmtplib", types.SimpleNamespace(send=fake_send))

    with pytest.raises(svc.EmailSendError):
        await svc.send_email(_req())


# ---------------------------------------------------------------------------
# Outbound — endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_endpoint_logged(log_only):
    from httpx import ASGITransport, AsyncClient
    from services.email_gateway.main import create_app

    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        async with app.router.lifespan_context(app):
            resp = await client.post(
                "/email-gateway/send", json=_req().model_dump(mode="json")
            )

    assert resp.status_code == 200
    assert resp.json()["outcome"] == "logged"
