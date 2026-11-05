"""Identity configuration — who signs people in, and which app account is PortLess.

Authentication is auth-service's, reached through the shared RouteToRich API
gateway (the same one MakeMeRich uses): sign-up, sign-in, refresh, logout and
password reset all happen there. PortLess only *checks* a caller's token, by
asking the gateway's ``GET /auth/me`` (see shared/auth/identity.py), and keeps
what auth-service has no notion of: organizations and the Portless-staff
allowlist, in the local ``users`` / ``organizations`` collections.

The local collections fall back to the app-wide MONGO_URI / MONGO_DB_NAME
(see the root .env) when AUTH_MONGO_URI / AUTH_MONGO_DB_NAME are unset.
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
    # users to them, and see data across every organization; see
    # organization.py::is_portless_user().
    portless_emails_raw: str = Field(
        default="", validation_alias=AliasChoices("AUTH_PORTLESS_EMAILS", "PORTLESS_EMAILS")
    )

    # The shared RouteToRich API gateway, whose /auth/me answers "who holds this
    # token". It is the same gateway the LLM calls go through, so it falls back
    # to LLM_GATEWAY_URL. Unset → no token can be checked and every request 401s.
    gateway_url: str = Field(
        default="", validation_alias=AliasChoices("AUTH_GATEWAY_URL", "LLM_GATEWAY_URL")
    )

    # PortLess's app account in auth-service (a UUID minted by the
    # account-management console — the same value the UI sends as account_id on
    # sign-in, and the LLM service account signs in under). Set → a token scoped
    # to any OTHER application (a MakeMeRich login, say) is refused with 403.
    # Unset → any account is accepted and the readiness gate fails.
    account_id: str = Field(
        default="", validation_alias=AliasChoices("PORTLESS_ACCOUNT_ID", "LLM_GATEWAY_ACCOUNT_ID")
    )

    # MakeMeRich's model: believe the X-User-ID / X-User-Email / X-User-Name /
    # X-Account-ID headers the gateway injects, with no call back to it. Sound
    # ONLY while nothing but the gateway can reach this service — anyone who can
    # reach it directly can send those headers. Off by default: PortLess moves
    # money, so it checks every token with the gateway instead (cached briefly).
    trust_gateway_headers: bool = Field(
        default=False, validation_alias=AliasChoices("AUTH_TRUST_GATEWAY_HEADERS")
    )

    # How long a checked token's identity is reused before asking the gateway
    # again. Also the longest a signed-out (revoked) token keeps working here.
    introspection_cache_seconds: float = Field(
        default=60.0, validation_alias=AliasChoices("AUTH_INTROSPECTION_CACHE_SECONDS")
    )
    introspection_timeout_seconds: float = 10.0

    @property
    def portless_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.portless_emails_raw.split(",") if e.strip()}


settings = AuthSettings()
