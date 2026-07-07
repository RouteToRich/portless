"""Maps an inbound email to a NegotiationSession.

Matching strategy (applied in strict priority order):

  1. In-Reply-To header
     Parse <EMAIL-{id}.{negotiation_id}@portless.io> from the In-Reply-To
     header.  The EmailDispatcher embeds this Message-ID on every outbound
     email, so genuine replies always carry this header.

  2. References header
     Same pattern, scanned across all space-separated Message-IDs in the
     References chain.  Catches cases where In-Reply-To is missing but the
     thread history is in References.

  3. Subject [NEG:{negotiation_id}] token
     The EmailDispatcher appends [NEG:{uuid}] to every outbound subject.
     Email clients that strip In-Reply-To/References still preserve the
     subject line, making this a reliable fallback.

  4. UNMATCHED — no mapping found.
     The inbound record is saved with status=UNMATCHED and an operator
     can resolve it via the manual submission endpoint supplying
     negotiation_id explicitly.

Why no sender-address fallback
──────────────────────────────
A sender address lookup (find outbound emails TO this address and use
the negotiation_id from those records) would require a cross-service
query against the negotiation agent's email_history collection.  This
creates a tight coupling and a false-positive risk: the same supplier
email could be involved in multiple concurrent negotiations.  The three
header-based strategies above are unambiguous and should cover all real
traffic once the EmailDispatcher embeds Message-ID correctly.

In production microservices mode
─────────────────────────────────
The _negotiation_is_active() and _get_party_for_negotiation() helpers
import directly from agents.negotiation_agent (monolith mode).  Replace
them with HTTP calls to GET /negotiation-agent/negotiations/{id} to run
each service on its own port.
"""
from __future__ import annotations

import logging
import re

from .schemas import MappingConfidence, MappingResult

logger = logging.getLogger(__name__)

# Matches: <EMAIL-XXXXXXXXXX.{uuid}@portless.io>
# Both the EMAIL- prefix and the full 36-char UUID are required.
_MSG_ID_PATTERN = re.compile(
    r"<EMAIL-[A-Z0-9]+\."
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"@portless\.io>",
    re.IGNORECASE,
)

# Matches: [NEG:{uuid}] anywhere in the subject line
_SUBJECT_TOKEN_PATTERN = re.compile(
    r"\[NEG:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\]",
    re.IGNORECASE,
)

_TERMINAL_STATUSES = {"AGREED", "FAILED", "ESCALATED"}


def _parse_neg_id_from_header(header_value: str) -> str | None:
    """Extract negotiation_id UUID from a single Message-ID header value."""
    m = _MSG_ID_PATTERN.search(header_value)
    return m.group(1) if m else None


async def _negotiation_is_active(negotiation_id: str) -> bool:
    """Return True if the session exists and has not reached a terminal state."""
    try:
        from agents.negotiation_agent.repository import get_repository  # type: ignore[import]
        session = await get_repository().get_session(negotiation_id)
        return session is not None and session.status not in _TERMINAL_STATUSES
    except Exception as exc:
        logger.warning("Could not verify negotiation status for %s: %s", negotiation_id, exc)
        # Optimistic: let the service layer handle an invalid/inactive id cleanly.
        return True


async def _get_party(negotiation_id: str, from_address: str) -> str | None:
    """Determine whether the sender is the buyer or supplier for this negotiation.

    Looks at outbound emails that were sent TO from_address for this negotiation.
    The party stored on the outbound record ('buyer'/'supplier') tells us who
    they are.
    """
    try:
        from agents.negotiation_agent.email_history import get_email_repository  # type: ignore[import]
        records = await get_email_repository().list_by_negotiation(negotiation_id)
        for r in records:
            if (
                r.direction.value == "outbound"
                and r.to_address.lower() == from_address.lower()
            ):
                return r.party
    except Exception as exc:
        logger.warning("Party lookup failed for negotiation %s: %s", negotiation_id, exc)
    return None


async def map_email(
    from_address: str,
    subject: str,
    in_reply_to: str,
    references: list[str],
    negotiation_id_override: str | None = None,
    party_override: str | None = None,
) -> MappingResult:
    """Attempt to map an inbound email to an active NegotiationSession.

    Returns a MappingResult.  When confidence=UNMATCHED the negotiation_id
    is None and the caller should set status=UNMATCHED on the record.
    """

    # ── Manual override (operator supplied negotiation_id) ─────────────────
    if negotiation_id_override:
        active = await _negotiation_is_active(negotiation_id_override)
        if not active:
            return MappingResult(
                confidence=MappingConfidence.MANUAL_OVERRIDE,
                reason=f"Negotiation {negotiation_id_override} is terminal or not found",
            )
        party = party_override or await _get_party(negotiation_id_override, from_address)
        return MappingResult(
            negotiation_id=negotiation_id_override,
            party=party,
            confidence=MappingConfidence.MANUAL_OVERRIDE,
            reason="Operator-supplied negotiation_id",
        )

    # ── Step 1: In-Reply-To header ─────────────────────────────────────────
    if in_reply_to:
        neg_id = _parse_neg_id_from_header(in_reply_to)
        if neg_id and await _negotiation_is_active(neg_id):
            # party_override (IMAP folder hint) is the fallback when
            # email history has no outbound record to this sender yet.
            party = await _get_party(neg_id, from_address) or party_override
            return MappingResult(
                negotiation_id=neg_id,
                party=party,
                confidence=MappingConfidence.HEADER,
                reason=f"Matched via In-Reply-To: {in_reply_to[:80]}",
            )

    # ── Step 2: References header ──────────────────────────────────────────
    for ref in references:
        neg_id = _parse_neg_id_from_header(ref)
        if neg_id and await _negotiation_is_active(neg_id):
            party = await _get_party(neg_id, from_address) or party_override
            return MappingResult(
                negotiation_id=neg_id,
                party=party,
                confidence=MappingConfidence.HEADER,
                reason=f"Matched via References: {ref[:80]}",
            )

    # ── Step 3: Subject [NEG:...] token ───────────────────────────────────
    m = _SUBJECT_TOKEN_PATTERN.search(subject)
    if m:
        neg_id = m.group(1)
        if await _negotiation_is_active(neg_id):
            party = await _get_party(neg_id, from_address) or party_override
            return MappingResult(
                negotiation_id=neg_id,
                party=party,
                confidence=MappingConfidence.SUBJECT_TOKEN,
                reason=f"Matched via subject token [NEG:{neg_id}]",
            )

    # ── No match ───────────────────────────────────────────────────────────
    return MappingResult(
        confidence=MappingConfidence.UNMATCHED,
        reason=(
            f"No negotiation_id found in In-Reply-To, References, or subject. "
            f"from={from_address}  subject={subject[:80]}"
        ),
    )
