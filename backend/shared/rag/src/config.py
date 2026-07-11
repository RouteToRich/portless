"""RAG module configuration.

Env-var prefix: RAG_  (e.g. RAG_MONGO_URI, RAG_RETRIEVAL_TOP_K)

Env file load order (later file wins):
  1. backend/.env                  — shared root, loaded from CWD
  2. backend/shared/rag/.env       — RAG-specific overrides

Vector database: MongoDB Atlas with $vectorSearch aggregation.

Atlas Vector Search index (create once in Atlas UI or via ensure_vector_index()):
  - Index type:    vectorSearch
  - Field path:    embedding
  - numDimensions: LLM_EMBEDDING_DIMENSIONS (in shared/llm_gateway/.env; default
                   1536 for text-embedding-3-small)
  - Similarity:    RAG_VECTOR_SIMILARITY     (default cosine)

Embeddings: RAG has no embedding-model config of its own — it calls
shared.llm_gateway.embeddings.GatewayEmbeddings(), which reads
LLM_EMBEDDING_PROVIDER / LLM_EMBEDDING_MODEL / LLM_EMBEDDING_API_KEY /
LLM_EMBEDDING_DIMENSIONS from shared/llm_gateway/.env (the single source of
truth for every model/provider/key in the app). See shared/llm_gateway/config.py.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Module .env lives one level above src/ (i.e. backend/shared/rag/.env)
_MODULE_ENV = Path(__file__).parent.parent / ".env"


class RAGSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        # Load root .env first, then module-local .env (module wins on conflict)
        env_file=(".env", str(_MODULE_ENV)),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ──────────────────────────────────────────────── MongoDB Atlas
    mongo_uri: str = Field(
        default="", validation_alias=AliasChoices("RAG_MONGO_URI", "MONGO_URI")
    )
    mongo_db_name: str = Field(
        default="portless", validation_alias=AliasChoices("RAG_MONGO_DB_NAME", "MONGO_DB_NAME")
    )
    chunks_collection: str = "chunks"
    parents_collection: str = "parent_docs"
    ingestion_jobs_collection: str = "ingestion_jobs"
    mongo_max_pool_size: int = 50
    mongo_connect_timeout_ms: int = 5_000
    mongo_server_selection_timeout_ms: int = 5_000

    # ──────────────────────────────────────────── Atlas Vector Search
    # The index must exist before queries run — see ensure_vector_index() in
    # vector_store.py for programmatic creation or the Atlas UI instructions.
    # numDimensions comes from the LLM Gateway's embedding config
    # (LLM_EMBEDDING_DIMENSIONS in shared/llm_gateway/.env) — RAG declares no
    # embedding-model config of its own; see shared.llm_gateway.embeddings.
    vector_index_name: str = "vector_index"
    vector_similarity: str = "cosine"      # "cosine" | "euclidean" | "dotProduct"

    # ──────────────────────────────────────────────────── Chunking
    chunk_size: int = 900
    chunk_overlap: int = 120
    semantic_breakpoint_percentile: int = 25
    child_chunks_per_parent: int = 3

    # ──────────────────────────────────────────────── Retrieval
    retrieval_top_k: int = 8
    # numCandidates should be 10–20× top_k for good recall on Atlas
    retrieval_num_candidates: int = 100
    # Max documents fetched for in-memory BM25 keyword search
    keyword_search_fetch_limit: int = 500

    # ──────────────────────────────────────────────── Reranking
    use_cross_encoder_reranker: bool = False
    cross_encoder_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    # ──────────────────────────────────────────────── Generation
    rag_temperature: float = 0.0
    rag_max_tokens: int = 2048

    # ─────────────────────────────────────────── Document loading
    enable_pdf_layout_extraction: bool = True
    enable_pdf_ocr: bool = False

    # ──────────────────────────── Uploaded-file storage (raw bytes)
    # Raw uploaded knowledge files are persisted in MongoDB GridFS — the sole
    # backend (no S3/blob store is used anywhere in this app). GridFS lives in
    # the same shared database (mongo_db_name) as every other collection.
    file_store_gridfs_bucket: str = "rag_files"  # GridFS bucket name

    # ──────────────────────────────────────────────── Security
    use_llm_prompt_injection_classifier: bool = False

    # ──────────────────────────────────────────────── Evaluation
    advanced_eval_provider: str = "local"  # "local" | "ragas" | "deepeval" | "llm"

    # ─────────────────────────────────── graph retrieval (hybrid RAG)
    # Set graph_retrieval_enabled=true to add a relationship-aware retrieval leg
    # alongside vector + keyword search. Backends: "mongo" (recommended — same
    # shared MongoDB, no new credentials/services), "neo4j" (needs
    # graph_db_uri/user/password), or "arangodb"/"falkordb" (reserved, no-op
    # until wired). When disabled, the query pipeline runs exactly as before
    # (empty graph results, fused harmlessly by RRF).
    graph_retrieval_enabled: bool = False
    graph_retrieval_top_k: int = 8
    graph_db_backend: str = "neo4j"         # "mongo" | "neo4j" | "arangodb" | "falkordb"
    graph_db_uri: str = ""                  # neo4j only, e.g. bolt://localhost:7687 (or neo4j+s://…)
    graph_db_user: str = ""                 # neo4j only — RAG_GRAPH_DB_USER (placeholder)
    graph_db_password: str = ""             # neo4j only — RAG_GRAPH_DB_PASSWORD (placeholder)
    graph_db_database: str = ""             # neo4j only — RAG_GRAPH_DB_DATABASE, blank = server default
    # mongo backend only — collection name in the same shared database
    # (mongo_uri / mongo_db_name above); no separate connection needed.
    graph_chunks_collection: str = "graph_chunks"


settings = RAGSettings()
