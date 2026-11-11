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


def _is_self_echo(from_address: str) -> bool:
    """True if *from_address* is PortLess's own configured outbound sender.

    Reads GMAIL_SENDER_ADDRESS from the Email Gateway's own settings (the
    single source of truth for "who PortLess sends as") rather than
    duplicating the address here. Compared case-insensitively and only when
    both sides are non-empty — an empty configured sender (dev/CI, no real
    mailbox wired up) must never make every inbound email match.
    """
    if not from_address:
        return False
    try:
        from services.email_gateway.config import settings as gateway_settings  # type: ignore[import]
        sender = gateway_settings.gmail_sender_address
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read email_gateway sender address: %s", exc)
        return False
    if not sender:
        return False
    return from_address.strip().casefold() == sender.strip().casefold()


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


# Words that mark a figure as the sender's OWN counter rather than an echo of
# ours ("your price of USD 1,316 is too high — we can counter at USD 1,200").
_COUNTER_CONTEXT = re.compile(
    r"\b(?:counter(?:[- ]?offer)?|(?:can|could|will|would)\s+(?:do|go|pay|offer|accept|proceed|agree)|"
    r"willing\s+to\s+(?:pay|go|accept)|propos\w*|proceed\s+at|accept\s+at|agree\s+(?:at|to)|revised|improve\w*|"
    r"our\s+(?:offer|target|budget|counter|best)|budget\s+(?:is|of)|best\s+(?:price|offer)\s+(?:is|of)|"
    r"we\s+offer|i\s+offer|offer\s+you)\b",
    re.IGNORECASE,
)


async def _extract_price(body: str, ignore_price: float | None = None) -> float | None:
    """Extract the counter price from an email body, normalised to USD.

    Every price in the new part of the reply is a candidate (quoted history is
    dropped first). A figure equal to ``ignore_price`` — our own last offer to
    this party, which counterparties usually repeat ("your USD 1,316.43 is above
    our budget") — is skipped when the reply holds another figure, and a figure
    introduced as their counter ("we can counter at …", "we can offer …") beats
    the rest. Otherwise the first figure in the text wins.

    Amounts denominated in INR (₹/INR/Rs near the number) are converted to USD
    at the current rate before the USD plausibility bounds are applied, so a
    rupee-quoted supplier reply doesn't read as a ~96× USD figure.
    """
    try:
        from agents.negotiation_agent.guards import _new_reply_text  # type: ignore[import]
        text = _new_reply_text(body) or body
    except Exception:  # noqa: BLE001
        text = body
    if "[PRICE:" in body:  # the manual endpoint's marker is authoritative
        text = body

    rate: float | None = None
    found: dict[int, tuple[float, bool]] = {}  # number start → (USD value, counter context)
    for pattern in _PRICE_PATTERNS:
        for m in pattern.finditer(text):
            start = m.start(1)
            if start in found:
                continue
            try:
                value = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if _looks_like_inr(text, m):
                rate = rate or await _usd_inr_rate()
                usd = round(value / rate, 4)
                logger.info("Extracted INR price ₹%s → $%.4f USD (rate %.2f)", value, usd, rate)
                value = usd
            if not settings.price_extraction_min <= value <= settings.price_extraction_max:
                continue
            before = text[max(0, m.start() - 45):start]
            found[start] = (value, bool(_COUNTER_CONTEXT.search(before)) or "[PRICE:" in m.group(0))
    if not found:
        return None
    ordered = [found[k] for k in sorted(found)]
    if ignore_price:
        others = [c for c in ordered if abs(c[0] - ignore_price) > 0.01]
        if others:
            ordered = others
    countered = [v for v, ctx in ordered if ctx]
    return countered[0] if countered else ordered[0][0]


# ---------------------------------------------------------------------------
# Main processing entry point
# ---------------------------------------------------------------------------

async def process_inbound(
    raw: InboundRawEmail,
    negotiation_id_override: str | None = None,
    party_override: str | None = None,
    counter_price_override: float | None = None,
) -> ProcessingResult:
    """Process one inbound email end-to-end, then remember it in knowledge-service
    (RAG) with the deal and party the mapping found."""
    result = await _process_inbound(raw, negotiation_id_override, party_override, counter_price_override)
    if result.message != "Duplicate — already processed":
        try:
            from shared.orchestration.memory import remember_email
            record = await get_repository().get(result.inbound_id)
            await remember_email(
                direction="inbound", from_address=raw.from_address, to_address=raw.to_address,
                subject=raw.subject, body=raw.body_text or "", message_id=raw.message_id or result.inbound_id,
                deal_id=getattr(record, "deal_id", None), party=getattr(record, "party", None),
            )
        except Exception:  # noqa: BLE001 — memory never fails inbound processing
            logger.debug("Inbound memory skipped for %s", result.inbound_id)
    return result


async def _process_inbound(
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

    # ── Reject self-echo before anything else touches it ──────────────────
    # A shared send+poll mailbox (or an auto-forward/label rule that files
    # PortLess's own sent mail into the polled folder) can hand the exact
    # email PortLess just sent right back to this function as if it were a
    # genuine reply — from_address equals our own outbound sender. Treating
    # that as real input starts a self-feeding loop: draft -> send -> echo
    # back in -> draft again, without either counterparty ever replying.
    # Checked case-insensitively (email local-parts/domains are effectively
    # case-insensitive in practice) before mapping/price-extraction/run_round
    # ever run, so no round is ever advanced off our own mail.
    if _is_self_echo(raw.from_address):
        record = record.model_copy(update={
            "status": InboundEmailStatus.SELF_ECHO,
            "error": (
                f"from_address {raw.from_address!r} matches PortLess's own "
                "outbound sender — treated as a self-echo, not a reply"
            ),
            "updated_at": datetime.now(timezone.utc),
        })
        await repo.save(record)
        logger.warning(
            "Inbound %s is a self-echo (from=%s) — discarding, no round advanced",
            inbound_id, raw.from_address,
        )
        return ProcessingResult(
            inbound_id=inbound_id,
            status=InboundEmailStatus.SELF_ECHO,
            message="Self-echo: from_address matches our own outbound sender.",
        )

    # ── Map to negotiation session ─────────────────────────────────────────
    mapping = await map_email(
        from_address=raw.from_address,
        subject=raw.subject,
        in_reply_to=raw.in_reply_to,
        references=raw.references,
        negotiation_id_override=negotiation_id_override,
        party_override=party_override,
    )

    # ── Buyer answering an outreach introduction (buyer funnel) ────────────
    if getattr(mapping, "outreach_deal_id", None) and not mapping.negotiation_id:
        return await _handle_outreach_reply(record, raw, mapping)

    # ── Supplier quote replying to an RFQ e-mail (sourcing stage) ──────────
    if getattr(mapping, "rfq_id", None) and not mapping.negotiation_id:
        return await _handle_rfq_reply(record, raw, mapping, counter_price_override)

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

    # ── Who really sent it? ────────────────────────────────────────────────
    # Many negotiations run at once. A reply only moves this one when we know
    # which side it is from AND the sender is a known address of that side.
    # Anything else — a forwarded thread, a third party, the OTHER side writing
    # on this thread, a colleague on a free-mail address — is held for an
    # operator, and never enters the drafting agents' history (so no buyer is
    # ever answered with a supplier's words, or vice versa).
    if mapping.party not in ("buyer", "supplier") or mapping.sender_check not in ("verified", "operator"):
        return await _hold_unverified_sender(record, mapping)

    # Record inbound email in negotiation agent's own email_history audit log
    try:
        from agents.negotiation_agent.service import record_inbound_email  # type: ignore[import]
        from agents.negotiation_agent.repository import get_repository as neg_repo  # type: ignore[import]
        neg_session = await neg_repo().get_session(mapping.negotiation_id)
        await record_inbound_email(
            negotiation_id=mapping.negotiation_id,
            deal_id=deal_id or "",
            round_no=neg_session.round_no if neg_session else 0,
            party=mapping.party,
            from_address=raw.from_address,
            subject=raw.subject,
            body=raw.body_text,
        )
    except Exception as exc:
        logger.debug("record_inbound_email (non-fatal): %s", exc)

    # The deal's own supplier, verified: its facility address (where the goods are
    # inspected) is read from whatever it writes, at any stage.
    if mapping.party == "supplier" and deal_id:
        from shared.orchestration import supplier_address
        await supplier_address.capture(deal_id, raw.body_text or "", by=f"supplier e-mail {inbound_id}")

    # ── Post-agreement reply (PI acceptance etc.) — never a negotiation round ──
    if mapping.session_status == "AGREED":
        return await _handle_post_agreement_reply(record, raw, mapping)

    # ── Extract counter-price ──────────────────────────────────────────────
    our_last_offer = None
    try:
        from agents.negotiation_agent.repository import get_repository as _neg_repo  # type: ignore[import]
        _session = await _neg_repo().get_session(mapping.negotiation_id)
        if _session is not None:
            our_last_offer = (_session.last_offer_to_buyer if mapping.party == "buyer"
                              else _session.last_offer_to_supplier)
    except Exception as exc:  # noqa: BLE001
        logger.debug("last-offer lookup (non-fatal): %s", exc)
    price = counter_price_override or await _extract_price(raw.body_text, ignore_price=our_last_offer)
    if price is None:
        price = await _price_of_bare_acceptance(mapping.negotiation_id, mapping.party, raw.body_text)

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
                party=mapping.party,
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


async def _price_of_bare_acceptance(negotiation_id: str, party: str, body: str) -> float | None:
    """"We accept your offer" names no price: it accepts the last price we put
    to that party. Returns it, so the round runs and the agreement reaches its
    CONFIRM_NEGOTIATION_AGREEMENT gate. None for anything that is not an
    acceptance, or when we never offered that party a price."""
    try:
        from agents.negotiation_agent.graph import _INTENT_UNAVAILABLE, classify_inbound_intent  # type: ignore[import]
        from agents.negotiation_agent.guards import is_explicit_acceptance  # type: ignore[import]
        from agents.negotiation_agent.repository import get_repository as neg_repo  # type: ignore[import]

        session = await neg_repo().get_session(negotiation_id)
        offered = None
        if session is not None:
            offered = session.last_offer_to_buyer if party == "buyer" else session.last_offer_to_supplier
        if not offered:
            return None
        verdict = await classify_inbound_intent(party, body, offered)
        intent = verdict["intent"]
        accepted = is_explicit_acceptance(body) if intent == _INTENT_UNAVAILABLE else intent == "ACCEPT"
        return float(offered) if accepted else None
    except Exception as exc:  # noqa: BLE001 — an operator reads it instead
        logger.warning("Bare-acceptance check failed for negotiation %s: %s", negotiation_id, exc)
        return None


_SENDER_REASON = {
    "other_party": "the sender is a known address of the OTHER side of this negotiation",
    "ambiguous": "the sender matches both the buyer and the supplier",
    "unknown": "the sender is not a known address of either side",
}


async def _hold_unverified_sender(record: InboundEmailRecord, mapping) -> ProcessingResult:
    """Park a reply whose sender could not be tied to the right side. Nothing
    advances; an operator confirms who it is from and resubmits it through
    ``POST /email-processor/inbound/manual`` with ``negotiation_id`` + ``party``."""
    repo = get_repository()
    check = mapping.sender_check or "unknown"
    if mapping.party not in ("buyer", "supplier"):
        why = "it could not be determined whether it is from the buyer or the supplier"
    else:
        why = f"it looks like the {mapping.party}'s reply, but {_SENDER_REASON.get(check, check)}"
    error = f"Sender {record.from_address} not verified for negotiation {mapping.negotiation_id}: {why}"
    hitl_id = await _create_price_hitl(
        record,
        summary=(f"Reply from {record.from_address} on negotiation {mapping.negotiation_id} was NOT processed: "
                 f"{why}. Confirm who sent it, then resubmit it with the negotiation id and party. "
                 f"Preview: {record.body_text[:200]}"),
        risk_flags=["sender_not_verified", f"sender_{check}"],
    )
    record = record.model_copy(update={
        "status": InboundEmailStatus.NEEDS_OPERATOR,
        "error": error,
        "hitl_approval_id": hitl_id,
        "updated_at": datetime.now(timezone.utc),
    })
    await repo.save(record)
    logger.warning("Inbound %s held: %s", record.inbound_id, error)
    return ProcessingResult(
        inbound_id=record.inbound_id,
        status=InboundEmailStatus.NEEDS_OPERATOR,
        negotiation_id=mapping.negotiation_id,
        message=error,
    )


# ---------------------------------------------------------------------------
# Outreach replies (buyer funnel)
# ---------------------------------------------------------------------------

async def _handle_outreach_reply(record: InboundEmailRecord, raw: InboundRawEmail, mapping) -> ProcessingResult:
    repo = get_repository()
    deal_id, candidate_id = mapping.outreach_deal_id, mapping.outreach_candidate_id
    record = record.model_copy(update={
        "status": InboundEmailStatus.MAPPED, "deal_id": deal_id, "party": "buyer",
        "mapping_confidence": mapping.confidence, "updated_at": datetime.now(timezone.utc),
    })
    await repo.save(record)
    try:
        from shared.orchestration import outreach
        verdict = await outreach.handle_reply(deal_id, candidate_id, raw.from_address, raw.body_text)
    except Exception as exc:  # noqa: BLE001
        logger.error("Outreach reply %s: handling failed: %s", record.inbound_id, exc)
        verdict = {"verdict": "unclear", "error": str(exc)}

    if verdict.get("verdict") in ("interested", "declined") and verdict.get("matched", True):
        record = record.model_copy(update={
            "status": InboundEmailStatus.PROCESSED, "processed_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        })
        await repo.save(record)
        return ProcessingResult(
            inbound_id=record.inbound_id, status=InboundEmailStatus.PROCESSED,
            message=f"Buyer reply classified as {verdict['verdict']} for deal {deal_id}"
                    + (" — buyer set, negotiation opens" if verdict.get("buyer_set") else "")
                    + (" — no interested buyer left" if verdict.get("exhausted") else ""),
        )

    if verdict.get("sender_mismatch"):
        summary = (f"{record.from_address} answered the outreach to candidate {candidate_id} of deal {deal_id}, "
                   "but is not the address we wrote to — confirm who this is before treating it as that "
                   f"buyer's reply. Preview: {record.body_text[:200]}")
        flags = ["outreach_reply", "sender_not_verified"]
    else:
        summary = (f"Buyer {record.from_address} replied to the outreach for deal {deal_id} but the "
                   f"intent is unclear — read it and mark the buyer interested / skip. Preview: {record.body_text[:200]}")
        flags = ["outreach_reply", "intent_unclear"]
    hitl_id = await _create_price_hitl(record, summary=summary, risk_flags=flags)
    record = record.model_copy(update={
        "status": InboundEmailStatus.NEEDS_OPERATOR, "hitl_approval_id": hitl_id,
        "updated_at": datetime.now(timezone.utc),
    })
    await repo.save(record)
    return ProcessingResult(
        inbound_id=record.inbound_id, status=InboundEmailStatus.NEEDS_OPERATOR,
        message=f"Outreach reply stored for operator review (approval {hitl_id}).",
    )


# ---------------------------------------------------------------------------
# RFQ replies (sourcing stage) — supplier quote by e-mail
# ---------------------------------------------------------------------------
# RFQs go out by e-mail with an [RFQ:<id>] subject token (sourcing
# _dispatch_by_email). The supplier's reply lands here: the price is extracted
# the same way as a negotiation counter-offer and handed to
# sourcing.receive_quote_by_email, which verifies the supplier (GSTN / DGFT),
# re-scores it and moves the deal MATCHING → NEGOTIATING. No price → the
# operator reads it (PARSE_INBOUND_EMAIL).

async def _handle_rfq_reply(
    record: InboundEmailRecord,
    raw: InboundRawEmail,
    mapping,
    counter_price_override: float | None,
) -> ProcessingResult:
    repo = get_repository()
    rfq_id = mapping.rfq_id
    deal_id: str | None = None
    try:
        from services.sourcing.service import get_rfq  # type: ignore[import]
        rfq = await get_rfq(rfq_id)
        deal_id = rfq.deal_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("RFQ reply %s: RFQ %s lookup failed: %s", record.inbound_id, rfq_id, exc)

    record = record.model_copy(update={
        "status": InboundEmailStatus.MAPPED,
        "deal_id": deal_id,
        "party": "supplier",
        "mapping_confidence": mapping.confidence,
        "updated_at": datetime.now(timezone.utc),
    })
    await repo.save(record)

    price = counter_price_override or await _extract_price(raw.body_text)
    if price is None or deal_id is None:
        hitl_id = await _create_price_hitl(
            record,
            summary=(
                f"Supplier quote reply to RFQ {rfq_id} from {record.from_address} — "
                f"{'RFQ not found' if deal_id is None else 'no price could be extracted'}. "
                f"Preview: {record.body_text[:200]}"
            ),
            risk_flags=["rfq_reply", "price_extraction_failed" if deal_id else "rfq_not_found"],
        )
        record = record.model_copy(update={
            "status": InboundEmailStatus.NEEDS_OPERATOR,
            "hitl_approval_id": hitl_id,
            "updated_at": datetime.now(timezone.utc),
        })
        await repo.save(record)
        return ProcessingResult(
            inbound_id=record.inbound_id,
            status=InboundEmailStatus.NEEDS_OPERATOR,
            message=f"RFQ {rfq_id} reply stored for operator review (approval {hitl_id}).",
        )

    try:
        from services.sourcing.service import receive_quote_by_email  # type: ignore[import]
        quote = await receive_quote_by_email(
            rfq_id, record.from_address, price, body_text=raw.body_text,
        )
        record = record.model_copy(update={
            "status": InboundEmailStatus.PROCESSED,
            "extracted_price": price,
            "processed_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        })
        await repo.save(record)
        logger.info(
            "Inbound %s → quote %s for RFQ %s (supplier %s, %.2f USD/MT, decision=%s)",
            record.inbound_id, quote.quote_id, rfq_id, quote.supplier_id, price, quote.re_scored_decision,
        )
        return ProcessingResult(
            inbound_id=record.inbound_id,
            status=InboundEmailStatus.PROCESSED,
            message=f"Quote {quote.quote_id} recorded for RFQ {rfq_id} (decision {quote.re_scored_decision}).",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("RFQ reply %s: receive_quote failed: %s", record.inbound_id, exc)
        hitl_id = await _create_price_hitl(
            record,
            summary=f"Supplier quote for RFQ {rfq_id} could not be recorded ({exc}). Preview: {record.body_text[:200]}",
            risk_flags=["rfq_reply", "quote_intake_failed"],
        )
        record = record.model_copy(update={
            "status": InboundEmailStatus.NEEDS_OPERATOR,
            "extracted_price": price,
            "hitl_approval_id": hitl_id,
            "error": str(exc),
            "updated_at": datetime.now(timezone.utc),
        })
        await repo.save(record)
        return ProcessingResult(
            inbound_id=record.inbound_id,
            status=InboundEmailStatus.NEEDS_OPERATOR,
            message=f"Quote intake failed: {exc}",
        )


# ---------------------------------------------------------------------------
# Post-agreement replies (deal-stage handling)
# ---------------------------------------------------------------------------
# Once a negotiation is AGREED the thread stays alive: the buyer receives the
# Proforma Invoice and the escrow funding instructions on it, the supplier
# receives the PO and the inspection booking. Their replies map back here
# via the same [NEG:...] token but must never feed run_round() (there is no
# round to run). The one reply we can act on automatically is the buyer
# accepting the PI — detected with the same conservative acceptance guard the
# negotiation agent uses, then gated behind CONFIRM_PI_ACCEPTANCE (policy
# level, default L0). Everything else becomes a PARSE_INBOUND_EMAIL approval
# so a human reads it.

async def _handle_post_agreement_reply(
    record: InboundEmailRecord,
    raw: InboundRawEmail,
    mapping,
) -> ProcessingResult:
    repo = get_repository()
    deal_status: str | None = None
    pi = None
    if record.deal_id:
        try:
            from services.deals.service import get_deal  # type: ignore[import]
            deal = await get_deal(record.deal_id)
            deal_status = deal.status.value if hasattr(deal.status, "value") else str(deal.status)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Post-agreement reply: deal %s lookup failed: %s", record.deal_id, exc)
        try:
            from services.dealmaker.repository import get_repository as _dm_repo  # type: ignore[import]
            pi = await _dm_repo().get_pi_by_deal(record.deal_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Post-agreement reply: PI lookup failed: %s", exc)

    try:
        from agents.negotiation_agent.guards import is_explicit_acceptance  # type: ignore[import]
        accepted = is_explicit_acceptance(raw.body_text)
    except Exception:  # noqa: BLE001
        accepted = False

    pi_expired = False
    if pi is not None and pi.status == "SENT":
        from services.dealmaker.service import is_pi_expired  # type: ignore[import]
        pi_expired = is_pi_expired(pi)

    if (
        mapping.party == "buyer"
        and deal_status == "PI_SENT"
        and pi is not None
        and pi.status == "SENT"
        and accepted
        and not pi_expired
    ):
        hitl_id = await _create_pi_acceptance_hitl(record, pi)
        record = record.model_copy(update={
            "status": InboundEmailStatus.PROCESSED,
            "hitl_approval_id": hitl_id,
            "processed_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        })
        await repo.save(record)
        logger.info(
            "Inbound %s reads as buyer acceptance of PI %s — CONFIRM_PI_ACCEPTANCE %s created",
            record.inbound_id, pi.pi_id, hitl_id,
        )
        return ProcessingResult(
            inbound_id=record.inbound_id,
            status=InboundEmailStatus.PROCESSED,
            negotiation_id=record.negotiation_id,
            message=f"Buyer acceptance of PI {pi.pi_id} detected; approval {hitl_id} created.",
        )

    # After delivery: the buyer's acceptance of the goods releases the final
    # escrow tranche (via the CONFIRM_BUYER_ACCEPTANCE gate); a complaint
    # blocks the auto-release immediately and goes to a human.
    if mapping.party == "buyer" and deal_status in ("CLEARED", "PAID") and record.deal_id:
        outcome = await _handle_delivery_reply(record, raw, accepted)
        if outcome is not None:
            return outcome

    # The supplier answers the firm order request (before the PI) or
    # acknowledges the Purchase Order (before the inspection is booked).
    replaced_supplier = False
    if mapping.party == "supplier" and record.deal_id and deal_status:
        try:
            from shared.orchestration import supplier_commitment
            message = await supplier_commitment.handle_supplier_reply(
                record.deal_id, deal_status, raw.body_text or "", record.inbound_id,
                from_address=record.from_address,
            )
            if message is None:
                replaced_supplier = await supplier_commitment.sent_by_replaced_supplier(
                    record.deal_id, record.from_address)
        except Exception as exc:  # noqa: BLE001 — a person reads it instead
            logger.warning("Supplier reply %s: commitment handling failed: %s", record.inbound_id, exc)
            message = None
        if message:
            record = record.model_copy(update={
                "status": InboundEmailStatus.PROCESSED,
                "processed_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            })
            await repo.save(record)
            return ProcessingResult(
                inbound_id=record.inbound_id, status=InboundEmailStatus.PROCESSED,
                negotiation_id=record.negotiation_id, message=message,
            )

    hitl_id = await _create_price_hitl(
        record,
        summary=(
            f"Post-agreement reply from {record.party or 'counterparty'} "
            f"{record.from_address} (deal {deal_status or 'unknown'}) — read and act. "
            + (f"PI {pi.pi_id} has expired: extend it before an acceptance can be recorded. " if pi_expired else "")
            + ("This supplier no longer holds the order request: the order moved to a standby supplier. "
               if replaced_supplier else "")
            + f"Preview: {record.body_text[:200]}"
        ),
        risk_flags=["post_agreement_reply", *(["pi_expired"] if pi_expired else []),
                    *(["supplier_replaced"] if replaced_supplier else [])],
    )
    record = record.model_copy(update={
        "status": InboundEmailStatus.NEEDS_OPERATOR,
        "hitl_approval_id": hitl_id,
        "updated_at": datetime.now(timezone.utc),
    })
    await repo.save(record)
    return ProcessingResult(
        inbound_id=record.inbound_id,
        status=InboundEmailStatus.NEEDS_OPERATOR,
        negotiation_id=record.negotiation_id,
        message=f"Post-agreement reply stored for operator review (approval {hitl_id}).",
    )


_DISPUTE_PATTERN = re.compile(
    r"\b(damaged|dispute|reject(?:ed|ing)? the (?:goods|shipment|cargo)|short[- ]?shipment|"
    r"quality (?:issue|problem|complaint)|not (?:acceptable|as per|according to)|"
    r"wrong (?:goods|product|grade)|claim)\b",
    re.IGNORECASE,
)


async def _handle_delivery_reply(record: InboundEmailRecord, raw: InboundRawEmail, accepted: bool):
    """Buyer reply after CLEARED: acceptance → CONFIRM_BUYER_ACCEPTANCE gate;
    complaint → escrow dispute (blocks the inspection-window auto-release) +
    operator review. Returns None when neither applies (caller falls back)."""
    repo = get_repository()
    try:
        from services.escrow_guard.repository import get_repository as _eg_repo  # type: ignore[import]
        hold = await _eg_repo().get_hold_by_deal(record.deal_id or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Delivery reply: escrow lookup failed for %s: %s", record.deal_id, exc)
        return None
    if hold is None:
        return None
    pending_final = [t for t in (hold.tranches or []) if t.trigger == "ACCEPTED" and t.status == "PENDING"]
    if not pending_final or hold.disputed:
        return None
    final_amount = round(sum((t.amount or 0.0) for t in pending_final), 2)

    if _DISPUTE_PATTERN.search(raw.body_text or ""):
        try:
            from services.escrow_guard.schemas import DisputeEscrowRequest  # type: ignore[import]
            from services.escrow_guard.service import dispute_escrow  # type: ignore[import]
            await dispute_escrow(hold.escrow_id, DisputeEscrowRequest(
                reason=f"Buyer e-mail: {raw.body_text[:300]}", raised_by=record.from_address,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.error("Delivery reply: could not raise dispute on %s: %s", hold.escrow_id, exc)
        hitl_id = await _create_price_hitl(
            record,
            summary=(f"Buyer {record.from_address} reports a problem with the delivered goods — "
                     f"final tranche {final_amount:,.2f} {hold.currency} is on hold. Preview: {record.body_text[:200]}"),
            risk_flags=["buyer_complaint", "escrow_disputed"],
        )
        record = record.model_copy(update={
            "status": InboundEmailStatus.NEEDS_OPERATOR, "hitl_approval_id": hitl_id,
            "updated_at": datetime.now(timezone.utc),
        })
        await repo.save(record)
        return ProcessingResult(
            inbound_id=record.inbound_id, status=InboundEmailStatus.NEEDS_OPERATOR,
            negotiation_id=record.negotiation_id,
            message=f"Buyer complaint — escrow {hold.escrow_id} disputed, approval {hitl_id} created.",
        )

    if not accepted:
        return None
    try:
        from services.hitl_approval.service import create_approval  # type: ignore[import]
        from services.hitl_approval.schemas import CreateApprovalRequest  # type: ignore[import]
        from shared.orchestration.autonomy_policy import resolve_gate
        spec = resolve_gate("CONFIRM_BUYER_ACCEPTANCE", amount=final_amount, conditions_ok=True)
        approval = await create_approval(CreateApprovalRequest(
            deal_id=record.deal_id or "",
            action="CONFIRM_BUYER_ACCEPTANCE",
            summary=(f"Buyer {record.from_address} accepts the delivered goods — release the final "
                     f"tranche {final_amount:,.2f} {hold.currency} of escrow {hold.escrow_id}. "
                     f"Preview: {record.body_text[:200]}"),
            financial_impact_usd=final_amount,
            risk_flags=["money_movement", "buyer_acceptance_detected"],
            autonomy_level=spec.level,
            expires_at=spec.expires_at_iso(),
            default_on_timeout=spec.default_on_timeout,
            callback_payload={
                "deal_id": record.deal_id, "escrow_id": hold.escrow_id,
                "accepted_by": record.from_address, "notes": raw.body_text[:500],
                "inbound_id": record.inbound_id,
            },
            idempotency_key=f"buyer-accept-{hold.escrow_id}",
        ))
        hitl_id = approval.approval_id
    except Exception as exc:  # noqa: BLE001
        logger.error("Delivery reply: CONFIRM_BUYER_ACCEPTANCE creation failed: %s", exc)
        return None
    record = record.model_copy(update={
        "status": InboundEmailStatus.PROCESSED, "hitl_approval_id": hitl_id,
        "processed_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
    })
    await repo.save(record)
    logger.info("Inbound %s reads as buyer goods acceptance — CONFIRM_BUYER_ACCEPTANCE %s", record.inbound_id, hitl_id)
    return ProcessingResult(
        inbound_id=record.inbound_id, status=InboundEmailStatus.PROCESSED,
        negotiation_id=record.negotiation_id,
        message=f"Buyer acceptance of the goods detected; approval {hitl_id} created.",
    )


async def _create_pi_acceptance_hitl(record: InboundEmailRecord, pi) -> str:
    try:
        from services.hitl_approval.service import create_approval  # type: ignore[import]
        from services.hitl_approval.schemas import CreateApprovalRequest  # type: ignore[import]
        from shared.orchestration.autonomy_policy import resolve_level

        req = CreateApprovalRequest(
            deal_id=record.deal_id or "",
            action="CONFIRM_PI_ACCEPTANCE",
            summary=(
                f"Buyer {record.from_address} appears to accept PI {pi.pi_id} "
                f"({pi.currency} {pi.total_value:,.2f}). Preview: {record.body_text[:200]}"
            ),
            financial_impact_usd=float(pi.total_value),
            risk_flags=["buyer_acceptance_detected"],
            autonomy_level=resolve_level("CONFIRM_PI_ACCEPTANCE"),
            callback_payload={
                "pi_id": pi.pi_id,
                "deal_id": record.deal_id,
                "accepted_by": record.from_address,
                "buyer_email": record.from_address,
                "notes": f"Accepted by email reply (inbound {record.inbound_id}): {record.subject}",
                "inbound_id": record.inbound_id,
            },
            idempotency_key=f"pi-accept-{pi.pi_id}",
        )
        approval = await create_approval(req)
        return approval.approval_id
    except Exception as exc:  # noqa: BLE001
        logger.error("CONFIRM_PI_ACCEPTANCE creation failed for %s: %s", record.inbound_id, exc)
        return ""


# ---------------------------------------------------------------------------
# HITL helper
# ---------------------------------------------------------------------------

async def _create_price_hitl(
    record: InboundEmailRecord,
    summary: str | None = None,
    risk_flags: list[str] | None = None,
) -> str:
    """Create a HITL approval so an operator can read the email and supply
    what the pipeline could not extract (price / intent)."""
    try:
        from services.hitl_approval.service import create_approval  # type: ignore[import]
        from services.hitl_approval.schemas import CreateApprovalRequest  # type: ignore[import]

        req = CreateApprovalRequest(
            deal_id=record.deal_id or record.negotiation_id or record.inbound_id,
            action="PARSE_INBOUND_EMAIL",
            summary=summary or (
                f"Inbound email from {record.from_address} — price could not be "
                f"extracted automatically. Preview: {record.body_text[:200]}"
            ),
            risk_flags=risk_flags or ["price_extraction_failed"],
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
