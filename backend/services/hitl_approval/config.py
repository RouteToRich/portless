"""HITL Approval service configuration."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"


class HitlApprovalSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── MongoDB ────────────────────────────────────────────────────────────
    hitl_approval_mongo_uri: str = ""
    hitl_approval_mongo_db_name: str = "hitl_approval"
    hitl_approval_collection: str = "approvals"

    # ── UI integration ─────────────────────────────────────────────────────
    # Base URL of the PortLess UI app (used to generate deep-links in
    # approval notifications). Update once the UI is deployed.
    ui_base_url: str = "http://localhost:5173"

    # Webhook to POST when an approval is created / resolved.
    # Leave empty to skip. The UI backend can register here for push
    # notifications while SSE is used for browser direct connections.
    notify_webhook_url: str = ""
    notify_webhook_secret: str = ""  # HMAC-SHA256 signing key for the webhook

    # ── Timeouts ───────────────────────────────────────────────────────────
    # How often the background watcher checks for expired approvals (seconds)
    timeout_poll_interval_seconds: int = 30

    # Default expire duration (seconds) for L1 approvals when expires_at is
    # not supplied by the caller.
    l1_default_timeout_seconds: int = 3600  # 1 hour


settings = HitlApprovalSettings()
