"""Exception-Resolver service configuration."""
from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"


class ExceptionResolverSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── MongoDB ────────────────────────────────────────────────────────────
    exception_resolver_mongo_uri: str = Field(
        default="", validation_alias=AliasChoices("EXCEPTION_RESOLVER_MONGO_URI", "MONGO_URI")
    )
    exception_resolver_mongo_db_name: str = Field(
        default="portless",
        validation_alias=AliasChoices("EXCEPTION_RESOLVER_MONGO_DB_NAME", "MONGO_DB_NAME"),
    )
    exception_resolver_collection: str = "exceptions"

    # ── Agent behaviour ────────────────────────────────────────────────────
    # How many remediation attempts before marking UNRESOLVABLE
    max_auto_fix_attempts: int = 3
    # LLM caller tag for request tracking
    llm_caller_tag: str = "exception_resolver"
    # Autonomy level for ESCALATE HITL approvals raised by the agent
    escalation_autonomy_level: str = "L1"
    # Max tokens for LLM analysis response
    llm_max_tokens: int = 1024


settings = ExceptionResolverSettings()
