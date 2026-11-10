"""Who is who — the guard that keeps every e-mail on the right thread.

PortLess runs many deals at once, each with a buyer and a supplier (and, before
the negotiation, several buyer candidates and several RFQ suppliers). One
wrong address, one reply attributed to the wrong side, one memory snippet from
the other side of the table, and a supplier reads the buyer's price or a buyer
gets an e-mail meant for someone else. Every path consults this module:

* **identity** — ``same_owner`` (exact address, or the same company domain;
  free-mail domains such as gmail.com only ever match exactly) and
  ``party_addresses`` (every known address of the buyer and the supplier of a
  negotiation: session, deal parties, the selected outreach candidate);
* **inbound** — ``classify_sender``: a reply only advances a negotiation when
  the sender is a known address of the party it was attributed to; a sender
  that belongs to the *other* party, or to nobody, goes to an operator;
* **outbound** — ``preflight``: right before SMTP, an approved draft is
  re-checked (not already sent, real address, recipient is the current contact
  of the party the draft was written for and never the other party, the body
  does not name the other side, the thread has not moved on since it was
  drafted, the negotiation is still open);
* **drafting** — ``leak_findings``: other side's name / address / domain or
  price inside a draft → flagged and forced to human review;
* **memory** — ``knowledge_filter``: the drafting agents only ever see memory
  about *this* counterparty (its own e-mails, notes about it, product notes),
  never deal summaries, the other side's e-mails or other buyers' threads.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable

FREEMAIL = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.co.uk", "hotmail.com", "outlook.com",
    "live.com", "msn.com", "aol.com", "icloud.com", "me.com", "mail.com", "gmx.com", "gmx.de", "web.de",
    "yandex.com", "yandex.ru", "proton.me", "protonmail.com", "zoho.com", "rediffmail.com", "qq.com",
    "163.com", "126.com", "sina.com", "naver.com",
})

PLACEHOLDER_DOMAIN = "portless.placeholder"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
OTHER = {"buyer": "supplier", "supplier": "buyer"}


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def norm(address: str | None) -> str:
    a = (address or "").strip().lower()
    if "<" in a and ">" in a:  # "Name <a@b.com>"
        a = a[a.index("<") + 1:a.index(">")].strip()
    return a


def domain(address: str | None) -> str:
    a = norm(address)
    return a.rsplit("@", 1)[-1] if "@" in a else ""


def is_real_address(address: str | None) -> bool:
    a = norm(address)
    return bool(_EMAIL_RE.match(a)) and not a.endswith("@" + PLACEHOLDER_DOMAIN)


def same_owner(a: str | None, b: str | None) -> bool:
    """Same mailbox, or the same company domain (never a shared free-mail domain)."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    da, db = domain(na), domain(nb)
    return bool(da) and da == db and da not in FREEMAIL and da != PLACEHOLDER_DOMAIN


def _role(p: Any) -> str:
    r = getattr(p, "role", "")
    return str(getattr(r, "value", r) or "").lower()


def party_addresses(session: Any, deal: Any | None = None) -> dict[str, set[str]]:
    """Every known real address of each side of one negotiation."""
    out: dict[str, set[str]] = {"buyer": set(), "supplier": set()}
    for role, attr in (("buyer", "buyer_email"), ("supplier", "seller_email")):
        if is_real_address(getattr(session, attr, None)):
            out[role].add(norm(getattr(session, attr)))
    if deal is not None:
        for p in getattr(deal, "parties", None) or []:
            role = _role(p)
            if role in out and is_real_address(getattr(p, "email", None)):
                out[role].add(norm(p.email))
        for c in getattr(deal, "buyer_candidates", None) or []:
            if getattr(c, "status", None) in ("selected", "interested"):
                for a in (getattr(c, "reply_address", None), getattr(c, "email", None)):
                    if is_real_address(a):
                        out["buyer"].add(norm(a))
    return out


def owner_of(address: str | None, addrs: dict[str, set[str]]) -> str | None:
    """'buyer' / 'supplier' when the address belongs to exactly one side, else None."""
    exact = {role for role, known in addrs.items() if norm(address) in known}
    if len(exact) == 1:
        return exact.pop()
    hits = {role for role, known in addrs.items() if any(same_owner(address, k) for k in known)}
    return hits.pop() if len(hits) == 1 else None


def classify_sender(from_address: str | None, party: str | None, addrs: dict[str, set[str]]) -> str:
    """``verified`` (a known address of ``party``) · ``other_party`` (belongs to
    the other side) · ``ambiguous`` (matches both sides) · ``unknown``."""
    sender = norm(from_address)
    own_set = addrs.get(party or "", set())
    other_set = addrs.get(OTHER.get(party or "", ""), set())
    # An exact address beats a shared domain (both sides on one domain).
    if sender in own_set and sender not in other_set:
        return "verified"
    if sender in other_set and sender not in own_set:
        return "other_party"
    mine = any(same_owner(sender, a) for a in own_set)
    theirs = any(same_owner(sender, a) for a in other_set)
    if mine and theirs:
        return "ambiguous"
    if mine:
        return "verified"
    if theirs:
        return "other_party"
    return "unknown"


# ---------------------------------------------------------------------------
# Content: does a draft for one side mention the other side?
# ---------------------------------------------------------------------------

_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(?![\w])")
_LEGAL_SUFFIX = re.compile(
    r"\b(private|pvt|ltd|limited|llc|inc|gmbh|co|company|corp|corporation|plc|sa|srl|bv|ag|exports?|"
    r"imports?|international|industries|trading|traders|enterprises|group|chemicals?|foods?)\b\.?",
    re.IGNORECASE,
)


def _name_core(name: str | None) -> str:
    """'Swadesh India Chemical Pvt Ltd' → 'swadesh india' (distinctive part only)."""
    core = _LEGAL_SUFFIX.sub(" ", name or "")
    core = re.sub(r"[^a-z0-9 ]+", " ", core.lower())
    return " ".join(core.split())


def _numbers(text: str) -> list[float]:
    out = []
    for m in _NUM.finditer(text or ""):
        whole = m.group(1).replace(",", "")
        try:
            out.append(float(f"{whole}.{m.group(2)}" if m.group(2) else whole))
        except ValueError:
            continue
    return out


def _party_names(session: Any, deal: Any | None, role: str) -> list[str]:
    names = []
    if deal is not None:
        for p in getattr(deal, "parties", None) or []:
            if _role(p) == role and getattr(p, "legal_name", None):
                names.append(p.legal_name)
        if role == "supplier":
            for o in getattr(deal, "supplier_offers", None) or []:
                if o.supplier_id == getattr(session, "seller_id", None) and o.company_name:
                    names.append(o.company_name)
    return names


def other_side_prices(session: Any, party: str) -> list[float]:
    """Prices of the OTHER side that must never appear in an e-mail to ``party``."""
    if party == "buyer":
        vals = [session.supplier_quote_price, session.supplier_last_quote, session.last_offer_to_supplier]
    else:
        vals = [session.buyer_target_price, session.buyer_last_offer, session.last_offer_to_buyer]
    return [float(v) for v in vals if v]


def leak_findings(body: str, party: str, session: Any, deal: Any | None = None, *,
                  addrs: dict[str, set[str]] | None = None, check_prices: bool = True) -> list[str]:
    """What of the other side shows up in an e-mail meant for ``party``."""
    other = OTHER.get(party)
    if not other or not body:
        return []
    text = body.lower()
    found: list[str] = []
    addrs = addrs if addrs is not None else party_addresses(session, deal)
    for a in addrs.get(other, set()):
        if a in text:
            found.append(f"{other} e-mail address {a}")
        d = domain(a)
        if d and d not in FREEMAIL and re.search(rf"(?<![\w.-]){re.escape(d)}(?![\w-])", text):
            found.append(f"{other} domain {d}")
    for name in _party_names(session, deal, other):
        core = _name_core(name)
        if len(core) >= 4 and re.search(rf"\b{re.escape(core)}\b", re.sub(r"[^a-z0-9 ]+", " ", text)):
            found.append(f"{other} name '{name}'")
    if check_prices:
        own = other_side_prices(session, other)  # this party's own figures are fine to mention
        for price in other_side_prices(session, party):
            if any(abs(n - price) < 0.5 for n in _numbers(body)) and not any(abs(price - o) < 0.5 for o in own):
                found.append(f"{other}'s price {price:g}")
    return sorted(set(found))


# ---------------------------------------------------------------------------
# Outbound preflight — the last check before SMTP
# ---------------------------------------------------------------------------

def is_notification(record: Any) -> bool:
    return getattr(record, "kind", None) == "notification" or str(getattr(record, "email_id", "")).startswith("EMAIL-N")


def preflight(record: Any, session: Any | None, deal: Any | None, thread: list[Any]) -> list[str]:
    """Reasons an approved outbound e-mail must NOT be sent. ``[]`` → safe."""
    reasons: list[str] = []
    party = getattr(record, "party", None)
    to = getattr(record, "to_address", None)
    if party not in OTHER:
        reasons.append(f"unknown recipient party {party!r}")
        return reasons
    if not is_real_address(to):
        reasons.append(f"no real address for the {party} ({to})")
    if session is None:
        reasons.append("negotiation session not found")
        return reasons
    if getattr(record, "deal_id", None) and record.deal_id != session.deal_id:
        reasons.append(f"draft belongs to deal {record.deal_id}, session to {session.deal_id}")

    addrs = party_addresses(session, deal)
    for a in addrs["buyer"] & addrs["supplier"]:
        reasons.append(f"buyer and supplier share the address {a} — fix the parties first")
    owner = owner_of(to, addrs)
    if owner and owner != party:
        reasons.append(f"recipient {norm(to)} is the {owner}, but this e-mail was written for the {party}")
    elif is_real_address(to) and addrs[party] and norm(to) not in addrs[party]:
        reasons.append(f"recipient {norm(to)} is no longer a contact of the {party} "
                       f"(current: {', '.join(sorted(addrs[party]))})")

    leaks = leak_findings(record.body, party, session, deal, addrs=addrs, check_prices=False)
    if leaks:
        reasons.append("body mentions the other side: " + "; ".join(leaks))

    if not is_notification(record):
        if getattr(session, "status", None) in ("FAILED", "ESCALATED"):
            reasons.append(f"negotiation is {session.status}")
        created = getattr(record, "created_at", None)
        for r in thread:
            if r.email_id == record.email_id or r.party != party or not isinstance(created, datetime):
                continue
            if str(getattr(r.direction, "value", r.direction)) == "inbound" and r.created_at > created:
                reasons.append(f"the {party} replied after this draft was written — it no longer answers them")
                break
            if (str(getattr(r.direction, "value", r.direction)) == "outbound" and not is_notification(r)
                    and r.created_at > created
                    and str(getattr(r.status, "value", r.status)) in ("sent", "modified_sent", "logged_only")):
                reasons.append(f"a newer e-mail to the {party} was already sent")
                break
    return reasons


# ---------------------------------------------------------------------------
# Memory scoping for the drafting agents
# ---------------------------------------------------------------------------

def knowledge_filter(role: str, counterparty_addresses: set[str]) -> Callable[[dict[str, Any]], bool]:
    """Accept only memory that is about THIS counterparty or the product.

    Rejected: deal summaries (margins, the other side's price), graph facts (no
    metadata to vet), e-mails with the other side or with other companies,
    notes about the other side."""
    other = OTHER.get(role, "")

    def accept(result: dict[str, Any]) -> bool:
        meta = result.get("metadata") or {}
        kind = meta.get("kind")
        if kind == "product":
            return True
        if kind == "note":
            return meta.get("subject_type") in (role, "product", "market")
        if kind == "email":
            if meta.get("party") == other:
                return False
            addr = norm(meta.get("counterparty_address"))
            if addr:
                return any(same_owner(addr, a) for a in counterparty_addresses)
            dom = (meta.get("counterparty_domain") or "").lower()
            return bool(dom) and dom not in FREEMAIL and any(domain(a) == dom for a in counterparty_addresses)
        return False

    return accept
