"""Regulatory watch — DGFT and CBIC updates checked daily, not by memory (todo 3.4).

Once a day (deal watcher, leader-locked; ``REGULATORY_WATCH_ENABLED``, pinned off in tests) the
watcher reads the pages in ``REGULATORY_WATCH_URLS`` — by default DGFT notifications, public
notices and trade notices, and CBIC customs (export duty) notifications — and keeps every new
item (link text + URL, Mongo ``regulatory_items``). A new item that names a catalogue product (its
name or a synonym) or one of its HS codes (4 / 6 / 8 digits) is a **hit**:

- the HS policy confirmation of that code is marked "re-confirm" (``compliance.mark_reconfirm``),
  so the trade check reviews it again before outreach, the PI and the PO;
- open deals of that code get a ``REGULATORY:<hs>`` exception (the PI / PO wait for a person);
- Telegram is paged and the digest lists it.

The DGFT denied-entity list page (``REGULATORY_DENIED_LIST_URL``) is read too: a link dated after
the loaded list (``denied_list.meta``) warns that a newer list is published.

Pages are parsed for links only (``<a href>…</a>``); a page that cannot be read is reported, never
fatal. Field names and page layouts change — the watcher reports what it read, not more.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_URLS = (
    "dgft_notifications|https://www.dgft.gov.in/CP/?opt=notification",
    "dgft_public_notices|https://www.dgft.gov.in/CP/?opt=public-notice",
    "dgft_trade_notices|https://www.dgft.gov.in/CP/?opt=trade-notice",
    "cbic_customs|https://taxinformation.cbic.gov.in/content-page/explore-notification",
)
_LINK = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_CODE = re.compile(r"(?<!\d)(\d{4}(?:[\s.]?\d{2}){0,2})(?!\d)")
_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2}|\d{2}[./-]\d{2}[./-]20\d{2})\b")
_seen: dict[str, dict[str, Any]] = {}


def _iso_date(raw: str) -> str | None:
    """"2026-11-02" or "02.11.2026" / "02/11/2026" (day first, as DGFT writes) → ISO date."""
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def enabled() -> bool:
    return os.getenv("REGULATORY_WATCH_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def sources() -> list[tuple[str, str]]:
    raw = os.getenv("REGULATORY_WATCH_URLS", "").strip()
    items = [x for x in (raw.split(",") if raw else DEFAULT_URLS) if x.strip()]
    out = []
    for item in items:
        name, _, url = item.partition("|")
        out.append((name.strip(), url.strip()) if url else (url or name, name.strip()))
    return out


def links(html: str, base: str = "") -> list[dict[str, str]]:
    """Pure: every link with visible text on a page."""
    out = []
    for href, text in _LINK.findall(html or ""):
        title = " ".join(unescape(_TAG.sub(" ", text)).split())
        if len(title) < 6:
            continue
        if href.startswith("/") and base:
            m = re.match(r"(https?://[^/]+)", base)
            href = (m.group(1) if m else "") + href
        out.append({"title": title, "url": href})
    return out


def match(title: str, lines: list[Any]) -> dict[str, list[str]]:
    """Pure: catalogue HS codes and product names a notice title names."""
    t = " " + " ".join(title.lower().split()) + " "
    codes_in_title = {re.sub(r"[\s.]", "", c) for c in _CODE.findall(title)}
    hs, products = [], []
    for line in lines:
        code = "".join(ch for ch in str(getattr(line, "hs_code", "")) if ch.isdigit())
        all_codes = {code, *[c for c in (getattr(line, "hs8_codes", None) or [])]}
        if any(c and any(x.startswith(c[:4]) and c.startswith(x[:len(c)][:len(x)]) for x in codes_in_title)
               for c in all_codes):
            hs.append(code)
            continue
        for name in [getattr(line, "name", ""), *(getattr(line, "synonyms", None) or [])]:
            n = " ".join(str(name or "").lower().split())
            if n and re.search(r"(?<![a-z])" + re.escape(n) + r"s?(?![a-z])", t):
                products.append(getattr(line, "name", n))
                hs.append(code)
                break
    return {"hs_codes": sorted(set(hs)), "products": sorted(set(products))}


def _collection() -> Any:
    try:
        from shared.mongo import collection
        return collection("regulatory_items")
    except Exception:  # noqa: BLE001
        return None


async def _known(item_id: str) -> bool:
    if item_id in _seen:
        return True
    col = _collection()
    if col is not None:
        try:
            return await col.find_one({"_id": item_id}) is not None
        except Exception:  # noqa: BLE001
            return False
    return False


async def _store(item: dict[str, Any]) -> None:
    _seen[item["_id"]] = item
    col = _collection()
    if col is not None:
        try:
            await col.replace_one({"_id": item["_id"]}, item, upsert=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("RegulatoryWatch: item not stored: %s", exc)


async def _act_on_hit(item: dict[str, Any]) -> list[str]:
    from shared.orchestration import compliance
    from shared.orchestration.continuations import _publish_exception
    note = f"{item['source']}: {item['title'][:200]} ({item['url']})"
    deals_held: list[str] = []
    for hs in item["hs_codes"]:
        await compliance.mark_reconfirm(hs, note)
    try:
        from services.deals.repository import get_repository  # type: ignore[import]
        deals = await get_repository().list_active(limit=5000)
    except Exception:  # noqa: BLE001
        deals = []
    for d in deals:
        status = str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", "")))
        if status in ("PAID", "CANCELLED", "REFUNDED", "NEGOTIATION_FAILED", "IN_TRANSIT", "CLEARED"):
            continue
        if any(str(getattr(d, "hs_code", "")).startswith(h[:6]) for h in item["hs_codes"]):
            await _publish_exception(d.deal_id, f"REGULATORY:{item['hs_codes'][0]}",
                                     f"a new notice may change the export policy — {note}",
                                     {"category": "UNKNOWN", "notice": item, "action_required": "reconfirm_export_policy"})
            deals_held.append(d.deal_id)
    try:
        from shared import telegram
        await telegram.send_message(f"Regulatory notice for {', '.join(item['hs_codes'])}: {item['title'][:160]}\n"
                                    f"{item['url']}\n{len(deals_held)} open deal(s) held for re-confirmation")
    except Exception:  # noqa: BLE001
        pass
    return deals_held


async def run(*, transport: Any = None, now: datetime | None = None) -> dict[str, Any]:
    """Read every source once; store new items; act on hits. Never raises."""
    now = now or datetime.now(timezone.utc)
    from shared import catalogue
    lines = catalogue.active_lines()
    out: dict[str, Any] = {"read": [], "failed": [], "new": 0, "hits": []}
    import httpx
    async with httpx.AsyncClient(timeout=20.0, transport=transport, follow_redirects=True) as client:
        for name, url in sources():
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                page = resp.text
            except Exception as exc:  # noqa: BLE001
                out["failed"].append({"source": name, "error": str(exc)[:200]})
                continue
            out["read"].append(name)
            for link in links(page, url):
                item_id = hashlib.sha1(f"{name}|{link['url']}|{link['title']}".encode()).hexdigest()
                if await _known(item_id):
                    continue
                m = match(link["title"], lines)
                item = {"_id": item_id, "source": name, **link, **m, "seen_at": now.isoformat(),
                        "hit": bool(m["hs_codes"])}
                await _store(item)
                out["new"] += 1
                if item["hit"]:
                    item["deals_held"] = await _act_on_hit(item)
                    await _store(item)
                    out["hits"].append({k: item[k] for k in ("source", "title", "url", "hs_codes", "products")})
        out["denied_list"] = await _denied_list_newer(client)
    return out


async def _denied_list_newer(client: Any) -> dict[str, Any] | None:
    """A DGFT denied-entity list newer than the loaded one is published?"""
    url = os.getenv("REGULATORY_DENIED_LIST_URL", "").strip()
    if not url:
        return None
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}
    newest = max((d for d in (_iso_date(x) for x in _DATE.findall(resp.text)) if d), default=None)
    loaded = None
    try:
        from shared.orchestration import denied_list
        loaded = (await denied_list.status()).get("loaded_at")
    except Exception:  # noqa: BLE001
        loaded = None
    newer = bool(newest and (not loaded or newest > str(loaded)[:10]))
    if newer:
        try:
            from shared import telegram
            await telegram.send_message(f"A newer DGFT denied-entity list ({newest}) is published — load it "
                                        "(PUT /deals/compliance/dgft-denied)")
        except Exception:  # noqa: BLE001
            pass
    return {"published": newest, "loaded": loaded, "newer": newer}


_last_run: dict[str, str] = {}


async def daily(now: datetime | None = None, *, transport: Any = None) -> dict[str, Any] | None:
    """Deal watcher hook: at most once a UTC day."""
    if not enabled():
        return None
    now = now or datetime.now(timezone.utc)
    day = now.date().isoformat()
    if _last_run.get("day") == day:
        return None
    _last_run["day"] = day
    return await run(transport=transport, now=now)


async def recent_hits(now: datetime | None = None, days: int = 7) -> list[dict[str, Any]]:
    """Hits of the last ``days`` (digest)."""
    since = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()
    rows = [i for i in _seen.values() if i.get("hit") and i.get("seen_at", "") >= since]
    col = _collection()
    if col is not None:
        try:
            rows = [d async for d in col.find({"hit": True, "seen_at": {"$gte": since}})]
        except Exception:  # noqa: BLE001
            pass
    return sorted(rows, key=lambda i: i.get("seen_at", ""), reverse=True)


def reset_for_tests() -> None:
    _seen.clear()
    _last_run.clear()
