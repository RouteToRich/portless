"""Email Gateway configuration — a single home for IMAP + SMTP credentials.

Single responsibility: email transport. The gateway monitors/fetches inbound
mail over IMAP (automatic) and sends outbound mail over SMTP (API). It does not
match, persist, or process emails — inbound messages are forwarded to the Email
Processor service.

Leave IMAP_USER empty to disable inbound polling; leave GMAIL_SENDER_ADDRESS
empty to run outbound in log-only mode. Neither is required for dev/CI.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"


class EmailGatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── PortLess email identity ────────────────────────────────────────────
    portless_email_domain: str = "portless.io"

    # ── IMAP inbox (inbound transport) ────────────────────────────────────
    #
    # Gmail setup: enable IMAP (Settings → Forwarding and POP/IMAP), enable
    # 2-Step Verification, then create an App Password
    # (https://myaccount.google.com/apppasswords) → IMAP_PASSWORD.
    #
    # Leave IMAP_USER empty to disable IMAP polling (the SMTP sender still works).
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_use_ssl: bool = True
    imap_poll_interval_seconds: int = 900  # 15 minutes
    # Optional dedicated Gmail labels to segregate buyer vs supplier replies.
    imap_buyer_folder: str = "INBOX"
    imap_supplier_folder: str = "INBOX"

    # ── SMTP (outbound transport, Gmail via aiosmtplib STARTTLS) ───────────
    #
    # Reuses the same Gmail account + App Password as IMAP above (set
    # GMAIL_SENDER_ADDRESS = the IMAP_USER address). Leave GMAIL_SENDER_ADDRESS
    # empty → outbound messages are logged only (dev/CI).
    gmail_sender_address: str = ""
    gmail_app_password: str = ""
    smtp_from_name: str = "PortLess Trade Agent"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587  # STARTTLS

    # ── Inbound forwarding ─────────────────────────────────────────────────
    # False (default): forward each fetched email in-process to the Email
    #   Processor (services.email_processor.service.process_inbound).
    # True: POST each fetched email to the Email Processor's receiving API at
    #   shared settings.email_processor_url — use to run the two services apart.
    forward_via_http: bool = False


settings = EmailGatewaySettings()
