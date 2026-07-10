"""Embedding access for the LLM Gateway.

Centralises embedding-model calls the same way ``client.py`` centralises chat:
every embedding request in the app goes through ``GatewayEmbeddings``, which
resolves the provider + credentials from the gateway config and falls back to
deterministic local embeddings when no API key is available. No provider SDK is
instantiated outside this module (mirrors the chat connector factory).

Key resolution (first non-empty wins):
  1. the explicit ``api_key`` argument (e.g. ``RAG_EMBEDDING_API_KEY`` passed by RAG)
  2. the gateway's primary OpenAI key (``LLM_PRIMARY_API_KEY`` when the primary
     connector is ``openai``)
  3. none → ``LocalHashEmbeddings`` (offline / zero-cost)

The backend is resolved lazily (on first embed call) so constructing a
``GatewayEmbeddings`` never touches numpy / sklearn / the network.
"""
from __future__ import annotations

import logging
from typing import Iterable

from langchain_core.embeddings import Embeddings

from .config import llm_gateway_settings

logger = logging.getLogger(__name__)

_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
_DEFAULT_EMBEDDING_DIMENSIONS = 1536


class LocalHashEmbeddings(Embeddings):
    """Deterministic local embeddings — no API key, no external calls.

    HashingVectorizer with L2 normalisation. Consistent within a process but not
    semantically meaningful across restarts. For dev / CI / offline use only.
    """

    def __init__(self, n_features: int = _DEFAULT_EMBEDDING_DIMENSIONS) -> None:
        from sklearn.feature_extraction.text import HashingVectorizer  # lazy

        self._vectorizer = HashingVectorizer(
            n_features=n_features,
            alternate_sign=False,
            norm="l2",
            stop_words="english",
        )

    def _embed(self, texts: Iterable[str]) -> list[list[float]]:
        import numpy as np  # lazy

        matrix = self._vectorizer.transform(list(texts))
        return matrix.astype(np.float32).toarray().tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]


def _resolve_openai_key(explicit_key: str) -> str:
    """Explicit key → the gateway's primary OpenAI key (if the primary is OpenAI)."""
    if explicit_key:
        return explicit_key
    gw = llm_gateway_settings
    if gw.primary_connector == "openai" and gw.primary_api_key:
        return gw.primary_api_key
    return ""


def _resolve_backend(provider: str, model: str, api_key: str, dimensions: int) -> Embeddings:
    """Pick OpenAI vs local, degrading to local on any problem."""
    if provider != "openai":
        logger.info("Gateway embeddings: LocalHashEmbeddings (provider=%s)", provider)
        return LocalHashEmbeddings(n_features=dimensions)

    key = _resolve_openai_key(api_key)
    if not key:
        logger.warning(
            "Gateway embeddings: no OpenAI key (RAG_EMBEDDING_API_KEY / LLM_PRIMARY_API_KEY) "
            "— falling back to LocalHashEmbeddings. Set one to enable semantic embeddings."
        )
        return LocalHashEmbeddings(n_features=dimensions)

    try:
        from langchain_openai import OpenAIEmbeddings  # lazy

        logger.info("Gateway embeddings: OpenAIEmbeddings model=%s (key via gateway)", model)
        return OpenAIEmbeddings(model=model, api_key=key)
    except ImportError as exc:
        logger.error(
            "langchain-openai not installed — falling back to LocalHashEmbeddings. %s", exc
        )
        return LocalHashEmbeddings(n_features=dimensions)


class GatewayEmbeddings(Embeddings):
    """The single embedding entry point for the whole app.

    Callers (e.g. RAG) pass their provider / model / key / dimensions; the gateway
    owns the actual provider call and the offline fallback. Implements the
    LangChain ``Embeddings`` interface so it drops straight into the RAG pipeline.
    """

    def __init__(
        self,
        provider: str = "openai",
        model: str = _DEFAULT_EMBEDDING_MODEL,
        api_key: str = "",
        dimensions: int = _DEFAULT_EMBEDDING_DIMENSIONS,
    ) -> None:
        self._provider = provider
        self._model = model
        self._api_key = api_key
        self._dimensions = dimensions
        self._impl_cache: Embeddings | None = None

    @property
    def _impl(self) -> Embeddings:
        if self._impl_cache is None:
            self._impl_cache = _resolve_backend(
                self._provider, self._model, self._api_key, self._dimensions
            )
        return self._impl_cache

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._impl.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._impl.embed_query(text)
