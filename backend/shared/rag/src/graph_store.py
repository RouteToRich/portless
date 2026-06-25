"""Graph store placeholder — reserved for graph DB / hybrid RAG approach.

CURRENT STATE: stub that returns empty results.
The vector + keyword + RRF pipeline works without graph retrieval.
Enable via RAG_GRAPH_RETRIEVAL_ENABLED=true once a real backend is wired.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FUTURE: Graph DB / Hybrid RAG Design
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Why add a graph layer?
  Vector search finds semantically similar chunks but misses structural
  relationships: "What clauses reference entity X?", multi-hop chains like
  Author → Doc → Concept, shared-entity bridging across documents.

Recommended backends (set RAG_GRAPH_DB_BACKEND):

  "neo4j"     — mature, Cypher queries, great for relationship traversal
                 RAG_GRAPH_DB_URI = bolt://localhost:7687
                 pip install neo4j langchain-community

  "arangodb"  — document + graph + vector in one store; AQL queries
                 RAG_GRAPH_DB_URI = http://localhost:8529
                 pip install python-arango

  "falkordb"  — Redis-compatible property graph; fast for small graphs
                 RAG_GRAPH_DB_URI = redis://localhost:6379
                 pip install falkordb

Graph node schema (example — Neo4j Cypher):
  (:Chunk   {chunk_id, content, source, section, page_number})
  (:Entity  {name, type})         -- extracted by NER on ingestion
  (:Document{source, title})
  Relationships:
    (Chunk)-[:PART_OF]->(Document)
    (Chunk)-[:MENTIONS]->(Entity)
    (Entity)-[:RELATED_TO]->(Entity)
    (Chunk)-[:ADJACENT_TO]->(Chunk)  -- prev/next siblings

Hybrid RAG retrieval flow (once enabled):
  1. NER extraction on ingestion → entity nodes
  2. GraphStore.retrieve() → entity-linked chunks via graph traversal
  3. graph_results fed into hybrid_fusion (RRF with semantic + keyword)

Implementation checklist (TODO when implementing):
  □ Add NER extraction step in graph.py ingestion pipeline
  □ Implement GraphStore.upsert_chunks() — write nodes + edges to graph DB
  □ Implement GraphStore.retrieve() — entity + adjacency traversal
  □ Add DB connection management (reuse pattern from mongo_connection.py)
  □ Add graph index creation/migration scripts
  □ Set RAG_GRAPH_RETRIEVAL_ENABLED=true and RAG_GRAPH_DB_URI in .env
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.documents import Document

from .config import settings
from .vector_store import ScoredDocument

logger = logging.getLogger(__name__)


class GraphStore:
    """Stub graph store.  All methods are no-ops until a real backend is wired.

    The interface is intentionally minimal — callers in graph.py will never
    need to change when this stub is replaced with a real implementation.
    """

    async def upsert_chunks(self, chunks: list[Document]) -> int:
        """TODO(graph_rag): extract entities, write nodes + edges to graph DB."""
        if settings.graph_retrieval_enabled:
            logger.warning(
                "GraphStore.upsert_chunks called but no backend is implemented. "
                "Set RAG_GRAPH_RETRIEVAL_ENABLED=false or wire a real backend. "
                "Skipping graph ingestion."
            )
        return 0

    async def retrieve(
        self,
        question: str,
        top_k: int,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[ScoredDocument]:
        """TODO(graph_rag): extract query entities, traverse graph, return scored chunks."""
        if settings.graph_retrieval_enabled:
            logger.warning(
                "GraphStore.retrieve called but no backend is implemented. "
                "Returning empty graph results — only vector + keyword results are used."
            )
        return []

    async def delete_by_source(self, source: str) -> int:
        """TODO(graph_rag): delete all nodes/edges for a given source document."""
        return 0
