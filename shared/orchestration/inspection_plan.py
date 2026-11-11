"""Which inspections a deal gets, and when — from the buyer's requirement.

  DUPRO  During Production Inspection — at ~``QC_DUPRO_PRODUCTION_PCT`` % produced
         (default 30 %), ``QC_DUPRO_DAYS_BEFORE_READY`` days (default 10) before the
         supplier's goods-ready date, never sooner than the agency's lead time.
         Catches a bad lot while it can still be corrected. A FAIL tells the
         supplier to rework and the operator to look; the deal does not move.
  PSI    Pre-Shipment Inspection — 100 % produced, ≥ ``QC_MIN_PACKED_PCT`` packed, on
         the goods-ready date, with supervised loading and sealing. The release
         check: PASS → QC_PASSED (export tranche, freight), FAIL → QC_FAILED.
         Always part of the plan.
  PEO    Pre-Customs Clearance Inspection — at the port of loading before the
         export filing; the FILE_CUSTOMS gate waits for its PASS.

The buyer's choice comes from its replies (``detect_types``) or staff
(``PUT /deals/{id}/inspection-plan``) and is kept on the deal
(``inspection_types``); default ``QC_DEFAULT_INSPECTION_TYPES`` (``PSI``).
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

ORDER = ("DUPRO", "PSI", "PEO")

META: dict[str, dict[str, Any]] = {
    "DUPRO": {"label": "During Production Inspection (DUPRO)", "short": "During production",
              "when": "while the goods are in production (about a third produced)",
              "aliases": (r"dupro", r"during[\s-]production", r"in[\s-]process inspection", r"mid[\s-]production")},
    "PSI": {"label": "Pre-Shipment Inspection (PSI)", "short": "Pre-shipment",
            "when": "when the goods are fully produced and packed, with supervised loading and sealing",
            "aliases": (r"psi", r"pre[\s-]shipment", r"final random inspection", r"\bfri\b")},
    "PEO": {"label": "Pre-Customs Clearance Inspection (PEO)", "short": "Pre-customs clearance",
            "when": "at the port of loading, before the export customs filing",
            "aliases": (r"peo", r"pre[\s-]customs", r"before customs clearance", r"pre[\s-]clearance")},
}

_NEGATION = re.compile(r"\b(no|not|without|skip|don't need|do not need|no need for)\b[^.,;!?\n]{0,20}$")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def default_types() -> list[str]:
    raw = os.getenv("QC_DEFAULT_INSPECTION_TYPES", "PSI")
    return normalise([t for t in raw.split(",")])


def normalise(types: list[str] | None) -> list[str]:
    """Known types, in phase order, PSI always included."""
    wanted = {str(t).strip().upper() for t in types or [] if str(t).strip()}
    wanted.add("PSI")
    return [t for t in ORDER if t in wanted]


def detect_types(text: str | None) -> list[str] | None:
    """The inspection phases a buyer's reply asks for (None when it names none).
    Quoted history and negated mentions ("no DUPRO needed") are ignored."""
    if not text:
        return None
    lines = []
    for line in text.splitlines():
        if re.match(r"^\s*on .{0,200}wrote:\s*$", line, re.IGNORECASE):
            break
        if not line.lstrip().startswith(">"):
            lines.append(line)
    lowered = "\n".join(lines).lower()
    found = []
    for key in ORDER:
        for alias in META[key]["aliases"]:
            for m in re.finditer(rf"(?<![\w-]){alias}(?![\w])", lowered):
                if not _NEGATION.search(lowered[max(0, m.start() - 32):m.start()]):
                    found.append(key)
                    break
            if key in found:
                break
    return normalise(found) if found else None


async def plan_for(deal_id: str) -> list[str]:
    """The deal's inspection phases: the deal's own plan, else the default."""
    try:
        from services.deals.repository import get_repository as deals_repo  # type: ignore[import]
        deal = await deals_repo().get(deal_id)
        if deal is not None and getattr(deal, "inspection_types", None):
            return normalise(deal.inspection_types)
    except Exception as exc:  # noqa: BLE001
        logger.debug("InspectionPlan: deal %s unavailable: %s", deal_id, exc)
    return default_types()


async def set_plan(deal_id: str, types: list[str], source: str) -> list[str]:
    """Store the plan on the deal (``source`` = buyer / operator). Returns it."""
    from services.deals.repository import get_repository as deals_repo  # type: ignore[import]
    plan = normalise(types)
    await deals_repo().set_fields(deal_id, {"inspection_types": plan, "inspection_types_source": source})
    logger.info("InspectionPlan: deal %s → %s (%s)", deal_id, "/".join(plan), source)
    return plan


def dupro_date(goods_ready: date | str | None, on: date | None = None) -> date | None:
    """The DUPRO day for a goods-ready date, or None when production ends too soon
    for a during-production visit (it would collide with the PSI)."""
    from shared.orchestration import qc_schedule
    ready = qc_schedule.to_date(goods_ready)
    if ready is None:
        return None
    earliest = qc_schedule.earliest_inspection_date(on)
    target = qc_schedule.next_business_day(ready - timedelta(days=_env_int("QC_DUPRO_DAYS_BEFORE_READY", 10)))
    target = max(target, earliest)
    return target if (ready - target).days >= 3 else None


def peo_date(on: date | None = None) -> date:
    from shared.orchestration import qc_schedule
    return qc_schedule.earliest_inspection_date(on)


def production_pct(inspection_type: str) -> int:
    return _env_int("QC_DUPRO_PRODUCTION_PCT", 30) if inspection_type == "DUPRO" else 100


def label(inspection_type: str) -> str:
    return META.get(inspection_type, {}).get("label", inspection_type)


def summary(types: list[str]) -> str:
    """Buyer / supplier-facing one-liner per phase."""
    return "; ".join(f"{META[t]['label']} — {META[t]['when']}" for t in normalise(types))
