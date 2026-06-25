"""DocuGen service configuration."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"


class DocuGenSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── MongoDB ────────────────────────────────────────────────────────────
    docugen_mongo_uri: str = ""
    docugen_mongo_db_name: str = "docugen"
    docugen_docs_collection: str = "documents"

    # ── Document storage ───────────────────────────────────────────────────
    docugen_storage_dir: str = "/tmp/portless-docs/docugen"

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
