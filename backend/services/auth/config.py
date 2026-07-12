"""Auth service configuration.

Falls back to the app-wide MONGO_URI / MONGO_DB_NAME (see backend/.env) when
AUTH_MONGO_URI / AUTH_MONGO_DB_NAME are unset — Auth writes to the same single
shared database as every other module, in its own `users` collection.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    auth_mongo_uri: str = Field(
        default="", validation_alias=AliasChoices("AUTH_MONGO_URI", "MONGO_URI")
    )
    auth_mongo_db_name: str = Field(
        default="portless", validation_alias=AliasChoices("AUTH_MONGO_DB_NAME", "MONGO_DB_NAME")
    )
    auth_users_collection: str = "users"
    auth_organizations_collection: str = "organizations"

    # Explicit allowlist of Portless platform-staff emails (comma-separated).
    # There is no domain matching or other derivation — an email either is or
    # isn't on this list. Portless staff can manage organizations, assign
    # users to them, use the Knowledge Base, and see data across every
    # organization; see organization.py::is_portless_user().
    portless_emails_raw: str = Field(
        default="", validation_alias=AliasChoices("AUTH_PORTLESS_EMAILS", "PORTLESS_EMAILS")
    )

    @property
    def portless_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.portless_emails_raw.split(",") if e.strip()}


settings = AuthSettings()
