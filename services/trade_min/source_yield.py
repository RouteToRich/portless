"""Per-source yield of the directory (todo 21.3 "per-source yield on the radar / directory").

For each source that put companies in the directory (``apify_company_search``, ``manual``,
``indiamart``, a future list actor …): how many companies it found, how many passed the check,
were contacted, replied, quoted / showed interest, reached a deal and traded — and, for the Apify
actors, what the actor cost over the period and the cost per verified / replied / traded company.
The next actor money goes to the source that turns into deals.

``GET /trade-min/directory/yield?role=&days=``. A company found by several sources counts for
each of them (the sources list on the record). Never raises.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Relationship stages at or beyond each funnel step (relationships.relationship_of).
_CONTACTED = {"contacted", "replied", "interested", "quoted", "in_deal", "traded", "declined"}
_REPLIED = {"replied", "interested", "quoted", "in_deal", "traded", "declined"}
_ENGAGED = {"interested", "quoted", "in_deal", "traded"}
_IN_DEAL = {"in_deal", "traded"}

# Which actor's spend belongs to a directory source (apify_usage per-actor keys).
SOURCE_ACTOR = {
    "apify_company_search": "COMPANY_CONTACT_FINDER",
    "apify_contact_finder": "COMPANY_CONTACT_FINDER",
    "apify_export_markets": "EXPORT_MARKET_FINDER",
}


def _empty() -> dict[str, Any]:
    return {"found": 0, "verified": 0, "contacted": 0, "replied": 0, "engaged": 0, "in_deal": 0, "traded": 0}


def tally(rows: list[Any], since: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Funnel counts per source over annotated directory records (pure)."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        first = getattr(r, "first_seen_at", None)
        if since is not None and first is not None:
            first = first if first.tzinfo else first.replace(tzinfo=timezone.utc)
            if first < since:
                continue
        stage = str(((getattr(r, "relationship", None) or {}).get("stage")) or "new")
        verified = getattr(getattr(r, "validation", None), "status", None) == "verified"
        for src in dict.fromkeys([s for s in (getattr(r, "sources", None) or [getattr(r, "source", "")]) if s]):
            row = out.setdefault(src, _empty())
            row["found"] += 1
            row["verified"] += int(verified)
            row["contacted"] += int(stage in _CONTACTED)
            row["replied"] += int(stage in _REPLIED)
            row["engaged"] += int(stage in _ENGAGED)
            row["in_deal"] += int(stage in _IN_DEAL)
            row["traded"] += int(stage == "traded")
    for row in out.values():
        found = row["found"] or 1
        row["verified_pct"] = round(100.0 * row["verified"] / found, 1)
        row["reply_pct"] = round(100.0 * row["replied"] / row["contacted"], 1) if row["contacted"] else None
    return out


async def _actor_spend(days: int, now: datetime) -> dict[str, float]:
    """USD per actor key over the last ``days`` (platform_costs day documents)."""
    spend: dict[str, float] = {}
    try:
        from shared.mongo import collection
        col = collection("platform_costs")
    except Exception:  # noqa: BLE001
        col = None
    wanted = [(now - timedelta(days=i)).date().isoformat() for i in range(days)]
    if col is not None:
        try:
            async for doc in col.find({"_id": {"$in": wanted}}):
                for actor, v in (doc.get("apify_by_actor") or {}).items():
                    spend[actor] = spend.get(actor, 0.0) + float((v or {}).get("usd") or 0.0)
            return spend
        except Exception as exc:  # noqa: BLE001
            logger.debug("SourceYield: spend unreadable: %s", exc)
    try:
        from shared.connectors import apify_usage
        for actor, v in (apify_usage._by_actor_day.get(apify_usage.day_key(now)) or {}).items():  # noqa: SLF001
            spend[actor] = spend.get(actor, 0.0) + float(v.get("usd") or 0.0)
    except Exception:  # noqa: BLE001
        pass
    return spend


async def report(role: str | None = None, days: int = 90, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    days = max(1, min(int(days), 366))
    rows: list[Any] = []
    try:
        from services.trade_min import directory, relationships
        store = await directory.get_directory()
        for r in ("buyer", "supplier"):
            if role and r != role:
                continue
            rows += await relationships.annotate(await store.list(r, limit=100_000))
    except Exception as exc:  # noqa: BLE001
        logger.warning("SourceYield: directory unavailable: %s", exc)
    per_source = tally(rows, since=now - timedelta(days=days))
    spend = await _actor_spend(days, now)
    total_found = sum(v["found"] for v in per_source.values()) or 0
    actor_found: dict[str, int] = {}
    for src, row in per_source.items():
        actor = SOURCE_ACTOR.get(src)
        if actor:
            actor_found[actor] = actor_found.get(actor, 0) + row["found"]
    sources = []
    for src, row in sorted(per_source.items(), key=lambda kv: (-kv[1]["traded"], -kv[1]["engaged"], -kv[1]["found"])):
        actor = SOURCE_ACTOR.get(src)
        usd = None
        if actor and spend.get(actor) is not None:
            # an actor shared by two sources splits its spend by what each found
            usd = round(spend[actor] * row["found"] / max(1, actor_found.get(actor, 1)), 2)
        entry = {"source": src, **row, "apify_usd": usd}
        for step in ("verified", "replied", "traded"):
            entry[f"usd_per_{step}"] = round(usd / row[step], 2) if usd and row[step] else None
        sources.append(entry)
    return {"role": role or "all", "days": days, "companies": total_found, "sources": sources,
            "note": "A company found by several sources counts for each. Apify spend is read from the "
                    "daily budget counter (platform_costs)."}
