"""RAG module configuration.

Env-var prefix: RAG_  (e.g. RAG_MONGO_URI, RAG_RETRIEVAL_TOP_K)

Env file load order (later file wins):
  1. backend/.env                  — shared root, loaded from CWD
  2. backend/shared/rag/.env       — RAG-specific overrides

Vector database: MongoDB Atlas with $vectorSearch aggregation.

Atlas Vector Search index (create once in Atlas UI or via ensure_vector_index()):
  - Index type:    vectorSearch
  - Field path:    embedding
  - numDimensions: RAG_EMBEDDING_DIMENSIONS (default 1536 for text-embedding-3-small)
  - Similarity:    RAG_VECTOR_SIMILARITY     (default cosine)

Embeddings: routed through llm-gateway API key.
  - RAG_EMBEDDING_PROVIDER=openai  → OpenAIEmbeddings using llm-gateway primary key
  - RAG_EMBEDDING_PROVIDER=local   → LocalHashEmbeddings (offline / zero cost)
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Module .env lives one level above src/ (i.e. backend/shared/rag/.env)
_MODULE_ENV = Path(__file__).parent.parent / ".env"

_DEFAULT_MONGO_URI = (
    "mongodb+srv://richminds84_db_user:8M7dOW4srdGAy7aM"
    "@cluster0.taios8r.mongodb.net/?appName=Cluster0"
)


class RAGSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        # Load root .env first, then module-local .env (module wins on conflict)
        env_file=(".env", str(_MODULE_ENV)),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ──────────────────────────────────────────────── MongoDB Atlas
    mongo_uri: str = _DEFAULT_MONGO_URI
    mongo_db_name: str = "rag"
    chunks_collection: str = "chunks"
    parents_collection: str = "parent_docs"
    ingestion_jobs_collection: str = "ingestion_jobs"
    mongo_max_pool_size: int = 50
    mongo_connect_timeout_ms: int = 5_000
    mongo_server_selection_timeout_ms: int = 5_000

    # ──────────────────────────────────────────── Atlas Vector Search
    # The index must exist before queries run — see ensure_vector_index() in
    # vector_store.py for programmatic creation or the Atlas UI instructions.
    vector_index_name: str = "vector_index"
    embedding_dimensions: int = 1536       # text-embedding-3-small = 1536
    vector_similarity: str = "cosine"      # "cosine" | "euclidean" | "dotProduct"

    # ─────────────────────────── Embeddings (via llm-gateway credentials)
    # embedding_provider="openai" reads embedding_api_key, then falls back to
    # LLM_PRIMARY_API_KEY env var, then LocalHashEmbeddings if still empty.
    embedding_provider: str = "openai"     # "openai" | "local"
    embedding_model: str = "text-embedding-3-small"
    embedding_api_key: str = ""            # override with RAG_EMBEDDING_API_KEY

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
    # Where raw uploaded knowledge files are persisted durably. "mongodb"
    # stores bytes in GridFS — no external infra needed. Swap to "s3" later
    # by setting RAG_FILE_STORE_BACKEND=s3 and filling the s3_* fields; the
    # S3 implementation lives in file_store.py (S3FileStore).
    file_store_backend: str = "mongodb"          # "mongodb" | "s3"
    file_store_gridfs_bucket: str = "rag_files"  # GridFS bucket name
    # S3 / blob-store settings — only read when file_store_backend="s3".
    # Leave the credential fields empty to use the ambient AWS credential chain
    # (IAM role / ~/.aws / env). Fill them in for an explicit key pair or an
    # S3-compatible store (MinIO, R2, etc.).
    s3_bucket: str = ""                           # RAG_S3_BUCKET
    s3_region: str = ""                           # RAG_S3_REGION
    s3_endpoint_url: str = ""                     # RAG_S3_ENDPOINT_URL — MinIO/R2/etc.
    s3_access_key_id: str = ""                    # RAG_S3_ACCESS_KEY_ID (placeholder)
    s3_secret_access_key: str = ""                # RAG_S3_SECRET_ACCESS_KEY (placeholder)
    s3_prefix: str = ""                           # RAG_S3_PREFIX — optional key prefix

    # ──────────────────────────────────────────────── Security
    use_llm_prompt_injection_classifier: bool = False

    # ──────────────────────────────────────────────── Evaluation
    advanced_eval_provider: str = "local"  # "local" | "ragas" | "deepeval" | "llm"

    # ─────────────────────────────────── graph retrieval (hybrid RAG)
    # Set graph_retrieval_enabled=true and fill graph_db_uri/user/password to add
    # a relationship-aware retrieval leg alongside vector + keyword search. The
    # Neo4j backend is implemented (graph_store.py); "arangodb" / "falkordb" fall
    # back to a no-op until a backend is added. When disabled, the query pipeline
    # runs exactly as before (empty graph results, fused harmlessly by RRF).
    graph_retrieval_enabled: bool = False
    graph_retrieval_top_k: int = 8
    graph_db_backend: str = "neo4j"         # "neo4j" | "arangodb" | "falkordb"
    graph_db_uri: str = ""                  # e.g. bolt://localhost:7687 (or neo4j+s://…)
    graph_db_user: str = ""                 # RAG_GRAPH_DB_USER (placeholder)
    graph_db_password: str = ""             # RAG_GRAPH_DB_PASSWORD (placeholder)
    graph_db_database: str = ""             # RAG_GRAPH_DB_DATABASE — blank = server default


settings = RAGSettings()
