"""Enumerations shared across services (mirrors the master specification)."""
from __future__ import annotations

from enum import Enum


class DealStatus(str, Enum):
    """Deal lifecycle states — see LLD 4.2 Deal State Machine."""

    DRAFT = "DRAFT"
    MATCHING = "MATCHING"
    NEGOTIATING = "NEGOTIATING"
    NEGOTIATION_AGREED = "NEGOTIATION_AGREED"
    NEGOTIATION_FAILED = "NEGOTIATION_FAILED"
    PI_SENT = "PI_SENT"
    PI_ACCEPTED = "PI_ACCEPTED"
    ESCROW_PENDING = "ESCROW_PENDING"
    ESCROW_FUNDED = "ESCROW_FUNDED"
    PO_ISSUED = "PO_ISSUED"
    SUPPLIER_DISPATCH_READY = "SUPPLIER_DISPATCH_READY"
    QC_PASSED = "QC_PASSED"
    QC_FAILED = "QC_FAILED"
    IN_TRANSIT = "IN_TRANSIT"
    CLEARED = "CLEARED"
    PAID = "PAID"
    DISPUTE = "DISPUTE"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


class PartyRole(str, Enum):
    BUYER = "buyer"
    SUPPLIER = "supplier"


class AutonomyLevel(str, Enum):
    """Per-action-class autonomy — see LLD 4.6."""

    L0 = "L0"  # Always ask
    L1 = "L1"  # Ask, auto-approve on timeout
    L2 = "L2"  # Auto, notify after
    L3 = "L3"  # Fully autonomous, log only


class ThirdPartyType(str, Enum):
    QC = "qc"
    FREIGHT = "freight"
    CHA = "cha"


class DocumentType(str, Enum):
    PROFORMA_INVOICE = "PI"
    COMMERCIAL_INVOICE = "CI"
    PACKING_LIST = "PL"
    CERTIFICATE_OF_ORIGIN = "CoO"
    SDF = "SDF"
    SHIPPING_BILL = "SHIPPING_BILL"


class ApprovalAction(str, Enum):
    RELEASE_SUPPLIER_PAYOUT = "RELEASE_SUPPLIER_PAYOUT"
    BOOK_FREIGHT = "BOOK_FREIGHT"
    ISSUE_PO = "ISSUE_PO"
    FILE_CUSTOMS = "FILE_CUSTOMS"
    RESOLVE_EXCEPTION = "RESOLVE_EXCEPTION"
