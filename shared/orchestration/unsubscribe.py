"""Unsubscribe link in every buyer introduction and follow-up.

The introduction already promises "reply 'not interested' and we will not write again". The
link does the same without a reply:

  ``{PORTLESS_PUBLIC_BASE_URL}/trade-min/unsubscribe?d=<deal>&c=<candidate>&sig=<hmac>``

``sig`` = HMAC-SHA256(``UNSUBSCRIBE_LINK_SECRET``, else ``ESCROW_PROOF_LINK_SECRET``,
``deal:candidate``). No expiry — an opt-out must always work. Opening the link shows a page
with one "Unsubscribe" button (a POST): mail scanners that open every link in an e-mail must
not unsubscribe anyone. The POST puts the company on the directory's do-not-contact list
(every prospect, every later scan) and, while the candidate is still being courted, closes it
as declined and moves the funnel to the next buyer. A buyer already in a deal keeps its deal;
only the marketing stops.

Suppliers (review board B-12): every RFQ, quote reconfirmation and best-and-final e-mail
carries ``supplier_opt_out_line(email)`` — the same link with ``d=supplier`` and ``c=<address>``.
The POST puts the supplier's company (domain; a free-mail address exactly) on the directory's
supplier do-not-contact list, which every supplier e-mail path checks (``contact_guard``);
a supplier the directory does not know yet gets a record holding just that.

Without a public base URL or a secret no link is printed and the reply opt-out stays.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

PATH = "/trade-min/unsubscribe"
SUPPLIER = "supplier"
"""``d`` of a supplier link (``c`` is the supplier's address)."""
_COURTED = ("pending", "contacted", "followed_up", "no_reply")


def _clean(name: str) -> str:
    from shared.integration_mode import is_placeholder
    value = os.getenv(name, "").strip()
    return "" if is_placeholder(value) else value


def _secret() -> str:
    return _clean("UNSUBSCRIBE_LINK_SECRET") or _clean("ESCROW_PROOF_LINK_SECRET")


def _base() -> str:
    return _clean("PORTLESS_PUBLIC_BASE_URL").rstrip("/")


def _sign(deal_id: str, candidate_id: str) -> str:
    return hmac.new(_secret().encode(), f"unsubscribe:{deal_id}:{candidate_id}".encode(), hashlib.sha256).hexdigest()


def link(deal_id: str, candidate_id: str) -> str | None:
    if not _secret() or not _base() or not deal_id or not candidate_id:
        return None
    return f"{_base()}{PATH}?" + urlencode({"d": deal_id, "c": candidate_id, "sig": _sign(deal_id, candidate_id)})


def verify(deal_id: str | None, candidate_id: str | None, sig: str | None) -> bool:
    if not _secret() or not deal_id or not candidate_id or not sig:
        return False
    return hmac.compare_digest(sig, _sign(deal_id, candidate_id))


def opt_out_line(deal_id: str, candidate_id: str) -> str:
    """The last line of an outreach e-mail."""
    url = link(deal_id, candidate_id)
    if url:
        return ("If you would prefer not to hear from us, reply \"not interested\" or unsubscribe here: "
                f"{url}")
    return "If you would prefer not to hear from us, simply reply \"not interested\" and we will not write again."


def supplier_link(email: str | None) -> str | None:
    email = (email or "").strip().lower()
    return link(SUPPLIER, email) if "@" in email else None


def supplier_opt_out_line(email: str | None) -> str:
    """The last line of a supplier e-mail (RFQ, reconfirmation, best and final)."""
    url = supplier_link(email)
    if url:
        return ("If you would prefer not to receive requests from us, reply \"not interested\" or unsubscribe here: "
                f"{url}")
    return "If you would prefer not to receive requests from us, simply reply \"not interested\"."


async def apply_supplier(email: str) -> dict[str, Any]:
    """A supplier's opt-out: its directory record(s) set do-not-contact (a new record when unknown)."""
    from services.trade_min import directory, relationships  # type: ignore[import]
    from services.trade_min.schemas import CounterpartyRecord  # type: ignore[import]

    email = (email or "").strip().lower()
    if "@" not in email:
        raise LookupError("not an e-mail address")
    store = await directory.get_directory()
    keys = {k for k in relationships.keys_for(email) if k.startswith("d:")}
    now = datetime.now(timezone.utc)
    fields = {"do_not_contact": True, "dnc_at": now, "dnc_reason": "unsubscribed with the link in our e-mail"}
    marked: list[str] = []
    for r in await store.list("supplier", limit=100_000):
        same_email = (r.email or "").strip().lower() == email
        same_company = bool(keys) and bool({k for k in relationships.record_keys(r) if k.startswith("d:")} & keys)
        if same_email or same_company:
            if not r.do_not_contact:
                await store.set_contact_preference(r.id, fields)
            marked.append(r.id)
    if not marked:
        rec = await store.upsert(CounterpartyRecord(id=f"supplier:{email}", role="supplier", name=email,
                                                    email=email, source="unsubscribe", sources=["unsubscribe"],
                                                    first_seen_at=now, last_seen_at=now))
        await store.set_contact_preference(rec.id, fields)
        marked.append(rec.id)
    logger.warning("Unsubscribe: supplier %s — do not contact (%s)", email, ", ".join(marked))
    return {"supplier": email, "do_not_contact": True, "records": marked}


async def apply(deal_id: str, candidate_id: str) -> dict[str, Any]:
    """Record the opt-out. Raises LookupError when the deal or candidate does not exist."""
    if deal_id == SUPPLIER:
        return await apply_supplier(candidate_id)
    from services.deals.service import get_deal, update_buyer_candidate  # type: ignore[import]
    from shared.orchestration import outreach

    deal = await get_deal(deal_id)
    cand = next((c for c in (getattr(deal, "buyer_candidates", None) or []) if c.candidate_id == candidate_id), None)
    if cand is None:
        raise LookupError(f"candidate {candidate_id!r} not on deal {deal_id}")
    await outreach._mark_do_not_contact(cand, deal_id, "unsubscribed with the link in our e-mail")
    status = getattr(cand, "status", "pending")
    result: dict[str, Any] = {"deal_id": deal_id, "candidate_id": candidate_id, "do_not_contact": True,
                              "declined": False}
    if status in _COURTED:
        await update_buyer_candidate(deal_id, candidate_id, {
            "status": "declined", "replied_at": datetime.now(timezone.utc),
            "reply_preview": "unsubscribed with the link in our e-mail",
        })
        result["declined"] = True
        try:
            result["next"] = await outreach.advance(deal_id)
        except Exception as exc:  # noqa: BLE001 - the opt-out is recorded either way
            logger.warning("Unsubscribe: funnel of deal %s not advanced: %s", deal_id, exc)
    logger.warning("Unsubscribe: %s (%s) on deal %s — do not contact%s", getattr(cand, "legal_name", "?"),
                   candidate_id, deal_id, ", declined" if result["declined"] else " (already in the deal: kept)")
    return result


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>body{{font:16px/1.5 system-ui,sans-serif;max-width:520px;"
        "margin:64px auto;padding:0 16px;color:#1a1a1a}button{font:inherit;padding:8px 20px;border-radius:6px;"
        "border:1px solid #444;background:#1a1a1a;color:#fff;cursor:pointer}</style></head><body>"
        f"<h1 style='font-size:22px'>{html.escape(title)}</h1>{body}</body></html>"
    )


def confirm_page(deal_id: str, candidate_id: str, sig: str) -> str:
    from shared import company_profile as cp
    brand = html.escape(cp.brand())
    action = f"{PATH}?" + html.escape(urlencode({"d": deal_id, "c": candidate_id, "sig": sig}))
    return _page("Unsubscribe", (
        f"<p>Stop all e-mails from {brand} to your company?</p>"
        f"<form method='post' action='{action}'><button type='submit'>Unsubscribe</button></form>"
    ))


def done_page() -> str:
    return _page("You are unsubscribed", "<p>We will not write to your company again. Thank you, and sorry for the interruption.</p>")


def invalid_page() -> str:
    return _page("Link not valid", "<p>This unsubscribe link is not valid. Reply \"not interested\" to our e-mail and we will not write again.</p>")
