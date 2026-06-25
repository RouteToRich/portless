"""Sourcing service configuration."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = Path(__file__).parent.parent.parent / ".env"

_DEFAULT_RFQ_MSG = (
    "PortLess Trade Agent requesting a formal quotation for {quantity_mt} MT of "
    "HS-code {hs_code} at a target CIF price of {target_price_usd} USD/unit. "
    "Deal reference: {deal_id}. Please respond with your best FOB price, lead "
    "time, and payment terms within {validity_days} days."
)


class SourcingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    sourcing_mongo_uri: str = ""
    sourcing_mongo_db_name: str = "sourcing"
    sourcing_rfq_collection: str = "rfqs"
    sourcing_quotes_collection: str = "quotes"

    rfq_validity_days: int = 7
    rfq_message_template: str = _DEFAULT_RFQ_MSG


settings = SourcingSettings()
