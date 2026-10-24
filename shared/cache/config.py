"""Configuration for the application-wide MongoDB cache.

Env-var prefix: ``CACHE_`` (e.g. ``CACHE_ENABLED``, ``CACHE_MONGO_URI``).

The cache writes to the single shared app database (``MONGO_DB_NAME``, default
"portless") like every other module. ``mongo_uri`` / ``mongo_db_name`` resolve
from ``CACHE_MONGO_URI`` / ``CACHE_MONGO_DB_NAME`` first, then the shared
``MONGO_URI`` / ``MONGO_DB_NAME``. If Mongo is unreachable the cache degrades to
an in-process in-memory backend.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
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

    # Mongo target. Resolves to the shared MONGO_URI when CACHE_MONGO_URI is
    # unset; empty → in-memory.
    mongo_uri: str = Field(
        default="", validation_alias=AliasChoices("CACHE_MONGO_URI", "MONGO_URI")
    )
    mongo_db_name: str = Field(
        default="portless", validation_alias=AliasChoices("CACHE_MONGO_DB_NAME", "MONGO_DB_NAME")
    )
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
