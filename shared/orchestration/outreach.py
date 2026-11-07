"""Buyer outreach funnel — turn the buyers a scan identified into one buyer
who actually wants to talk, before the negotiation agent ever writes to them.

Before this module a deal had exactly one buyer (the top signal) and the
negotiation agent's first e-mail to that cold importer was a price
negotiation. Now:

  deal.created ─▶ ``start_outreach``: the first ``OUTREACH_PARALLEL`` buyer
                  candidates with an address each get a templated introduction
                  (actor pitch angle + fit reason, escrow + third-party QC
                  story, a clear ask) behind a ``SEND_BUYER_OUTREACH`` gate
                  (policy default L2 → sent immediately). Subject carries
                  ``[BUY:<deal_id>:<candidate_id>]`` so the reply maps back.
  reply        ─▶ ``handle_reply``: classified interested / declined / unclear.
                  interested → that buyer becomes the deal's buyer party (real
                  address, contact_source ``outreach``), the negotiation opens
                  when the deal is NEGOTIATING; declined → the next candidate
                  is contacted; unclear → PARSE_INBOUND_EMAIL for a human.
  silence      ─▶ ``sweep_followups`` (deal watcher): one follow-up after
                  ``OUTREACH_FOLLOWUP_DAYS``, ``no_reply`` after
                  ``OUTREACH_NO_REPLY_DAYS`` more, then the next candidate.
  exhausted    ─▶ ``deal.exception`` (category NEGOTIATION_FAILED, stage
                  ``OUTREACH:exhausted``) — no buyer wanted the product.

The negotiation leg waits (``waiting_for_buyer``) while the funnel is open, so
no buyer is ever pitched a counter-price before saying they are interested.
Operator-created deals without candidates are untouched.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

ACTION = "SEND_BUYER_OUTREACH"
_TOKEN_RE = re.compile(r"\[BUY:(DEAL-[A-Z0-9-]+):([^\]\s]+)\]", re.IGNORECASE)

_INTERESTED = re.compile(
    r"\b(interested|send (?:me|us) (?:a |the |your )?(?:quote|quotation|price|prices|offer|samples?|catalog(?:ue)?|spec)|"
    r"please (?:share|send|quote|provide)|what(?:'s| is) (?:your|the) (?:price|moq|lead time)|"
    r"(?:your|the) (?:best )?(?:price|rate|moq)\b|yes,? please|let'?s (?:talk|discuss|proceed)|"
    r"call me|schedule a call|happy to (?:discuss|talk|receive)|we (?:would|'d) like to|tell me more)\b",
    re.IGNORECASE,
)
_DECLINED = re.compile(
    r"\b(not interested|no,? thanks?|no thank you|unsubscribe|do not contact|don'?t contact|"
    r"remove (?:me|us)|no longer (?:buy|import|source)|we don'?t (?:buy|import|need)|"
    r"not (?:buying|looking|sourcing|relevant)|already have a supplier|stop (?:emailing|contacting))\b",
    re.IGNORECASE,
)


def enabled() -> bool:
    return os.getenv("OUTREACH_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def parallel() -> int:
    return max(1, _int("OUTREACH_PARALLEL", 2))


def followup_days() -> int:
    return max(1, _int("OUTREACH_FOLLOWUP_DAYS", 3))


def no_reply_days() -> int:
    return max(1, _int("OUTREACH_NO_REPLY_DAYS", 7))


NOT_DELIVERED = "not delivered — no outbound mailbox configured (GMAIL_SENDER_ADDRESS); e-mail logged only"


def subject_token(deal_id: str, candidate_id: str) -> str:
    return f"[BUY:{deal_id}:{candidate_id}]"


def parse_token(subject: str) -> tuple[str, str] | None:
    m = _TOKEN_RE.search(subject or "")
    return (m.group(1).upper(), m.group(2)) if m else None


def _status(c: Any) -> str:
    return getattr(c, "status", None) or (c.get("status") if isinstance(c, dict) else "") or ""


def _candidates(deal: Any) -> list[Any]:
    return list(getattr(deal, "buyer_candidates", None) or [])


def waiting_for_buyer(deal: Any) -> bool:
    """True while the funnel is open and nobody has said yes yet — the
    negotiation must not start. False for deals without candidates or whose
    buyer party was set by an operator."""
    cands = _candidates(deal)
    if not cands or not enabled():
        return False
    if any(_status(c) in ("interested", "selected") for c in cands):
        return False
    for p in getattr(deal, "parties", None) or []:
        role = getattr(getattr(p, "role", ""), "value", getattr(p, "role", "")) or ""
        if str(role).lower() == "buyer" and getattr(p, "email", None) and getattr(p, "contact_source", None) == "operator":
            return False
    return True


def classify_reply(text: str | None) -> str:
    """``interested`` / ``declined`` / ``unclear`` from the new reply text only."""
    if not text:
        return "unclear"
    try:
        from agents.negotiation_agent.guards import _new_reply_text  # type: ignore[import]
        body = _new_reply_text(text) or text
    except Exception:  # noqa: BLE001
        body = text
    if _DECLINED.search(body):
        return "declined"
    if _INTERESTED.search(body):
        return "interested"
    return "unclear"


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------

def _entity() -> str:
    try:
        from services.dealmaker.config import settings as dm  # type: ignore[import]
        return dm.portless_entity_name
    except Exception:  # noqa: BLE001
        return "PortLess"


def _product_label(deal: Any) -> str:
    """Display name for the e-mail ("crude glycerine"), else "the HS 1520 product".
    Never the Apify search keyword ("glycerol alcohol") — that reads badly in a letter."""
    from shared.product_label import product_phrase
    return product_phrase(getattr(deal, "hs_code", "") or "")


def _greeting(cand: Any) -> str:
    name = (getattr(cand, "legal_name", None) or "").strip()
    return f"Dear {name} team," if name else "Dear Sir or Madam,"


def draft(deal: Any, cand: Any, *, followup: bool = False) -> tuple[str, str]:
    """The buyer's first e-mail from PortLess — written to earn a reply.

    Structure: why we are writing to *them* (actor pitch angle, or the product
    they buy) → what removes their risk (escrow with staged release, inspection
    by the agency they pick, verified manufacturer — exactly as the escrow
    works, see agents/negotiation_agent/briefing.py) → a low-effort ask whose
    answers are precisely what a CIF offer needs → a concrete promise (firm
    offer in two working days) → an easy opt-out.
    """
    from shared.product_label import product_title

    product = _product_label(deal)
    title = product_title(getattr(deal, "hs_code", "") or "")
    pitch = (getattr(cand, "pitch_angle", None) or "").strip()
    qty = getattr(cand, "quantity_mt", None) or getattr(deal, "quantity_mt", None)
    token = subject_token(deal.deal_id, getattr(cand, "candidate_id"))
    if followup:
        subject = f"Re: {title} from India — quick follow-up {token}"
        body = (
            f"{_greeting(cand)}\n\n"
            f"Following up on my note about {product} supply from India. If the timing is not right, a "
            "one-line reply is enough and I will close the file. If it is, send me the grade, quantity and "
            "destination port, and you will have a firm CIF offer within two working days — with payment "
            "into escrow and an inspection by the agency you choose.\n\n"
            f"Best regards,\n{_entity()} — Trade Operations\n"
        )
        return subject, body
    subject = f"{title} from India — inspected before shipment, paid through escrow {token}"
    opener = pitch or (
        f"I am writing because your company buys {product}, and we can offer a lower-risk way to buy it "
        "directly from Indian manufacturers."
    )
    body = (
        f"{_greeting(cand)}\n\n"
        f"{opener}\n\n"
        f"{_entity()} is an Indian merchant exporter. What that means for you:\n"
        "  - You choose the inspector. SGS, QIMA, Bureau Veritas, Intertek or TÜV SÜD checks every lot "
        "before it ships and you get the photo-documented report; the inspection cost is in our price.\n"
        "  - Your money is protected. You pay into a neutral escrow account, not to us. A production advance "
        "is released when the order is placed with the manufacturer; the rest only after the inspection "
        "passes, the goods clear customs and you accept them.\n"
        f"  - Verified manufacturers only (GST and IEC checked), with freight and export documents handled "
        "by us — one CIF price to your port.\n\n"
        + (f"We can supply lots of {qty:g} MT or multiples. " if qty else "Typical lots are 20–40 MT. ")
        + "If this is relevant, a short reply with these is all I need to send you a firm offer within "
        "two working days:\n"
        "  1. grade / specification and packaging\n"
        "  2. quantity (MT) and when you need it delivered\n"
        "  3. destination port\n"
        "  4. your target price per MT, if you have one\n"
        "  5. certificates or documents you need at destination\n\n"
        f"Best regards,\n{_entity()} — Trade Operations\n\n"
        "If you would rather not hear from us, reply \"not interested\" and we will not write again.\n"
    )
    return subject, body


# ---------------------------------------------------------------------------
# Sending (through the SEND_BUYER_OUTREACH gate)
# ---------------------------------------------------------------------------

async def _gate(deal: Any, cand: Any, *, followup: bool) -> str | None:
    from services.hitl_approval.schemas import CreateApprovalRequest  # type: ignore[import]
    from services.hitl_approval.service import create_approval  # type: ignore[import]
    from shared.orchestration.autonomy_policy import resolve_gate

    subject, body = draft(deal, cand, followup=followup)
    spec = resolve_gate(ACTION, deal)
    kind = "followup" if followup else "intro"
    approval = await create_approval(CreateApprovalRequest(
        deal_id=deal.deal_id,
        action=ACTION,
        summary=f"{'Follow-up' if followup else 'Outreach'} → {cand.legal_name} <{cand.email}>: {subject[:120]}",
        risk_flags=["templated_outreach"] + (["followup"] if followup else []),
        autonomy_level=spec.level,
        expires_at=spec.expires_at_iso(),
        default_on_timeout=spec.default_on_timeout,
        callback_payload={
            "deal_id": deal.deal_id, "candidate_id": cand.candidate_id, "to_address": cand.email,
            "subject": subject, "body": body, "kind": kind,
        },
        # A retry after an undelivered send needs its own gate (the first one is resolved).
        idempotency_key=f"outreach-{kind}-{deal.deal_id}-{cand.candidate_id}"
                        + (f"-a{cand.send_attempts}" if getattr(cand, "send_attempts", 0) else ""),
    ))
    return approval.approval_id


async def start_outreach(deal: Any, *, limit: int | None = None) -> int:
    """Open gates for the next pending candidates (up to the parallel slots).
    Returns the number of candidates contacted in this call."""
    if not enabled():
        return 0
    cands = _candidates(deal)
    from shared.orchestration import matchmaking
    # With the match board, an interested buyer does not close the funnel — more
    # interested buyers mean a better pair; it closes when a match is confirmed.
    closing = ("selected",) if matchmaking.enabled() else ("interested", "selected")
    if not cands or any(_status(c) in closing for c in cands):
        return 0
    active = [c for c in cands if _status(c) in ("contacted", "followed_up")]
    slots = max(0, (limit if limit is not None else parallel()) - len(active))
    pending = [c for c in cands if _status(c) == "pending" and getattr(c, "email", None)]
    sent = 0
    for cand in pending[:slots]:
        try:
            await _gate(deal, cand, followup=False)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("Outreach: gate for %s/%s failed: %s", deal.deal_id, cand.candidate_id, exc)
    skipped = [c for c in cands if _status(c) == "pending" and not getattr(c, "email", None)]
    if skipped and not pending:
        logger.warning("Outreach: deal %s has %d candidate(s) without an address — enrich or set one",
                       deal.deal_id, len(skipped))
    if sent:
        logger.info("Outreach: %d introduction(s) gated for deal %s", sent, deal.deal_id)
    return sent


async def send_outreach(deal_id: str, candidate_id: str, to_address: str, subject: str, body: str,
                        kind: str = "intro") -> dict[str, Any]:
    """Approve callback: send the e-mail through the gateway and mark the candidate."""
    from services.deals.service import get_deal, update_buyer_candidate  # type: ignore[import]
    from services.email_gateway.schemas import SendEmailRequest  # type: ignore[import]
    from services.email_gateway.sender import send_email  # type: ignore[import]

    email_id = f"OUT{kind[:1].upper()}{deal_id.replace('-', '')[-10:]}{re.sub(r'[^A-Z0-9]', '', candidate_id.upper())[:12]}"
    result = await send_email(SendEmailRequest(
        to_address=to_address, subject=subject, body=body, negotiation_id="", round_no=0, email_id=email_id,
    ))
    outcome = getattr(getattr(result, "outcome", None), "value", getattr(result, "outcome", "sent"))
    now = datetime.now(timezone.utc)
    updates: dict[str, Any] = {"email_id": email_id}
    if str(outcome) == "logged":
        # No outbound mailbox: the e-mail was written to the log, nobody received
        # it. The candidate stays where it was (pending / contacted) with the
        # reason, so the funnel's clocks do not start and "Start outreach" can
        # send it for real once the mailbox is set up.
        deal = await get_deal(deal_id)
        cand = next((c for c in deal.buyer_candidates if c.candidate_id == candidate_id), None)
        await update_buyer_candidate(deal_id, candidate_id, {
            "send_error": NOT_DELIVERED,
            "send_attempts": (getattr(cand, "send_attempts", 0) or 0) + 1,
        })
        logger.warning("Outreach: %s to %s (%s) for deal %s NOT delivered — %s",
                       kind, to_address, candidate_id, deal_id, NOT_DELIVERED)
        return {"candidate_id": candidate_id, "outcome": "logged", "email_id": email_id, "delivered": False}
    updates["send_error"] = None
    if kind == "followup":
        updates.update({"status": "followed_up", "followed_up_at": now})
    else:
        updates.update({"status": "contacted", "contacted_at": now})
    await update_buyer_candidate(deal_id, candidate_id, updates)
    logger.info("Outreach: %s to %s (%s) for deal %s → %s", kind, to_address, candidate_id, deal_id, outcome)
    return {"candidate_id": candidate_id, "outcome": str(outcome), "email_id": email_id, "delivered": True}


async def skip_candidate(deal_id: str, candidate_id: str, reason: str = "declined by operator") -> dict[str, Any]:
    """Reject callback / operator skip: mark declined and move to the next buyer."""
    from services.deals.service import update_buyer_candidate  # type: ignore[import]
    await update_buyer_candidate(deal_id, candidate_id, {
        "status": "declined", "replied_at": datetime.now(timezone.utc), "reply_preview": reason[:200],
    })
    return await advance(deal_id)


# ---------------------------------------------------------------------------
# Replies
# ---------------------------------------------------------------------------

def _find(deal: Any, candidate_id: str | None, from_address: str | None) -> Any | None:
    cands = _candidates(deal)
    if candidate_id:
        for c in cands:
            if c.candidate_id == candidate_id:
                return c
    if from_address:
        addr = from_address.lower()
        domain = addr.split("@")[-1]
        for c in cands:
            email = (getattr(c, "email", None) or "").lower()
            if email and (email == addr or email.split("@")[-1] == domain):
                return c
    return None


async def handle_reply(deal_id: str, candidate_id: str | None, from_address: str, body: str) -> dict[str, Any]:
    """Classify a buyer's reply and move the funnel. Returns ``{"verdict", ...}``."""
    from services.deals.service import get_deal, update_buyer_candidate, update_party  # type: ignore[import]

    deal = await get_deal(deal_id)
    cand = _find(deal, candidate_id, from_address)
    verdict = classify_reply(body)
    now = datetime.now(timezone.utc)
    if cand is None:
        logger.warning("Outreach: reply from %s for deal %s matches no candidate", from_address, deal_id)
        return {"verdict": verdict, "candidate_id": None, "matched": False}

    from shared.orchestration import matchmaking
    if matchmaking.enabled():
        # Prospect matching: the reply fills the buyer's requirement and the match
        # board decides — nobody becomes "the buyer" until CONFIRM_MATCH.
        return await matchmaking.handle_buyer_reply(deal, cand, from_address, body, verdict)

    preview = (body or "")[:200]
    if verdict == "interested":
        await update_buyer_candidate(deal_id, cand.candidate_id, {
            "status": "selected", "replied_at": now, "reply_preview": preview,
        })
        current = next((p for p in getattr(deal, "parties", None) or []
                        if str(getattr(getattr(p, "role", ""), "value", getattr(p, "role", ""))).lower() == "buyer"), None)
        deal = await update_party(deal_id, "buyer", {
            "legal_name": cand.legal_name, "country": cand.country, "email": from_address,
            "website": getattr(cand, "website", None), "phone": getattr(cand, "phone", None),
            "contact_source": f"outreach:{getattr(cand, 'source', None) or 'reply'}",
            "email_status": "ok", "party_id": cand.candidate_id, "sanctions_status": "unchecked",
        }, source=None, replace=current is not None and getattr(current, "party_id", None) != cand.candidate_id)
        await _buyer_confirmed(deal, from_address)
        logger.info("Outreach: %s (%s) is INTERESTED in deal %s — buyer party set", cand.legal_name, from_address, deal_id)
        return {"verdict": verdict, "candidate_id": cand.candidate_id, "matched": True, "buyer_set": True}

    if verdict == "declined":
        await update_buyer_candidate(deal_id, cand.candidate_id, {
            "status": "declined", "replied_at": now, "reply_preview": preview,
        })
        nxt = await advance(deal_id)
        return {"verdict": verdict, "candidate_id": cand.candidate_id, "matched": True, **nxt}

    await update_buyer_candidate(deal_id, cand.candidate_id, {"replied_at": now, "reply_preview": preview})
    return {"verdict": verdict, "candidate_id": cand.candidate_id, "matched": True}


async def _buyer_confirmed(deal: Any, buyer_email: str) -> None:
    """An interested buyer is on the deal: open / re-address the negotiation."""
    status = getattr(getattr(deal, "status", ""), "value", getattr(deal, "status", "")) or ""
    try:
        from agents.negotiation_agent.repository import get_repository as _neg_repo  # type: ignore[import]
        repo = _neg_repo()
        session = await repo.get_session_by_deal(deal.deal_id)
        if session is not None and (not session.buyer_email or session.buyer_email.endswith("@portless.placeholder")):
            session.buyer_email = buyer_email
            session.updated_at = datetime.now(timezone.utc)
            await repo.save_session(session)
            logger.info("Outreach: negotiation %s re-addressed to %s", session.negotiation_id, buyer_email)
            return
        if session is not None:
            return
    except Exception as exc:  # noqa: BLE001
        logger.debug("Outreach: negotiation session lookup failed for %s: %s", deal.deal_id, exc)
    if str(status) == "NEGOTIATING":
        try:
            from shared.domain.events import DomainEvent, EventTopic
            from shared.orchestration.continuations import on_negotiation_started
            await on_negotiation_started(DomainEvent(
                topic=EventTopic.NEGOTIATION_STARTED, deal_id=deal.deal_id, payload={"resumed": "buyer_interested"},
            ))
        except Exception as exc:  # noqa: BLE001
            logger.error("Outreach: could not open the negotiation for %s: %s", deal.deal_id, exc)


async def advance(deal_id: str) -> dict[str, Any]:
    """Contact the next pending candidate(s); raise an exception when exhausted."""
    from services.deals.service import get_deal  # type: ignore[import]
    deal = await get_deal(deal_id)
    cands = _candidates(deal)
    if any(_status(c) in ("interested", "selected") for c in cands):
        return {"advanced": 0, "exhausted": False}
    sent = await start_outreach(deal)
    open_ = [c for c in cands if _status(c) in ("pending", "contacted", "followed_up")]
    exhausted = sent == 0 and not open_ and bool(cands)
    if exhausted:
        try:
            from shared.bus import get_publisher
            from shared.domain.events import DomainEvent, EventTopic
            await get_publisher().publish(DomainEvent(
                topic=EventTopic.DEAL_EXCEPTION, deal_id=deal_id,
                payload={
                    "source": "outreach", "stage": "OUTREACH:exhausted", "category": "NEGOTIATION_FAILED",
                    "error": f"none of the {len(cands)} identified buyers is interested",
                    "action_required": "find_new_buyers_or_cancel",
                    "candidates": [{"name": c.legal_name, "status": _status(c)} for c in cands],
                },
            ))
        except Exception:  # noqa: BLE001
            logger.exception("Outreach: could not emit exhausted exception for %s", deal_id)
        logger.warning("Outreach: deal %s — every buyer candidate declined or never replied", deal_id)
    return {"advanced": sent, "exhausted": exhausted}


# ---------------------------------------------------------------------------
# Follow-ups (deal watcher)
# ---------------------------------------------------------------------------

async def sweep_followups(now: datetime | None = None) -> dict[str, int]:
    """One follow-up after ``OUTREACH_FOLLOWUP_DAYS`` of silence, ``no_reply``
    after ``OUTREACH_NO_REPLY_DAYS`` more, then the next candidate."""
    counts = {"followups": 0, "no_reply": 0, "advanced": 0}
    if not enabled():
        return counts
    now = now or datetime.now(timezone.utc)
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deals = await get_repository().list_active(limit=1000)
    except Exception as exc:  # noqa: BLE001
        logger.error("Outreach: could not list deals: %s", exc)
        return counts
    for deal in deals:
        status = getattr(getattr(deal, "status", ""), "value", getattr(deal, "status", "")) or ""
        if str(status) not in ("MATCHING", "NEGOTIATING") or not waiting_for_buyer(deal):
            continue
        changed = False
        for c in _candidates(deal):
            try:
                if _status(c) == "contacted" and c.contacted_at and _aware(c.contacted_at) + timedelta(days=followup_days()) <= now:
                    await _gate(deal, c, followup=True)
                    counts["followups"] += 1
                elif _status(c) == "followed_up" and (c.followed_up_at or c.contacted_at) and \
                        _aware(c.followed_up_at or c.contacted_at) + timedelta(days=no_reply_days()) <= now:
                    from services.deals.service import update_buyer_candidate  # type: ignore[import]
                    await update_buyer_candidate(deal.deal_id, c.candidate_id, {"status": "no_reply"})
                    counts["no_reply"] += 1
                    changed = True
            except Exception as exc:  # noqa: BLE001
                logger.error("Outreach: follow-up for %s/%s failed: %s", deal.deal_id, c.candidate_id, exc)
        if changed:
            res = await advance(deal.deal_id)
            counts["advanced"] += int(res.get("advanced") or 0)
    return counts


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
