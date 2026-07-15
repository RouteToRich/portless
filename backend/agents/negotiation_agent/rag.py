"""Trade playbook retrieval for the Negotiation Agent — uses shared.rag.

The shared RAG module (MongoDB Atlas vector store + LLM Gateway generation)
indexes ingested trade playbooks and policy documents. This module queries it
with an HS-code–focused question and returns the generated context string.

Fallback behaviour
------------------
If the RAG pipeline raises (Atlas not configured, no indexed documents, etc.)
the module falls back to the static playbook stubs below so the negotiation
agent degrades gracefully in dev / early-stage deployments.
"""
from __future__ import annotations

import asyncio
import logging

from .config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Static fallback stubs (used when RAG returns empty or raises)
# ---------------------------------------------------------------------------

_FALLBACK_PLAYBOOKS: dict[str, str] = {
    "1006": (
        "Rice (HS 1006): Global benchmark is Thailand 5% broken (FOB Bangkok). "
        "Standard payment: LC at sight. Lead time: 21–30 days. "
        "Quality: moisture ≤ 14%, broken ≤ 5%. "
        "Concession rhythm: supplier opens +3–5%, buyer counters −8%. "
        "Convergence in ~3 rounds. Incoterm: FOB or CIF."
    ),
    "1001": (
        "Wheat (HS 1001): CME CBOT front-month is the reference. "
        "Protein premium: +$8–12/MT for hard red winter. "
        "Payment: open account 90 days (established) or LC (new buyers). "
        "Lead time: 14–21 days Black Sea; 30–35 days Australia. MOQ: 500 MT."
    ),
    "6101": (
        "Apparel (HS 61): Factory-gate pricing dominates. "
        "Buyer MOQ: 500–1000 pcs/SKU. Lead time: 45–60 days. "
        "Payment: 30% TT advance + 70% on BL. "
        "OEKO-TEX 100 required by EU buyers. Concession: 2–3%/round."
    ),
    "DEFAULT": (
        "Generic trade context: anchor buyer −10% of market, supplier +12–15%. "
        "Standard convergence: 3–5 rounds for commodities. "
        "Payment: LC at sight first transaction; open account after 3 shipments. "
        "Incoterm default: FOB origin / CIF destination."
    ),
}


def _fallback(hs_code: str) -> str:
    for prefix_len in (4, 2):
        hit = _FALLBACK_PLAYBOOKS.get(hs_code[:prefix_len])
        if hit:
            return hit
    return _FALLBACK_PLAYBOOKS["DEFAULT"]


# ---------------------------------------------------------------------------
# RAG retrieval
# ---------------------------------------------------------------------------

async def load_rag_context(hs_code: str) -> str:
    """Query the shared RAG pipeline for trade playbook context.

    Uses an HS-code–scoped metadata filter so only indexed documents
    tagged with the matching product category are retrieved.

    Falls back to static stubs if the pipeline is unavailable.
    """
    if not settings.rag_enabled:
        return _fallback(hs_code)

    question = (
        f"Negotiation tactics, price benchmarks, payment terms, and trade rules "
        f"for product HS code {hs_code}. Include typical buyer/supplier concession "
        f"patterns, quality standards, and incoterm preferences."
    )
    try:
        from shared.rag.src import query as rag_query  # type: ignore[import]

        result = await asyncio.wait_for(
            rag_query(question=question, metadata_filter={"hs_code": hs_code}),
            timeout=settings.rag_timeout_seconds,
        )
        answer: str = result.get("answer", "")

        # If no indexed content exists, also try to join raw chunk text
        if not answer:
            chunks = result.get("selected_chunks", [])
            if chunks:
                answer = "\n\n".join(
                    getattr(c, "page_content", str(c)) for c in chunks[:5]
                )

        if answer:
            logger.debug("RAG context retrieved for hs_code=%s (%d chars)", hs_code, len(answer))
            return answer

        logger.debug("RAG returned empty for hs_code=%s — using static fallback", hs_code)
        return _fallback(hs_code)

    except Exception as exc:
        logger.warning(
            "RAG query failed for hs_code=%s (%s: %s) — using static fallback",
            hs_code,
            type(exc).__name__,
            exc,
        )
        return _fallback(hs_code)
