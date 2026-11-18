"""Check a supplier offer entered by staff before it reaches the match board.

A quote taken by phone (``POST /deals/{id}/supplier-offers``) used to land on the board with
no check at all. Now the supplier is looked up in the directory (by e-mail, website domain or
name): a fresh check there is reused (``TRADE_MIN_VALIDATION_MAX_AGE_DAYS``); otherwise the
same counterparty check as a scan runs (sanctions, e-mail, GST / IEC registries). The result
goes onto the offer — ``gst_verified`` / ``iec_active`` only when a registry answered (None =
unchecked), ``verification_failed`` for a failed check (never matched) — and the board is
re-scored (``matchmaking.evaluate``) so the next gate opens.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


async def _directory_record(offer: Any) -> Any | None:
    from services.trade_min import directory, relationships  # type: ignore[import]
    store = await directory.get_directory()
    keys = relationships.keys_for(getattr(offer, "email", None), getattr(offer, "supplier_id", None),
                                  name=getattr(offer, "company_name", None))
    for r in await store.list("supplier", limit=100_000):
        if r.id == getattr(offer, "supplier_id", None) or relationships.record_keys(r) & keys:
            return r
    return None


def _flag(word: str | None) -> bool | None:
    return True if word == "active" else False if word == "inactive" else None


async def check_offer(offer: Any, *, gstin: str | None = None, iec: str | None = None) -> Any:
    """The offer with the supplier's check applied. Never raises."""
    try:
        from services.trade_min import validation  # type: ignore[import]
        from services.trade_min.schemas import CounterpartyRecord  # type: ignore[import]
        rec = await _directory_record(offer)
        v = rec.validation if rec is not None else None
        if not validation.is_fresh(v):
            now = datetime.now(timezone.utc)
            subject = rec or CounterpartyRecord(
                id=f"supplier:{offer.supplier_id}", role="supplier", name=offer.company_name or offer.supplier_id,
                country="IN", email=offer.email, gstin=gstin, iec=iec, source="operator_offer",
                first_seen_at=now, last_seen_at=now)
            if gstin or iec:
                subject = subject.model_copy(update={"gstin": gstin or subject.gstin, "iec": iec or subject.iec})
            checked = await validation.validate_record(subject.model_copy(deep=True))
            v = checked.validation
            if rec is not None and v is not None:
                await (await _store()).set_validation(checked)
        if v is None:
            return offer
        return offer.model_copy(update={
            "gst_verified": _flag(v.gst), "iec_active": _flag(v.iec),
            "verification_failed": v.status == "failed" or offer.verification_failed,
        })
    except Exception as exc:  # noqa: BLE001 - the offer goes on the board unchecked, as before
        logger.warning("OfferChecks: %s not checked: %s", getattr(offer, "supplier_id", "?"), exc)
        return offer


async def _store():
    from services.trade_min import directory  # type: ignore[import]
    return await directory.get_directory()
