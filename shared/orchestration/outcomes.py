"""Outcome tracking — the feedback loop that makes the match board's numbers honest.

Every ``CONFIRM_MATCH`` proposal is recorded with what the board predicted (chance
of closing and its parts, margin, price bases). The record then follows reality:

- the operator's decision on the gate (approved / rejected);
- the result — **signed** when the deal reaches ``PI_ACCEPTED`` (the buyer signed
  the proforma invoice), **failed** when it ends before that (negotiation failed,
  cancelled);
- the completion — completed (``PAID``) / disputed / refunded / cancelled.

Indicative offers are tracked the same way (sent → accepted / countered / declined).

From these records:

1. **Calibration** — predicted chance vs observed sign rate per probability band.
   Once ``MATCH_CALIBRATION_MIN`` (20) matches are resolved, the board's raw
   probability is replaced by the calibrated one (band rate shrunk toward the raw
   value with 10 pseudo-observations), so "60 %" means six in ten actually sign.
2. **Track records** — per buyer (signed vs failed after a match) and per supplier
   (completed vs disputed / refunded / failed); a weak record lowers that side's
   factor in the chance of closing (never below 0.5).
3. **Offer pricing** — acceptance rate of indicative offers nudges the next range.
4. **Autonomy evidence** — operator agreement, calibration error and sign rate
   above the threshold, with a recommendation for when a gate can move from L0 to
   L1 / L2. The recommendation is shown, never applied by itself.

Storage: Mongo ``match_outcomes`` when ``MONGO_URI`` is set, else in memory.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_COLLECTION = "match_outcomes"
_BINS = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0001)
_SHRINK = 10.0
_SIGNED = "PI_ACCEPTED"
_FAILED_BEFORE_SIGN = ("NEGOTIATION_FAILED", "CANCELLED")
_COMPLETION = {"PAID": "completed", "DISPUTE": "disputed", "REFUNDED": "refunded", "CANCELLED": "cancelled"}


class Outcome(BaseModel):
    outcome_id: str = Field(default_factory=lambda: f"OUT-{uuid4().hex[:12].upper()}")
    kind: Literal["match", "indicative_offer"] = "match"
    deal_id: str
    hs_code: str | None = None
    market: str | None = None
    candidate_id: str
    buyer_name: str | None = None
    offer_id: str | None = None
    supplier_id: str | None = None
    supplier_name: str | None = None
    approval_id: str | None = None
    # prediction
    predicted_probability: float | None = None
    calibrated_probability: float | None = None
    p_margin: float | None = None
    spec_fit: float | None = None
    engagement: float | None = None
    reliability: float | None = None
    timing_fit: float | None = None
    margin_pct: float | None = None
    buyer_price: float | None = None
    buyer_price_basis: str | None = None
    supplier_price: float | None = None
    quantity_mt: float | None = None
    # indicative offer
    offer_low: float | None = None
    offer_high: float | None = None
    offer_vs_estimate: float | None = None
    """Offered low price ÷ the buyer-side estimate at the time (1.05 = 5 % above)."""
    response: str | None = None
    # what happened
    proposed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    decision: Literal["pending", "approved", "rejected"] = "pending"
    decided_at: datetime | None = None
    result: Literal["open", "signed", "failed", "not_pursued"] = "open"
    result_status: str | None = None
    result_reason: str | None = None
    resolved_at: datetime | None = None
    completion: str | None = None
    completed_at: datetime | None = None


_memory: dict[str, Outcome] = {}
_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 300.0


def reset_for_tests() -> None:
    _memory.clear()
    _cache.clear()


def calibration_min() -> int:
    try:
        return int(os.getenv("MATCH_CALIBRATION_MIN", "20"))
    except ValueError:
        return 20


def _mongo():
    if os.getenv("OUTCOMES_MONGO", "true").strip().lower() in ("0", "false", "no", "off"):
        return None
    try:
        from shared.config import settings
        if not settings.mongo_uri:
            return None
        import motor.motor_asyncio as motor  # type: ignore[import]
        client: Any = motor.AsyncIOMotorClient(settings.mongo_uri)
        return client[settings.mongo_db_name][_COLLECTION]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Outcomes: Mongo unavailable (%s) — in-memory only", exc)
        return None


async def _save(o: Outcome) -> None:
    _cache.clear()
    col = _mongo()
    if col is not None:
        try:
            await col.replace_one({"outcome_id": o.outcome_id}, o.model_dump(mode="python"), upsert=True)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Outcomes: could not persist %s: %s", o.outcome_id, exc)
    _memory[o.outcome_id] = o


async def all_outcomes(*, kind: str | None = None, deal_id: str | None = None, limit: int = 5000) -> list[Outcome]:
    query: dict[str, Any] = {}
    if kind:
        query["kind"] = kind
    if deal_id:
        query["deal_id"] = deal_id
    rows: dict[str, Outcome] = {}
    col = _mongo()
    if col is not None:
        try:
            async for doc in col.find(query).sort("proposed_at", -1).limit(limit):
                doc.pop("_id", None)
                o = Outcome(**doc)
                rows[o.outcome_id] = o
        except Exception as exc:  # noqa: BLE001
            logger.warning("Outcomes: read failed: %s", exc)
    for o in _memory.values():
        if (kind is None or o.kind == kind) and (deal_id is None or o.deal_id == deal_id):
            rows.setdefault(o.outcome_id, o)
    return sorted(rows.values(), key=lambda o: o.proposed_at, reverse=True)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

async def record_proposal(deal: Any, pair: Any, approval_id: str | None) -> Outcome | None:
    """A CONFIRM_MATCH gate was opened for this pair."""
    try:
        existing = [o for o in await all_outcomes(kind="match", deal_id=deal.deal_id)
                    if o.candidate_id == pair.candidate_id and o.offer_id == pair.offer_id and o.decision == "pending"]
        o = existing[0] if existing else Outcome(deal_id=deal.deal_id, candidate_id=pair.candidate_id)
        o = o.model_copy(update={
            "hs_code": deal.hs_code, "market": getattr(deal, "market", None), "buyer_name": pair.buyer_name,
            "offer_id": pair.offer_id, "supplier_id": pair.supplier_id, "supplier_name": pair.supplier_name,
            "approval_id": approval_id, "predicted_probability": getattr(pair, "probability_raw", pair.probability),
            "calibrated_probability": pair.probability if getattr(pair, "calibrated", False) else None,
            "p_margin": pair.p_margin, "spec_fit": pair.spec_fit, "engagement": pair.engagement,
            "reliability": pair.reliability, "timing_fit": pair.timing_fit, "margin_pct": pair.margin_pct,
            "buyer_price": pair.buyer_price.value, "buyer_price_basis": pair.buyer_price.basis,
            "supplier_price": pair.supplier_price.value, "quantity_mt": pair.quantity_mt,
        })
        await _save(o)
        return o
    except Exception as exc:  # noqa: BLE001 — tracking never blocks a gate
        logger.warning("Outcomes: proposal not recorded for %s: %s", deal.deal_id, exc)
        return None


async def mark_decision(deal_id: str, candidate_id: str, offer_id: str, decision: str) -> None:
    try:
        now = datetime.now(timezone.utc)
        for o in await all_outcomes(kind="match", deal_id=deal_id):
            if o.candidate_id == candidate_id and o.offer_id == offer_id and o.decision == "pending":
                upd: dict[str, Any] = {"decision": decision, "decided_at": now}
                if decision == "rejected":
                    upd.update({"result": "not_pursued", "resolved_at": now})
                await _save(o.model_copy(update=upd))
                return
    except Exception as exc:  # noqa: BLE001
        logger.warning("Outcomes: decision not recorded for %s: %s", deal_id, exc)


async def record_offer_sent(deal: Any, candidate_id: str, offer_id: str, low: float, high: float,
                            buyer_estimate: float | None) -> None:
    try:
        await _save(Outcome(
            kind="indicative_offer", deal_id=deal.deal_id, hs_code=deal.hs_code, market=getattr(deal, "market", None),
            candidate_id=candidate_id, offer_id=offer_id, offer_low=low, offer_high=high,
            offer_vs_estimate=round(low / buyer_estimate, 4) if buyer_estimate else None, decision="approved",
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Outcomes: offer not recorded for %s: %s", deal.deal_id, exc)


async def record_offer_response(deal_id: str, candidate_id: str, response: str) -> None:
    try:
        now = datetime.now(timezone.utc)
        for o in await all_outcomes(kind="indicative_offer", deal_id=deal_id):
            if o.candidate_id == candidate_id and o.response is None:
                await _save(o.model_copy(update={"response": response, "resolved_at": now,
                                                 "result": "signed" if response == "accepted" else "failed"}))
                return
    except Exception as exc:  # noqa: BLE001
        logger.warning("Outcomes: offer response not recorded for %s: %s", deal_id, exc)


async def on_status(deal: Any, status: str, reason: str | None = None) -> None:
    """Follow the deal after a match: signed / failed, then completion."""
    try:
        now = datetime.now(timezone.utc)
        for o in await all_outcomes(kind="match", deal_id=deal.deal_id):
            if o.decision != "approved":
                continue
            upd: dict[str, Any] = {}
            if o.result == "open" and status == _SIGNED:
                upd.update({"result": "signed", "result_status": status, "resolved_at": now})
            elif o.result == "open" and status in _FAILED_BEFORE_SIGN:
                upd.update({"result": "failed", "result_status": status, "result_reason": reason, "resolved_at": now})
            signed = o.result == "signed" or upd.get("result") == "signed"
            if status in _COMPLETION and o.completion is None and signed:
                upd.update({"completion": _COMPLETION[status], "completed_at": now})
            if upd:
                await _save(o.model_copy(update=upd))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Outcomes: status %s not tracked for %s: %s", status, getattr(deal, "deal_id", "?"), exc)


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------

def _bin(p: float) -> int:
    for i in range(len(_BINS) - 1):
        if _BINS[i] <= p < _BINS[i + 1]:
            return i
    return len(_BINS) - 2


def calibration_table(rows: list[Outcome]) -> dict[str, Any]:
    """Pure: predicted vs observed per band, Brier score, expected calibration error."""
    resolved = [o for o in rows if o.kind == "match" and o.result in ("signed", "failed")
                and o.predicted_probability is not None]
    bins = [{"low": _BINS[i], "high": min(1.0, _BINS[i + 1]), "n": 0, "signed": 0, "mean_predicted": None,
             "observed_rate": None} for i in range(len(_BINS) - 1)]
    sums = [0.0] * len(bins)
    brier = 0.0
    for o in resolved:
        p = float(o.predicted_probability)
        y = 1.0 if o.result == "signed" else 0.0
        b = bins[_bin(p)]
        b["n"] += 1
        b["signed"] += int(y)
        sums[_bin(p)] += p
        brier += (p - y) ** 2
    ece = 0.0
    for i, b in enumerate(bins):
        if b["n"]:
            b["mean_predicted"] = round(sums[i] / b["n"], 4)
            b["observed_rate"] = round(b["signed"] / b["n"], 4)
            ece += b["n"] * abs(b["mean_predicted"] - b["observed_rate"])
    n = len(resolved)
    return {"n_resolved": n, "bins": bins, "brier": round(brier / n, 4) if n else None,
            "ece": round(ece / n, 4) if n else None, "active": n >= calibration_min()}


def calibrate(p: float, table: dict[str, Any]) -> float:
    """Raw probability → calibrated (band rate shrunk toward the raw value)."""
    if not table.get("active"):
        return p
    b = table["bins"][_bin(p)]
    if not b["n"]:
        return p
    return round((b["n"] * b["observed_rate"] + _SHRINK * p) / (b["n"] + _SHRINK), 4)


def track_records(rows: list[Outcome]) -> dict[str, dict[str, Any]]:
    """Per counterparty id: buyers (signed / failed after a confirmed match) and
    suppliers (completed / disputed / refunded / failed)."""
    rec: dict[str, dict[str, Any]] = {}
    for o in rows:
        if o.kind != "match" or o.decision != "approved":
            continue
        b = rec.setdefault(f"buyer:{o.candidate_id}", {"deals": 0, "signed": 0, "failed": 0})
        b["deals"] += 1
        b["signed"] += int(o.result == "signed")
        b["failed"] += int(o.result == "failed")
        if o.supplier_id:
            s = rec.setdefault(f"supplier:{o.supplier_id}", {"deals": 0, "completed": 0, "bad": 0, "failed": 0})
            s["deals"] += 1
            s["completed"] += int(o.completion == "completed")
            s["bad"] += int(o.completion in ("disputed", "refunded"))
            s["failed"] += int(o.result == "failed")
    for key, r in rec.items():
        if key.startswith("buyer:"):
            r["factor"] = round(max(0.5, (r["signed"] + 1) / (r["signed"] + r["failed"] + 1)), 3)
        else:
            r["factor"] = round(max(0.5, (r["completed"] + 1) / (r["completed"] + r["bad"] + 0.5 * r["failed"] + 1)), 3)
    return rec


def offer_pricing(rows: list[Outcome], hs_code: str | None = None) -> dict[str, Any]:
    offers = [o for o in rows if o.kind == "indicative_offer" and o.response and (hs_code is None or o.hs_code == hs_code)]
    n = len(offers)
    acc = sum(1 for o in offers if o.response == "accepted")
    cnt = sum(1 for o in offers if o.response == "countered")
    rate = acc / n if n else None
    nudge = 1.0
    if n >= 5 and rate is not None:
        nudge = 1.03 if rate > 0.6 else 0.98 if rate < 0.2 else 1.0
    return {"responses": n, "accepted": acc, "countered": cnt, "declined": n - acc - cnt,
            "acceptance_rate": round(rate, 3) if rate is not None else None, "price_nudge": nudge}


async def learning_context(hs_code: str | None = None) -> dict[str, Any]:
    """Everything the board needs from past outcomes, cached for 5 minutes."""
    key = f"ctx:{hs_code}"
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL:
        return hit[1]
    rows = await all_outcomes()
    ctx = {"calibration": calibration_table(rows), "track_records": track_records(rows),
           "offer_pricing": offer_pricing(rows, hs_code)}
    _cache[key] = (time.monotonic(), ctx)
    return ctx


def autonomy_evidence(rows: list[Outcome], *, threshold: float = 0.45) -> dict[str, Any]:
    """Evidence for moving the prospect gates off L0 — shown, never applied by itself."""
    matches = [o for o in rows if o.kind == "match"]
    decided = [o for o in matches if o.decision in ("approved", "rejected")]
    approved = [o for o in decided if o.decision == "approved"]
    resolved = [o for o in approved if o.result in ("signed", "failed")]
    high = [o for o in resolved if (o.predicted_probability or 0) >= threshold]
    table = calibration_table(rows)
    agreement = len(approved) / len(decided) if decided else None
    sign_high = sum(1 for o in high if o.result == "signed") / len(high) if high else None

    level, why = "L0", []
    if len(decided) < 30:
        why.append(f"{len(decided)} of 30 operator decisions on proposed matches so far")
    if len(resolved) < calibration_min():
        why.append(f"{len(resolved)} of {calibration_min()} matches resolved (signed or failed)")
    if not why:
        ece = table["ece"] or 1.0
        if agreement and agreement >= 0.97 and sign_high and sign_high >= 0.7 and ece <= 0.08 and len(decided) >= 60:
            level = "L2"
            why.append("operators approve ≥97 % of proposals, ≥70 % of them sign, calibration error ≤8 %")
        elif agreement and agreement >= 0.9 and sign_high and sign_high >= 0.6 and ece <= 0.1:
            level = "L1"
            why.append("operators approve ≥90 %, ≥60 % sign, calibration error ≤10 % — approve on timeout")
        else:
            why.append(f"agreement {agreement or 0:.0%}, sign rate above threshold {sign_high or 0:.0%}, "
                       f"calibration error {ece:.0%} — not yet reliable enough")

    offers = [o for o in rows if o.kind == "indicative_offer"]
    answered = [o for o in offers if o.response]
    useful = [o for o in answered if o.response in ("accepted", "countered")]
    offer_level, offer_why = "L0", []
    if len(answered) < 20:
        offer_why.append(f"{len(answered)} of 20 offer responses so far")
    else:
        rate = len(useful) / len(answered)
        offer_level = "L1" if rate >= 0.5 else "L0"
        offer_why.append(f"{rate:.0%} of buyers answer an indicative offer with a price or an acceptance")

    return {
        "CONFIRM_MATCH": {"proposals": len(matches), "decided": len(decided), "approved": len(approved),
                          "operator_agreement": round(agreement, 3) if agreement is not None else None,
                          "resolved": len(resolved),
                          "signed": sum(1 for o in resolved if o.result == "signed"),
                          "sign_rate_above_threshold": round(sign_high, 3) if sign_high is not None else None,
                          "calibration": table, "recommended_level": level, "why": why},
        "SEND_INDICATIVE_OFFER": {"sent": len(offers), "answered": len(answered),
                                  "accepted_or_countered": len(useful), "recommended_level": offer_level,
                                  "why": offer_why},
    }
