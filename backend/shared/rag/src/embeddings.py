"""Embeddings for the RAG module — routed through the LLM Gateway credentials.

Priority for API key resolution (first non-empty wins):
  1. RAG_EMBEDDING_API_KEY env var
  2. LLM_PRIMARY_API_KEY env var (gateway primary key)
  3. LocalHashEmbeddings fallback (no external API, deterministic)

This keeps embedding calls aligned with the gateway's credential management
without requiring a separate secrets store.
"""
from __future__ import annotations

import logging
from typing import Iterable

import numpy as np
from langchain_core.embeddings import Embeddings

from .config import settings

logger = logging.getLogger(__name__)


class GatewayEmbeddings(Embeddings):
    """Embeddings backed by the LLM Gateway's primary OpenAI API key.

    Reads credentials from RAGSettings / LLMGatewaySettings so no embedding
    key needs to be managed separately from the gateway config.

    Falls back to LocalHashEmbeddings when no OpenAI key is available so the
    pipeline stays runnable in offline / zero-cost environments.
    """

    def __init__(self) -> None:
        self._impl = _resolve_embedding_backend()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._impl.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._impl.embed_query(text)


class LocalHashEmbeddings(Embeddings):
    """Deterministic local embeddings — no API key, no external calls.

    Uses HashingVectorizer with L2 normalisation.  Vectors are consistent
    within a process but not semantically meaningful across restarts.
    Use for local dev / integration tests only.
    """

    def __init__(self, n_features: int = 1536) -> None:
        from sklearn.feature_extraction.text import HashingVectorizer

        self._vectorizer = HashingVectorizer(
            n_features=n_features,
            alternate_sign=False,
            norm="l2",
            stop_words="english",
        )

    def _embed(self, texts: Iterable[str]) -> list[list[float]]:
        matrix = self._vectorizer.transform(list(texts))
        return matrix.astype(np.float32).toarray().tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]


# ────────────────────────────────────────────────────────── backend resolver

def _resolve_embedding_backend() -> Embeddings:
    """Return the best available embedding implementation.

    Decision tree:
      1. RAG_EMBEDDING_PROVIDER=openai → try OpenAIEmbeddings with resolved key
      2. Key missing or provider=local  → LocalHashEmbeddings
    """
    if settings.embedding_provider != "openai":
        logger.info("RAG embeddings: using LocalHashEmbeddings (provider=%s)", settings.embedding_provider)
        return LocalHashEmbeddings(n_features=settings.embedding_dimensions)

    api_key = _resolve_openai_key()
    if not api_key:
        logger.warning(
            "RAG embeddings: no OpenAI key found in RAG_EMBEDDING_API_KEY or "
            "LLM_PRIMARY_API_KEY — falling back to LocalHashEmbeddings. "
            "Set one of those env vars to enable semantic embeddings."
        )
        return LocalHashEmbeddings(n_features=settings.embedding_dimensions)

    try:
        from langchain_openai import OpenAIEmbeddings

        backend = OpenAIEmbeddings(model=settings.embedding_model, api_key=api_key)
        logger.info(
            "RAG embeddings: OpenAIEmbeddings model=%s (key via gateway)",
            settings.embedding_model,
        )
        return backend
    except ImportError as exc:
        logger.error(
            "langchain-openai not installed — falling back to LocalHashEmbeddings. %s", exc
        )
        return LocalHashEmbeddings(n_features=settings.embedding_dimensions)


def _resolve_openai_key() -> str:
    """Resolve the OpenAI API key: RAG config first, then LLM gateway config."""
    if settings.embedding_api_key:
        return settings.embedding_api_key

    try:
        from shared.llm_gateway.config import llm_gateway_settings as gw

        if gw.primary_connector == "openai" and gw.primary_api_key:
            return gw.primary_api_key
    except ImportError:
        pass

    return ""


def get_embeddings() -> Embeddings:
    """Return the configured embedding implementation (singleton-safe)."""
    return GatewayEmbeddings()
