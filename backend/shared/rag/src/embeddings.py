from __future__ import annotations

from typing import Iterable

import numpy as np
from langchain_core.embeddings import Embeddings
from sklearn.feature_extraction.text import HashingVectorizer

from .config import settings


class LocalHashEmbeddings(Embeddings):
    """Deterministic local embeddings for practice without an API key."""

    def __init__(self, n_features: int = 768) -> None:
        """Create the local hashing vectorizer used as the embedding backend.

        Implementation:
            `HashingVectorizer` converts text into fixed-size numeric vectors
            without fitting a model or calling an external API. `alternate_sign`
            is disabled so values are non-negative, and L2 normalization makes
            cosine-style similarity more stable.

        Usage:
            This constructor is called by `get_embeddings` when OpenAI embeddings
            are disabled. It is useful for demos, tests, and offline practice.

        How it helps other functions:
            The resulting vectorizer powers `_embed`, which is used by
            `embed_documents`, `embed_query`, semantic chunking, and Chroma
            vector search.
        """
        self.vectorizer = HashingVectorizer(
            n_features=n_features,
            alternate_sign=False,
            norm="l2",
            stop_words="english",
        )

    def _embed(self, texts: Iterable[str]) -> list[list[float]]:
        """Convert one or more text values into numeric vectors.

        Implementation:
            The iterable is materialized into a list, transformed by the hashing
            vectorizer, converted to `float32`, and returned as plain Python
            lists so it matches LangChain's `Embeddings` interface.

        Usage:
            This private helper centralizes the conversion logic for both
            document and query embeddings.

        How it helps other functions:
            `embed_documents` and `embed_query` depend on this function to keep
            ingestion-time and query-time vector generation consistent.
        """
        matrix = self.vectorizer.transform(list(texts))
        return matrix.astype(np.float32).toarray().tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed document chunks for ingestion and semantic comparison.

        Implementation:
            Delegates to `_embed` so every document chunk is represented in the
            same local vector space.

        Usage:
            Chroma calls this when inserting chunks, and the semantic chunker
            calls it when comparing neighboring paragraphs.

        How it helps other functions:
            The vectors produced here are stored in the vector DB and later
            compared with vectors from `embed_query` during semantic retrieval.
        """
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        """Embed a user query into the same vector space as stored chunks.

        Implementation:
            Wraps the query in a single-item list, reuses `_embed`, and returns
            the first vector.

        Usage:
            The LangGraph `embed_query` node calls this before retrieval. Chroma
            also uses it internally for similarity search.

        How it helps other functions:
            Consistent query embeddings allow `VectorStore.semantic_search` to
            compare the user question against previously inserted chunk vectors.
        """
        return self._embed([text])[0]


def get_embeddings() -> Embeddings:
    """Return the configured embedding implementation.

    Implementation:
        If `USE_OPENAI_EMBEDDINGS=true`, the function lazily imports and returns
        `OpenAIEmbeddings`. Otherwise it returns `LocalHashEmbeddings`, keeping
        the project runnable without credentials.

    Usage:
        Graph nodes call this whenever they need embeddings for chunking,
        insertion, query embedding, or retrieval.

    How it helps other functions:
        This function is the single switch point between local practice mode and
        production-style model embeddings, so the rest of the pipeline does not
        need provider-specific logic.
    """
    if settings.use_openai_embeddings:
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(model=settings.openai_embedding_model)
    return LocalHashEmbeddings()
