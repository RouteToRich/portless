"""Escrow-Guard service configuration."""
from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"


class EscrowGuardSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── MongoDB ────────────────────────────────────────────────────────────
    escrow_guard_mongo_uri: str = Field(
        default="", validation_alias=AliasChoices("ESCROW_GUARD_MONGO_URI", "MONGO_URI")
    )
    escrow_guard_mongo_db_name: str = Field(
        default="portless", validation_alias=AliasChoices("ESCROW_GUARD_MONGO_DB_NAME", "MONGO_DB_NAME")
    )
    escrow_guard_holds_collection: str = "escrow_holds"
    escrow_guard_payouts_collection: str = "payouts"

    # ── Business rules ────────────────────────────────────────────────────
    default_payment_provider: str = "stripe"
    payout_provider: str = "payoneer"

    # ── Neutral staged-release escrow (target multi-provider model) ─────────
    # Provider used when the buyer expresses no preference. One of the
    # registered escrow connectors: "tazapay" | "escrow_com".
    escrow_default_provider: str = "tazapay"

    # Final-tranche buyer-acceptance window. After the last proof-gated tranche
    # releases, the buyer has this long to accept or dispute; on lapse the final
    # tranche auto-releases (silence = consent).
    inspection_window_hours: int = 72
    inspection_watcher_interval_seconds: int = 60

    # ── Portless entity (for ledger) ──────────────────────────────────────
    portless_entity_name: str = "PortLess Trade Solutions Pvt Ltd"


settings = EscrowGuardSettings()
