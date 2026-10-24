"""DealMaker service configuration."""
from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"


class DealMakerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── MongoDB ────────────────────────────────────────────────────────────
    dealmaker_mongo_uri: str = Field(
        default="", validation_alias=AliasChoices("DEALMAKER_MONGO_URI", "MONGO_URI")
    )
    dealmaker_mongo_db_name: str = Field(
        default="portless", validation_alias=AliasChoices("DEALMAKER_MONGO_DB_NAME", "MONGO_DB_NAME")
    )
    dealmaker_pi_collection: str = "proforma_invoices"
    dealmaker_po_collection: str = "purchase_orders"

    # ── Document storage ───────────────────────────────────────────────────
    # Local fallback used only when Google Drive isn't configured (or an upload
    # fails). Defaults under the OS temp dir — a hardcoded "/tmp/..." resolves
    # to C:\tmp on Windows, whose creation at the drive root needs admin rights
    # (PermissionError). Override with DEALMAKER_STORAGE_DIR.
    dealmaker_storage_dir: str = Field(
        default_factory=lambda: str(
            Path(tempfile.gettempdir()) / "portless-docs" / "dealmaker"
        )
    )

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
