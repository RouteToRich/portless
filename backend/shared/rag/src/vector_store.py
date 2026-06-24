from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from rank_bm25 import BM25Okapi

from .config import settings
from .parent_store import load_parent_document


@dataclass
class ScoredDocument:
    document: Document
    score: float
    source: str


class VectorStore:
    def __init__(self, embeddings: Embeddings) -> None:
        """Initialize the Chroma vector database wrapper.

        Implementation:
            The configured persistence directory is created if needed, then a
            Chroma collection is opened with the embedding function supplied by
            `get_embeddings`.

        Usage:
            Graph nodes instantiate this wrapper during ingestion and retrieval.
            Keeping this setup in one place avoids repeating Chroma connection
            details throughout the codebase.

        How it helps other functions:
            `upsert`, `semantic_search`, and `keyword_search` all use the same
            collection and embedding function, keeping stored chunks and query
            vectors compatible.
        """
        settings.chroma_dir.mkdir(parents=True, exist_ok=True)
        self.store = Chroma(
            collection_name=settings.collection_name,
            embedding_function=embeddings,
            persist_directory=str(settings.chroma_dir),
        )

    def upsert(self, chunks: list[Document]) -> int:
        """Insert chunk documents into Chroma using stable chunk IDs.

        Implementation:
            If there are no chunks, the function returns `0`. Otherwise it first
            deletes existing chunks that share the same `content_hash`, then
            reads each chunk's `chunk_id` metadata and uses those values as
            Chroma IDs while adding documents.

        Usage:
            The ingestion graph calls this from `embed_and_insert` after chunking
            is complete.

        How it helps other functions:
            The inserted chunks become the searchable corpus for semantic search,
            keyword search, metadata-filtered retrieval, hybrid fusion,
            parent-child expansion, reranking, prompt augmentation, and final
            answer generation.
        """
        if not chunks:
            return 0
        self.delete_by_content_hashes(
            sorted(
                {
                    str(chunk.metadata["content_hash"])
                    for chunk in chunks
                    if chunk.metadata.get("content_hash")
                }
            )
        )
        ids = [str(chunk.metadata["chunk_id"]) for chunk in chunks]
        self.store.add_documents(chunks, ids=ids)
        return len(chunks)

    def delete_by_content_hashes(self, content_hashes: list[str]) -> int:
        """Delete existing chunks that belong to changed source content.

        Implementation:
            For each content hash, the function asks Chroma for matching record
            IDs and deletes those IDs before new chunks are inserted. This gives
            the practice project a simple re-indexing path without needing a
            separate ingestion database.

        Usage:
            `upsert` calls this automatically before inserting new chunks. It can
            also be called directly if you need to remove stale content.

        How it helps other functions:
            Re-indexing prevents duplicate stale chunks from appearing in
            retrieval, reranking, prompt augmentation, citation validation, and
            evaluation results.
        """
        deleted = 0
        for content_hash in content_hashes:
            payload = self.store.get(where={"content_hash": content_hash})
            ids = payload.get("ids", [])
            if ids:
                self.store.delete(ids=ids)
                deleted += len(ids)
        return deleted

    def semantic_search(
        self,
        query: str,
        top_k: int,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[ScoredDocument]:
        """Retrieve semantically similar chunks from Chroma.

        Implementation:
            Chroma embeds the query with the configured embedding function and
            compares it against stored chunk embeddings. If `metadata_filter` is
            supplied, Chroma restricts results to matching metadata before
            returning relevance scores with the documents.

        Usage:
            The query graph calls this in the `semantic_search` node. It is good
            for meaning-based matches where exact words may differ.

        How it helps other functions:
            Its filtered or unfiltered results are combined with BM25 keyword
            results in `hybrid_fusion` so downstream reranking receives both
            semantic and exact-match candidates.
        """
        results = self.store.similarity_search_with_relevance_scores(
            query,
            k=top_k,
            filter=metadata_filter,
        )
        return [
            ScoredDocument(document=document, score=float(score), source="semantic")
            for document, score in results
        ]

    def keyword_search(
        self,
        query: str,
        top_k: int,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[ScoredDocument]:
        """Retrieve exact-term matches using BM25 over stored chunk text.

        Implementation:
            The function reads documents and metadata from Chroma, tokenizes each
            chunk, applies an optional exact-match metadata filter, builds a BM25
            index in memory, scores the query, and returns the highest-scoring
            positive matches.

        Usage:
            The query graph calls this in the `keyword_search` node. It is useful
            for exact names, acronyms, CVEs, product names, API names, and other
            terms that semantic search can blur.

        How it helps other functions:
            These lexical candidates feed `hybrid_fusion`, improving recall
            before parent-child expansion, reranking, and knee-point selection
            choose final context.
        """
        payload = self.store.get(where=metadata_filter, include=["documents", "metadatas"])
        docs = payload.get("documents", [])
        metadatas = payload.get("metadatas", [])
        if not docs:
            return []

        tokenized_docs = [_tokenize(doc) for doc in docs]
        bm25 = BM25Okapi(tokenized_docs)
        scores = bm25.get_scores(_tokenize(query))
        ranked_indices = sorted(range(len(scores)), key=lambda idx: scores[idx], reverse=True)[:top_k]

        return [
            ScoredDocument(
                document=Document(page_content=docs[idx], metadata=metadatas[idx] or {}),
                score=float(scores[idx]),
                source="keyword",
            )
            for idx in ranked_indices
            if scores[idx] > 0
        ]


def expand_parent_context(candidates: list[ScoredDocument]) -> list[ScoredDocument]:
    """Replace child chunk text with parent context where available.

    Implementation:
        Each retrieved child chunk may carry `parent_id` metadata. This function
        deduplicates by parent ID, loads the broader parent text from the
        external parent store, and creates a new `Document` while recording
        `expanded_from_child_id`.

    Usage:
        The query graph calls this after hybrid fusion and before reranking.

    How it helps other functions:
        Retrieval can stay precise at child-chunk level, while reranking,
        knee-point selection, prompt augmentation, and generation receive enough
        surrounding context without storing large parent text in Chroma metadata.
    """
    expanded: list[ScoredDocument] = []
    seen_parent_ids: set[str] = set()
    for candidate in candidates:
        metadata = dict(candidate.document.metadata)
        parent_id = str(metadata.get("parent_id") or metadata.get("chunk_id"))
        if parent_id in seen_parent_ids:
            continue
        seen_parent_ids.add(parent_id)
        parent_document = load_parent_document(parent_id)
        parent_content = parent_document.page_content if parent_document else candidate.document.page_content
        if parent_document:
            metadata.update(parent_document.metadata)
        metadata["expanded_from_child_id"] = metadata.get("chunk_id")
        expanded.append(
            ScoredDocument(
                document=Document(page_content=parent_content, metadata=metadata),
                score=candidate.score,
                source="parent_child",
            )
        )
    return expanded


def hybrid_fusion(
    semantic_results: list[ScoredDocument],
    keyword_results: list[ScoredDocument],
    top_k: int,
    rrf_k: int = 60,
    graph_results: list[ScoredDocument] | None = None,
) -> list[ScoredDocument]:
    """Merge semantic, keyword, and graph results with reciprocal rank fusion.

    Implementation:
        Results from semantic, keyword, and optional graph retrievers are keyed
        by document identity and scored using `1 / (rrf_k + rank)`. A chunk
        appearing near the top of any list receives a strong fused score, and
        duplicates are collapsed.

    Usage:
        The query graph calls this after semantic, keyword, and graph search
        finish. `top_k` controls how many fused candidates move into reranking.

    How it helps other functions:
        Reranking works best when it receives a broad but relevant candidate
        pool. This function creates that pool by combining meaning-based,
        exact-match, and relationship-aware retrieval.
    """
    by_id: dict[str, ScoredDocument] = {}
    fused_scores: dict[str, float] = {}

    for result_set in [semantic_results, keyword_results, graph_results or []]:
        for rank, result in enumerate(result_set, start=1):
            doc_id = _doc_id(result.document)
            by_id[doc_id] = result
            fused_scores[doc_id] = fused_scores.get(doc_id, 0.0) + 1.0 / (rrf_k + rank)

    ranked = sorted(fused_scores.items(), key=lambda item: item[1], reverse=True)[:top_k]
    return [
        ScoredDocument(
            document=by_id[doc_id].document,
            score=score,
            source="hybrid",
        )
        for doc_id, score in ranked
    ]


def _tokenize(text: str) -> list[str]:
    """Normalize text into simple BM25 tokens.

    Implementation:
        A regular expression extracts alphanumeric and underscore terms, then
        lowercases them for case-insensitive matching.

    Usage:
        `keyword_search` uses this for both indexed chunks and the user query.

    How it helps other functions:
        Consistent tokenization makes BM25 scores stable, which improves the
        quality of candidates passed into hybrid fusion.
    """
    return re.findall(r"[a-zA-Z0-9_]+", text.lower())


def _doc_id(document: Document) -> str:
    """Choose a stable identity key for a retrieved document.

    Implementation:
        The function prefers `chunk_id`, falls back to `source`, and finally uses
        a hash of the page content if no metadata identifier exists.

    Usage:
        `hybrid_fusion` uses this helper to collapse duplicate results that were
        found by both semantic and keyword retrieval.

    How it helps other functions:
        Deduplication keeps reranking and prompt augmentation from wasting
        context budget on repeated chunks.
    """
    return str(document.metadata.get("chunk_id") or document.metadata.get("source") or hash(document.page_content))
