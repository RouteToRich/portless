"""FastAPI router for the RAG module.

Mount this router in the main app:

    from shared.rag.src.router import router as rag_router
    app.include_router(rag_router, prefix="/rag")

Endpoints:
    POST   /rag/ingest              — start an ingestion job
    GET    /rag/ingest/{job_id}     — poll ingestion job status
    POST   /rag/query               — ask a question (full RAG pipeline)
    DELETE /rag/documents/{source}  — delete all chunks for a source path
    GET    /rag/health              — liveness + MongoDB ping
    GET    /rag/stats               — document count stats
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, Field

from .config import settings
from .graph import ingest, query
from .vector_store import VectorStore, ensure_indexes
from .parent_store import MongoParentStore
from .graph_store import GraphStore
from .embeddings import get_embeddings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["rag"])


# ──────────────────────────────────────────────────────── request / response schemas

class IngestRequest(BaseModel):
    input_paths: list[str] = Field(
        ...,
        description="List of absolute file or directory paths to ingest.",
        examples=[["/data/docs/contract.pdf"]],
    )
    chunk_strategy: str = Field(
        default="recursive",
        description="Chunking strategy: 'recursive' (default) or 'semantic'.",
    )


class IngestResponse(BaseModel):
    job_id: str
    status: str
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str          # "pending" | "running" | "completed" | "failed"
    inserted_count: int | None = None
    graph_inserted_count: int | None = None
    error: str | None = None
    started_at: str
    completed_at: str | None = None


class QueryRequest(BaseModel):
    question: str = Field(..., description="The question to answer using the RAG pipeline.")
    metadata_filter: dict[str, Any] | None = Field(
        default=None,
        description="Optional metadata filters applied at retrieval time.",
    )
    user_id: str | None = None
    team_id: str | None = None


class QueryResponse(BaseModel):
    question: str
    answer: str
    sources: list[dict[str, Any]]
    citation_validation: dict[str, Any]
    evaluation_metrics: dict[str, Any]


class DeleteResponse(BaseModel):
    source: str
    chunks_deleted: int
    parents_deleted: int


class HealthResponse(BaseModel):
    status: str
    mongodb: str
    vector_index: str


class StatsResponse(BaseModel):
    total_chunks: int
    collection: str
    db: str


# ──────────────────────────────────────────────────── in-memory job registry
# For multi-worker production deployments replace with a MongoDB-backed job store.

_jobs: dict[str, dict[str, Any]] = {}


# ──────────────────────────────────────────────────── startup hook

async def startup() -> None:
    """Call from app lifespan to create MongoDB indexes on startup."""
    try:
        await ensure_indexes()
        logger.info("RAG module: MongoDB indexes ensured.")
    except Exception as exc:
        logger.warning("RAG module startup index check failed: %s", exc)


# ──────────────────────────────────────────────────────── endpoints

@router.post(
    "/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start document ingestion",
)
async def start_ingest(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
) -> IngestResponse:
    """Submit an ingestion job.  Returns immediately with a job_id to poll."""
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "status": "pending",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "inserted_count": None,
        "graph_inserted_count": None,
        "error": None,
    }
    background_tasks.add_task(
        _run_ingest_job,
        job_id=job_id,
        input_paths=request.input_paths,
        chunk_strategy=request.chunk_strategy,
    )
    return IngestResponse(
        job_id=job_id,
        status="pending",
        message="Ingestion job accepted. Poll /rag/ingest/{job_id} for status.",
    )


@router.get(
    "/ingest/{job_id}",
    response_model=JobStatusResponse,
    summary="Poll ingestion job status",
)
async def get_ingest_status(job_id: str) -> JobStatusResponse:
    """Check the status of a previously submitted ingestion job."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found.",
        )
    return JobStatusResponse(job_id=job_id, **job)


@router.post(
    "/query",
    response_model=QueryResponse,
    summary="Ask a question via RAG",
)
async def rag_query(request: QueryRequest) -> QueryResponse:
    """Run the full RAG pipeline (embed → retrieve → rerank → generate)."""
    try:
        result = await query(
            question=request.question,
            metadata_filter=request.metadata_filter,
            user_id=request.user_id,
            team_id=request.team_id,
        )
    except Exception as exc:
        logger.error("RAG query failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"RAG pipeline error: {exc}",
        ) from exc

    sources = [
        {
            "source": chunk.metadata.get("source"),
            "title": chunk.metadata.get("title"),
            "section": chunk.metadata.get("section"),
            "page_number": chunk.metadata.get("page_number"),
            "chunk_id": chunk.metadata.get("chunk_id"),
        }
        for chunk in result.get("selected_chunks", [])
    ]

    return QueryResponse(
        question=request.question,
        answer=result.get("answer", ""),
        sources=sources,
        citation_validation=dict(result.get("citation_validation", {})),
        evaluation_metrics=dict(result.get("evaluation_metrics", {})),
    )


@router.delete(
    "/documents/{source:path}",
    response_model=DeleteResponse,
    summary="Delete all chunks and parents for a source",
)
async def delete_document(source: str) -> DeleteResponse:
    """Remove all vector chunks, parent docs, and graph nodes for a given source path."""
    embeddings = get_embeddings()
    vector_store = VectorStore(embeddings)
    parent_store = MongoParentStore()
    graph_store = GraphStore()

    chunks_deleted, parents_deleted, _ = await asyncio.gather(
        vector_store.delete_by_source(source),
        parent_store.delete_by_source(source),
        graph_store.delete_by_source(source),
    )

    logger.info(
        "Deleted source '%s': %d chunks, %d parents.", source, chunks_deleted, parents_deleted
    )
    return DeleteResponse(
        source=source,
        chunks_deleted=chunks_deleted,
        parents_deleted=parents_deleted,
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="RAG module health check",
)
async def health_check() -> HealthResponse:
    """Verify MongoDB connectivity and index presence."""
    mongo_status = "unknown"
    index_status = "unknown"

    try:
        from shared.llm_gateway.mongo_connection import get_connection

        conn = await get_connection(
            uri=settings.mongo_uri,
            db_name=settings.mongo_db_name,
        )
        alive = await conn.ping()
        mongo_status = "ok" if alive else "unreachable"

        col = conn.get_collection(settings.chunks_collection)
        indexes = await col.index_information()
        index_status = "ok" if "chunk_id_unique" in indexes else "missing_regular_indexes"

    except Exception as exc:
        mongo_status = f"error: {exc}"

    overall = "ok" if mongo_status == "ok" else "degraded"
    return HealthResponse(
        status=overall,
        mongodb=mongo_status,
        vector_index=index_status,
    )


@router.get(
    "/stats",
    response_model=StatsResponse,
    summary="RAG document stats",
)
async def get_stats() -> StatsResponse:
    """Return the total number of indexed chunks."""
    embeddings = get_embeddings()
    vector_store = VectorStore(embeddings)
    total = await vector_store.count()
    return StatsResponse(
        total_chunks=total,
        collection=settings.chunks_collection,
        db=settings.mongo_db_name,
    )


# ──────────────────────────────────────────────────── background job runner

async def _run_ingest_job(
    job_id: str,
    input_paths: list[str],
    chunk_strategy: str,
) -> None:
    """Execute ingestion in the background and update the job registry."""
    _jobs[job_id]["status"] = "running"
    try:
        result = await ingest(input_paths=input_paths, chunk_strategy=chunk_strategy)  # type: ignore[arg-type]
        _jobs[job_id].update(
            {
                "status": "completed",
                "inserted_count": result.get("inserted_count", 0),
                "graph_inserted_count": result.get("graph_inserted_count", 0),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        logger.info(
            "Ingestion job %s completed: %d chunk(s) inserted.",
            job_id,
            result.get("inserted_count", 0),
        )
    except Exception as exc:
        _jobs[job_id].update(
            {
                "status": "failed",
                "error": str(exc),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        logger.error("Ingestion job %s failed: %s", job_id, exc, exc_info=True)
