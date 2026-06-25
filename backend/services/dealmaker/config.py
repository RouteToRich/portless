"""DealMaker service configuration."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"


class DealMakerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── MongoDB ────────────────────────────────────────────────────────────
    dealmaker_mongo_uri: str = ""
    dealmaker_mongo_db_name: str = "dealmaker"
    dealmaker_pi_collection: str = "proforma_invoices"
    dealmaker_po_collection: str = "purchase_orders"

    # ── Document storage ───────────────────────────────────────────────────
    # Local filesystem path where rendered HTML documents are written.
    # In production, swap for an S3/GCS bucket URI and update _write_document().
    dealmaker_storage_dir: str = "/tmp/portless-docs/dealmaker"

    # ── Business rules ─────────────────────────────────────────────────────
    # Default PI validity period when the caller does not specify one.
    pi_default_validity_days: int = 7
    # Absolute minimum margin % required before a PO may be issued.
    po_margin_floor_pct: float = 8.0
    # Seller name used on documents when no party record exists.
    portless_entity_name: str = "PortLess Trade Solutions Pvt Ltd"
    portless_entity_address: str = "12 Commerce Street, Mumbai, MH 400001, India"
    portless_entity_gstin: str = ""
    portless_entity_iec: str = ""


settings = DealMakerSettings()
