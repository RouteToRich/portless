"""Configuration for the application-wide MongoDB cache.

Env-var prefix: ``CACHE_`` (e.g. ``CACHE_ENABLED``, ``CACHE_MONGO_URI``).

The cache reuses the same Atlas cluster as the rest of the app. When
``CACHE_MONGO_URI`` is left empty it falls back to the LLM-gateway's default
Mongo URI (which already points at the shared cluster), and if Mongo is
unreachable it degrades to an in-process in-memory cache.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"


class CacheSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CACHE_",
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Master kill switch — set CACHE_ENABLED=0 to bypass and always hit live.
    enabled: bool = True

    # Mongo target. Empty uri → fall back to the LLM-gateway default cluster,
    # then to in-memory if that is unreachable.
    mongo_uri: str = ""
    mongo_db_name: str = "cache"
    collection: str = "api_cache"

    def resolved_uri(self) -> str:
        if self.mongo_uri:
            return self.mongo_uri
        try:
            from shared.llm_gateway.config import llm_gateway_settings
            return llm_gateway_settings.mongo_uri
        except Exception:
            return ""


cache_settings = CacheSettings()
