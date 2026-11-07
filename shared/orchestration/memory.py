"""What PortLess remembers in knowledge-service (RAG) — and what it reads back.

Written (background, never blocking a flow):

- **e-mails** — every delivered outbound e-mail (introductions, RFQs, indicative
  offers, negotiation, notifications) and every inbound reply, tagged with the
  deal, the counterparty and the direction (``kind=email``);
- **notes** — operator notes about a buyer, supplier, product or market
  (``POST /knowledge/notes``) and automatic ones from outcomes (QC failed,
  dispute, deal lost and why) (``kind=note``);
- **product knowledge** — each catalogue product line: grades, markets, seller
  profile (``kind=product``);
- **deal summaries** — when a match is confirmed, the contract signed, and when
  the deal ends: parties, prices, margin, outcome and reason (``kind=deal_summary``).

Read by the LLM steps only (``context_for``): the negotiation agent's drafts, the
reply reader, the exception resolver. Money decisions never read from here.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable

from shared import knowledge

logger = logging.getLogger(__name__)

_BUY = re.compile(r"\[BUY:([A-Z0-9-]+):([^\]]+)\]")
_RFQ = re.compile(r"\[RFQ:([A-Z0-9-]+)\]")
_NEG = re.compile(r"\[NEG:([0-9a-f-]{36})\]")


def _slug(text: str | None) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", (text or "unknown")).strip("-")[:80] or "unknown"


async def _deal_from_subject(subject: str) -> tuple[str | None, str | None]:
    """(deal_id, counterparty id) from the subject tokens PortLess puts on its mail."""
    if (m := _BUY.search(subject or "")):
        return m.group(1), m.group(2)
    if (m := _RFQ.search(subject or "")):
        try:
            from services.sourcing.service import get_rfq  # type: ignore[import]
            return (await get_rfq(m.group(1))).deal_id, None
        except Exception:  # noqa: BLE001
            return None, None
    if (m := _NEG.search(subject or "")):
        try:
            from agents.negotiation_agent.repository import get_repository  # type: ignore[import]
            session = await get_repository().get_session(m.group(1))
            return (getattr(session, "deal_id", None), None) if session else (None, None)
        except Exception:  # noqa: BLE001
            return None, None
    return None, None


async def _deal_meta(deal_id: str | None) -> dict[str, Any]:
    if not deal_id:
        return {}
    try:
        from services.deals.service import get_deal  # type: ignore[import]
        deal = await get_deal(deal_id)
        return {"deal_id": deal_id, "hs_code": deal.hs_code, "market": deal.market}
    except Exception:  # noqa: BLE001
        return {"deal_id": deal_id}


async def remember_email(*, direction: str, from_address: str, to_address: str, subject: str, body: str,
                         message_id: str | None = None, deal_id: str | None = None,
                         party: str | None = None) -> None:
    if not knowledge.enabled():
        return
    try:
        token_deal, counterparty = await _deal_from_subject(subject)
        deal_id = deal_id or token_deal
        meta = await _deal_meta(deal_id)
        other = to_address if direction == "outbound" else from_address
        domain = (other or "").split("@")[-1].lower()
        if direction == "inbound":
            try:
                from agents.negotiation_agent.guards import _new_reply_text  # type: ignore[import]
                body = _new_reply_text(body) or body
            except Exception:  # noqa: BLE001
                pass
        when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text = (f"E-mail {direction} — {when}\nFrom: {from_address}\nTo: {to_address}\nSubject: {subject}\n"
                + (f"Deal: {deal_id} (HS {meta.get('hs_code')} → {meta.get('market')})\n" if deal_id else "")
                + (f"Counterparty role: {party}\n" if party else "") + f"\n{body}")
        mid = _slug(message_id or f"{when}-{subject}")
        knowledge.remember(text, path=f"emails/{_slug(deal_id or 'unmapped')}/{mid}.txt", metadata={
            "kind": "email", "direction": direction, "counterparty_domain": domain,
            "counterparty_address": (other or "").strip().lower(),
            "counterparty_id": counterparty, "party": party, **meta,
        })
    except Exception as exc:  # noqa: BLE001
        logger.debug("Memory: e-mail not remembered: %s", exc)


def remember_note(*, subject_type: str, subject_id: str, text: str, author: str | None = None,
                  extra: dict[str, Any] | None = None) -> bool:
    """A note about a buyer / supplier / product / market / deal."""
    when = datetime.now(timezone.utc)
    body = (f"Note about {subject_type} {subject_id} — {when:%Y-%m-%d}" + (f" by {author}" if author else "")
            + f"\n\n{text}")
    task = knowledge.remember(body, path=f"notes/{_slug(subject_type)}/{_slug(subject_id)}/{when:%Y%m%dT%H%M%S}.txt",
                              metadata={"kind": "note", "subject_type": subject_type, "subject_id": subject_id,
                                        "author": author, **(extra or {})})
    return task is not None


def remember_product(line: Any) -> None:
    """One catalogue product line as product knowledge."""
    try:
        hs = getattr(line, "hs_code", None) or ""
        parts = [f"Product: {getattr(line, 'name', '')} (HS {hs})"]
        for label, attr in (("Bill-of-lading product names", "products"), ("Target markets", "markets"),
                            ("Typical seller", "seller_profile"), ("Default quantity (MT)", "default_quantity_mt"),
                            ("Grades", "grades"), ("Notes", "notes")):
            v = getattr(line, attr, None)
            if v:
                parts.append(f"{label}: {', '.join(map(str, v)) if isinstance(v, (list, tuple)) else v}")
        knowledge.remember("\n".join(parts), path=f"products/{_slug(hs)}.txt",
                           metadata={"kind": "product", "hs_code": hs})
    except Exception as exc:  # noqa: BLE001
        logger.debug("Memory: product not remembered: %s", exc)


async def remember_deal_summary(deal: Any, event: str, reason: str | None = None) -> None:
    """A narrative of the deal at a milestone (match confirmed, signed, ended)."""
    if not knowledge.enabled():
        return
    try:
        parties = {str(getattr(p.role, "value", p.role)): p for p in (getattr(deal, "parties", None) or [])}
        b, s = parties.get("buyer"), parties.get("supplier")
        pair = getattr(deal, "selected_pair", None) or {}
        score = pair.get("score") or {}
        lines = [
            f"Deal {deal.deal_id} — {event} ({datetime.now(timezone.utc):%Y-%m-%d})",
            f"Product: HS {deal.hs_code} to {deal.market}; quantity {getattr(deal, 'quantity_mt', None) or '?'} MT",
            f"Buyer: {b.legal_name} ({b.country})" if b else "Buyer: not decided",
            f"Supplier: {s.legal_name} ({s.country})" if s else "Supplier: not decided",
            f"Status: {getattr(deal.status, 'value', deal.status)}",
        ]
        if getattr(deal, "buyer_target_price_usd", None):
            lines.append(f"Buyer price: USD {deal.buyer_target_price_usd:,.2f}/MT ({getattr(deal, 'buyer_price_source', '') or 'source unknown'})")
        if score:
            lines.append(f"Match: supplier FOB {score.get('supplier_price', {}).get('value')}, margin "
                         f"{score.get('margin_pct')}%, predicted chance {score.get('probability')}")
        if getattr(deal, "realized_margin_usd", None) is not None:
            lines.append(f"Realised margin: USD {deal.realized_margin_usd:,.2f}")
        hist = [f"{h.status.value if hasattr(h.status, 'value') else h.status}"
                + (f" ({h.reason})" if h.reason else "") for h in (getattr(deal, "status_history", None) or [])][-8:]
        if hist:
            lines.append("Path: " + " → ".join(hist))
        if reason:
            lines.append(f"Reason: {reason}")
        knowledge.remember("\n".join(lines), path=f"deals/{_slug(deal.deal_id)}.txt", metadata={
            "kind": "deal_summary", "deal_id": deal.deal_id, "hs_code": deal.hs_code, "market": deal.market,
            "event": event, "buyer": b.legal_name if b else None, "supplier": s.legal_name if s else None,
        })
    except Exception as exc:  # noqa: BLE001
        logger.debug("Memory: deal summary not remembered: %s", exc)


async def context_for(*, purpose: str, product: str | None = None, hs_code: str | None = None,
                      market: str | None = None, counterparties: list[str] | None = None,
                      extra: str | None = None, top_k: int = 6, max_chars: int = 2500,
                      accept: Callable[[dict[str, Any]], bool] | None = None) -> str:
    """A prompt block of what PortLess knows that is relevant — '' when nothing / off.

    ``accept`` scopes what may reach the prompt (the negotiation agent passes
    ``email_safety.knowledge_filter`` so a buyer-facing draft never sees the
    supplier's e-mails, deal summaries with margins, or other buyers' threads)."""
    if not knowledge.enabled():
        return ""
    names = [c for c in (counterparties or []) if c]
    query = " ".join(filter(None, [
        purpose, product, f"HS {hs_code}" if hs_code else None, f"market {market}" if market else None,
        ("about " + ", ".join(names)) if names else None, extra,
    ]))
    if accept is None:
        results = await knowledge.recall(query, top_k=top_k)
    else:
        results = [r for r in await knowledge.recall(query, top_k=top_k * 3) if accept(r)][:top_k]
    return knowledge.as_context(results, max_chars=max_chars)
