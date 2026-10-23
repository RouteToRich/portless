"""Per-action autonomy policy — the single lever that turns the pipeline from
"ask a human at every step" into "run by itself, ask only where it matters".

Every HITL gate the pipeline creates (negotiation emails, agreement
confirmation, RFQ dispatch, PO, freight, customs, escrow tranche release,
supplier payout, counterparty notifications, …) asks this module which
autonomy level to create the approval at:

    L0  always ask — PENDING until a human resolves it
    L1  ask with timeout — auto-resolves with ``default_on_timeout``
    L2  auto-approve immediately, notify the operator (non-blocking)
    L3  fully autonomous — auto-approve and log only

Resolution order for ``resolve_level(action, deal)``:

  1. runtime override — set through ``PUT /hitl-approval/autonomy-policy``
     (the Autonomy Settings page); persisted in Mongo when ``MONGO_URI`` is
     configured, in-memory otherwise
  2. environment — ``AUTONOMY_<ACTION>=L2`` (e.g. ``AUTONOMY_SEND_NEGOTIATION_EMAIL``)
  3. the deal's own ``autonomy_profile`` — only for action classes that
     declare ``follows_deal_profile`` (PO, freight, customs, payout, tranche
     release), which preserves the pre-policy behaviour of those gates
  4. the built-in default for the action class

A safety ceiling then applies to money-moving actions (supplier payout and
escrow tranche release): they can never resolve above ``AUTONOMY_MONEY_CEILING``
(default ``L1``) — raising that ceiling is an explicit operator decision, not
a side effect of flipping a deal to L3.

Two hard overrides the callers apply themselves (documented here so the
policy is readable in one place):

  • a negotiation email draft flagged ``degraded_llm_fallback`` or
    ``unresolved_placeholders`` is always gated at L0 — a template with
    holes is never auto-sent regardless of policy;
  • the very first outbound negotiation email honours
    ``NegotiationSettings.hitl_on_first_outbound`` (default on) and stays L0.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

LEVELS: tuple[str, ...] = ("L0", "L1", "L2", "L3")
_LEVEL_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}


@dataclass(frozen=True)
class ActionClass:
    action: str
    label: str
    description: str
    default: str
    follows_deal_profile: bool = False
    money_moving: bool = False


# Order here is the order the Autonomy Settings page shows them in.
ACTION_CLASSES: tuple[ActionClass, ...] = (
    ActionClass(
        "DISPATCH_RFQ", "RFQ Dispatch",
        "Send RFQs to the shortlisted suppliers once a deal reaches MATCHING.",
        default="L2",
    ),
    ActionClass(
        "CONFIRM_SUPPLIER_SELECTION", "Supplier Selection",
        "Confirm a supplier the Match Engine did not auto-SOURCE (thin data / "
        "verification gap / re-match after a failed negotiation).",
        default="L0",
    ),
    ActionClass(
        "SEND_NEGOTIATION_EMAIL", "Negotiation Emails",
        "Send LLM-drafted negotiation emails to the buyer and supplier.",
        default="L0",
    ),
    ActionClass(
        "CONFIRM_NEGOTIATION_AGREEMENT", "Agreement Confirmation",
        "Close a negotiation the agent reads as agreed (unblocks PI issuance).",
        default="L0",
    ),
    ActionClass(
        "SEND_DEAL_NOTIFICATION", "Counterparty Notifications",
        "Templated status emails: PI issued, escrow funding link, PO issued, "
        "QC booked, shipment booked, cargo cleared, reminders.",
        default="L2",
    ),
    ActionClass(
        "CONFIRM_COUNTERPARTY_RISK", "Counterparty Risk",
        "Sanctions / registry screening (PortLess counterparty-check actor) returned REVIEW "
        "for the buyer — confirm before the Proforma Invoice goes out. BLOCK never asks.",
        default="L0",
    ),
    ActionClass(
        "CONFIRM_PI_ACCEPTANCE", "PI Acceptance",
        "Accept the Proforma Invoice when the buyer's email reply reads as an acceptance.",
        default="L0",
    ),
    ActionClass(
        "CONFIRM_BUYER_ACCEPTANCE", "Buyer Goods Acceptance",
        "The buyer's e-mail after delivery reads as acceptance of the goods — release the "
        "final escrow tranche (otherwise the inspection window decides).",
        default="L0", money_moving=True,
    ),
    ActionClass(
        "ISSUE_PO", "PO Issuance",
        "Issue the Purchase Order to the supplier once the escrow is funded.",
        default="L0", follows_deal_profile=True,
    ),
    ActionClass(
        "BOOK_FREIGHT", "Freight Booking",
        "Book the container once QC passes.",
        default="L0", follows_deal_profile=True,
    ),
    ActionClass(
        "FILE_CUSTOMS", "Customs Filing",
        "File the Shipping Bill with the CHA / ICEGATE once freight is booked.",
        default="L0", follows_deal_profile=True,
    ),
    ActionClass(
        "RELEASE_ESCROW_TRANCHE", "Escrow Tranche Release",
        "Release a staged escrow tranche when its proof arrives (PO, QC pass, customs cleared).",
        default="L0", follows_deal_profile=True, money_moving=True,
    ),
    ActionClass(
        "RELEASE_SUPPLIER_PAYOUT", "Supplier Payout",
        "Pay the supplier once the cargo has cleared.",
        default="L0", follows_deal_profile=True, money_moving=True,
    ),
    ActionClass(
        "RESOLVE_EXCEPTION", "Exception Resolution",
        "Operator decision requested by the Exception Resolver agent.",
        default="L0",
    ),
    ActionClass(
        "PARSE_INBOUND_EMAIL", "Unparsed Inbound Email",
        "An inbound email needs a human to read it (no price found / post-agreement reply).",
        default="L0",
    ),
)

_BY_ACTION: dict[str, ActionClass] = {a.action: a for a in ACTION_CLASSES}

_DEFAULT_MONEY_CEILING = "L1"

# Runtime overrides (action → level). Loaded from the policy store at startup,
# updated by ``set_policy``. Module-level so ``resolve_level`` stays sync and
# cheap — it is called inside hot pipeline paths.
_overrides: dict[str, str] = {}
_overrides_loaded = False


def _norm(level: Any) -> str | None:
    if level is None:
        return None
    value = getattr(level, "value", level)
    value = str(value).strip().upper()
    return value if value in _LEVEL_RANK else None


def action_classes() -> tuple[ActionClass, ...]:
    return ACTION_CLASSES


def is_money_moving(action: str) -> bool:
    cls = _BY_ACTION.get(action)
    return bool(cls and cls.money_moving)


def money_ceiling() -> str:
    return _norm(os.getenv("AUTONOMY_MONEY_CEILING")) or _DEFAULT_MONEY_CEILING


def _env_level(action: str) -> str | None:
    return _norm(os.getenv(f"AUTONOMY_{action}"))


def resolve_level(action: str, deal: Any | None = None) -> str:
    """Autonomy level to create an approval for *action* at (see module doc)."""
    cls = _BY_ACTION.get(action)
    level = _norm(_overrides.get(action)) or _env_level(action)
    source = "override" if _overrides.get(action) else ("env" if level else None)
    if level is None and cls is not None and cls.follows_deal_profile and deal is not None:
        level = _norm(getattr(deal, "autonomy_profile", None))
        source = "deal_profile" if level else None
    if level is None:
        level = cls.default if cls is not None else "L0"
        source = "default"

    if cls is not None and cls.money_moving:
        ceiling = money_ceiling()
        if _LEVEL_RANK[level] > _LEVEL_RANK[ceiling]:
            logger.warning(
                "AutonomyPolicy: %s resolved to %s (%s) but AUTONOMY_MONEY_CEILING=%s — "
                "clamping; raise the ceiling explicitly to automate money movement",
                action, level, source, ceiling,
            )
            level = ceiling
    return level


def effective_policy() -> dict[str, dict[str, Any]]:
    """Per-action view for the settings UI: effective level + where it came from."""
    out: dict[str, dict[str, Any]] = {}
    for cls in ACTION_CLASSES:
        override = _norm(_overrides.get(cls.action))
        env = _env_level(cls.action)
        effective = resolve_level(cls.action)
        source = "override" if override else ("env" if env else "default")
        requested = override or env or cls.default
        if cls.money_moving and _LEVEL_RANK[effective] < _LEVEL_RANK[requested]:
            source = "money_ceiling"
        out[cls.action] = {
            "action": cls.action,
            "label": cls.label,
            "description": cls.description,
            "default": cls.default,
            "override": override,
            "env": env,
            "effective": effective,
            "source": source,
            "follows_deal_profile": cls.follows_deal_profile,
            "money_moving": cls.money_moving,
        }
    return out


# ---------------------------------------------------------------------------
# Override store (Mongo when configured, in-memory otherwise)
# ---------------------------------------------------------------------------

_COLLECTION = "autonomy_policy"
_DOC_ID = "global"


def _mongo_collection():
    try:
        from shared.config import settings
        if not settings.mongo_uri:
            return None
        import motor.motor_asyncio as motor  # type: ignore[import]
        client: Any = motor.AsyncIOMotorClient(settings.mongo_uri)
        return client[settings.mongo_db_name][_COLLECTION]
    except Exception as exc:  # noqa: BLE001 — policy must never block startup
        logger.warning("AutonomyPolicy: Mongo unavailable (%s) — overrides are in-memory only", exc)
        return None


async def load_policy() -> dict[str, str]:
    """Load persisted overrides (called once from the app lifespan). Never raises."""
    global _overrides, _overrides_loaded
    col = _mongo_collection()
    if col is not None:
        try:
            doc = await col.find_one({"_id": _DOC_ID})
            if doc:
                _overrides = {
                    a: lvl for a, lvl in (doc.get("levels") or {}).items()
                    if a in _BY_ACTION and _norm(lvl)
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("AutonomyPolicy: could not load persisted policy: %s", exc)
    _overrides_loaded = True
    if _overrides:
        logger.info("AutonomyPolicy: %d override(s) active: %s", len(_overrides), _overrides)
    return dict(_overrides)


async def set_policy(levels: dict[str, str | None], updated_by: str = "operator") -> dict[str, str]:
    """Replace/extend overrides. ``None`` (or an empty string) clears one action."""
    global _overrides
    merged = dict(_overrides)
    for action, lvl in levels.items():
        if action not in _BY_ACTION:
            raise ValueError(f"Unknown action class {action!r}")
        if lvl in (None, ""):
            merged.pop(action, None)
            continue
        norm = _norm(lvl)
        if norm is None:
            raise ValueError(
                f"Invalid autonomy level {lvl!r} for {action} (expected one of {LEVELS})"
            )
        merged[action] = norm
    _overrides = merged
    col = _mongo_collection()
    if col is not None:
        try:
            await col.replace_one(
                {"_id": _DOC_ID},
                {"_id": _DOC_ID, "levels": merged, "updated_by": updated_by,
                 "updated_at": datetime.now(timezone.utc)},
                upsert=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("AutonomyPolicy: persisting policy failed (kept in memory): %s", exc)
    logger.info("AutonomyPolicy: overrides updated by %s → %s", updated_by, merged)
    return dict(merged)


def reset_for_tests() -> None:
    """Clear runtime overrides (unit tests only)."""
    global _overrides, _overrides_loaded
    _overrides = {}
    _overrides_loaded = False
