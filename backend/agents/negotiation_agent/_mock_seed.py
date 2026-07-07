"""Seed the in-memory negotiation repositories with 5 realistic demo scenarios.

Called by on_startup() only when no NEGOTIATION_MONGO_URI is set (dev / CI).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .email_history import EmailDirection, EmailHistoryRecord, EmailStatus, get_email_repository
from .repository import get_repository
from .schemas import NegotiationPolicy, NegotiationSession

logger = logging.getLogger(__name__)

_PORTLESS_EMAIL = "portless-trade@portless.io"


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 5 mock scenarios
# ---------------------------------------------------------------------------

_SESSIONS: list[dict] = [
    # 1 ── Active negotiation, Round 2 (Steel, India → Germany)
    {
        "negotiation_id": "NEG-STEEL-001",
        "deal_id": "DEAL-2024-STEEL-001",
        "trade_match_id": "TM-STEEL-001",
        "buyer_id": "BUYER-DE-001",
        "seller_id": "SELLER-IN-001",
        "product_code": "7208",
        "buyer_country_code": "DE",
        "buyer_target_price": 800.0,
        "supplier_quote_price": 890.0,
        "composite_match_score": 0.82,
        "policy": NegotiationPolicy(target_margin_pct=12.0, margin_floor_pct=8.0, max_rounds=5),
        "status": "ROUND",
        "round_no": 2,
        "current_bid": 820.0,
        "current_ask": 858.0,
        "agreed_price": None,
        "margin_at_current": 11.2,
        "hitl_required": False,
        "created_at": _dt("2024-06-10T08:00:00"),
        "updated_at": _dt("2024-06-10T11:30:00"),
    },
    # 2 ── HITL Pending, operator must approve outbound email (Cotton, India → France)
    {
        "negotiation_id": "NEG-TEX-002",
        "deal_id": "DEAL-2024-TEX-002",
        "trade_match_id": "TM-TEX-002",
        "buyer_id": "BUYER-FR-002",
        "seller_id": "SELLER-IN-002",
        "product_code": "5208",
        "buyer_country_code": "FR",
        "buyer_target_price": 3.10,
        "supplier_quote_price": 3.85,
        "composite_match_score": 0.74,
        "policy": NegotiationPolicy(target_margin_pct=10.0, margin_floor_pct=8.0, max_rounds=5),
        "status": "HITL_PENDING",
        "round_no": 1,
        "current_bid": 3.25,
        "current_ask": 3.60,
        "agreed_price": None,
        "margin_at_current": 8.3,
        "hitl_required": True,
        "created_at": _dt("2024-06-11T09:15:00"),
        "updated_at": _dt("2024-06-11T10:45:00"),
    },
    # 3 ── Agreed (Electronics / IC chips, India → Singapore)
    {
        "negotiation_id": "NEG-ELEC-003",
        "deal_id": "DEAL-2024-ELEC-003",
        "trade_match_id": "TM-ELEC-003",
        "buyer_id": "BUYER-SG-003",
        "seller_id": "SELLER-IN-003",
        "product_code": "8542",
        "buyer_country_code": "SG",
        "buyer_target_price": 1200.0,
        "supplier_quote_price": 1320.0,
        "composite_match_score": 0.91,
        "policy": NegotiationPolicy(target_margin_pct=12.0, margin_floor_pct=8.0, max_rounds=5),
        "status": "AGREED",
        "round_no": 3,
        "current_bid": 1248.0,
        "current_ask": 1252.0,
        "agreed_price": 1250.0,
        "margin_at_current": 10.4,
        "hitl_required": False,
        "created_at": _dt("2024-06-08T07:00:00"),
        "updated_at": _dt("2024-06-09T16:20:00"),
    },
    # 4 ── Failed (Industrial Chemicals, India → USA — price gap too wide)
    {
        "negotiation_id": "NEG-CHEM-004",
        "deal_id": "DEAL-2024-CHEM-004",
        "trade_match_id": "TM-CHEM-004",
        "buyer_id": "BUYER-US-004",
        "seller_id": "SELLER-IN-004",
        "product_code": "2909",
        "buyer_country_code": "US",
        "buyer_target_price": 440.0,
        "supplier_quote_price": 620.0,
        "composite_match_score": 0.55,
        "policy": NegotiationPolicy(target_margin_pct=10.0, margin_floor_pct=8.0, max_rounds=4),
        "status": "FAILED",
        "round_no": 4,
        "current_bid": 450.0,
        "current_ask": 590.0,
        "agreed_price": None,
        "margin_at_current": 5.1,
        "hitl_required": False,
        "created_at": _dt("2024-06-05T06:30:00"),
        "updated_at": _dt("2024-06-07T14:00:00"),
    },
    # 5 ── Escalated (Wheat / Agri, India → UAE — margin near floor, escalated to HITL)
    {
        "negotiation_id": "NEG-AGRI-005",
        "deal_id": "DEAL-2024-AGRI-005",
        "trade_match_id": "TM-AGRI-005",
        "buyer_id": "BUYER-AE-005",
        "seller_id": "SELLER-IN-005",
        "product_code": "1001",
        "buyer_country_code": "AE",
        "buyer_target_price": 270.0,
        "supplier_quote_price": 310.0,
        "composite_match_score": 0.68,
        "policy": NegotiationPolicy(target_margin_pct=10.0, margin_floor_pct=8.0, max_rounds=5),
        "status": "ESCALATED",
        "round_no": 3,
        "current_bid": 278.0,
        "current_ask": 305.0,
        "agreed_price": None,
        "margin_at_current": 7.8,
        "hitl_required": True,
        "created_at": _dt("2024-06-12T05:45:00"),
        "updated_at": _dt("2024-06-12T09:10:00"),
    },
]

# ---------------------------------------------------------------------------
# Mock email threads — (negotiation_id, list[dict])
# ---------------------------------------------------------------------------

_EMAILS: dict[str, list[dict]] = {
    "NEG-STEEL-001": [
        {
            "email_id": "EMAIL-STEEL-001-A",
            "negotiation_id": "NEG-STEEL-001",
            "deal_id": "DEAL-2024-STEEL-001",
            "round_no": 1,
            "direction": EmailDirection.OUTBOUND,
            "party": "supplier",
            "to_address": "procurement@steelwerk.de",
            "from_address": _PORTLESS_EMAIL,
            "subject": "HS 7208 Flat-Rolled Steel — Portless Counter-Offer Round 1",
            "body": (
                "Dear StahlWerk GmbH Procurement Team,\n\n"
                "Thank you for your inquiry regarding HS 7208 flat-rolled steel coils.\n\n"
                "We are pleased to present an initial counter-offer of USD 835/MT (CIF Hamburg), "
                "reflecting current mill prices from our certified Indian supplier. "
                "This pricing is valid for a minimum order of 500 MT with a 45-day lead time.\n\n"
                "We look forward to your response.\n\nBest regards,\nPortless Trade Desk"
            ),
            "original_body": None,
            "approval_id": None,
            "status": EmailStatus.SENT,
            "sent_at": _dt("2024-06-10T08:45:00"),
            "created_at": _dt("2024-06-10T08:30:00"),
            "updated_at": _dt("2024-06-10T08:45:00"),
        },
        {
            "email_id": "EMAIL-STEEL-001-B",
            "negotiation_id": "NEG-STEEL-001",
            "deal_id": "DEAL-2024-STEEL-001",
            "round_no": 1,
            "direction": EmailDirection.INBOUND,
            "party": "buyer",
            "to_address": _PORTLESS_EMAIL,
            "from_address": "procurement@steelwerk.de",
            "subject": "Re: HS 7208 Flat-Rolled Steel — Portless Counter-Offer Round 1",
            "body": (
                "Hi Portless Team,\n\n"
                "Thank you for the offer. USD 835/MT is above our budget. "
                "Our maximum is USD 820/MT. Can you see if the supplier can accommodate?\n\n"
                "Best,\nKlaus Müller\nStahlWerk GmbH"
            ),
            "original_body": None,
            "approval_id": None,
            "status": EmailStatus.RECEIVED,
            "sent_at": _dt("2024-06-10T10:20:00"),
            "created_at": _dt("2024-06-10T10:20:00"),
            "updated_at": _dt("2024-06-10T10:20:00"),
        },
        {
            "email_id": "EMAIL-STEEL-001-C",
            "negotiation_id": "NEG-STEEL-001",
            "deal_id": "DEAL-2024-STEEL-001",
            "round_no": 2,
            "direction": EmailDirection.OUTBOUND,
            "party": "supplier",
            "to_address": "procurement@steelwerk.de",
            "from_address": _PORTLESS_EMAIL,
            "subject": "Re: HS 7208 Flat-Rolled Steel — Counter Round 2",
            "body": (
                "Dear Klaus,\n\n"
                "We have negotiated with the mill. "
                "The supplier can meet you at USD 828/MT — splitting the difference from their floor. "
                "Quality certifications (IS 2062 + EN 10025) will be included.\n\n"
                "Please confirm to proceed with the proforma invoice.\n\nRegards,\nPortless Trade Desk"
            ),
            "original_body": None,
            "approval_id": None,
            "status": EmailStatus.SENT,
            "sent_at": _dt("2024-06-10T11:30:00"),
            "created_at": _dt("2024-06-10T11:15:00"),
            "updated_at": _dt("2024-06-10T11:30:00"),
        },
    ],
    "NEG-TEX-002": [
        {
            "email_id": "EMAIL-TEX-002-A",
            "negotiation_id": "NEG-TEX-002",
            "deal_id": "DEAL-2024-TEX-002",
            "round_no": 1,
            "direction": EmailDirection.OUTBOUND,
            "party": "buyer",
            "to_address": "achats@maisontextile.fr",
            "from_address": _PORTLESS_EMAIL,
            "subject": "HS 5208 Cotton Woven Fabric — Opening Offer",
            "body": (
                "Bonjour Maison Textile,\n\n"
                "We are writing on behalf of our Indian supplier to present a quote "
                "for HS 5208 plain-weave cotton fabric: USD 3.45/metre (FOB Nhava Sheva), "
                "for a minimum of 50,000 metres. Lead time: 35 days.\n\n"
                "Best regards,\nPortless Trade Desk"
            ),
            "original_body": None,
            "approval_id": None,
            "status": EmailStatus.SENT,
            "sent_at": _dt("2024-06-11T09:20:00"),
            "created_at": _dt("2024-06-11T09:15:00"),
            "updated_at": _dt("2024-06-11T09:20:00"),
        },
        {
            "email_id": "EMAIL-TEX-002-B",
            "negotiation_id": "NEG-TEX-002",
            "deal_id": "DEAL-2024-TEX-002",
            "round_no": 1,
            "direction": EmailDirection.INBOUND,
            "party": "buyer",
            "to_address": _PORTLESS_EMAIL,
            "from_address": "achats@maisontextile.fr",
            "subject": "Re: HS 5208 Cotton Woven Fabric — Opening Offer",
            "body": (
                "Hello,\n\nThank you. We can accept USD 3.25/metre maximum. "
                "Also we require OEKO-TEX Standard 100 certification. "
                "Please confirm.\n\nCordialement,\nSophie Dubois"
            ),
            "original_body": None,
            "approval_id": None,
            "status": EmailStatus.RECEIVED,
            "sent_at": _dt("2024-06-11T10:05:00"),
            "created_at": _dt("2024-06-11T10:05:00"),
            "updated_at": _dt("2024-06-11T10:05:00"),
        },
        {
            "email_id": "EMAIL-TEX-002-C",
            "negotiation_id": "NEG-TEX-002",
            "deal_id": "DEAL-2024-TEX-002",
            "round_no": 1,
            "direction": EmailDirection.OUTBOUND,
            "party": "buyer",
            "to_address": "achats@maisontextile.fr",
            "from_address": _PORTLESS_EMAIL,
            "subject": "Re: HS 5208 Cotton Woven Fabric — Counter Round 1",
            "body": (
                "Bonjour Sophie,\n\n"
                "Our supplier confirms OEKO-TEX 100 compliance. "
                "We can offer USD 3.35/metre as a firm counter — "
                "this includes the certification overhead. "
                "Margin is tight at current pricing; awaiting your confirmation.\n\n"
                "Best,\nPortless Trade Desk"
            ),
            "original_body": None,
            "approval_id": "APPR-TEX-002-HITL",
            "status": EmailStatus.PENDING_APPROVAL,
            "sent_at": None,
            "created_at": _dt("2024-06-11T10:45:00"),
            "updated_at": _dt("2024-06-11T10:45:00"),
        },
    ],
    "NEG-ELEC-003": [
        {
            "email_id": "EMAIL-ELEC-003-A",
            "negotiation_id": "NEG-ELEC-003",
            "deal_id": "DEAL-2024-ELEC-003",
            "round_no": 1,
            "direction": EmailDirection.OUTBOUND,
            "party": "buyer",
            "to_address": "procurement@techsource.sg",
            "from_address": _PORTLESS_EMAIL,
            "subject": "HS 8542 Integrated Circuits — Opening Quote",
            "body": (
                "Dear TechSource Singapore,\n\n"
                "Attached is our opening quotation for HS 8542.31 (NAND Flash, 128 GB): "
                "USD 1,290/unit (DAP Singapore), MOQ 1,000 units, lead time 21 days.\n\n"
                "Portless Trade Desk"
            ),
            "original_body": None,
            "approval_id": None,
            "status": EmailStatus.SENT,
            "sent_at": _dt("2024-06-08T07:30:00"),
            "created_at": _dt("2024-06-08T07:00:00"),
            "updated_at": _dt("2024-06-08T07:30:00"),
        },
        {
            "email_id": "EMAIL-ELEC-003-B",
            "negotiation_id": "NEG-ELEC-003",
            "deal_id": "DEAL-2024-ELEC-003",
            "round_no": 1,
            "direction": EmailDirection.INBOUND,
            "party": "buyer",
            "to_address": _PORTLESS_EMAIL,
            "from_address": "procurement@techsource.sg",
            "subject": "Re: HS 8542 Integrated Circuits — Opening Quote",
            "body": (
                "Hi, our budget is USD 1,230. Can you do better?\n\nRegards, David Tan"
            ),
            "original_body": None,
            "approval_id": None,
            "status": EmailStatus.RECEIVED,
            "sent_at": _dt("2024-06-08T09:00:00"),
            "created_at": _dt("2024-06-08T09:00:00"),
            "updated_at": _dt("2024-06-08T09:00:00"),
        },
        {
            "email_id": "EMAIL-ELEC-003-C",
            "negotiation_id": "NEG-ELEC-003",
            "deal_id": "DEAL-2024-ELEC-003",
            "round_no": 2,
            "direction": EmailDirection.OUTBOUND,
            "party": "buyer",
            "to_address": "procurement@techsource.sg",
            "from_address": _PORTLESS_EMAIL,
            "subject": "Re: HS 8542 IC — Counter Round 2",
            "body": (
                "Dear David,\n\nWe can meet at USD 1,260. "
                "This is the supplier's best price given current wafer costs.\n\nPortless"
            ),
            "original_body": None,
            "approval_id": None,
            "status": EmailStatus.SENT,
            "sent_at": _dt("2024-06-08T11:00:00"),
            "created_at": _dt("2024-06-08T10:45:00"),
            "updated_at": _dt("2024-06-08T11:00:00"),
        },
        {
            "email_id": "EMAIL-ELEC-003-D",
            "negotiation_id": "NEG-ELEC-003",
            "deal_id": "DEAL-2024-ELEC-003",
            "round_no": 3,
            "direction": EmailDirection.INBOUND,
            "party": "buyer",
            "to_address": _PORTLESS_EMAIL,
            "from_address": "procurement@techsource.sg",
            "subject": "Re: HS 8542 IC — AGREED at 1250",
            "body": (
                "We accept USD 1,250. Please send the Proforma Invoice.\n\nDavid Tan"
            ),
            "original_body": None,
            "approval_id": None,
            "status": EmailStatus.RECEIVED,
            "sent_at": _dt("2024-06-09T16:20:00"),
            "created_at": _dt("2024-06-09T16:20:00"),
            "updated_at": _dt("2024-06-09T16:20:00"),
        },
    ],
    "NEG-CHEM-004": [
        {
            "email_id": "EMAIL-CHEM-004-A",
            "negotiation_id": "NEG-CHEM-004",
            "deal_id": "DEAL-2024-CHEM-004",
            "round_no": 1,
            "direction": EmailDirection.OUTBOUND,
            "party": "buyer",
            "to_address": "supply@chemcorp.us",
            "from_address": _PORTLESS_EMAIL,
            "subject": "HS 2909 Industrial Solvents — Opening Quotation",
            "body": (
                "Dear ChemCorp,\n\n"
                "We present an opening price of USD 580/MT (CIF Houston) for HS 2909 "
                "Diethyl Ether (technical grade). MOQ 20 MT. Lead time 40 days.\n\n"
                "Portless Trade Desk"
            ),
            "original_body": None,
            "approval_id": None,
            "status": EmailStatus.SENT,
            "sent_at": _dt("2024-06-05T07:00:00"),
            "created_at": _dt("2024-06-05T06:30:00"),
            "updated_at": _dt("2024-06-05T07:00:00"),
        },
        {
            "email_id": "EMAIL-CHEM-004-B",
            "negotiation_id": "NEG-CHEM-004",
            "deal_id": "DEAL-2024-CHEM-004",
            "round_no": 2,
            "direction": EmailDirection.INBOUND,
            "party": "buyer",
            "to_address": _PORTLESS_EMAIL,
            "from_address": "supply@chemcorp.us",
            "subject": "Re: HS 2909 Industrial Solvents",
            "body": (
                "Our ceiling price is USD 460/MT. We have competing offers at that level. "
                "Can you match?\n\nRegards, Jim Carter"
            ),
            "original_body": None,
            "approval_id": None,
            "status": EmailStatus.RECEIVED,
            "sent_at": _dt("2024-06-06T13:00:00"),
            "created_at": _dt("2024-06-06T13:00:00"),
            "updated_at": _dt("2024-06-06T13:00:00"),
        },
        {
            "email_id": "EMAIL-CHEM-004-C",
            "negotiation_id": "NEG-CHEM-004",
            "deal_id": "DEAL-2024-CHEM-004",
            "round_no": 4,
            "direction": EmailDirection.OUTBOUND,
            "party": "buyer",
            "to_address": "supply@chemcorp.us",
            "from_address": _PORTLESS_EMAIL,
            "subject": "Re: HS 2909 — Negotiation Closed (No Agreement)",
            "body": (
                "Dear Jim,\n\n"
                "After four rounds of negotiation, we are unable to bridge the price gap. "
                "The supplier's floor of USD 540/MT cannot meet your ceiling of USD 460/MT "
                "while maintaining required margins. We are closing this negotiation.\n\n"
                "We hope to work together on future opportunities.\n\nPortless Trade Desk"
            ),
            "original_body": None,
            "approval_id": None,
            "status": EmailStatus.SENT,
            "sent_at": _dt("2024-06-07T14:00:00"),
            "created_at": _dt("2024-06-07T13:45:00"),
            "updated_at": _dt("2024-06-07T14:00:00"),
        },
    ],
    "NEG-AGRI-005": [
        {
            "email_id": "EMAIL-AGRI-005-A",
            "negotiation_id": "NEG-AGRI-005",
            "deal_id": "DEAL-2024-AGRI-005",
            "round_no": 1,
            "direction": EmailDirection.OUTBOUND,
            "party": "buyer",
            "to_address": "grain@dubaifoodco.ae",
            "from_address": _PORTLESS_EMAIL,
            "subject": "HS 1001 Wheat — Offer Round 1",
            "body": (
                "Dear Dubai Food Co.,\n\n"
                "We offer Indian HRW wheat (HS 1001.99) at USD 295/MT (CIF Jebel Ali), "
                "100 MT minimum, 30-day lead time. FSSAI and phytosanitary certificates included.\n\n"
                "Portless Trade Desk"
            ),
            "original_body": None,
            "approval_id": None,
            "status": EmailStatus.SENT,
            "sent_at": _dt("2024-06-12T06:00:00"),
            "created_at": _dt("2024-06-12T05:45:00"),
            "updated_at": _dt("2024-06-12T06:00:00"),
        },
        {
            "email_id": "EMAIL-AGRI-005-B",
            "negotiation_id": "NEG-AGRI-005",
            "deal_id": "DEAL-2024-AGRI-005",
            "round_no": 2,
            "direction": EmailDirection.INBOUND,
            "party": "buyer",
            "to_address": _PORTLESS_EMAIL,
            "from_address": "grain@dubaifoodco.ae",
            "subject": "Re: HS 1001 Wheat",
            "body": (
                "We can go to USD 278/MT maximum — global wheat prices have dropped this week. "
                "Please check with your supplier.\n\nAhmed Al Rashidi"
            ),
            "original_body": None,
            "approval_id": None,
            "status": EmailStatus.RECEIVED,
            "sent_at": _dt("2024-06-12T08:30:00"),
            "created_at": _dt("2024-06-12T08:30:00"),
            "updated_at": _dt("2024-06-12T08:30:00"),
        },
    ],
}


# ---------------------------------------------------------------------------
# Public seeding entry-point
# ---------------------------------------------------------------------------

async def seed_mock_negotiation_data() -> None:
    """Populate the in-memory repositories with 5 demo negotiations."""
    session_repo = get_repository()
    email_repo = get_email_repository()

    # Skip if data already seeded (restart-safe)
    existing = await session_repo.list_sessions()
    if existing:
        logger.debug("Mock negotiation data already present — skipping seed")
        return

    logger.warning("Seeding 5 mock negotiation scenarios...")

    for s_dict in _SESSIONS:
        session = NegotiationSession(**s_dict)
        await session_repo.save_session(session)

    for neg_id, email_list in _EMAILS.items():
        for e_dict in email_list:
            record = EmailHistoryRecord(**e_dict)
            await email_repo.save(record)

    logger.warning("Mock negotiation seed complete (5 sessions, %d emails)", sum(len(v) for v in _EMAILS.values()))
