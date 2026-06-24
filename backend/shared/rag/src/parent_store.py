from __future__ import annotations

import json
from pathlib import Path

from langchain_core.documents import Document

from .config import settings


def parent_store_path() -> Path:
    """Return the JSON file path used for external parent document storage.

    Implementation:
        The function creates the configured parent-store directory and returns a
        stable `parents.json` file path inside it.

    Usage:
        `save_parent_documents` and `load_parent_document` call this whenever
        they read or write parent contexts.

    How it helps other functions:
        Keeping parent text outside Chroma metadata prevents large parent chunks
        from bloating vector metadata while still allowing retrieval expansion by
        `parent_id`.
    """
    settings.parent_store_dir.mkdir(parents=True, exist_ok=True)
    return settings.parent_store_dir / "parents.json"


def save_parent_documents(parent_documents: dict[str, Document]) -> None:
    """Persist parent context documents outside the vector database.

    Implementation:
        Existing JSON data is loaded, updated with new parent IDs, and written
        back with each parent document's page content and metadata.

    Usage:
        The chunking module calls this after it groups child chunks into parent
        context blocks.

    How it helps other functions:
        `expand_parent_context` can later load broader parent context without
        storing large `parent_content` values in every Chroma record.
    """
    path = parent_store_path()
    existing = _read_parent_store(path)
    for parent_id, document in parent_documents.items():
        existing[parent_id] = {
            "page_content": document.page_content,
            "metadata": document.metadata,
        }
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def load_parent_document(parent_id: str) -> Document | None:
    """Load one parent context document by ID.

    Implementation:
        The function reads the JSON parent store, looks up the requested parent
        ID, and reconstructs a LangChain `Document` if found.

    Usage:
        `expand_parent_context` calls this during query-time parent-child
        expansion.

    How it helps other functions:
        It gives reranking and prompt augmentation broader context while keeping
        vector search indexed on precise child chunks.
    """
    payload = _read_parent_store(parent_store_path()).get(parent_id)
    if not payload:
        return None
    return Document(
        page_content=str(payload.get("page_content", "")),
        metadata=dict(payload.get("metadata", {})),
    )


def delete_parent_documents(parent_ids: list[str]) -> int:
    """Delete parent documents from external storage.

    Implementation:
        The function removes matching parent IDs from the JSON store and writes
        the remaining data back to disk.

    Usage:
        Future cleanup or re-indexing workflows can call this when old parent
        groups are no longer needed.

    How it helps other functions:
        It keeps external parent storage aligned with the vector database when
        documents are re-indexed or removed.
    """
    path = parent_store_path()
    existing = _read_parent_store(path)
    deleted = 0
    for parent_id in parent_ids:
        if parent_id in existing:
            del existing[parent_id]
            deleted += 1
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return deleted


def _read_parent_store(path: Path) -> dict[str, dict[str, object]]:
    """Read parent-store JSON safely.

    Implementation:
        If the store does not exist or cannot be decoded, the function returns an
        empty dictionary. This keeps ingestion and retrieval resilient in a fresh
        project checkout.

    Usage:
        All parent-store read/write helpers call this internally.

    How it helps other functions:
        It isolates file handling and error tolerance from chunking and retrieval
        logic.
    """
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
