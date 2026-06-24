from __future__ import annotations

import re
from typing import Literal

import numpy as np
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from .config import settings
from .parent_store import save_parent_documents


ChunkStrategy = Literal["recursive", "semantic"]


def chunk_documents(
    documents: list[Document],
    embeddings: Embeddings,
    strategy: ChunkStrategy = "recursive",
) -> list[Document]:
    """Route documents through the selected chunking implementation.

    Usage:
        The ingestion graph calls this function from its `chunk` node after
        `load_documents` has produced LangChain `Document` objects. Pass
        `strategy="recursive"` for the default deterministic splitter, or
        `strategy="semantic"` when you want embedding-based topic boundaries.

    How it helps other functions:
        The returned chunks are enriched later by either chunking function with
        chunk metadata. `VectorStore.upsert` depends on that metadata, especially
        `chunk_id`, to insert stable records into Chroma.
    """
    if strategy == "semantic":
        return semantic_chunk_documents(documents, embeddings)
    return recursive_chunk_documents(documents)


def recursive_chunk_documents(documents: list[Document]) -> list[Document]:
    """Split documents using LangChain's recursive character splitter.

    Implementation:
        The splitter tries separators from most meaningful to least meaningful:
        markdown headings, paragraphs, lines, sentence boundaries, spaces, and
        finally individual characters. This keeps chunks readable while still
        enforcing `settings.chunk_size` and `settings.chunk_overlap`.

    Usage:
        Use this as the safest default chunker for most Markdown and text files.
        It is predictable, fast, and does not require embedding calls.

    How it helps other functions:
        It sends raw chunks to `_number_chunks`, which attaches `chunk_id`,
        `chunk_index`, `chunk_strategy`, and `chunk_chars`. Retrieval, citation,
        reranking, and prompt-building functions use that metadata later.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=["\n## ", "\n### ", "\n\n", "\n", ". ", " ", ""],
    )
    chunks = splitter.split_documents(documents)
    return _number_chunks(chunks, "recursive")


def semantic_chunk_documents(documents: list[Document], embeddings: Embeddings) -> list[Document]:
    """Split documents by detecting semantic topic shifts.

    Implementation:
        Each document is first split into semantic units, usually paragraphs.
        The function embeds those units, computes cosine similarity between
        neighboring units, and creates a new chunk when similarity drops below
        the configured percentile threshold. A size guard also forces a break
        when a chunk grows too large.

    Usage:
        Use this when documents mix topics and paragraph boundaries do not map
        cleanly to retrieval boundaries. It costs more than recursive splitting
        because it embeds units during ingestion.

    How it helps other functions:
        Better semantic boundaries usually improve `VectorStore.semantic_search`
        and reduce irrelevant context in `build_augmented_prompt`. The final
        chunks still flow through `_number_chunks`, so the vector DB receives
        the same metadata shape as recursive chunks.
    """
    chunks: list[Document] = []
    for document in documents:
        units = _split_semantic_units(document.page_content)
        if len(units) <= 1:
            chunks.append(
                Document(
                    page_content=document.page_content,
                    metadata={**document.metadata, "chunk_strategy": "semantic"},
                )
            )
            continue

        vectors = embeddings.embed_documents(units)
        similarities = [_cosine(vectors[i], vectors[i + 1]) for i in range(len(vectors) - 1)]
        threshold = float(np.percentile(similarities, settings.semantic_breakpoint_percentile))

        current: list[str] = [units[0]]
        for idx, similarity in enumerate(similarities, start=1):
            current_text = "\n\n".join(current)
            should_break = similarity < threshold and len(current_text) >= settings.chunk_size // 3
            too_large = len(current_text) >= settings.chunk_size
            if should_break or too_large:
                chunks.append(
                    Document(
                        page_content=current_text,
                        metadata={**document.metadata, "chunk_strategy": "semantic"},
                    )
                )
                current = [units[idx]]
            else:
                current.append(units[idx])

        if current:
            chunks.append(
                Document(
                    page_content="\n\n".join(current),
                    metadata={**document.metadata, "chunk_strategy": "semantic"},
                )
            )

    return _number_chunks(chunks, "semantic")


def _split_semantic_units(text: str) -> list[str]:
    """Create candidate units for semantic chunking.

    Implementation:
        The function prefers paragraphs because they usually contain complete
        ideas. If a paragraph is larger than the configured chunk size, it falls
        back to sentence splitting so the semantic chunker can still place
        boundaries inside long sections.

    Usage:
        This is an internal helper for `semantic_chunk_documents`; callers do
        not usually use it directly.

    How it helps other functions:
        It controls the granularity of semantic comparison. Smaller units give
        `_cosine` more boundary opportunities, while paragraph-sized units keep
        chunks coherent for retrieval and generation.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    units: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= settings.chunk_size:
            units.append(paragraph)
        else:
            units.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+", paragraph) if s.strip())
    return units


def _cosine(left: list[float], right: list[float]) -> float:
    """Measure semantic similarity between two embedding vectors.

    Implementation:
        The vectors are converted to NumPy arrays, normalized by their vector
        lengths, and compared with a dot product. A zero-vector guard returns
        `0.0` instead of raising a division error.

    Usage:
        `semantic_chunk_documents` uses this to decide whether adjacent text
        units are topically close enough to stay in the same chunk.

    How it helps other functions:
        Its similarity scores directly determine semantic breakpoints. Cleaner
        breakpoints lead to better vector search matches and less noisy context
        for the generation step.
    """
    a = np.asarray(left)
    b = np.asarray(right)
    denominator = np.linalg.norm(a) * np.linalg.norm(b)
    if denominator == 0:
        return 0.0
    return float(np.dot(a, b) / denominator)


def _number_chunks(chunks: list[Document], strategy: str) -> list[Document]:
    """Attach stable chunk-level metadata after splitting.

    Implementation:
        The function counts chunks per logical source section and creates a
        deterministic `chunk_id` using source, section/page, strategy, and
        per-source index. It also groups every N child chunks into a parent
        context block, persists parent context externally, and stores only
        `parent_id` and `parent_index` metadata on child chunks.

    Usage:
        Both `recursive_chunk_documents` and `semantic_chunk_documents` call this
        before returning chunks to the ingestion graph.

    How it helps other functions:
        `VectorStore.upsert` uses `chunk_id` as the Chroma document ID. Retrieval
        uses `parent_id` for parent-child context expansion through the external
        parent store, while prompt augmentation uses `chunk_index`, `section`,
        `page_number`, and `source` for citations and traceability.
    """
    total_by_group: dict[str, int] = {}
    grouped_chunks: dict[str, list[tuple[int, Document]]] = {}

    for chunk in chunks:
        group_key = _chunk_group_key(chunk)
        chunk_index = total_by_group.get(group_key, 0)
        total_by_group[group_key] = chunk_index + 1
        grouped_chunks.setdefault(group_key, []).append((chunk_index, chunk))

    numbered: list[Document] = []
    parent_documents: dict[str, Document] = {}
    for group_key, group_items in grouped_chunks.items():
        parent_groups = _build_parent_groups(group_items)
        for chunk_index, chunk in group_items:
            parent_index = chunk_index // settings.child_chunks_per_parent
            parent_id = f"{group_key}::{strategy}::parent::{parent_index}"
            if parent_id not in parent_documents:
                parent_documents[parent_id] = Document(
                    page_content=parent_groups[parent_index],
                    metadata={
                        **chunk.metadata,
                        "parent_id": parent_id,
                        "parent_index": parent_index,
                        "parent_chunk_count": settings.child_chunks_per_parent,
                    },
                )
            numbered.append(
                Document(
                    page_content=chunk.page_content,
                    metadata={
                        **chunk.metadata,
                        "chunk_id": f"{group_key}::{strategy}::child::{chunk_index}",
                        "chunk_index": chunk_index,
                        "chunk_strategy": chunk.metadata.get("chunk_strategy", strategy),
                        "chunk_chars": len(chunk.page_content),
                        "parent_id": parent_id,
                        "parent_index": parent_index,
                    },
                )
            )
    save_parent_documents(parent_documents)
    return numbered


def _chunk_group_key(chunk: Document) -> str:
    """Build the logical grouping key used for chunk and parent IDs.

    Implementation:
        The key combines source path, section name, and page number. This keeps
        chunks from different Markdown sections or PDF pages separate even when
        they come from the same file.

    Usage:
        `_number_chunks` calls this before assigning chunk IDs and parent IDs.

    How it helps other functions:
        Stable group keys make parent-child context deterministic and make
        re-indexing by content hash less likely to collide across sections.
    """
    source = str(chunk.metadata.get("source", "unknown"))
    section = str(chunk.metadata.get("section", "unknown-section"))
    page = str(chunk.metadata.get("page_number", "no-page"))
    safe_section = re.sub(r"[^a-zA-Z0-9_.-]+", "-", section).strip("-") or "section"
    return f"{source}::{safe_section}::page-{page}"


def _build_parent_groups(group_items: list[tuple[int, Document]]) -> dict[int, str]:
    """Create parent context text by grouping neighboring child chunks.

    Implementation:
        Child chunks are grouped by `settings.child_chunks_per_parent`. The
        parent content is the concatenation of neighboring child chunk text in
        that group.

    Usage:
        `_number_chunks` stores the returned parent text in each child's
        metadata as `parent_content`.

    How it helps other functions:
        Retrieval can match on a precise child chunk but later expand to the
        broader parent context before prompt construction.
    """
    parent_groups: dict[int, list[str]] = {}
    for chunk_index, chunk in group_items:
        parent_index = chunk_index // settings.child_chunks_per_parent
        parent_groups.setdefault(parent_index, []).append(chunk.page_content)
    return {parent_index: "\n\n".join(parts) for parent_index, parts in parent_groups.items()}
