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


class QcStatus(str, Enum):
    """Where a deal's pre-shipment inspection stands — kept on the deal
    (``DealRecord.qc_status``) by ``shared/orchestration/qc_status.py`` from the
    deal status, the supplier's PO acknowledgement / goods-ready date and the
    QC order at the provider."""

    AWAITING_PO_ACK = "AWAITING_PO_ACK"            # PO sent, supplier has not acknowledged it
    AWAITING_READY_DATE = "AWAITING_READY_DATE"    # acknowledged; asked when the goods are ready
    NOT_BOOKED = "NOT_BOOKED"                      # ready to book, no booking yet
    BOOKING_FAILED = "BOOKING_FAILED"              # the provider refused / the request never reached it
    REQUESTED = "REQUESTED"                        # booked, provider has not confirmed yet
    SCHEDULED = "SCHEDULED"                        # provider confirmed the date / inspector
    INSPECTED = "INSPECTED"                        # inspection done, report not out yet
    OVERDUE = "OVERDUE"                            # inspection day + grace passed without a report
    RESULT_PENDING = "RESULT_PENDING"              # report leaves the decision to us — a person decides
    REINSPECTION = "REINSPECTION"                  # re-inspection booked after a failure
    PASSED = "PASSED"                              # report PASS
    FAILED = "FAILED"                              # report FAIL (deal QC_FAILED → DISPUTE)


class EscrowStatus(str, Enum):
    """Where the buyer's money stands at the deal's escrow provider — kept on the
    deal (``DealRecord.escrow_status``) by ``shared/orchestration/escrow_status.py``."""

    OPEN_FAILED = "OPEN_FAILED"                    # the provider refused to open the escrow
    AWAITING_FUNDING = "AWAITING_FUNDING"          # opened; the buyer has not paid
    PARTIALLY_FUNDED = "PARTIALLY_FUNDED"          # the provider holds less than the PI
    FUNDED = "FUNDED"                              # fully funded, nothing paid out yet
    WITH_PROVIDER = "WITH_PROVIDER"                # a stage's proof is with the provider, payout pending
    RELEASING = "RELEASING"                        # some stages paid out, more to come
    ACCEPTANCE_WINDOW = "ACCEPTANCE_WINDOW"        # goods delivered; the buyer's acceptance window is open
    DISPUTED = "DISPUTED"                          # the buyer disputed / rejected a stage
    SETTLED = "SETTLED"                            # every stage paid out
    REFUNDED = "REFUNDED"                          # the undisbursed money went back to the buyer


class FreightStatus(str, Enum):
    """Where the deal's shipment stands with the carrier — kept on the deal
    (``DealRecord.freight_status``) by ``shared/orchestration/freight_status.py``."""

    NOT_BOOKED = "NOT_BOOKED"                  # QC passed, no booking yet (gate pending)
    BOOKING_FAILED = "BOOKING_FAILED"          # the carrier refused the request / it never reached it
    BOOKING_REQUESTED = "BOOKING_REQUESTED"    # sent; the carrier has not confirmed
    BOOKING_REJECTED = "BOOKING_REJECTED"      # the carrier rejected / cancelled it
    BOOKING_CONFIRMED = "BOOKING_CONFIRMED"    # space and containers confirmed
    DOCS_PENDING = "DOCS_PENDING"              # VGM or the B/L draft waits for PortLess
    BL_ISSUED = "BL_ISSUED"                    # bill of lading issued
    LOADED = "LOADED"                          # container gated in / loaded on board
    DEPARTED = "DEPARTED"                      # vessel sailed
    ARRIVED = "ARRIVED"                        # vessel at the port of discharge
    DISCHARGED = "DISCHARGED"                  # container off the vessel
    DELIVERED = "DELIVERED"                    # container out to the buyer


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
    SUBMIT_VGM = "SUBMIT_VGM"
    """The verified gross mass of each container (inspector's loaded quantity +
    the container's tare) is ready → submit it to the carrier (a SOLAS
    declaration PortLess signs as shipper)."""
    APPROVE_TRANSPORT_DOCUMENT = "APPROVE_TRANSPORT_DOCUMENT"
    """The carrier's draft bill of lading is ready → the shipper approves it, and
    the carrier issues it."""
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
