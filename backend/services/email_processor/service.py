"""Email Processor service — processes inbound emails and advances negotiations.

Processing pipeline for each inbound email:

  1. Deduplicate by message_id (idempotent re-delivery from webhooks)
  2. Persist RECEIVED record immediately (crash-safe audit trace)
  3. Map to negotiation session (mapper.map_email — header / subject / override)
  4. Extract counter-price from body text (regex patterns)
  5a. price found + negotiation mapped  → call run_round(), status=PROCESSED
  5b. price missing                     → status=NEEDS_OPERATOR, create HITL
  5c. mapping unmatched                 → status=UNMATCHED, log warning
  6. Persist final status on the InboundEmailRecord
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

from .config import settings
from .mapper import map_email
from .repository import get_repository
from .schemas import (
    InboundEmailRecord,
    InboundEmailStatus,
    InboundRawEmail,
    MappingConfidence,
    ProcessingResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Price extraction
# ---------------------------------------------------------------------------
# Patterns ordered from most specific (explicit currency) to most general.
# Each captures the numeric amount in group 1.
_PRICE_PATTERNS = [
    re.compile(r"[₹$]\s*([\d,]+(?:\.\d{1,2})?)\s*(?:/\s*(?:MT|kg|unit|pc|pcs|piece|ton|lb))?", re.IGNORECASE),
    re.compile(r"([\d,]+(?:\.\d{1,2})?)\s*(?:USD|usd|INR|inr|Rs\.?|rupees?)\b"),
    re.compile(r"(?:price|offer|quote|bid|ask|propose|counter)[^\d$₹]{0,20}([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE),
    re.compile(r"([\d,]+(?:\.\d{1,2})?)\s*(?:per\s+(?:MT|kg|unit|piece|pc|pcs|ton))", re.IGNORECASE),
    # Internal marker injected by the manual submission endpoint
    re.compile(r"\[PRICE:([\d.]+)\s*(?:USD)?\]"),
]

# A price token is INR-denominated when a rupee marker sits immediately next to
# the number on either side — "₹90,901.91", "90901.91 INR", "Rs. 90,901".
# The supplier-facing negotiation agent quotes in INR by design (it converts the
# USD ask at the live USD/INR rate), so its replies echo a rupee figure. Without
# this the rupee amount is read as USD (~96× too high) and the negotiation's
# price-plausibility guard vetoes every supplier reply.
_INR_CONTEXT = re.compile(r"[₹]|\b(?:INR|inr|Rs\.?|rupees?)\b")


def _looks_like_inr(body: str, match: re.Match[str]) -> bool:
    """True if a rupee marker sits within a few chars of the matched number."""
    start, end = match.span(1)
    left = body[max(0, start - 6):start]
    right = body[end:end + 8]
    return bool(_INR_CONTEXT.search(left) or _INR_CONTEXT.search(right))


async def _usd_inr_rate() -> float:
    """Live USD/INR rate via the negotiation agent's shared helper; falls back to
    its configured static rate. Never raises — a rate is always returned."""
    try:
        from agents.negotiation_agent.market_intel import get_usd_inr_rate  # type: ignore[import]
        rate = await get_usd_inr_rate()
        if rate and rate > 0:
            return float(rate)
    except Exception as exc:  # noqa: BLE001
        logger.debug("USD/INR rate lookup failed (%s) — using static fallback", exc)
    try:
        from agents.negotiation_agent.config import settings as neg_settings  # type: ignore[import]
        return float(neg_settings.fallback_usd_inr_rate)
    except Exception:  # noqa: BLE001
        return 96.0


async def _extract_price(body: str) -> float | None:
    """Extract the counter price from an email body, normalised to USD.

    Amounts denominated in INR (₹/INR/Rs near the number) are converted to USD
    at the current rate before the USD plausibility bounds are applied, so a
    rupee-quoted supplier reply doesn't read as a ~96× USD figure.
    """
    for pattern in _PRICE_PATTERNS:
        m = pattern.search(body)
        if not m:
            continue
        try:
            value = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if _looks_like_inr(body, m):
            rate = await _usd_inr_rate()
            usd = round(value / rate, 4)
            logger.info(
                "Extracted INR price ₹%s → $%.4f USD (rate %.2f)", value, usd, rate
            )
            value = usd
        if settings.price_extraction_min <= value <= settings.price_extraction_max:
            return value
    return None


# ---------------------------------------------------------------------------
# Main processing entry point
# ---------------------------------------------------------------------------

async def process_inbound(
    raw: InboundRawEmail,
    negotiation_id_override: str | None = None,
    party_override: str | None = None,
    counter_price_override: float | None = None,
) -> ProcessingResult:
    """Process one inbound email end-to-end.  Safe to call multiple times
    for the same message_id (idempotent via deduplication)."""
    repo = get_repository()
    now = datetime.now(timezone.utc)
    inbound_id = f"IN-{uuid.uuid4().hex[:10].upper()}"

    # ── Deduplicate by message_id ──────────────────────────────────────────
    if raw.message_id:
        existing = await repo.find_by_message_id(raw.message_id)
        if existing:
            logger.info(
                "Duplicate inbound email message_id=%s — already stored as %s",
                raw.message_id, existing.inbound_id,
            )
            return ProcessingResult(
                inbound_id=existing.inbound_id,
                status=existing.status,
                negotiation_id=existing.negotiation_id,
                message="Duplicate — already processed",
            )

    # ── Persist initial record immediately (crash-safe) ───────────────────
    record = InboundEmailRecord(
        inbound_id=inbound_id,
        from_address=raw.from_address,
        to_address=raw.to_address,
        subject=raw.subject,
        body_text=raw.body_text,
        body_html=raw.body_html,
        message_id=raw.message_id,
        in_reply_to=raw.in_reply_to,
        references=raw.references,
        attachments=raw.attachments,
        source=raw.source,
        status=InboundEmailStatus.RECEIVED,
        received_at=raw.received_at,
        created_at=now,
        updated_at=now,
    )
    await repo.save(record)

    # ── Map to negotiation session ─────────────────────────────────────────
    mapping = await map_email(
        from_address=raw.from_address,
        subject=raw.subject,
        in_reply_to=raw.in_reply_to,
        references=raw.references,
        negotiation_id_override=negotiation_id_override,
        party_override=party_override,
    )

    if not mapping.negotiation_id:
        record = record.model_copy(update={
            "status": InboundEmailStatus.UNMATCHED,
            "mapping_confidence": mapping.confidence,
            "error": mapping.reason,
            "updated_at": datetime.now(timezone.utc),
        })
        await repo.save(record)
        logger.warning("Inbound %s unmatched: %s", inbound_id, mapping.reason)
        return ProcessingResult(
            inbound_id=inbound_id,
            status=InboundEmailStatus.UNMATCHED,
            message=mapping.reason,
        )

    # ── Update record with mapping result ─────────────────────────────────
    deal_id: str | None = None
    try:
        from agents.negotiation_agent.repository import get_repository as neg_repo  # type: ignore[import]
        session = await neg_repo().get_session(mapping.negotiation_id)
        deal_id = session.deal_id if session else None
    except Exception as exc:
        logger.debug("Could not fetch deal_id for negotiation %s: %s", mapping.negotiation_id, exc)

    record = record.model_copy(update={
        "status": InboundEmailStatus.MAPPED,
        "negotiation_id": mapping.negotiation_id,
        "deal_id": deal_id,
        "party": mapping.party,
        "mapping_confidence": mapping.confidence,
        "updated_at": datetime.now(timezone.utc),
    })
    await repo.save(record)

    # Record inbound email in negotiation agent's own email_history audit log
    try:
        from agents.negotiation_agent.service import record_inbound_email  # type: ignore[import]
        from agents.negotiation_agent.repository import get_repository as neg_repo  # type: ignore[import]
        neg_session = await neg_repo().get_session(mapping.negotiation_id)
        await record_inbound_email(
            negotiation_id=mapping.negotiation_id,
            deal_id=deal_id or "",
            round_no=neg_session.round_no if neg_session else 0,
            party=mapping.party or "unknown",
            from_address=raw.from_address,
            subject=raw.subject,
            body=raw.body_text,
        )
    except Exception as exc:
        logger.debug("record_inbound_email (non-fatal): %s", exc)

    # ── Extract counter-price ──────────────────────────────────────────────
    price = counter_price_override or await _extract_price(raw.body_text)

    if price is None:
        record = record.model_copy(update={
            "status": InboundEmailStatus.NEEDS_OPERATOR,
            "updated_at": datetime.now(timezone.utc),
        })
        await repo.save(record)
        hitl_id = await _create_price_hitl(record)
        record = record.model_copy(update={
            "hitl_approval_id": hitl_id,
            "updated_at": datetime.now(timezone.utc),
        })
        await repo.save(record)
        logger.info(
            "Inbound %s mapped to %s but no price found — HITL %s created",
            inbound_id, mapping.negotiation_id, hitl_id,
        )
        return ProcessingResult(
            inbound_id=inbound_id,
            status=InboundEmailStatus.NEEDS_OPERATOR,
            negotiation_id=mapping.negotiation_id,
            message=f"Price extraction failed. HITL approval {hitl_id} created for operator.",
        )

    record = record.model_copy(update={
        "extracted_price": price,
        "updated_at": datetime.now(timezone.utc),
    })
    await repo.save(record)

    # ── Advance the negotiation ────────────────────────────────────────────
    try:
        from agents.negotiation_agent.service import run_round  # type: ignore[import]
        from agents.negotiation_agent.schemas import RoundRequest  # type: ignore[import]

        neg_response = await run_round(
            mapping.negotiation_id,
            RoundRequest(
                counter_price=price,
                party=mapping.party or "buyer",
                channel="email",
                inbound_message=raw.body_text,
                attachment_refs=[a.filename for a in raw.attachments] or None,
            ),
        )

        record = record.model_copy(update={
            "status": InboundEmailStatus.PROCESSED,
            "processed_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        })
        await repo.save(record)
        logger.info(
            "Inbound %s → negotiation %s round %d price=%.4f neg_status=%s",
            inbound_id, mapping.negotiation_id, neg_response.round_no,
            price, neg_response.status,
        )
        return ProcessingResult(
            inbound_id=inbound_id,
            status=InboundEmailStatus.PROCESSED,
            negotiation_id=mapping.negotiation_id,
            round_no=neg_response.round_no,
            message=f"Round {neg_response.round_no} advanced. Negotiation status: {neg_response.status}",
        )
    except Exception as exc:
        logger.error("run_round failed for negotiation %s: %s", mapping.negotiation_id, exc)
        record = record.model_copy(update={
            "status": InboundEmailStatus.NEEDS_OPERATOR,
            "error": str(exc),
            "updated_at": datetime.now(timezone.utc),
        })
        await repo.save(record)
        hitl_id = await _create_price_hitl(record)
        record = record.model_copy(update={
            "hitl_approval_id": hitl_id,
            "updated_at": datetime.now(timezone.utc),
        })
        await repo.save(record)
        return ProcessingResult(
            inbound_id=inbound_id,
            status=InboundEmailStatus.NEEDS_OPERATOR,
            negotiation_id=mapping.negotiation_id,
            message=f"run_round() failed: {exc}",
        )


# ---------------------------------------------------------------------------
# HITL helper
# ---------------------------------------------------------------------------

async def _create_price_hitl(record: InboundEmailRecord) -> str:
    """Create a HITL approval so an operator can supply the missing price."""
    try:
        from services.hitl_approval.service import create_approval  # type: ignore[import]
        from services.hitl_approval.schemas import CreateApprovalRequest  # type: ignore[import]

        req = CreateApprovalRequest(
            deal_id=record.deal_id or record.negotiation_id or record.inbound_id,
            action="PARSE_INBOUND_EMAIL",
            summary=(
                f"Inbound email from {record.from_address} — price could not be "
                f"extracted automatically. Preview: {record.body_text[:200]}"
            ),
            risk_flags=["price_extraction_failed"],
            autonomy_level="L0",
            callback_payload={
                "inbound_id": record.inbound_id,
                "negotiation_id": record.negotiation_id,
                "party": record.party,
                "from_address": record.from_address,
                "body_preview": record.body_text[:500],
            },
            idempotency_key=record.inbound_id,
        )
        approval = await create_approval(req)
        return approval.approval_id
    except Exception as exc:
        logger.error("HITL creation failed for inbound %s: %s", record.inbound_id, exc)
        return ""


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

async def get_inbound(inbound_id: str) -> InboundEmailRecord:
    record = await get_repository().get(inbound_id)
    if not record:
        raise ValueError(f"Inbound email not found: {inbound_id}")
    return record


async def list_inbound(
    status: InboundEmailStatus | None = None,
    negotiation_id: str | None = None,
    limit: int = 100,
) -> list[InboundEmailRecord]:
    return await get_repository().list_all(
        status=status, negotiation_id=negotiation_id, limit=limit
    )
