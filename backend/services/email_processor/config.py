"""Email Processor service configuration.

Single responsibility: process inbound emails (match to a live negotiation,
extract price, persist, advance). No IMAP/SMTP here — that transport is owned by
the Email Gateway. This service only needs its store, identity, webhook signing
keys, and price bounds.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"


class EmailProcessorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── MongoDB ────────────────────────────────────────────────────────────
    email_processor_mongo_uri: str = ""
    email_processor_mongo_db_name: str = "email_processor"
    email_processor_inbound_collection: str = "inbound_emails"

    # ── PortLess email identity ────────────────────────────────────────────
    portless_email_domain: str = "portless.io"
    portless_trade_address: str = "portless-trade@portless.io"

    # ── SendGrid Inbound Parse webhook ────────────────────────────────────
    #
    # SendGrid dashboard setup
    # ─────────────────────────
    # 1. Settings → Mail Settings → Inbound Parse → Add Host & URL
    #    Hostname: mail.portless.io  (configure MX record on your domain)
    #    URL: https://api.portless.io/email-processor/inbound/sendgrid
    # 2. Enable "POST the raw, full MIME message"
    # 3. Copy the Signature Verification public key → SENDGRID_WEBHOOK_PUBLIC_KEY
    #
    # Install ecdsa for signature verification: pip install ecdsa
    # ──────────────────────────────────────────────────────────────────────
    sendgrid_webhook_public_key: str = ""

    # ── Mailgun Routes webhook ────────────────────────────────────────────
    #
    # Mailgun dashboard setup
    # ────────────────────────
    # 1. Add and verify your sending domain in Mailgun (set MX records).
    # 2. Sending → Routes → Create Route
    #    Filter: match_recipient("portless-trade@<your-mailgun-domain>")
    #    Action: forward("https://api.portless.io/email-processor/inbound/mailgun")
    # 3. Settings → API Security → HTTP Webhook Signing Key → MAILGUN_SIGNING_KEY
    # ──────────────────────────────────────────────────────────────────────
    mailgun_signing_key: str = ""

    # ── Price extraction bounds ────────────────────────────────────────────
    # Extracted prices outside this range are discarded as noise.
    price_extraction_min: float = 0.01
    price_extraction_max: float = 10_000_000.0


settings = EmailProcessorSettings()
