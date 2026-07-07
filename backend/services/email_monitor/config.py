"""Email Monitor service configuration."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"


class EmailMonitorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── MongoDB ────────────────────────────────────────────────────────────
    email_monitor_mongo_uri: str = ""
    email_monitor_mongo_db_name: str = "email_monitor"
    email_monitor_inbound_collection: str = "inbound_emails"

    # ── PortLess email identity ────────────────────────────────────────────
    portless_email_domain: str = "portless.io"
    portless_trade_address: str = "portless-trade@portless.io"

    # ── IMAP inbox (Gmail) — fill to enable real-time polling ─────────────
    #
    # Step-by-step Gmail IMAP setup
    # ──────────────────────────────
    # 1. Enable IMAP in Gmail:
    #    Gmail Settings → See All Settings → Forwarding and POP/IMAP
    #    → IMAP Access → Enable IMAP → Save Changes
    #
    # 2. Enable 2-Step Verification on the Gmail account:
    #    https://myaccount.google.com/security
    #
    # 3. Create a Gmail App Password (NOT your regular password):
    #    https://myaccount.google.com/apppasswords
    #    App: "Mail" / Device: "Other (Custom)" → name it "PortLess Monitor"
    #    Copy the generated 16-character password.
    #
    # 4. Set these env vars in .env:
    #    IMAP_MONITOR_USER=your-gmail@gmail.com
    #    IMAP_MONITOR_PASSWORD=xxxx xxxx xxxx xxxx
    #
    # Leave IMAP_MONITOR_USER empty to disable IMAP polling (use webhooks only).
    # ──────────────────────────────────────────────────────────────────────
    imap_monitor_host: str = "imap.gmail.com"
    imap_monitor_port: int = 993
    imap_monitor_user: str = ""
    imap_monitor_password: str = ""
    imap_monitor_use_ssl: bool = True
    imap_monitor_poll_interval_seconds: int = 600  # 10 minutes
    #
    # Buyer and supplier IMAP folders (Gmail labels)
    # ────────────────────────────────────────────────────
    # Each party gets its own Gmail label so buyer and supplier polling
    # tasks run independently.  To set up in Gmail:
    #
    # 1. Settings → Labels → Create new label → "PortLess-Buyers"
    #    Repeat for                              → "PortLess-Suppliers"
    #
    # 2. Settings → Filters and Blocked Addresses → Create new filter
    #    For buyer replies:
    #      Subject contains: [NEG:]  (adjust From: if buyer domains are known)
    #      Action: Skip Inbox, Apply label: PortLess-Buyers
    #    Repeat with label PortLess-Suppliers for supplier replies.
    #
    # 3. Set in .env:
    #    IMAP_MONITOR_BUYER_FOLDER=PortLess-Buyers
    #    IMAP_MONITOR_SUPPLIER_FOLDER=PortLess-Suppliers
    #
    # Leave both as "INBOX" during development — the message_id deduplication
    # in service.py prevents the same email from being processed twice even
    # when both polling tasks read the same folder.
    # ────────────────────────────────────────────────────
    imap_monitor_buyer_folder: str = "INBOX"
    imap_monitor_supplier_folder: str = "INBOX"

    # ── SendGrid Inbound Parse webhook ────────────────────────────────────
    #
    # SendGrid dashboard setup
    # ─────────────────────────
    # 1. Settings → Mail Settings → Inbound Parse → Add Host & URL
    #    Hostname: mail.portless.io  (configure MX record on your domain)
    #    URL: https://api.portless.io/email-monitor/inbound/sendgrid
    # 2. Enable "POST the raw, full MIME message"
    # 3. Enable "Check incoming emails for spam"
    # 4. Copy the Signature Verification public key:
    #    Settings → Mail Settings → Inbound Parse → Signature Verification
    #    → Copy Public Key → paste into SENDGRID_WEBHOOK_PUBLIC_KEY
    #
    # Install ecdsa for signature verification: pip install ecdsa
    # ──────────────────────────────────────────────────────────────────────
    sendgrid_webhook_public_key: str = ""

    # ── Mailgun Routes webhook ────────────────────────────────────────────
    #
    # Mailgun dashboard setup
    # ────────────────────────
    # 1. Add a domain in Mailgun and verify it (set MX records in your DNS).
    # 2. Sending → Routes → Create Route
    #    Filter expression: match_recipient("portless-trade@<your-mailgun-domain>")
    #    Actions: forward("https://api.portless.io/email-monitor/inbound/mailgun")
    #    Priority: 10
    # 3. Copy the HTTP Webhook Signing Key:
    #    Settings → API Security → HTTP Webhook Signing Key
    #    → paste into MAILGUN_SIGNING_KEY
    # ──────────────────────────────────────────────────────────────────────
    mailgun_signing_key: str = ""

    # ── Price extraction bounds ────────────────────────────────────────────
    # Extracted prices outside this range are discarded as noise.
    price_extraction_min: float = 0.01
    price_extraction_max: float = 10_000_000.0


settings = EmailMonitorSettings()
