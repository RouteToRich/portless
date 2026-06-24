from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, TypedDict

from langchain_core.documents import Document

from .config import settings
from .vector_store import ScoredDocument


class GraphPayload(TypedDict):
    chunks: dict[str, dict[str, Any]]
    entity_to_chunks: dict[str, list[str]]
    metadata_to_chunks: dict[str, list[str]]


class GraphStore:
    def __init__(self, store_path: Path | None = None) -> None:
        """Initialize the local graph database wrapper.

        Implementation:
            The graph is stored as a JSON file under `settings.graph_store_dir`.
            It keeps three adjacency structures: chunk nodes, entity-to-chunk
            edges, and metadata-to-chunk edges. The directory is created lazily
            so ingestion can write the graph without any external service.

        Usage:
            The ingestion LangGraph node creates `GraphStore()` and calls
            `upsert_chunks()`. The query LangGraph node creates `GraphStore()`
            and calls `retrieve()`.

        How it helps other functions:
            This class gives `graph.py` a relationship-aware retriever that can
            be fused with semantic and keyword candidates inside
            `hybrid_fusion()`.
        """
        settings.graph_store_dir.mkdir(parents=True, exist_ok=True)
        self.store_path = store_path or settings.graph_store_dir / "graph.json"

    def upsert_chunks(self, chunks: list[Document]) -> int:
        """Insert or replace chunk nodes and graph relationships.

        Implementation:
            The function reads the existing JSON graph, removes any old chunk
            nodes that share the incoming chunks' `content_hash`, then writes
            fresh chunk nodes and adjacency edges. Each chunk is connected to
            extracted entity nodes and selected metadata nodes such as source,
            file name, section, page number, and parent ID.

        Usage:
            `graph_insert()` in `graph.py` calls this after Chroma insertion so
            the graph store stays aligned with the vector store.

        How it helps other functions:
            Query-time graph retrieval depends on these relationships to find
            chunks related by shared entities or metadata even when vector
            similarity and BM25 miss them.
        """
        if not chunks:
            return 0

        graph = self._read()
        stale_hashes = {
            str(chunk.metadata.get("content_hash"))
            for chunk in chunks
            if chunk.metadata.get("content_hash")
        }
        for chunk_id, node in list(graph["chunks"].items()):
            if node.get("metadata", {}).get("content_hash") in stale_hashes:
                self._remove_chunk(graph, chunk_id)

        for chunk in chunks:
            chunk_id = str(chunk.metadata.get("chunk_id") or hash(chunk.page_content))
            metadata = dict(chunk.metadata)
            entities = extract_entities(chunk.page_content, metadata)
            metadata_keys = metadata_relationship_keys(metadata)
            graph["chunks"][chunk_id] = {
                "content": chunk.page_content,
                "metadata": metadata,
                "entities": sorted(entities),
                "metadata_keys": sorted(metadata_keys),
            }
            for entity in entities:
                _append_unique(graph["entity_to_chunks"], entity, chunk_id)
            for metadata_key in metadata_keys:
                _append_unique(graph["metadata_to_chunks"], metadata_key, chunk_id)

        self._write(graph)
        return len(chunks)

    def retrieve(
        self,
        query: str,
        top_k: int,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[ScoredDocument]:
        """Retrieve chunks through graph entity and metadata relationships.

        Implementation:
            The function extracts query entities, follows entity-to-chunk edges,
            scores candidate chunks by entity overlap with inverse-frequency
            weighting, applies optional metadata filters, and returns
            `ScoredDocument` objects with `source="graph"`.

        Usage:
            `graph_search()` in `graph.py` calls this after keyword search and
            before hybrid fusion.

        How it helps other functions:
            The returned candidates become a third retrieval signal inside
            `hybrid_fusion()`, improving recall for shared entities, source
            relationships, sections, pages, and technical identifiers.
        """
        graph = self._read()
        query_entities = extract_entities(query, {})
        if not query_entities:
            return []

        chunk_scores: Counter[str] = Counter()
        total_chunks = max(len(graph["chunks"]), 1)
        for entity in query_entities:
            related_chunk_ids = graph["entity_to_chunks"].get(entity, [])
            if not related_chunk_ids:
                continue
            idf = math.log((1 + total_chunks) / (1 + len(related_chunk_ids))) + 1.0
            for chunk_id in related_chunk_ids:
                chunk_scores[chunk_id] += idf

        ranked: list[ScoredDocument] = []
        for chunk_id, score in chunk_scores.most_common():
            node = graph["chunks"].get(chunk_id)
            if not node:
                continue
            metadata = dict(node.get("metadata", {}))
            if metadata_filter and not _metadata_matches(metadata, metadata_filter):
                continue
            ranked.append(
                ScoredDocument(
                    document=Document(page_content=str(node.get("content", "")), metadata=metadata),
                    score=float(score),
                    source="graph",
                )
            )
            if len(ranked) >= top_k:
                break
        return ranked

    def _read(self) -> GraphPayload:
        """Read the graph JSON file, returning an empty graph if it is missing.

        Implementation:
            The graph file is intentionally simple JSON so learners can inspect
            it directly. Missing or empty stores return the standard payload
            shape used by every method in this class.

        Usage:
            `upsert_chunks()` and `retrieve()` call this before modifying or
            traversing graph data.

        How it helps other functions:
            A stable payload shape keeps graph indexing and graph retrieval
            focused on their logic instead of repeatedly handling file setup.
        """
        if not self.store_path.exists():
            return {"chunks": {}, "entity_to_chunks": {}, "metadata_to_chunks": {}}
        with self.store_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        return {
            "chunks": dict(payload.get("chunks", {})),
            "entity_to_chunks": {
                key: list(value)
                for key, value in dict(payload.get("entity_to_chunks", {})).items()
            },
            "metadata_to_chunks": {
                key: list(value)
                for key, value in dict(payload.get("metadata_to_chunks", {})).items()
            },
        }

    def _write(self, graph: GraphPayload) -> None:
        """Persist the graph payload to disk.

        Implementation:
            The function writes formatted UTF-8 JSON with deterministic key
            ordering, which makes the store easier to inspect while learning and
            simpler to diff during development.

        Usage:
            `upsert_chunks()` calls this after chunk nodes and edges are updated.

        How it helps other functions:
            Persisting after ingestion makes graph retrieval available to later
            CLI `ask` runs, even when they happen in a separate process.
        """
        with self.store_path.open("w", encoding="utf-8") as file:
            json.dump(graph, file, indent=2, sort_keys=True)

    def _remove_chunk(self, graph: GraphPayload, chunk_id: str) -> None:
        """Remove one chunk node and all adjacency references to it.

        Implementation:
            The function deletes the chunk node and filters the chunk ID out of
            every entity and metadata adjacency list. Empty adjacency nodes are
            removed so stale relationships do not affect future retrieval.

        Usage:
            `upsert_chunks()` calls this when re-indexing changed source content.

        How it helps other functions:
            Removing stale graph edges keeps graph search aligned with Chroma's
            content-hash re-indexing behavior.
        """
        graph["chunks"].pop(chunk_id, None)
        for adjacency_name in ("entity_to_chunks", "metadata_to_chunks"):
            adjacency = graph[adjacency_name]
            for key, chunk_ids in list(adjacency.items()):
                filtered = [existing_id for existing_id in chunk_ids if existing_id != chunk_id]
                if filtered:
                    adjacency[key] = filtered
                else:
                    adjacency.pop(key, None)


def extract_entities(text: str, metadata: dict[str, Any]) -> set[str]:
    """Extract lightweight graph entities from chunk text and metadata.

    Implementation:
        The extractor tokenizes alphanumeric, underscore, slash, dot, colon, and
        hyphen terms, lowercases them, removes short stop words, and adds useful
        metadata values such as title, section, source, file name, and file type.
        It intentionally stays deterministic and dependency-free for the
        practice project.

    Usage:
        `GraphStore.upsert_chunks()` uses this to create entity nodes for each
        chunk. `GraphStore.retrieve()` uses the same logic on the query.

    How it helps other functions:
        Shared extraction logic keeps ingestion-time graph nodes and query-time
        graph traversal in the same vocabulary.
    """
    entities: set[str] = set()
    for value in [text, *(_metadata_text_values(metadata))]:
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_./:-]{1,}", str(value).lower()):
            normalized = token.strip("._/:")
            if len(normalized) >= 3 and normalized not in STOP_WORDS:
                entities.add(normalized)
    return entities


def metadata_relationship_keys(metadata: dict[str, Any]) -> set[str]:
    """Create graph relationship keys from high-value metadata fields.

    Implementation:
        The function converts selected metadata fields into namespaced keys such
        as `section:Architecture` or `page_number:3`. List metadata creates one
        key per value.

    Usage:
        `GraphStore.upsert_chunks()` stores these keys in `metadata_to_chunks`.

    How it helps other functions:
        Metadata relationships let graph retrieval model source, section, page,
        parent, and authorization links separately from free-text entities.
    """
    relationship_fields = {
        "source",
        "file_name",
        "file_type",
        "title",
        "section",
        "page_number",
        "parent_id",
        "authorized_users",
        "authorized_teams",
    }
    keys: set[str] = set()
    for field in relationship_fields:
        value = metadata.get(field)
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            keys.add(f"{field}:{item}")
    return keys


def _append_unique(adjacency: dict[str, list[str]], key: str, chunk_id: str) -> None:
    """Append a chunk ID to an adjacency list without duplicates.

    Implementation:
        The function initializes the list when the key is new, then appends only
        when the chunk ID is not already present.

    Usage:
        `GraphStore.upsert_chunks()` calls this for every entity and metadata
        relationship produced for a chunk.

    How it helps other functions:
        Duplicate-free adjacency lists make graph scoring stable and avoid
        inflating relationship strength during re-ingestion.
    """
    adjacency.setdefault(key, [])
    if chunk_id not in adjacency[key]:
        adjacency[key].append(chunk_id)


def _metadata_text_values(metadata: dict[str, Any]) -> list[str]:
    """Collect metadata values that should also behave like graph entities.

    Implementation:
        The function reads descriptive fields such as title, section, source,
        file name, and file type, flattening list values into strings.

    Usage:
        `extract_entities()` calls this while building the entity set for chunks.

    How it helps other functions:
        Including descriptive metadata in entity extraction helps graph search
        connect queries to chunks through document structure, not only body text.
    """
    fields = ["title", "section", "source", "file_name", "file_type"]
    values: list[str] = []
    for field in fields:
        value = metadata.get(field)
        if value is None:
            continue
        if isinstance(value, list):
            values.extend(str(item) for item in value)
        else:
            values.append(str(value))
    return values


def _metadata_matches(metadata: dict[str, Any], metadata_filter: dict[str, Any]) -> bool:
    """Return whether metadata satisfies exact-match graph retrieval filters.

    Implementation:
        Scalar metadata must equal the expected value. List metadata matches when
        the expected value is present in the list.

    Usage:
        `GraphStore.retrieve()` applies this after graph traversal and before
        returning candidates.

    How it helps other functions:
        It keeps graph search consistent with semantic and keyword retrieval when
        the CLI passes filters such as `section=Release` or `page_number=3`.
    """
    for key, expected in metadata_filter.items():
        actual = metadata.get(key)
        if isinstance(actual, list):
            if expected not in actual:
                return False
        elif actual != expected:
            return False
    return True


STOP_WORDS = {
    "about",
    "after",
    "also",
    "and",
    "are",
    "but",
    "can",
    "for",
    "from",
    "has",
    "how",
    "into",
    "the",
    "this",
    "that",
    "then",
    "use",
    "what",
    "when",
    "where",
    "with",
    "your",
}
