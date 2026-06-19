"""Downstream service clients wired for the API Gateway.

The gateway owns no business logic; it composes the typed REST clients of the
domain services. Swapping any service to a microservice changes nothing here —
only the corresponding base URL in configuration.
"""
from __future__ import annotations

from services.deals.client import DealsClient
from services.dealmaker.client import DealMakerClient
from services.docugen.client import DocuGenClient
from services.escrow_guard.client import EscrowGuardClient
from services.exception_resolver.client import ExceptionResolverClient
from services.hitl_approval.client import HitlApprovalClient
from services.match_engine.client import MatchEngineClient
from services.negotiator.client import NegotiatorClient
from services.shield_link.client import ShieldLinkClient
from services.sourcing.client import SourcingClient
from services.trade_min.client import TradeMinClient

deals = DealsClient()
trade_min = TradeMinClient()
match_engine = MatchEngineClient()
sourcing = SourcingClient()
negotiator = NegotiatorClient()
dealmaker = DealMakerClient()
shield_link = ShieldLinkClient()
docugen = DocuGenClient()
escrow_guard = EscrowGuardClient()
hitl_approval = HitlApprovalClient()
exception_resolver = ExceptionResolverClient()

ALL_CLIENTS = {
    "deals": deals,
    "trade-min": trade_min,
    "match-engine": match_engine,
    "sourcing": sourcing,
    "negotiator": negotiator,
    "dealmaker": dealmaker,
    "shield-link": shield_link,
    "docugen": docugen,
    "escrow-guard": escrow_guard,
    "hitl-approval": hitl_approval,
    "exception-resolver": exception_resolver,
}
