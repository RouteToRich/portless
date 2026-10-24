"""Shared domain layer: enums, DTOs, event catalog and the deal state machine."""

from .enums import (
    ApprovalAction,
    AutonomyLevel,
    DealStatus,
    DocumentType,
    PartyRole,
    ThirdPartyType,
)
from .events import EventTopic, DomainEvent
from .state_machine import DEAL_TRANSITIONS, can_transition

__all__ = [
    "ApprovalAction",
    "AutonomyLevel",
    "DealStatus",
    "DocumentType",
    "PartyRole",
    "ThirdPartyType",
    "EventTopic",
    "DomainEvent",
    "DEAL_TRANSITIONS",
    "can_transition",
]
