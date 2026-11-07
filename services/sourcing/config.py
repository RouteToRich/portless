"""Sourcing service configuration."""
from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"

# Never put the buyer's price in here: a supplier who reads "target CIF 900"
# quotes just under 900 instead of its real price. Available placeholders:
# {product}, {quantity}, {hs_code}, {deal_id}, {validity_days} — and, for
# custom templates only, {quantity_mt} / {target_price_usd}.
_DEFAULT_RFQ_MSG = (
    "We are sourcing {quantity} of {product} (HS {hs_code}) for an overseas buyer and "
    "would like your best quotation. Our reference: {deal_id}. A reply within "
    "{validity_days} days keeps you in this order."
)


class SourcingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    sourcing_mongo_uri: str = Field(
        default="", validation_alias=AliasChoices("SOURCING_MONGO_URI", "MONGO_URI")
    )
    sourcing_mongo_db_name: str = Field(
        default="portless", validation_alias=AliasChoices("SOURCING_MONGO_DB_NAME", "MONGO_DB_NAME")
    )
    sourcing_rfq_collection: str = "rfqs"
    sourcing_quotes_collection: str = "quotes"

    rfq_validity_days: int = 7
    rfq_message_template: str = _DEFAULT_RFQ_MSG


settings = SourcingSettings()
