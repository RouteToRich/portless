"""Embeddings for the RAG module — routed through the LLM Gateway.

Every embedding call goes through ``shared.llm_gateway.GatewayEmbeddings`` so it
shares the gateway's provider + credential management, exactly like chat calls do.
RAG only supplies its provider / model / dimensions from ``RAGSettings``; the
gateway owns the actual provider SDK call and the offline ``LocalHashEmbeddings``
fallback.

Key resolution (handled inside the gateway):
  1. RAG_EMBEDDING_API_KEY (passed here as ``api_key``)
  2. the gateway's primary OpenAI key (LLM_PRIMARY_API_KEY when primary=openai)
  3. none → LocalHashEmbeddings
"""
from __future__ import annotations

from langchain_core.embeddings import Embeddings

# Re-exported so existing imports (`from shared.rag.src.embeddings import ...`)
# keep working; the implementations now live in the LLM Gateway.
from shared.llm_gateway.embeddings import GatewayEmbeddings, LocalHashEmbeddings

from .config import settings

__all__ = ["get_embeddings", "GatewayEmbeddings", "LocalHashEmbeddings"]


def get_embeddings() -> Embeddings:
    """Return the app's embedding client, configured from RAG settings.

    Delegates to the LLM Gateway's ``GatewayEmbeddings`` — RAG does not import any
    provider SDK directly.
    """
    return GatewayEmbeddings(
        provider=settings.embedding_provider,
        model=settings.embedding_model,
        api_key=settings.embedding_api_key,
        dimensions=settings.embedding_dimensions,
    )
