from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

from .citations import CitationValidation, validate_citations
from langchain_core.documents import Document
from langgraph.graph import END, StateGraph

from .authorization import filter_authorized_results
from .chunking import ChunkStrategy, chunk_documents
from .config import settings
from .embeddings import get_embeddings
from .evaluation import EvaluationMetrics, evaluate_answer
from .graph_store import GraphStore
from .llm import build_augmented_prompt, generate_answer
from .loader import load_documents
from .rerank import rerank, select_by_knee_point
from .security import filter_prompt_injection_chunks
from .vector_store import ScoredDocument, VectorStore, expand_parent_context, hybrid_fusion


class IngestionState(TypedDict, total=False):
    input_paths: list[str]
    chunk_strategy: ChunkStrategy
    raw_documents: list[Document]
    chunks: list[Document]
    inserted_count: int
    graph_inserted_count: int


class QueryState(TypedDict, total=False):
    question: str
    metadata_filter: dict[str, Any]
    user_id: str
    team_id: str
    query_embedding: list[float]
    semantic_results: list[ScoredDocument]
    keyword_results: list[ScoredDocument]
    graph_results: list[ScoredDocument]
    hybrid_chunks: list[ScoredDocument]
    parent_chunks: list[ScoredDocument]
    reranked_chunks: list[ScoredDocument]
    selected_chunks: list[Document]
    augmented_prompt: str
    answer: str
    citation_validation: CitationValidation
    evaluation_metrics: EvaluationMetrics


def build_ingestion_graph():
    """Create the LangGraph workflow for document ingestion.

    Implementation:
        The graph has four nodes: `parse_and_load`, `chunk`,
        `embed_and_insert`, and `graph_insert`. Edges run linearly because
        ingestion has a fixed sequence: load source data, split it into chunks,
        store embeddings, and write graph relationships.

    Usage:
        The CLI calls this for `practice-rag ingest`. You can also import it in a
        notebook or test and invoke it with `input_paths` and `chunk_strategy`.

    How it helps other functions:
        This function wires the individual ingestion functions into an executable
        pipeline, ensuring vector storage and graph storage are both populated
        from the same chunks.
    """
    graph = StateGraph(IngestionState)
    graph.add_node("parse_and_load", parse_and_load)
    graph.add_node("chunk", chunk)
    graph.add_node("embed_and_insert", embed_and_insert)
    graph.add_node("graph_insert", graph_insert)
    graph.set_entry_point("parse_and_load")
    graph.add_edge("parse_and_load", "chunk")
    graph.add_edge("chunk", "embed_and_insert")
    graph.add_edge("embed_and_insert", "graph_insert")
    graph.add_edge("graph_insert", END)
    return graph.compile()


def build_query_graph():
    """Create the LangGraph workflow for retrieval and generation.

    Implementation:
        The graph embeds the query, performs metadata-filtered semantic,
        keyword, and graph search, applies authorization filters, fuses results,
        expands child chunks to parent context, reranks candidates, selects a
        knee-point cutoff, filters prompt-injection content, builds an augmented
        prompt, generates the answer, validates citations, and computes
        evaluation metrics.

    Usage:
        The CLI calls this for `practice-rag ask`. It can also be used directly
        from Python for integration tests or application endpoints.

    How it helps other functions:
        This is the orchestration layer that guarantees vector, keyword, and
        graph retrieval outputs flow into parent-child expansion and reranking,
        selected safe documents flow into augmentation, and generated answers
        flow into validation and evaluation.
    """
    graph = StateGraph(QueryState)
    graph.add_node("embed_query", embed_query)
    graph.add_node("semantic_search", semantic_search)
    graph.add_node("keyword_search", keyword_search)
    graph.add_node("graph_search", graph_search)
    graph.add_node("hybrid_fusion", fuse_results)
    graph.add_node("parent_child_expand", parent_child_expand)
    graph.add_node("rerank", rerank_results)
    graph.add_node("knee_point_select", knee_point_select)
    graph.add_node("safety_filter", safety_filter)
    graph.add_node("augment", augment)
    graph.add_node("generate", generate)
    graph.add_node("validate_citations", validate_answer_citations)
    graph.add_node("evaluate", evaluate)

    graph.set_entry_point("embed_query")
    graph.add_edge("embed_query", "semantic_search")
    graph.add_edge("semantic_search", "keyword_search")
    graph.add_edge("keyword_search", "graph_search")
    graph.add_edge("graph_search", "hybrid_fusion")
    graph.add_edge("hybrid_fusion", "parent_child_expand")
    graph.add_edge("parent_child_expand", "rerank")
    graph.add_edge("rerank", "knee_point_select")
    graph.add_edge("knee_point_select", "safety_filter")
    graph.add_edge("safety_filter", "augment")
    graph.add_edge("augment", "generate")
    graph.add_edge("generate", "validate_citations")
    graph.add_edge("validate_citations", "evaluate")
    graph.add_edge("evaluate", END)
    return graph.compile()


def parse_and_load(state: IngestionState) -> IngestionState:
    """Load raw files into LangChain documents.

    Implementation:
        The function reads `input_paths` from graph state, falls back to the
        configured data directory, converts strings to `Path` objects, and calls
        `load_documents`.

    Usage:
        This is the first node in the ingestion graph.

    How it helps other functions:
        It creates `raw_documents`, which the `chunk` node consumes to produce
        retrievable chunks.
    """
    paths = [Path(path) for path in state.get("input_paths") or [settings.data_dir]]
    return {"raw_documents": load_documents(paths)}


def chunk(state: IngestionState) -> IngestionState:
    """Split loaded documents into retrieval-ready chunks.

    Implementation:
        The function creates the configured embedding backend, reads the desired
        chunking strategy from state, and delegates to `chunk_documents`.

    Usage:
        This is the second ingestion node and runs after `parse_and_load`.

    How it helps other functions:
        It produces `chunks` with chunk-level metadata. `embed_and_insert` then
        stores those chunks in the vector database.
    """
    embeddings = get_embeddings()
    strategy = state.get("chunk_strategy", "recursive")
    return {"chunks": chunk_documents(state.get("raw_documents", []), embeddings, strategy)}


def embed_and_insert(state: IngestionState) -> IngestionState:
    """Embed chunks and insert them into the vector database.

    Implementation:
        The function creates the embedding backend and `VectorStore`, then calls
        `upsert` with chunks from graph state. Chroma invokes the embedding
        function during insertion.

    Usage:
        This is the final ingestion node.

    How it helps other functions:
        Once chunks are inserted, the query graph can retrieve them through
        semantic search, keyword search, hybrid fusion, and reranking.
    """
    embeddings = get_embeddings()
    vector_store = VectorStore(embeddings)
    inserted_count = vector_store.upsert(state.get("chunks", []))
    return {"inserted_count": inserted_count}


def graph_insert(state: IngestionState) -> IngestionState:
    """Insert chunks and relationships into the graph database.

    Implementation:
        The function creates `GraphStore` and writes chunk nodes, metadata nodes,
        entity nodes, and adjacency relationships derived from the same chunk
        list inserted into the vector database.

    Usage:
        This ingestion node runs after `embed_and_insert`.

    How it helps other functions:
        Query-time `graph_search` depends on this graph index to retrieve chunks
        through entity and metadata relationships, creating graph-hybrid RAG.
    """
    graph_store = GraphStore()
    return {"graph_inserted_count": graph_store.upsert_chunks(state.get("chunks", []))}


def embed_query(state: QueryState) -> QueryState:
    """Embed the user's question for semantic retrieval.

    Implementation:
        The function creates the configured embedding backend and calls
        `embed_query` on the question in graph state.

    Usage:
        This is the first query graph node. The stored `query_embedding` is also
        useful for debugging and observability.

    How it helps other functions:
        It ensures the query uses the same embedding space as ingested chunks,
        which is required for meaningful semantic search.
    """
    embeddings = get_embeddings()
    return {"query_embedding": embeddings.embed_query(state["question"])}


def semantic_search(state: QueryState) -> QueryState:
    """Run vector similarity search against Chroma.

    Implementation:
        The function opens the configured vector store and calls
        `semantic_search` with the question, retrieval `top_k`, and optional
        metadata filters from graph state.

    Usage:
        This node runs after `embed_query` in the query graph.

    How it helps other functions:
        It produces metadata- and authorization-scoped semantic candidates that
        are later combined with keyword results in `fuse_results`.
    """
    vector_store = VectorStore(get_embeddings())
    results = vector_store.semantic_search(
            state["question"],
            top_k=settings.retrieval_top_k,
            metadata_filter=state.get("metadata_filter") or None,
        )
    return {
        "semantic_results": filter_authorized_results(
            results,
            user_id=state.get("user_id"),
            team_id=state.get("team_id"),
        )
    }


def keyword_search(state: QueryState) -> QueryState:
    """Run BM25 keyword search over indexed chunks.

    Implementation:
        The function opens the vector store and calls its keyword search method,
        which builds an in-memory BM25 index from Chroma documents after applying
        optional metadata filters.

    Usage:
        This node runs after semantic search and uses the same user question.

    How it helps other functions:
        It supplies metadata- and authorization-scoped exact-match candidates for
        `fuse_results`, improving recall for identifiers, acronyms, and terms
        that embeddings may miss.
    """
    vector_store = VectorStore(get_embeddings())
    results = vector_store.keyword_search(
            state["question"],
            top_k=settings.retrieval_top_k,
            metadata_filter=state.get("metadata_filter") or None,
        )
    return {
        "keyword_results": filter_authorized_results(
            results,
            user_id=state.get("user_id"),
            team_id=state.get("team_id"),
        )
    }


def graph_search(state: QueryState) -> QueryState:
    """Run graph-database retrieval for relationship-aware candidates.

    Implementation:
        The function extracts query entities inside `GraphStore.retrieve`, looks
        up related chunk nodes through graph adjacency indexes, applies metadata
        filters, and then applies authorization filtering.

    Usage:
        This query node runs after keyword search and before hybrid fusion.

    How it helps other functions:
        It adds relationship-aware candidates that may not be surfaced by vector
        similarity or BM25, especially for shared entities, sections, pages,
        source files, parent groups, or technical identifiers.
    """
    results = GraphStore().retrieve(
        state["question"],
        top_k=settings.graph_retrieval_top_k,
        metadata_filter=state.get("metadata_filter") or None,
    )
    return {
        "graph_results": filter_authorized_results(
            results,
            user_id=state.get("user_id"),
            team_id=state.get("team_id"),
        )
    }


def fuse_results(state: QueryState) -> QueryState:
    """Combine semantic, keyword, and graph candidates into a hybrid result set.

    Implementation:
        The function reads semantic, keyword, and graph result lists from graph
        state and applies reciprocal rank fusion. It asks for a wider candidate
        pool than the final retrieval size because reranking and knee-point
        selection will narrow it.

    Usage:
        This query node runs after all three retrieval strategies complete.

    How it helps other functions:
        It produces `hybrid_chunks`, the vector-keyword-graph candidate list
        consumed by parent-child expansion and reranking.
    """
    return {
        "hybrid_chunks": hybrid_fusion(
            state.get("semantic_results", []),
            state.get("keyword_results", []),
            top_k=settings.retrieval_top_k * 2,
            graph_results=state.get("graph_results", []),
        )
    }


def rerank_results(state: QueryState) -> QueryState:
    """Reorder hybrid candidates using the local reranking function.

    Implementation:
        The function passes the original question and parent-expanded candidates
        to `rerank`, which either uses the configured cross-encoder or the local
        lexical fallback.

    Usage:
        This node runs after `fuse_results`.

    How it helps other functions:
        It prepares a relevance-ordered list for `knee_point_select`, making the
        final context smaller and more focused.
    """
    return {"reranked_chunks": rerank(state["question"], state.get("parent_chunks", []))}


def parent_child_expand(state: QueryState) -> QueryState:
    """Expand precise child retrieval matches into broader parent context.

    Implementation:
        The function reads hybrid child candidates from state and calls
        `expand_parent_context`, which uses `parent_content` metadata created
        during chunking.

    Usage:
        This query node runs after hybrid fusion and before reranking.

    How it helps other functions:
        It lets retrieval find precise child chunks while giving reranking,
        augmentation, and generation enough parent context to answer correctly.
    """
    return {"parent_chunks": expand_parent_context(state.get("hybrid_chunks", []))}


def knee_point_select(state: QueryState) -> QueryState:
    """Choose the final context chunks using the knee-point algorithm.

    Implementation:
        The function calls `select_by_knee_point` and unwraps each
        `ScoredDocument` into a plain `Document` for prompt construction.

    Usage:
        This node runs after reranking.

    How it helps other functions:
        It produces `selected_chunks`, which the `augment` and `generate` nodes
        use as the evidence set for the final answer.
    """
    selected = select_by_knee_point(state.get("reranked_chunks", []))
    return {"selected_chunks": [item.document for item in selected]}


def safety_filter(state: QueryState) -> QueryState:
    """Remove selected chunks that contain prompt-injection indicators.

    Implementation:
        The function delegates to `filter_prompt_injection_chunks`, which scans
        selected evidence for suspicious instruction patterns before those
        chunks are included in the prompt.

    Usage:
        This node runs after knee-point selection and before augmentation.

    How it helps other functions:
        It protects `build_augmented_prompt` and `generate_answer` from
        receiving malicious retrieved content as context.
    """
    return {"selected_chunks": filter_prompt_injection_chunks(state.get("selected_chunks", []))}


def augment(state: QueryState) -> QueryState:
    """Build the query-plus-context augmented prompt.

    Implementation:
        The function passes the user question and selected chunks to
        `build_augmented_prompt`, which adds citations and grounding
        instructions.

    Usage:
        This node runs immediately before generation.

    How it helps other functions:
        It creates `augmented_prompt` for observability and for LLM generation
        when hosted generation is enabled.
    """
    return {
        "augmented_prompt": build_augmented_prompt(
            state["question"],
            state.get("selected_chunks", []),
        )
    }


def generate(state: QueryState) -> QueryState:
    """Produce the final answer from selected evidence.

    Implementation:
        The function delegates to `generate_answer`, which either calls the
        configured chat model or returns a local evidence summary.

    Usage:
        This is the terminal query graph node.

    How it helps other functions:
        It converts all upstream retrieval and augmentation work into the
        user-facing `answer` field returned by graph invocation.
    """
    return {"answer": generate_answer(state["question"], state.get("selected_chunks", []))}


def validate_answer_citations(state: QueryState) -> QueryState:
    """Validate answer citation markers against selected chunks.

    Implementation:
        The function calls `validate_citations` with the generated answer and the
        final selected chunks, producing a structured validation report.

    Usage:
        This node runs after answer generation.

    How it helps other functions:
        The validation result feeds `evaluate` and gives callers a direct signal
        about whether the answer cites available evidence.
    """
    return {
        "citation_validation": validate_citations(
            state.get("answer", ""),
            state.get("selected_chunks", []),
        )
    }


def evaluate(state: QueryState) -> QueryState:
    """Compute local RAG evaluation metrics for the answer.

    Implementation:
        The function calls `evaluate_answer` with the question, answer, selected
        chunks, and citation validation output. The metrics are lightweight
        lexical approximations for practice use.

    Usage:
        This is the terminal query graph node.

    How it helps other functions:
        It returns `evaluation_metrics` in the final graph state, helping users
        inspect faithfulness, context precision, and answer relevance after each
        query.
    """
    return {
        "evaluation_metrics": evaluate_answer(
            state["question"],
            state.get("answer", ""),
            state.get("selected_chunks", []),
            state.get(
                "citation_validation",
                {
                    "valid": False,
                    "cited_numbers": [],
                    "missing_citations": True,
                    "invalid_citations": [],
                    "available_citations": [],
                },
            ),
        )
    }
