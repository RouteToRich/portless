"""Samples before the first order (todo 7.x "Samples before the first order").

Buyers of agri commodities usually ask for a sample before they commit. The step lives on the
deal (``DealRecord.samples``):

1. **Requested** — read from the buyer's reply without a model (``asks_for_sample``: "send us a
   sample", "can we get samples", "sample before we order") or recorded by staff
   (``POST /deals/{id}/samples``). The supplier is asked for it on the RFQ / negotiation thread
   by staff — a sample is not an export shipment.
2. **Shipped** — courier, AWB and cost recorded (``POST /deals/{id}/samples/{sid}/courier``:
   DHL / FedEx / Aramex / India Post …). The cost lands on the deal's run costs
   (``deal_costs`` ``samples`` / ``samples_usd``). Courier rules and the destination's food-import
   rules still apply — the desk checks them.
3. **Verdict** — the buyer approves or rejects (``POST /deals/{id}/samples/{sid}/verdict``). An
   approved sample becomes the deal's quality reference (``approved_sample_ref``): the PI / PO
   product description and the QC booking note say "as per approved sample ref …".

Every staff step is audited (``staff_relay``).
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

COURIERS = ("dhl", "fedex", "aramex", "ups", "india_post", "bluedart", "other")
VERDICTS = ("approved", "rejected")

_ASK = re.compile(
    r"\b(?:send|ship|courier|provide|share|need|want|require|request|get|have)\b[^.!?\n]{0,40}?\bsamples?\b"
    r"|\bsamples?\b[^.!?\n]{0,30}?\b(?:first|before|prior|please|required|needed)\b",
    re.IGNORECASE,
)
_NEGATED = re.compile(r"\b(?:no|not|without|don'?t)\b[^.!?\n]{0,15}?\bsamples?\b", re.IGNORECASE)


def asks_for_sample(text: str | None) -> bool:
    """Pure: the reply asks for a sample (and does not say it needs none)."""
    t = text or ""
    return bool(_ASK.search(t)) and not _NEGATED.search(t)


def approved_ref(deal: Any) -> str | None:
    """"as per approved sample ref SMP-…" for the PI / PO description and the QC note, else None."""
    ref = getattr(deal, "approved_sample_ref", None)
    return f"as per approved sample ref {ref}" if ref else None


async def _deal(deal_id: str) -> Any:
    from services.deals.repository import get_repository  # type: ignore[import]
    deal = await get_repository().get(deal_id)
    if deal is None:
        raise LookupError(f"deal {deal_id} not found")
    return deal


async def _save(deal_id: str, samples: list[dict[str, Any]], **extra: Any) -> None:
    from services.deals.repository import get_repository  # type: ignore[import]
    await get_repository().set_fields(deal_id, {"samples": samples, **extra})


def _find(samples: list[dict[str, Any]], sample_id: str) -> dict[str, Any]:
    row = next((s for s in samples if s.get("sample_id") == sample_id), None)
    if row is None:
        raise LookupError(f"sample {sample_id} not found")
    return row


async def request(deal_id: str, *, by: str, source: str = "staff", note: str | None = None,
                  buyer: str | None = None) -> dict[str, Any]:
    """Record a sample request. One open request per buyer: a second ask returns the open one."""
    deal = await _deal(deal_id)
    samples = [dict(s) for s in (getattr(deal, "samples", None) or [])]
    open_row = next((s for s in samples if s.get("status") in ("requested", "shipped")
                     and (s.get("buyer") or None) == (buyer or None)), None)
    if open_row is not None:
        return open_row
    row = {"sample_id": f"SMP-{uuid.uuid4().hex[:8].upper()}", "status": "requested", "source": source,
           "buyer": buyer, "note": (note or "")[:500] or None, "requested_by": by,
           "requested_at": datetime.now(timezone.utc).isoformat()}
    samples.append(row)
    await _save(deal_id, samples)
    return row


async def capture_from_reply(deal_id: str, body: str | None, *, buyer: str | None = None) -> dict[str, Any] | None:
    """A buyer reply that asks for a sample records the request (never raises)."""
    if not asks_for_sample(body):
        return None
    try:
        return await request(deal_id, by="reply", source="email", note=(body or "")[:300], buyer=buyer)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Samples: request from the reply of deal %s not recorded: %s", deal_id, exc)
        return None


async def record_courier(deal_id: str, sample_id: str, *, courier: str, awb: str, cost_usd: float | None,
                         by: str) -> dict[str, Any]:
    courier = (courier or "").strip().lower().replace(" ", "_")
    if courier not in COURIERS:
        raise ValueError(f"courier must be one of {', '.join(COURIERS)}")
    if not (awb or "").strip():
        raise ValueError("the AWB (air waybill) number is required")
    if cost_usd is not None and cost_usd < 0:
        raise ValueError("cost_usd cannot be negative")
    deal = await _deal(deal_id)
    samples = [dict(s) for s in (getattr(deal, "samples", None) or [])]
    row = _find(samples, sample_id)
    if row.get("status") != "requested":
        raise ValueError(f"sample {sample_id} is {row.get('status')}, not requested")
    row.update(status="shipped", courier=courier, awb=awb.strip(), cost_usd=cost_usd, shipped_by=by,
               shipped_at=datetime.now(timezone.utc).isoformat())
    await _save(deal_id, samples)
    from shared import deal_costs
    deal_costs.add(deal_id, samples=1, samples_usd=float(cost_usd or 0.0))
    return row


async def record_verdict(deal_id: str, sample_id: str, *, verdict: str, note: str | None, by: str) -> dict[str, Any]:
    verdict = (verdict or "").strip().lower()
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
    deal = await _deal(deal_id)
    samples = [dict(s) for s in (getattr(deal, "samples", None) or [])]
    row = _find(samples, sample_id)
    if row.get("status") not in ("requested", "shipped"):
        raise ValueError(f"sample {sample_id} already has a verdict ({row.get('status')})")
    row.update(status=verdict, verdict_note=(note or "")[:500] or None, verdict_by=by,
               verdict_at=datetime.now(timezone.utc).isoformat())
    extra = {"approved_sample_ref": sample_id} if verdict == "approved" else {}
    await _save(deal_id, samples, **extra)
    return row
