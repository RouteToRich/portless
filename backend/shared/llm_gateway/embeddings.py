"""Embedding access for the LLM Gateway.

Centralises embedding-model calls the same way ``client.py`` centralises chat:
every embedding request in the app goes through ``GatewayEmbeddings``, which
resolves the provider + credentials from the gateway config and falls back to
deterministic local embeddings when no API key is available. No provider SDK is
instantiated outside this module (mirrors the chat connector factory).

Supported providers (``LLM_EMBEDDING_PROVIDER``):
  - ``openai`` — via ``langchain_openai.OpenAIEmbeddings``
  - ``gemini`` — via the ``google-genai`` SDK directly (``GeminiEmbeddings``
    below); uses task-type-aware embedding (RETRIEVAL_DOCUMENT vs
    RETRIEVAL_QUERY) and requests ``output_dimensionality`` explicitly so the
    returned vector length always matches ``LLM_EMBEDDING_DIMENSIONS`` (Gemini
    embedding models are MRL-trained, so truncating to a smaller-than-native
    size like 768/1536 is an officially supported request param, not a hack).
  - anything else → ``LocalHashEmbeddings`` (offline / zero-cost)

Key resolution per provider (first non-empty wins):
  1. ``LLM_EMBEDDING_API_KEY`` (this module's own dedicated embedding key)
  2. the gateway's primary key (``LLM_PRIMARY_API_KEY`` when the primary
     connector matches the embedding provider)
  3. none → ``LocalHashEmbeddings``

The backend is resolved lazily (on first embed call) so constructing a
``GatewayEmbeddings`` never touches numpy / sklearn / the network.
"""
from __future__ import annotations

import logging
from typing import Iterable

from langchain_core.embeddings import Embeddings

from .config import llm_gateway_settings

logger = logging.getLogger(__name__)

_DEFAULT_EMBEDDING_DIMENSIONS = 1536
_GEMINI_BATCH_SIZE = 100  # Google's embed_content accepts a bounded batch per call


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


class GeminiEmbeddings(Embeddings):
    """Google Gemini embeddings via the ``google-genai`` SDK (no LangChain
    integration package needed — ``google-genai`` is already a dependency).

    Explicitly asks for RETRIEVAL_DOCUMENT vs RETRIEVAL_QUERY task types.
    Gemini's embedding API is asymmetric by design: documents and queries are
    meant to be embedded differently for best retrieval quality, unlike
    OpenAI's symmetric embeddings where the same call shape works for both.
    """

    def __init__(self, model: str, api_key: str, dimensions: int) -> None:
        from google import genai  # lazy — avoids the network/SDK import cost

        # Force the Gemini Developer API (simple API-key auth), not Vertex AI.
        # Some machines have GOOGLE_GENAI_USE_VERTEXAI=true set ambiently (e.g.
        # from gcloud/gemini-cli tooling) which would otherwise silently route
        # this client through Vertex/ADC and ignore api_key entirely.
        self._client = genai.Client(api_key=api_key, vertexai=False)
        self._model = model
        self._dimensions = dimensions

    def _embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        from google.genai import types  # lazy

        vectors: list[list[float]] = []
        for i in range(0, len(texts), _GEMINI_BATCH_SIZE):
            batch = texts[i : i + _GEMINI_BATCH_SIZE]
            response = self._client.models.embed_content(
                model=self._model,
                contents=batch,
                config=types.EmbedContentConfig(
                    output_dimensionality=self._dimensions,
                    task_type=task_type,
                ),
            )
            vectors.extend(e.values for e in response.embeddings)
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, task_type="RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], task_type="RETRIEVAL_QUERY")[0]


def _resolve_openai_key(explicit_key: str) -> str:
    """Explicit key → the gateway's primary OpenAI key (if the primary is OpenAI)."""
    if explicit_key:
        return explicit_key
    gw = llm_gateway_settings
    if gw.primary_connector == "openai" and gw.primary_api_key:
        return gw.primary_api_key
    return ""


def _resolve_gemini_key(explicit_key: str) -> str:
    """Explicit key → the gateway's primary key (if the primary connector is gemini)."""
    if explicit_key:
        return explicit_key
    gw = llm_gateway_settings
    if gw.primary_connector == "gemini" and gw.primary_api_key:
        return gw.primary_api_key
    return ""


def _resolve_backend(provider: str, model: str, api_key: str, dimensions: int) -> Embeddings:
    """Pick OpenAI vs Gemini vs local, degrading to local on any problem."""
    if provider == "openai":
        key = _resolve_openai_key(api_key)
        if not key:
            logger.warning(
                "Gateway embeddings: no OpenAI key (LLM_EMBEDDING_API_KEY / LLM_PRIMARY_API_KEY) "
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

    if provider == "gemini":
        key = _resolve_gemini_key(api_key)
        if not key:
            logger.warning(
                "Gateway embeddings: no Gemini key (LLM_EMBEDDING_API_KEY / LLM_PRIMARY_API_KEY) "
                "— falling back to LocalHashEmbeddings. Set one to enable semantic embeddings."
            )
            return LocalHashEmbeddings(n_features=dimensions)

        try:
            logger.info(
                "Gateway embeddings: GeminiEmbeddings model=%s dimensions=%s (key via gateway)",
                model,
                dimensions,
            )
            return GeminiEmbeddings(model=model, api_key=key, dimensions=dimensions)
        except ImportError as exc:
            logger.error(
                "google-genai not installed — falling back to LocalHashEmbeddings. %s", exc
            )
            return LocalHashEmbeddings(n_features=dimensions)

    logger.info("Gateway embeddings: LocalHashEmbeddings (provider=%s)", provider)
    return LocalHashEmbeddings(n_features=dimensions)


class GatewayEmbeddings(Embeddings):
    """The single embedding entry point for the whole app.

    Reads its provider / model / key / dimensions entirely from the gateway's
    own settings (``LLM_EMBEDDING_*`` in ``shared/llm_gateway/.env``) — callers
    such as RAG declare no model config of their own, they just ask for
    vectors. The constructor args exist only so tests can override without
    touching env vars; production callers always use ``GatewayEmbeddings()``.
    Implements the LangChain ``Embeddings`` interface so it drops straight into
    the RAG pipeline.
    """

    def __init__(
        self,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        dimensions: int | None = None,
    ) -> None:
        gw = llm_gateway_settings
        self._provider = provider if provider is not None else gw.embedding_provider
        self._model = model if model is not None else gw.embedding_model
        self._api_key = api_key if api_key is not None else gw.embedding_api_key
        self._dimensions = dimensions if dimensions is not None else gw.embedding_dimensions
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
