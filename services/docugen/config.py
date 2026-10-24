"""DocuGen service configuration."""
from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"


class DocuGenSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── MongoDB ────────────────────────────────────────────────────────────
    docugen_mongo_uri: str = Field(
        default="", validation_alias=AliasChoices("DOCUGEN_MONGO_URI", "MONGO_URI")
    )
    docugen_mongo_db_name: str = Field(
        default="portless", validation_alias=AliasChoices("DOCUGEN_MONGO_DB_NAME", "MONGO_DB_NAME")
    )
    docugen_docs_collection: str = "documents"

    # ── Document storage ───────────────────────────────────────────────────
    # Local fallback used only when Google Drive isn't configured (or an upload
    # fails). Defaults under the OS temp dir — a hardcoded "/tmp/..." resolves
    # to C:\tmp on Windows, whose creation at the drive root needs admin rights
    # (PermissionError). Override with DOCUGEN_STORAGE_DIR.
    docugen_storage_dir: str = Field(
        default_factory=lambda: str(
            Path(tempfile.gettempdir()) / "portless-docs" / "docugen"
        )
    )

    # ── Seller entity details ──────────────────────────────────────────────
    portless_entity_name: str = "PortLess Trade Solutions Pvt Ltd"
    portless_entity_address: str = "12 Commerce Street, Mumbai, MH 400001, India"
    portless_entity_gstin: str = ""
    portless_entity_iec: str = ""
    portless_entity_pan: str = ""

    # ── Customs filing ─────────────────────────────────────────────────────
    icegate_base_url: str = "https://www.icegate.gov.in"
    icegate_api_key: str = ""
    rodtep_rate_pct: float = 0.5


settings = DocuGenSettings()
