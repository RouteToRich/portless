"""Cargo insurance per Incoterm leg, the per-shipment declaration and claims (todo 4).

**Who insures which leg.** PortLess is the exporter of record, so the cover follows the risk it
carries under the sale term and the supplier's own term:

- **inland** (supplier's warehouse → port): PortLess's risk only when the supplier sells ex-works /
  FCA (on FOB the goods are the supplier's until they are on board) — line ``insurance_inland``,
  ``INSURANCE_INLAND_PCT`` (0.05 % of the price);
- **sea leg** for CIF / CIP: PortLess insures to the named port / place at ``INSURED_VALUE_PCT``
  (110) % of the invoice value, buyer named as the party paid — Institute Cargo Clauses (C) for CIF,
  (A) for CIP — line ``marine_insurance`` (``LANDED_INSURANCE_PCT`` × 110 %);
- **D-terms** (DAP / DPU / DDP): the same cover, carried on to the buyer's door;
- **F- and C-terms without insurance** (FOB / FCA / FAS / CFR / CPT): the buyer insures the main leg;
  PortLess keeps seller's-interest (contingency) cover for the case the buyer's cover does not
  respond — line ``insurance_contingency``, ``INSURANCE_CONTINGENCY_PCT`` (0.1).

**Declaration.** Each shipment is declared under the open policy before the ship sails; the
certificate number (``POST /deals/{id}/insurance/declaration``) is printed on the commercial invoice
and the insurance declaration.

**Claims.** ``POST /deals/{id}/insurance/claims`` opens a claim (reason, amount, delivery date); the
insurer must be told within ``INSURANCE_CLAIM_NOTICE_DAYS`` (7) of delivery, so a claim with no
notice by then is overdue (digest). Status: opened → notified → surveyed → filed → settled /
rejected; a settled claim records the amount recovered (the run costs and the timeline show it).
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

INSURED_BY_SELLER = ("CIF", "CIP", "DAP", "DPU", "DDP")
D_TERMS = ("DAP", "DPU", "DDP")
CLAIM_STATUSES = ("opened", "notified", "surveyed", "filed", "settled", "rejected")
_NEXT = {"opened": ("notified", "rejected"), "notified": ("surveyed", "filed", "rejected"),
         "surveyed": ("filed", "rejected"), "filed": ("settled", "rejected")}


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def insured_value_pct() -> float:
    return _f("INSURED_VALUE_PCT", 110.0)


def segments(incoterm: str | None, supplier_basis: str | None = "FOB", *, marine_pct: float | None = None) -> list[dict[str, Any]]:
    """Pure: the insurance lines for a sale term and the supplier's term — ``key`` / ``label`` /
    ``pct_of_price`` / ``note`` (the cost sheet turns them into CostLines)."""
    from shared.orchestration import landed_cost
    term = landed_cost.term(incoterm or "CIF")
    basis = (supplier_basis or "FOB").upper()
    out: list[dict[str, Any]] = []
    if basis in ("EXW", "EX-WORKS", "EX WORKS", "FCA"):
        out.append({"key": "insurance_inland", "label": "Inland transit insurance, supplier's warehouse to port",
                    "pct_of_price": _f("INSURANCE_INLAND_PCT", 0.05),
                    "note": "the supplier sells ex-works, so the truck to the port is at PortLess's risk"})
    base = landed_cost.insurance_pct() if marine_pct is None else marine_pct
    if term in INSURED_BY_SELLER:
        to = "to the buyer's door" if term in D_TERMS else "to the named port / place"
        clauses = "Institute Cargo Clauses (A)" if term in ("CIP", *D_TERMS) else "Institute Cargo Clauses (C)"
        out.append({"key": "marine_insurance", "label": f"Marine cargo insurance ({term}, {to})",
                    "pct_of_price": round(base * insured_value_pct() / 100.0, 4),
                    "note": f"{insured_value_pct():g} % of the invoice value, {clauses}, warehouse to warehouse, "
                            "buyer named as the party paid"})
    else:
        out.append({"key": "insurance_contingency", "label": f"Seller's-interest (contingency) cover ({term})",
                    "pct_of_price": _f("INSURANCE_CONTINGENCY_PCT", 0.1),
                    "note": f"the buyer insures the main leg under {term}; this covers PortLess if that cover "
                            "does not respond"})
    return out


# ── declaration ─────────────────────────────────────────────────────────────

async def _deal(deal_id: str) -> Any:
    from services.deals.repository import get_repository  # type: ignore[import]
    deal = await get_repository().get(deal_id)
    if deal is None:
        raise LookupError(f"deal {deal_id} not found")
    return deal


async def _save(deal_id: str, insurance: dict[str, Any]) -> None:
    from services.deals.repository import get_repository  # type: ignore[import]
    await get_repository().set_fields(deal_id, {"insurance": insurance})


async def record_declaration(deal_id: str, *, certificate_no: str, insurer: str | None,
                             sum_insured_usd: float | None, by: str) -> dict[str, Any]:
    if not (certificate_no or "").strip():
        raise ValueError("the insurance certificate number is required")
    if sum_insured_usd is not None and sum_insured_usd <= 0:
        raise ValueError("sum_insured_usd must be positive")
    deal = await _deal(deal_id)
    ins = dict(getattr(deal, "insurance", None) or {})
    ins["declaration"] = {"certificate_no": certificate_no.strip(), "insurer": (insurer or "").strip() or None,
                          "sum_insured_usd": sum_insured_usd, "declared_by": by,
                          "declared_at": datetime.now(timezone.utc).isoformat()}
    await _save(deal_id, ins)
    return ins["declaration"]


def certificate_no(deal: Any) -> str | None:
    return ((getattr(deal, "insurance", None) or {}).get("declaration") or {}).get("certificate_no")


# ── claims ──────────────────────────────────────────────────────────────────

def notice_days() -> int:
    return int(_f("INSURANCE_CLAIM_NOTICE_DAYS", 7))


def notice_due(delivered_on: str | None, opened_at: str) -> str:
    start = date.fromisoformat(delivered_on) if delivered_on else datetime.fromisoformat(opened_at).date()
    return (start + timedelta(days=notice_days())).isoformat()


async def open_claim(deal_id: str, *, reason: str, amount_usd: float, delivered_on: str | None, by: str) -> dict[str, Any]:
    if not (reason or "").strip():
        raise ValueError("say what was lost or damaged")
    if amount_usd is None or amount_usd <= 0:
        raise ValueError("amount_usd must be positive")
    if delivered_on:
        try:
            date.fromisoformat(delivered_on)
        except ValueError as exc:
            raise ValueError("delivered_on must be an ISO date") from exc
    deal = await _deal(deal_id)
    ins = dict(getattr(deal, "insurance", None) or {})
    now = datetime.now(timezone.utc).isoformat()
    claim = {"claim_id": f"CLM-{uuid.uuid4().hex[:8].upper()}", "status": "opened", "reason": reason.strip()[:500],
             "amount_usd": float(amount_usd), "delivered_on": delivered_on, "opened_by": by, "opened_at": now,
             "notice_due": notice_due(delivered_on, now), "history": [{"status": "opened", "by": by, "at": now}]}
    ins["claims"] = [*(ins.get("claims") or []), claim]
    await _save(deal_id, ins)
    return claim


async def update_claim(deal_id: str, claim_id: str, *, status: str, by: str, survey_ref: str | None = None,
                       recovered_usd: float | None = None, note: str | None = None) -> dict[str, Any]:
    deal = await _deal(deal_id)
    ins = dict(getattr(deal, "insurance", None) or {})
    claims = [dict(c) for c in (ins.get("claims") or [])]
    claim = next((c for c in claims if c["claim_id"] == claim_id), None)
    if claim is None:
        raise LookupError(f"claim {claim_id} not found")
    if status not in _NEXT.get(claim["status"], ()):
        raise ValueError(f"claim is {claim['status']}; next can be {', '.join(_NEXT.get(claim['status'], ())) or 'nothing'}")
    if status == "surveyed" and not (survey_ref or "").strip():
        raise ValueError("the survey report reference is required")
    if status == "settled" and (recovered_usd is None or recovered_usd < 0):
        raise ValueError("recovered_usd is required when the claim is settled")
    now = datetime.now(timezone.utc).isoformat()
    claim.update(status=status, **({"survey_ref": survey_ref.strip()} if survey_ref else {}),
                 **({"recovered_usd": float(recovered_usd)} if recovered_usd is not None else {}))
    if status == "notified":
        claim["notified_at"] = now
    claim["history"] = [*claim.get("history", []), {"status": status, "by": by, "at": now, "note": (note or "")[:300] or None}]
    ins["claims"] = claims
    await _save(deal_id, ins)
    if status == "settled" and recovered_usd:
        from shared import deal_costs
        deal_costs.add(deal_id, insurance_recovered_usd=float(recovered_usd))
    return claim


def overdue(deal: Any, today: date | None = None) -> list[dict[str, Any]]:
    """Pure: claims still not notified to the insurer after the notice period."""
    today = today or datetime.now(timezone.utc).date()
    return [c for c in ((getattr(deal, "insurance", None) or {}).get("claims") or [])
            if c.get("status") == "opened" and c.get("notice_due") and date.fromisoformat(c["notice_due"]) < today]
