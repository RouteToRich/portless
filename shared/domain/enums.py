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
    # Issued along the trade (shared/orchestration/documents.py):
    SALES_CONTRACT = "SALES_CONTRACT"                  # PI accepted — full terms with the buyer
    INSPECTION_ORDER = "INSPECTION_ORDER"              # QC booked — what the inspector checks, loading + sealing
    SHIPPING_INSTRUCTIONS = "SHIPPING_INSTRUCTIONS"    # freight booked — shipper / consignee / B/L instructions
    INSURANCE_DECLARATION = "INSURANCE_DECLARATION"    # freight booked, CIF / CIP — cover to declare to the insurer
    ACCEPTANCE_CERTIFICATE = "ACCEPTANCE_CERTIFICATE"  # final tranche released — delivery accepted (or window lapsed)
    REMITTANCE_ADVICE = "REMITTANCE_ADVICE"            # each completed supplier payout
    SETTLEMENT_STATEMENT = "SETTLEMENT_STATEMENT"      # escrow settled — the buyer's statement of account
    CREDIT_NOTE = "CREDIT_NOTE"                        # escrow refund to the buyer


class ApprovalAction(str, Enum):
    RELEASE_SUPPLIER_PAYOUT = "RELEASE_SUPPLIER_PAYOUT"
    BOOK_FREIGHT = "BOOK_FREIGHT"
    ISSUE_PO = "ISSUE_PO"
    FILE_CUSTOMS = "FILE_CUSTOMS"
    RESOLVE_EXCEPTION = "RESOLVE_EXCEPTION"
    SEND_NEGOTIATION_EMAIL = "SEND_NEGOTIATION_EMAIL"
    CONFIRM_NEGOTIATION_AGREEMENT = "CONFIRM_NEGOTIATION_AGREEMENT"

    # ── Automation legs (shared/orchestration/continuations.py) ──────────
    DISPATCH_RFQ = "DISPATCH_RFQ"
    """Deal reached MATCHING → send RFQs to the shortlisted suppliers."""
    CONFIRM_SUPPLIER_SELECTION = "CONFIRM_SUPPLIER_SELECTION"
    """A quote came in that the Match Engine did not auto-SOURCE (thin
    reliability data / verification gap / negotiation failed with the
    previous supplier) → operator confirms the proposed supplier and the
    deal moves to NEGOTIATING."""
    CONFIRM_PI_ACCEPTANCE = "CONFIRM_PI_ACCEPTANCE"
    """Buyer's email reply to the Proforma Invoice reads as an acceptance →
    confirm it (callback: DealMaker accept_pi)."""
    CONFIRM_SUPPLIER_ORDER = "CONFIRM_SUPPLIER_ORDER"
    """The supplier's reply to the firm order request reads as a confirmation →
    record the committed price and issue the buyer's Proforma Invoice
    (shared/orchestration/supplier_commitment.py)."""
    RELEASE_ESCROW_TRANCHE = "RELEASE_ESCROW_TRANCHE"
    """A tranche trigger (PO_ISSUED / QC_PASSED / CLEARED) was reached with
    its proof → release that escrow tranche. Money-moving."""
    SEND_DEAL_NOTIFICATION = "SEND_DEAL_NOTIFICATION"
    """Templated (non-LLM) status email to the buyer or supplier — PI issued,
    escrow funding instructions, PO issued, QC booked, shipment booked,
    cargo cleared, deal completed, reminders."""
    PARSE_INBOUND_EMAIL = "PARSE_INBOUND_EMAIL"
    """Inbound email mapped to a negotiation but no price could be extracted
    (or it arrived after the agreement) — operator reads it and acts via the
    manual inbound endpoint. Informational; the callback is a no-op."""
    CONFIRM_COUNTERPARTY_RISK = "CONFIRM_COUNTERPARTY_RISK"
    """Sanctions / registry screening of the buyer (Apify counterparty-check
    actor) returned ``review`` before the Proforma Invoice goes out → an
    operator confirms the counterparty; APPROVE issues the PI, REJECT raises a
    deal exception. A ``block`` verdict never reaches this gate — it stops the
    deal outright."""
    CONFIRM_BUYER_ACCEPTANCE = "CONFIRM_BUYER_ACCEPTANCE"
    REFUND_ESCROW = "REFUND_ESCROW"
    """An operator asks to return the undisbursed escrow balance to the buyer.
    Money-moving; REJECT leaves the money and the deal where they are."""
    CONFIRM_TRADE_COMPLIANCE = "CONFIRM_TRADE_COMPLIANCE"
    """Trade-compliance REVIEW before the Proforma Invoice (product export
    policy, destination, unscreened party, related parties, off-market price)
    → an operator accepts the findings; APPROVE issues the PI. Always L0."""
    SEND_BUYER_OUTREACH = "SEND_BUYER_OUTREACH"
    SEND_INDICATIVE_OFFER = "SEND_INDICATIVE_OFFER"
    CONFIRM_MATCH = "CONFIRM_MATCH"
    """Templated first introduction to a buyer the scan identified (the
    outreach funnel, shared/orchestration/outreach.py). Soft REJECT skips
    that buyer."""
    """The buyer's email after delivery reads as acceptance of the goods →
    confirm it (callback: Escrow-Guard ``accept_escrow`` releases the final
    ACCEPTED-trigger tranche). Money-moving; soft REJECT keeps the inspection
    window running."""
