"""Shield-Link service configuration."""
from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"


class ShieldLinkSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── MongoDB ────────────────────────────────────────────────────────────
    shield_link_mongo_uri: str = Field(
        default="", validation_alias=AliasChoices("SHIELD_LINK_MONGO_URI", "MONGO_URI")
    )
    shield_link_mongo_db_name: str = Field(
        default="portless", validation_alias=AliasChoices("SHIELD_LINK_MONGO_DB_NAME", "MONGO_DB_NAME")
    )
    shield_link_tpo_collection: str = "third_party_orders"

    # ── Default providers ─────────────────────────────────────────────────
    default_qc_provider: str = "qima"
    default_freight_provider: str = Field(
        default="maersk", validation_alias=AliasChoices("FREIGHT_PROVIDER", "SHIELD_LINK_DEFAULT_FREIGHT_PROVIDER"),
    )
    """Who books the shipment: ``maersk`` (carrier API — booking, tracking, VGM,
    eBL) or a forwarder (``cogoport`` / ``freightos``)."""
    freight_quote_provider: str = Field(
        default="cogoport", validation_alias=AliasChoices("FREIGHT_QUOTE_PROVIDER", "SHIELD_LINK_FREIGHT_QUOTE_PROVIDER"),
    )
    """Who quotes the lane for pricing (Maersk rates come from the contract)."""

    qc_fallback_strategy: str = Field(
        default="cheapest", validation_alias="SHIELD_LINK_QC_FALLBACK_STRATEGY"
    )
    """Which QC provider to book when neither the request nor the buyer opted
    one: "cheapest" (lowest indicative fee in the catalog — a buyer who never
    chose gets the most economical inspector) or "default"
    (default_qc_provider)."""

    # ── Webhook secrets (read from env directly by connectors) ────────────
    # SGS_WEBHOOK_SECRET, COGOPORT_WEBHOOK_SECRET, FREIGHTOS_WEBHOOK_SECRET


settings = ShieldLinkSettings()
