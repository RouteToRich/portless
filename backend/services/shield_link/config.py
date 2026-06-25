"""Shield-Link service configuration."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"


class ShieldLinkSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── MongoDB ────────────────────────────────────────────────────────────
    shield_link_mongo_uri: str = ""
    shield_link_mongo_db_name: str = "shield_link"
    shield_link_tpo_collection: str = "third_party_orders"

    # ── Default providers ─────────────────────────────────────────────────
    default_qc_provider: str = "sgs"
    default_freight_provider: str = "cogoport"

    # ── Webhook secrets (read from env directly by connectors) ────────────
    # SGS_WEBHOOK_SECRET, COGOPORT_WEBHOOK_SECRET, FREIGHTOS_WEBHOOK_SECRET


settings = ShieldLinkSettings()
