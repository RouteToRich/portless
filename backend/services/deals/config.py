"""Deals service configuration (LLD 4.2)."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"


class DealsSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    deals_mongo_uri: str = ""
    deals_mongo_db_name: str = "deals"
    deals_mongo_collection: str = "deals"


settings = DealsSettings()
