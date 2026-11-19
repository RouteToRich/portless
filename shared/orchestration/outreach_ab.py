"""Outreach angles, A/B-tested per corridor (todo 23.2 "outreach personalisation").

Each buyer introduction is written with one of two openers:
- ``a`` — why we write to *them* (the scan's pitch line, else "your company imports …");
- ``b`` — the risk-removal angle first (neutral escrow, inspection before shipment by the
  inspector PortLess can book), then the product.

The variant is stored on the candidate (``outreach_variant``) so replies count for it. Per
product × market, until each variant has been sent ``OUTREACH_AB_MIN_SENDS`` (20) times the two
alternate (stable per candidate); after that the variant with the better reply rate is used for
every new introduction (``GET /deals/outreach/variants`` shows the counts). Template words only —
no claim enters an e-mail that the template does not already make.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

VARIANTS = ("a", "b")


def min_sends() -> int:
    try:
        return max(1, int(os.getenv("OUTREACH_AB_MIN_SENDS", "20")))
    except ValueError:
        return 20


def _sent(c: Any) -> bool:
    return bool(getattr(c, "contacted_at", None))


def _replied(c: Any) -> bool:
    return bool(getattr(c, "replied_at", None)) or str(getattr(c, "status", "")) in ("interested", "selected", "declined")


def stats(deals: list[Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """Pure: per "HS6:MARKET", per variant — sent, replied, reply rate."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for d in deals:
        key = f"{str(getattr(d, 'hs_code', ''))[:6]}:{str(getattr(d, 'market', '') or '').upper()}"
        for c in getattr(d, "buyer_candidates", None) or []:
            v = getattr(c, "outreach_variant", None)
            if v not in VARIANTS or not _sent(c):
                continue
            row = out.setdefault(key, {}).setdefault(v, {"sent": 0, "replied": 0})
            row["sent"] += 1
            row["replied"] += int(_replied(c))
    for per in out.values():
        for row in per.values():
            row["reply_rate"] = round(row["replied"] / row["sent"], 3) if row["sent"] else None
    return out


def choose(candidate_id: str, corridor: dict[str, dict[str, Any]] | None) -> str:
    """The variant for a new introduction: alternate until both have enough sends, then the winner."""
    corridor = corridor or {}
    if all((corridor.get(v) or {}).get("sent", 0) >= min_sends() for v in VARIANTS):
        return max(VARIANTS, key=lambda v: (corridor[v]["reply_rate"] or 0.0, v == "a"))
    return VARIANTS[int(hashlib.sha1(candidate_id.encode()).hexdigest(), 16) % 2]


def opener_b(product: str) -> str:
    from shared.connectors.qc_providers import inspector_phrase
    return (f"Buying {product} from a new overseas supplier usually means paying up front and hoping the goods "
            "match the sample. We remove both risks: you pay into a neutral escrow account, and the goods are "
            f"inspected before shipment by {inspector_phrase()}. I am writing to offer you that way of buying "
            f"{product} directly from manufacturers in India whose registrations we check.")


async def variant_for(deal: Any, cand: Any) -> str:
    existing = getattr(cand, "outreach_variant", None)
    if existing in VARIANTS:
        return existing
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deals = await get_repository().list_active(limit=2000)
    except Exception:  # noqa: BLE001
        deals = []
    key = f"{str(getattr(deal, 'hs_code', ''))[:6]}:{str(getattr(deal, 'market', '') or '').upper()}"
    return choose(str(getattr(cand, "candidate_id", "")), stats(deals).get(key))
