"""Auto-replies and bounces are not replies (DEF-16).

An out-of-office answer to an introduction must not count as the buyer "replying" (reply rate,
"unclear" verdicts for an operator, a negotiation round), and a delivery failure means the
address is dead — the funnel should move on, not wait a week for an answer that cannot come.

``classify(...)`` → ``"bounce"`` | ``"auto_reply"`` | ``None`` from, in order:

* headers: ``X-Failed-Recipients``, a ``multipart/report; report-type=delivery-status`` body →
  bounce; ``Auto-Submitted`` (anything but ``no``), ``X-Autoreply`` / ``X-Autorespond``,
  ``Precedence: auto_reply | bulk | junk`` → auto-reply;
* sender: ``mailer-daemon@`` / ``postmaster@`` → bounce;
* subject: "Undeliverable", "Delivery Status Notification", "Mail delivery failed",
  "Returned mail", "Delivery has failed" → bounce; "Out of office", "Automatic reply",
  "Auto-reply", "Autoreply", "Away from", "On vacation" → auto-reply.

``failed_recipient(...)`` finds the dead address (header, ``Final-Recipient`` /
``Original-Recipient`` lines, else the first address in the body that is not ours).
"""
from __future__ import annotations

import re

_BOUNCE_SUBJECT = re.compile(r"undeliver|delivery status notification|mail delivery (?:failed|failure)|returned mail|"
                             r"delivery has failed|failure notice|could not be delivered", re.I)
_AUTO_SUBJECT = re.compile(r"out of (?:the )?office|automatic reply|auto[- ]?reply|autoreply|away from (?:the )?office|"
                           r"on (?:vacation|leave|holiday)|abwesenheit|absence", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _h(headers: dict[str, str] | None, name: str) -> str:
    for k, v in (headers or {}).items():
        if k.lower() == name.lower():
            return str(v or "")
    return ""


def classify(*, from_address: str, subject: str, headers: dict[str, str] | None = None, body: str = "") -> str | None:
    if _h(headers, "X-Failed-Recipients"):
        return "bounce"
    ctype = _h(headers, "Content-Type").lower()
    if "multipart/report" in ctype and "delivery-status" in ctype:
        return "bounce"
    sender = (from_address or "").lower()
    if sender.startswith(("mailer-daemon@", "postmaster@")) or "mailer-daemon" in sender:
        return "bounce"
    if _BOUNCE_SUBJECT.search(subject or ""):
        return "bounce"
    auto = _h(headers, "Auto-Submitted").strip().lower()
    if auto and auto != "no":
        return "auto_reply"
    if _h(headers, "X-Autoreply") or _h(headers, "X-Autorespond"):
        return "auto_reply"
    if _h(headers, "Precedence").strip().lower() in ("auto_reply", "bulk", "junk"):
        return "auto_reply"
    if _AUTO_SUBJECT.search(subject or ""):
        return "auto_reply"
    return None


def failed_recipient(*, headers: dict[str, str] | None = None, body: str = "", own: tuple[str, ...] = ()) -> str | None:
    hdr = _h(headers, "X-Failed-Recipients")
    if hdr:
        m = _EMAIL.search(hdr)
        if m:
            return m.group(0).lower()
    for line in (body or "").splitlines():
        if re.match(r"\s*(final|original)-recipient\s*:", line, re.I):
            m = _EMAIL.search(line)
            if m:
                return m.group(0).lower()
    ours = {o.lower() for o in own if o}
    for m in _EMAIL.finditer(body or ""):
        addr = m.group(0).lower()
        if addr not in ours and not addr.startswith(("mailer-daemon@", "postmaster@")):
            return addr
    return None


async def mark_bounced(address: str, reason: str) -> int:
    """Every outreach candidate at this address: ``send_error`` + ``email_status=bounced`` (the
    funnel stops writing to it). Returns how many candidates were marked. Never raises."""
    if not address:
        return 0
    marked = 0
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        from services.deals.service import update_buyer_candidate  # type: ignore[import]
        for deal in await get_repository().list_active(limit=2000):
            for c in getattr(deal, "buyer_candidates", None) or []:
                if (getattr(c, "email", None) or "").strip().lower() == address.lower():
                    await update_buyer_candidate(deal.deal_id, c.candidate_id, {
                        "send_error": f"bounced: {reason[:200]}", "email_status": "bounced",
                    })
                    marked += 1
    except Exception:  # noqa: BLE001
        return marked
    return marked
