"""Live web-search market context for negotiation email drafting.

Unlike rag.py's load_rag_context() — fetched once per session and cached on
session.rag_context because a trade playbook doesn't change round to round —
this is fetched fresh on every round, since "current market pricing" is only
useful if it's current at draft time.

Reuses shared/rag/src's Tavily adapter (WebSearchStore) and the same
prompt-injection filter (afilter_prompt_injection_chunks) the main RAG query
graph runs on every web-search chunk before it reaches a generation prompt —
raw open-web content flowing into a prompt that autonomously drafts business
email is a prompt-injection surface, and this repo already has the standard
answer for it. Every drafted email is also independently routed through a
mandatory L0 HITL approval gate before send (service.py's
_create_and_gate_email) regardless of what informed the draft.

Fails open to "" on: market_search_enabled=False, missing TAVILY_API_KEY, or
any HTTP/timeout/classifier error — never raises, never fabricates a result.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Final

from .config import settings
from .schemas import Party

if TYPE_CHECKING:
    from langchain_core.documents import Document

logger = logging.getLogger(__name__)

_SUPPLIER_ORIGIN_COUNTRY: Final[str] = "India"
_TOP_K: Final[int] = 3
_TIMEOUT_SECONDS: Final[float] = 20.0
_SNIPPET_CHARS: Final[int] = 300
_MAX_CONTEXT_CHARS: Final[int] = 1200

# USD/INR has stayed within roughly this band for years — used only as a
# sanity bound on parsed web-search figures, not a precision constraint.
_USD_INR_PLAUSIBLE_RANGE: Final[tuple[float, float]] = (60.0, 130.0)

# Financial pages mix today's spot rate with historical averages and future
# forecasts in the same paragraph ("forecasted to hit 98.48 by the end of
# 2026", "average exchange rate in January 2026: 90.80") — a bare "grab any
# plausible-looking number" regex latches onto whichever appears first,
# which is often not today's rate. These patterns require an explicit
# "today"/"current"/"1 USD =" anchor immediately next to the number, tried
# in priority order; anything that doesn't match one of these is rejected
# rather than guessed at.
_USD_INR_RATE_PATTERNS: Final[list[re.Pattern[str]]] = [
    # Table/list rows: "Today | 95.5429 | 95.56" or "Today: ₹95.56"
    re.compile(r"\btoday\b[^\d]{0,20}(\d{2,3}(?:\.\d{1,4})?)", re.IGNORECASE),
    # "1 USD = ₹95.56" / "1 dollar equals 95.56 INR" / "current rate: 95.56"
    re.compile(
        r"(?:\b1\s*(?:us\s*)?(?:dollar|usd)\b[^\d]{0,15}(?:=|is|equals)|"
        r"\bcurrent\s+(?:usd\W?inr\s+)?(?:exchange\s+)?rate\b)[^\d]{0,15}"
        r"(?:₹|rs\.?|inr)?\s*(\d{2,3}(?:\.\d{1,4})?)",
        re.IGNORECASE,
    ),
]
_FORECAST_WORD_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(forecast|predicted?|projection|by\s+(?:the\s+end\s+of\s+)?20\d\d|"
    r"next\s+(?:week|month|year)|average|worst|best|history|historical)\b",
    re.IGNORECASE,
)


def _build_query(product_code: str, country_code: str, role: Party) -> str:
    year = datetime.now(timezone.utc).year
    if role == "buyer":
        return (
            f"Current {year} import market pricing and demand conditions for product "
            f"HS code {product_code} in {country_code}. Typical CIF import prices, "
            f"recent price trends, and buyer demand outlook."
        )
    return (
        f"Current {year} India export pricing and Ex-Works rates for product "
        f"HS code {product_code}. Typical FOB/Ex-Works prices, recent price trends, "
        f"and export supply conditions."
    )


def _format_results(chunks: list[Document]) -> str:
    if not chunks:
        return ""
    lines: list[str] = []
    for doc in chunks:
        title = doc.metadata.get("title") or "untitled"
        source = doc.metadata.get("source") or ""
        snippet = " ".join(doc.page_content.split())
        if len(snippet) > _SNIPPET_CHARS:
            snippet = snippet[:_SNIPPET_CHARS] + "…"
        lines.append(f"- {title} ({source}): {snippet}")
    text = "\n".join(lines)
    if len(text) > _MAX_CONTEXT_CHARS:
        text = text[:_MAX_CONTEXT_CHARS] + "…"
    return text


async def fetch_market_intel(product_code: str, country_code: str, role: Party) -> str:
    """Fresh, filtered web-search market color for one side of a negotiation round."""
    if not settings.market_search_enabled:
        return ""
    try:
        from shared.rag.src.security import afilter_prompt_injection_chunks
        from shared.rag.src.web_search import WebSearchStore

        async def _run() -> str:
            query = _build_query(product_code, country_code, role)
            scored = await WebSearchStore().retrieve(query, top_k=_TOP_K)
            safe_docs = await afilter_prompt_injection_chunks([sd.document for sd in scored])
            return _format_results(safe_docs)

        return await asyncio.wait_for(_run(), timeout=_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("Market intel fetch failed for role=%s product=%s: %s", role, product_code, exc)
        return ""


async def fetch_both(product_code: str, buyer_country_code: str) -> dict[str, str]:
    """Party-scoped market context, fetched concurrently.

    Mirrors service.py's _build_conversation_history() return shape
    ({"buyer": ..., "supplier": ...}). The supplier leg always searches
    India — no seller_country_code field exists on NegotiationSession,
    matching the India-as-sole-export-origin convention already used
    throughout shared/connectors/trade_intel.py.
    """
    results = await asyncio.gather(
        fetch_market_intel(product_code, buyer_country_code, "buyer"),
        fetch_market_intel(product_code, _SUPPLIER_ORIGIN_COUNTRY, "supplier"),
        return_exceptions=True,
    )
    buyer, supplier = (r if isinstance(r, str) else "" for r in results)
    return {"buyer": buyer, "supplier": supplier}


# ---------------------------------------------------------------------------
# USD/INR exchange rate — the supplier agent must never invent this itself
# ---------------------------------------------------------------------------
# The supplier-facing prompt communicates in INR, but a bare "convert this to
# INR" instruction with no rate supplied sends the model into a token-burning
# reasoning spiral trying to guess one (observed: every supplier_agent LLM
# call exhausted its full max_tokens budget on this alone and never reached
# valid JSON). Fetching and computing the conversion here — deterministically,
# in code — removes the ambiguity entirely.

_FORECAST_CONTEXT_CHARS: Final[int] = 40


def _parse_usd_inr_rate(chunks: list["Document"]) -> float | None:
    """Extraction of today's USD/INR rate from search snippets — deliberately
    strict, since financial pages mix today's spot rate with historical
    averages and multi-year forecasts in the same paragraph. Only accepts a
    number matched by one of the explicit "today"/"current rate"/"1 USD ="
    anchors in _USD_INR_RATE_PATTERNS, within the plausible band, and with no
    forecast/historical language in the immediately surrounding text (a
    "Today ... rise to 95.77 in the next 7 days" sentence still reads as
    "today" to the anchor pattern but is actually a forward-looking forecast).
    Returns None — never a guess — when no snippet passes all three checks."""
    for doc in chunks:
        text = doc.page_content
        for pattern in _USD_INR_RATE_PATTERNS:
            for match in pattern.finditer(text):
                try:
                    value = float(match.group(1))
                except (ValueError, IndexError):
                    continue
                if not (_USD_INR_PLAUSIBLE_RANGE[0] <= value <= _USD_INR_PLAUSIBLE_RANGE[1]):
                    continue
                start = max(0, match.start() - _FORECAST_CONTEXT_CHARS)
                end = min(len(text), match.end() + _FORECAST_CONTEXT_CHARS)
                if _FORECAST_WORD_PATTERN.search(text[start:end]):
                    continue
                return value
    return None


async def fetch_usd_inr_rate() -> float | None:
    """Live USD/INR rate via the same Tavily web-search adapter used for
    market context. Returns None on any failure — never raises, never
    fabricates a number outside the plausible band. Callers should fall back
    to config.settings.fallback_usd_inr_rate rather than treat None as 0."""
    if not settings.market_search_enabled:
        return None
    try:
        from shared.rag.src.security import afilter_prompt_injection_chunks
        from shared.rag.src.web_search import WebSearchStore

        async def _run() -> float | None:
            year = datetime.now(timezone.utc).year
            scored = await WebSearchStore().retrieve(
                f"USD to INR exchange rate today {year}", top_k=_TOP_K
            )
            safe_docs = await afilter_prompt_injection_chunks([sd.document for sd in scored])
            return _parse_usd_inr_rate(safe_docs)

        return await asyncio.wait_for(_run(), timeout=_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("USD/INR rate fetch failed: %s", exc)
        return None


async def get_usd_inr_rate() -> float:
    """Always returns a usable rate — live search result if available and
    parseable, else the periodically-updated static fallback."""
    rate = await fetch_usd_inr_rate()
    return rate if rate is not None else settings.fallback_usd_inr_rate
