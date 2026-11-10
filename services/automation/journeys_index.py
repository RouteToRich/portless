"""The real-LLM journeys, so the dashboard can list them without importing the workers' code."""
from __future__ import annotations

JOURNEYS: list[dict[str, str]] = [
    {"id": "UC-01", "fn": "uc_whole_deal", "title": "LLM negotiation with counter-offers, then the whole deal to cash"},
    {"id": "UC-02", "fn": "uc_question", "title": "The buyer asks a question instead of quoting"},
    {"id": "UC-03", "fn": "uc_decline", "title": "The buyer declines the offer"},
    {"id": "UC-04", "fn": "uc_stranger", "title": "A stranger answers on the negotiation thread"},
    {"id": "UC-05", "fn": "uc_other_side", "title": "The other party writes on this party's thread"},
    {"id": "UC-06", "fn": "uc_reject_draft", "title": "An operator rejects a drafted e-mail"},
    {"id": "UC-07", "fn": "uc_absurd_price", "title": "A counterparty quotes an absurd price"},
    {"id": "UC-08", "fn": "uc_below_floor", "title": "A price below the margin floor"},
    {"id": "UC-09", "fn": "uc_hesitant", "title": "A hesitant reply is not read as agreement"},
    {"id": "UC-10", "fn": "uc_conditional_pi", "title": "A conditional answer to the Proforma Invoice"},
    {"id": "UC-11", "fn": "uc_complaint", "title": "A damaged-goods complaint after delivery"},
    {"id": "UC-12", "fn": "uc_bypass_attempts", "title": "Staff trying to go around the approval gates"},
    {"id": "UC-13", "fn": "uc_supplier_declines", "title": "The supplier declines the firm order; standby suppliers until one confirms"},
    {"id": "UC-14", "fn": "uc_supplier_silent", "title": "The supplier stays silent; the standby is asked, then nobody is left"},
]
