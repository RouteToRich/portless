from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    project_root: Path = Path(__file__).resolve().parents[2]
    data_dir: Path = project_root / "data"
    chroma_dir: Path = project_root / os.getenv("CHROMA_DIR", ".chroma")
    collection_name: str = os.getenv("COLLECTION_NAME", "practice_rag")
    use_openai_embeddings: bool = os.getenv("USE_OPENAI_EMBEDDINGS", "false").lower() == "true"
    use_openai_llm: bool = os.getenv("USE_OPENAI_LLM", "false").lower() == "true"
    openai_embedding_model: str = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    openai_chat_model: str = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
    chunk_size: int = int(os.getenv("CHUNK_SIZE", "900"))
    chunk_overlap: int = int(os.getenv("CHUNK_OVERLAP", "120"))
    semantic_breakpoint_percentile: int = int(os.getenv("SEMANTIC_BREAKPOINT_PERCENTILE", "25"))
    retrieval_top_k: int = int(os.getenv("RETRIEVAL_TOP_K", "8"))
    child_chunks_per_parent: int = int(os.getenv("CHILD_CHUNKS_PER_PARENT", "3"))
    use_cross_encoder_reranker: bool = os.getenv("USE_CROSS_ENCODER_RERANKER", "false").lower() == "true"
    cross_encoder_model: str = os.getenv("CROSS_ENCODER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
    parent_store_dir: Path = project_root / os.getenv("PARENT_STORE_DIR", ".parent_store")
    graph_store_dir: Path = project_root / os.getenv("GRAPH_STORE_DIR", ".graph_store")
    graph_retrieval_top_k: int = int(os.getenv("GRAPH_RETRIEVAL_TOP_K", "8"))
    enable_pdf_layout_extraction: bool = os.getenv("ENABLE_PDF_LAYOUT_EXTRACTION", "true").lower() == "true"
    enable_pdf_ocr: bool = os.getenv("ENABLE_PDF_OCR", "false").lower() == "true"
    use_llm_prompt_injection_classifier: bool = (
        os.getenv("USE_LLM_PROMPT_INJECTION_CLASSIFIER", "false").lower() == "true"
    )
    advanced_eval_provider: str = os.getenv("ADVANCED_EVAL_PROVIDER", "local")


settings = Settings()
