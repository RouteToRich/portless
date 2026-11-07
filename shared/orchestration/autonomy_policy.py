"""Per-action autonomy policy — the single lever that turns the pipeline from
"ask a human at every step" into "run by itself, ask only where it matters".

Every HITL gate the pipeline creates (negotiation emails, agreement
confirmation, RFQ dispatch, buyer outreach, PO, freight, customs, escrow
tranche release, supplier payout, counterparty notifications, …) asks this
module **how** to create the approval:

    L0  always ask — PENDING until a human resolves it
    L1  ask with timeout — auto-resolves with ``default_on_timeout`` after
        ``timeout_seconds``
    L2  auto-approve immediately, notify the operator (non-blocking)
    L3  fully autonomous — auto-approve and log only

Version 2 (2026-10-24) adds the two settings that make L1 a safety net instead
of a one-hour deadline, and the value threshold that lets small money moves run
unattended:

  • ``timeout_seconds`` per action (default 24 h, negotiation e-mails 4 h)
  • ``default_on_timeout`` per action — APPROVE or REJECT when the timeout
    lapses (money actions default to REJECT; set APPROVE for a proof-gated
    tranche release once you trust the proofs)
  • ``auto_approve_below_usd`` — a money-moving gate whose amount is at or
    below the threshold, with its preconditions met (proof refs present,
    ledger reconciled), is created at L2. Per action, with a global
    ``money_auto_approve_below_usd`` fallback. Off (``None``) by default.

Resolution order for every setting: runtime override (Autonomy Settings page /
``PUT /hitl-approval/autonomy-policy``, Mongo-persisted) → environment
(``AUTONOMY_<ACTION>``, ``AUTONOMY_<ACTION>_TIMEOUT_SECONDS``,
``AUTONOMY_<ACTION>_ON_TIMEOUT``, ``AUTONOMY_<ACTION>_AUTO_APPROVE_BELOW_USD``,
``AUTONOMY_MONEY_AUTO_APPROVE_BELOW_USD``) → the deal's ``autonomy_profile``
(level only, for the action classes that declare ``follows_deal_profile``) →
the built-in default.

A safety ceiling applies to the *level* of money-moving actions
(``AUTONOMY_MONEY_CEILING``, default L1). The value threshold is the explicit,
separately configured exception to that ceiling: an operator who sets it is
choosing to automate money below that amount.

Two hard overrides the callers apply themselves: a negotiation draft flagged
``degraded_llm_fallback`` / ``unresolved_placeholders`` is always L0, and the
first outbound negotiation e-mail honours ``hitl_on_first_outbound``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

LEVELS: tuple[str, ...] = ("L0", "L1", "L2", "L3")
_LEVEL_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}
_ON_TIMEOUT = ("APPROVE", "REJECT")

_HOUR = 3600
_DAY = 24 * _HOUR


@dataclass(frozen=True)
class ActionClass:
    action: str
    label: str
    description: str
    default: str
    follows_deal_profile: bool = False
    money_moving: bool = False
    default_timeout_seconds: int = _DAY
    default_on_timeout: str = "REJECT"


@dataclass
class GateSpec:
    """How to create one approval: level + L1 timeout behaviour + provenance."""

    action: str
    level: str
    timeout_seconds: int
    default_on_timeout: str
    source: str
    auto_approve_below_usd: float | None = None
    threshold_applied: bool = False

    def expires_at_iso(self, now: datetime | None = None) -> str | None:
        if self.level != "L1":
            return None
        return ((now or datetime.now(timezone.utc)) + timedelta(seconds=self.timeout_seconds)).isoformat()


# Order here is the order the Autonomy Settings page shows them in.
ACTION_CLASSES: tuple[ActionClass, ...] = (
    ActionClass(
        "DISPATCH_RFQ", "RFQ Dispatch",
        "Send RFQs to the shortlisted suppliers once a deal reaches MATCHING.",
        default="L2",
    ),
    ActionClass(
        "SEND_BUYER_OUTREACH", "Buyer Outreach",
        "First templated introduction to a buyer the scan identified (pitch from the "
        "actor's fit reason). Replies are classified; the first interested buyer gets the deal.",
        default="L2", default_timeout_seconds=_DAY, default_on_timeout="APPROVE",
    ),
    ActionClass(
        "SEND_INDICATIVE_OFFER", "Indicative Offers",
        "Send an interested buyer a price range built from a supplier's quote + landed cost + "
        "margin floor, so their reaction (accept / counter) reveals what they will pay.",
        default="L0",
    ),
    ActionClass(
        "CONFIRM_MATCH", "Match Confirmation",
        "The match board's best buyer × supplier pair has firm prices and a high enough chance "
        "of closing — confirm it and the prospect becomes a deal (negotiation opens).",
        default="L0",
    ),
    ActionClass(
        "CONFIRM_SUPPLIER_SELECTION", "Supplier Selection",
        "Confirm a supplier the Match Engine did not auto-SOURCE (thin data / "
        "verification gap / re-match after a failed negotiation). A GST+IEC-verified "
        "quote inside the margin corridor is auto-sourced regardless of this level.",
        default="L0",
    ),
    ActionClass(
        "SEND_NEGOTIATION_EMAIL", "Negotiation Emails",
        "Send LLM-drafted negotiation emails to the buyer and supplier.",
        default="L0", default_timeout_seconds=4 * _HOUR, default_on_timeout="APPROVE",
    ),
    ActionClass(
        "CONFIRM_NEGOTIATION_AGREEMENT", "Agreement Confirmation",
        "Close a negotiation the agent reads as agreed (unblocks PI issuance).",
        default="L0", default_timeout_seconds=_DAY, default_on_timeout="APPROVE",
    ),
    ActionClass(
        "SEND_DEAL_NOTIFICATION", "Counterparty Notifications",
        "Templated status emails: PI issued, escrow funding link, PO issued, "
        "QC booked, shipment booked, cargo cleared, reminders.",
        default="L2", default_on_timeout="APPROVE",
    ),
    ActionClass(
        "CONFIRM_COUNTERPARTY_RISK", "Counterparty Risk",
        "Sanctions / registry screening (PortLess counterparty-check actor) returned REVIEW "
        "for the buyer — confirm before the Proforma Invoice goes out. BLOCK never asks.",
        default="L0",
    ),
    ActionClass(
        "CONFIRM_TRADE_COMPLIANCE", "Trade Compliance",
        "Product export policy (ITC-HS / SCOMET), destination, unscreened party, related "
        "parties or an off-market price need a look before the Proforma Invoice. Always asks; "
        "a BLOCK (prohibited product, embargoed destination) never reaches a gate.",
        default="L0",
    ),
    ActionClass(
        "CONFIRM_PI_ACCEPTANCE", "PI Acceptance",
        "Accept the Proforma Invoice when the buyer's email reply reads as an acceptance.",
        default="L0", default_timeout_seconds=_DAY, default_on_timeout="APPROVE",
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
        default="L0", follows_deal_profile=True, default_on_timeout="APPROVE",
    ),
    ActionClass(
        "BOOK_FREIGHT", "Freight Booking",
        "Book the container once QC passes.",
        default="L0", follows_deal_profile=True, default_on_timeout="APPROVE",
    ),
    ActionClass(
        "FILE_CUSTOMS", "Customs Filing",
        "File the Shipping Bill with the CHA / ICEGATE once freight is booked.",
        default="L0", follows_deal_profile=True, default_on_timeout="APPROVE",
    ),
    ActionClass(
        "RELEASE_ESCROW_TRANCHE", "Escrow Tranche Release",
        "Release a staged escrow tranche when its proof arrives (PO, QC pass, customs cleared). "
        "Auto-approved below the value threshold when the proof refs are present and the "
        "ledger reconciles.",
        default="L0", follows_deal_profile=True, money_moving=True,
    ),
    ActionClass(
        "RELEASE_SUPPLIER_PAYOUT", "Supplier Payout",
        "Pay the supplier once the cargo has cleared — capped at the funds released from "
        "escrow. Auto-approved below the value threshold when the ledger reconciles.",
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

# Runtime overrides. ``_overrides[action]`` = {"level", "timeout_seconds",
# "default_on_timeout", "auto_approve_below_usd"} (any subset). Module-level so
# ``resolve_gate`` stays sync and cheap — it is called inside hot pipeline paths.
_overrides: dict[str, dict[str, Any]] = {}
_money_threshold_override: float | None = None
_overrides_loaded = False


def _norm(level: Any) -> str | None:
    if level is None:
        return None
    value = getattr(level, "value", level)
    value = str(value).strip().upper()
    return value if value in _LEVEL_RANK else None


def _norm_on_timeout(value: Any) -> str | None:
    if value is None:
        return None
    v = str(value).strip().upper()
    return v if v in _ON_TIMEOUT else None


def _norm_seconds(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        s = int(float(value))
    except (TypeError, ValueError):
        return None
    return s if s > 0 else None


def _norm_usd(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def action_classes() -> tuple[ActionClass, ...]:
    return ACTION_CLASSES


def is_money_moving(action: str) -> bool:
    cls = _BY_ACTION.get(action)
    return bool(cls and cls.money_moving)


def money_ceiling() -> str:
    return _norm(os.getenv("AUTONOMY_MONEY_CEILING")) or _DEFAULT_MONEY_CEILING


def money_auto_approve_below_usd() -> float | None:
    """Global value threshold for money-moving gates (override → env → None)."""
    if _money_threshold_override is not None:
        return _money_threshold_override
    return _norm_usd(os.getenv("AUTONOMY_MONEY_AUTO_APPROVE_BELOW_USD"))


def _env_level(action: str) -> str | None:
    return _norm(os.getenv(f"AUTONOMY_{action}"))


def _ov(action: str, key: str) -> Any:
    return (_overrides.get(action) or {}).get(key)


def resolve_level(action: str, deal: Any | None = None) -> str:
    """Autonomy level to create an approval for *action* at (see module doc)."""
    level, _ = _resolve_level_with_source(action, deal)
    return level


def _resolve_level_with_source(action: str, deal: Any | None) -> tuple[str, str]:
    cls = _BY_ACTION.get(action)
    level = _norm(_ov(action, "level")) or _env_level(action)
    source = "override" if _norm(_ov(action, "level")) else ("env" if level else "")
    if level is None and cls is not None and cls.follows_deal_profile and deal is not None:
        level = _norm(getattr(deal, "autonomy_profile", None))
        source = "deal_profile" if level else ""
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
            source = "money_ceiling"
    return level, source


def timeout_seconds(action: str) -> int:
    cls = _BY_ACTION.get(action)
    return (
        _norm_seconds(_ov(action, "timeout_seconds"))
        or _norm_seconds(os.getenv(f"AUTONOMY_{action}_TIMEOUT_SECONDS"))
        or (cls.default_timeout_seconds if cls else _DAY)
    )


def default_on_timeout(action: str) -> str:
    cls = _BY_ACTION.get(action)
    return (
        _norm_on_timeout(_ov(action, "default_on_timeout"))
        or _norm_on_timeout(os.getenv(f"AUTONOMY_{action}_ON_TIMEOUT"))
        or (cls.default_on_timeout if cls else "REJECT")
    )


def auto_approve_below_usd(action: str) -> float | None:
    """Value threshold for *action* (money-moving only): override → env → global."""
    cls = _BY_ACTION.get(action)
    if cls is None or not cls.money_moving:
        return None
    return (
        _norm_usd(_ov(action, "auto_approve_below_usd"))
        or _norm_usd(os.getenv(f"AUTONOMY_{action}_AUTO_APPROVE_BELOW_USD"))
        or money_auto_approve_below_usd()
    )


def resolve_gate(
    action: str,
    deal: Any | None = None,
    *,
    amount: float | None = None,
    conditions_ok: bool = True,
) -> GateSpec:
    """Everything a caller needs to create the approval for *action*.

    ``amount`` (USD) and ``conditions_ok`` only matter for money-moving actions:
    at or below the value threshold, with the preconditions met, the gate is
    created at L2 even though the money ceiling would otherwise cap it at L1.
    """
    level, source = _resolve_level_with_source(action, deal)
    threshold = auto_approve_below_usd(action)
    applied = False
    if (
        threshold is not None and amount is not None and conditions_ok
        and 0 < float(amount) <= threshold and _LEVEL_RANK[level] < _LEVEL_RANK["L2"]
    ):
        level, source, applied = "L2", "value_threshold", True
        logger.info(
            "AutonomyPolicy: %s auto-approved — %.2f USD within the value threshold %.2f",
            action, float(amount), threshold,
        )
    return GateSpec(
        action=action,
        level=level,
        timeout_seconds=timeout_seconds(action),
        default_on_timeout=default_on_timeout(action),
        source=source,
        auto_approve_below_usd=threshold,
        threshold_applied=applied,
    )


def effective_policy() -> dict[str, dict[str, Any]]:
    """Per-action view for the settings UI: effective values + where they came from."""
    out: dict[str, dict[str, Any]] = {}
    for cls in ACTION_CLASSES:
        override = _norm(_ov(cls.action, "level"))
        env = _env_level(cls.action)
        effective, source = _resolve_level_with_source(cls.action, None)
        requested = override or env or cls.default
        if cls.money_moving and _LEVEL_RANK[effective] < _LEVEL_RANK[requested]:
            source = "money_ceiling"
        elif source in ("", "deal_profile"):
            source = "default"
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
            # v2 gate settings
            "timeout_seconds": timeout_seconds(cls.action),
            "timeout_override": _norm_seconds(_ov(cls.action, "timeout_seconds")),
            "default_timeout_seconds": cls.default_timeout_seconds,
            "default_on_timeout": default_on_timeout(cls.action),
            "on_timeout_override": _norm_on_timeout(_ov(cls.action, "default_on_timeout")),
            "auto_approve_below_usd": auto_approve_below_usd(cls.action),
            "threshold_override": _norm_usd(_ov(cls.action, "auto_approve_below_usd")),
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


def _clean_gate(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if _norm(raw.get("level")):
        out["level"] = _norm(raw.get("level"))
    if _norm_seconds(raw.get("timeout_seconds")):
        out["timeout_seconds"] = _norm_seconds(raw.get("timeout_seconds"))
    if _norm_on_timeout(raw.get("default_on_timeout")):
        out["default_on_timeout"] = _norm_on_timeout(raw.get("default_on_timeout"))
    if _norm_usd(raw.get("auto_approve_below_usd")):
        out["auto_approve_below_usd"] = _norm_usd(raw.get("auto_approve_below_usd"))
    return out


def _doc_from_state() -> dict[str, Any]:
    return {
        "_id": _DOC_ID,
        "levels": {a: g["level"] for a, g in _overrides.items() if g.get("level")},
        "gates": _overrides,
        "money_auto_approve_below_usd": _money_threshold_override,
    }


async def load_policy() -> dict[str, dict[str, Any]]:
    """Load persisted overrides (called once from the app lifespan). Never raises."""
    global _overrides, _overrides_loaded, _money_threshold_override
    col = _mongo_collection()
    if col is not None:
        try:
            doc = await col.find_one({"_id": _DOC_ID})
            if doc:
                gates: dict[str, dict[str, Any]] = {}
                for a, lvl in (doc.get("levels") or {}).items():  # v1 documents
                    if a in _BY_ACTION and _norm(lvl):
                        gates[a] = {"level": _norm(lvl)}
                for a, g in (doc.get("gates") or {}).items():
                    if a in _BY_ACTION and isinstance(g, dict):
                        cleaned = _clean_gate(g)
                        if cleaned:
                            gates[a] = {**gates.get(a, {}), **cleaned}
                _overrides = gates
                _money_threshold_override = _norm_usd(doc.get("money_auto_approve_below_usd"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("AutonomyPolicy: could not load persisted policy: %s", exc)
    _overrides_loaded = True
    if _overrides or _money_threshold_override:
        logger.info("AutonomyPolicy: overrides active: %s (money threshold %s)",
                    _overrides, _money_threshold_override)
    return {a: dict(g) for a, g in _overrides.items()}


async def set_policy(
    levels: dict[str, Any] | None = None,
    updated_by: str = "operator",
    *,
    gates: dict[str, dict[str, Any] | None] | None = None,
    money_auto_approve_below_usd: float | None | str = "__unchanged__",
) -> dict[str, dict[str, Any]]:
    """Replace/extend overrides.

    ``levels`` keeps the v1 contract (``action → L0..L3``; ``None``/"" clears the
    level). ``gates`` sets any of ``level`` / ``timeout_seconds`` /
    ``default_on_timeout`` / ``auto_approve_below_usd`` per action; ``None``
    clears every setting of that action. ``money_auto_approve_below_usd``
    sets or clears (``None``) the global value threshold.
    """
    global _overrides, _money_threshold_override
    merged: dict[str, dict[str, Any]] = {a: dict(g) for a, g in _overrides.items()}

    for action, lvl in (levels or {}).items():
        if action not in _BY_ACTION:
            raise ValueError(f"Unknown action class {action!r}")
        if lvl in (None, ""):
            merged.get(action, {}).pop("level", None)
            continue
        norm = _norm(lvl)
        if norm is None:
            raise ValueError(f"Invalid autonomy level {lvl!r} for {action} (expected one of {LEVELS})")
        merged.setdefault(action, {})["level"] = norm

    for action, g in (gates or {}).items():
        if action not in _BY_ACTION:
            raise ValueError(f"Unknown action class {action!r}")
        if g is None:
            merged.pop(action, None)
            continue
        current = merged.setdefault(action, {})
        for key in ("level", "timeout_seconds", "default_on_timeout", "auto_approve_below_usd"):
            if key not in g:
                continue
            value = g[key]
            if value in (None, ""):
                current.pop(key, None)
                continue
            cleaned = _clean_gate({key: value})
            if key not in cleaned:
                raise ValueError(f"Invalid {key} {value!r} for {action}")
            if key == "auto_approve_below_usd" and not _BY_ACTION[action].money_moving:
                raise ValueError(f"{action} is not money-moving — no value threshold applies")
            current[key] = cleaned[key]

    merged = {a: g for a, g in merged.items() if g}
    if money_auto_approve_below_usd != "__unchanged__":
        if money_auto_approve_below_usd in (None, "", 0, "0"):
            _money_threshold_override = None
        else:
            value = _norm_usd(money_auto_approve_below_usd)
            if value is None:
                raise ValueError(f"Invalid money threshold {money_auto_approve_below_usd!r}")
            _money_threshold_override = value

    _overrides = merged
    col = _mongo_collection()
    if col is not None:
        try:
            await col.replace_one(
                {"_id": _DOC_ID},
                {**_doc_from_state(), "updated_by": updated_by,
                 "updated_at": datetime.now(timezone.utc)},
                upsert=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("AutonomyPolicy: persisting policy failed (kept in memory): %s", exc)
    logger.info("AutonomyPolicy: overrides updated by %s → %s (money threshold %s)",
                updated_by, merged, _money_threshold_override)
    # v1 contract: action → level (the full per-action settings are in effective_policy()).
    return {a: g["level"] for a, g in merged.items() if g.get("level")}


def current_overrides() -> dict[str, dict[str, Any]]:
    """Raw per-action overrides (level / timeout / on-timeout / threshold)."""
    return {a: dict(g) for a, g in _overrides.items()}


def reset_for_tests() -> None:
    """Clear runtime overrides (unit tests only)."""
    global _overrides, _overrides_loaded, _money_threshold_override
    _overrides = {}
    _money_threshold_override = None
    _overrides_loaded = False
