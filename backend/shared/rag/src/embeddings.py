"""Embeddings for the RAG module — routed through the LLM Gateway.

Every embedding call goes through ``shared.llm_gateway.GatewayEmbeddings``, which
owns the model/provider/credential config *and* the actual provider SDK call /
offline ``LocalHashEmbeddings`` fallback. RAG declares no embedding-model config
of its own — see ``LLM_EMBEDDING_*`` in ``shared/llm_gateway/.env`` /
``shared/llm_gateway/config.py`` for the single source of truth.
"""
from __future__ import annotations

from langchain_core.embeddings import Embeddings

# Re-exported so existing imports (`from shared.rag.src.embeddings import ...`)
# keep working; the implementations now live in the LLM Gateway.
from shared.llm_gateway.embeddings import GatewayEmbeddings, LocalHashEmbeddings

__all__ = ["get_embeddings", "GatewayEmbeddings", "LocalHashEmbeddings"]


def get_embeddings() -> Embeddings:
    """Return the app's embedding client.

    Delegates entirely to the LLM Gateway's ``GatewayEmbeddings`` — RAG passes no
    model config and does not import any provider SDK directly.
    """
    return GatewayEmbeddings()
