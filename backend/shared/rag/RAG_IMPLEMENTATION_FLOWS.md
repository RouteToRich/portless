# RAG Implementation Flows
### Detailed Walkthrough of the Portless Shared RAG Module (`shared/rag/`)

This document explains every major flow in this RAG implementation. It is a
companion to `README.md`, and to `workflow_llm_gateway.md` for the generation
backend.

> **Consolidated 2026-07-29.** The former `workflow_rag.md` has been merged into
> this file — its status tables, readiness checklist, and roadmap now live in
> §39–§41, corrected against the source. There is one RAG reference document.

The module lives in `shared/rag/src/` and is mounted into the Portless backend
as the `/rag/*` FastAPI routes (`router.py`), powering the Knowledge Base UI
page and the Negotiation Agent's playbook context. A thin `practice-rag` CLI
(`cli.py`) remains for local experimentation.

The design:

```text
Ingestion:
POST /rag/upload (files persisted to MongoDB GridFS) or POST /rag/ingest (paths)
-> documents + metadata -> section/page metadata -> chunks
-> parent-child chunk metadata -> embeddings (LLM Gateway)
-> MongoDB Atlas vector store, re-indexed by content_hash
-> graph store Document/Chunk/Entity nodes (Mongo or Neo4j backend, optional)

Query:
question + optional metadata filters -> query embedding
-> Atlas $vectorSearch + BM25 keyword search + graph search + live web search (Tavily, optional)
-> reciprocal-rank hybrid fusion -> parent-child expansion
-> reranking or cross-encoder reranking -> knee-point chunk selection
-> prompt-injection filtering -> augmented prompt -> answer (LLM Gateway)
-> citation validation -> evaluation metrics
```

### Platform integration at a glance (verified against source 2026-07-29)

| Concern | Implementation |
|---|---|
| Vector store | MongoDB Atlas `$vectorSearch` (`chunks` collection; **`ensure_indexes()`** creates both the regular indexes and the Atlas vector index; `RAG_MONGO_URI` falls back to the shared `MONGO_URI`) |
| Startup wiring | `shared.rag.src.router.startup()` — called from `app.py`'s lifespan; runs `ensure_indexes()` + `ensure_graph_indexes()`, logging a warning (never raising) if either fails |
| Embeddings | `shared.llm_gateway.GatewayEmbeddings` — model/provider/key configured once via `LLM_EMBEDDING_*`; offline `LocalHashEmbeddings` fallback when no key |
| Generation | `shared.llm_gateway.LLMClient` — primary model → Groq fallback chain, retries, MongoDB call tracking; **falls back to a local extractive answer** if the whole chain fails (see §20) |
| Raw uploaded files | MongoDB **GridFS** (`rag_files` bucket) via `file_store.py` — the app's only blob store; listed at `GET /rag/files` |
| Parent documents | MongoDB `parent_docs` collection (`parent_store.py`) |
| Ingestion job status | **In-process `_jobs` dict in `router.py`** — not persisted. `RAG_INGESTION_JOBS_COLLECTION` is declared in `config.py` but currently unused (see §37) |
| Graph retrieval (optional) | `RAG_GRAPH_RETRIEVAL_ENABLED` + `RAG_GRAPH_DB_BACKEND=mongo` (same shared DB, recommended) or `neo4j` (Cypher over async driver); **config default is `neo4j`**; no-op backend when disabled/misconfigured |
| Live web search (optional) | 4th retrieval leg via Tavily (`RAG_WEB_SEARCH_ENABLED`, `TAVILY_API_KEY`); fails open to empty results — never fabricates hits; results pass the same safety filter |
| HTTP API | `POST /rag/ingest`, `POST /rag/upload`, `GET /rag/ingest/{job_id}` (async job polling), `POST /rag/query`, `DELETE /rag/documents/{source}`, `GET /rag/files`, `GET /rag/stats` — gated to Portless platform staff (`require_portless`). **`GET /rag/health` is deliberately ungated** so liveness probes work without a token |
| Consumers | Knowledge Base UI page (upload + test chat); Negotiation Agent `load_rag_context()` (with `NEGOTIATION_RAG_ENABLED` escape hatch + `RAG_TIMEOUT_SECONDS` budget, falling back to a static playbook stub); Negotiation market intel reuses the web-search adapter + safety filter |

---

## 1. High-Level Architecture

```text
                  INGESTION TIME

  POST /rag/upload (multipart files)          POST /rag/ingest (server paths)
        |                                            |
        v                                            |
  file_store.py                                      |
  persist raw bytes to MongoDB GridFS (rag_files)    |
        |                                            |
        +--------------------+-----------------------+
                             v
  router.py: background ingestion job (status in the in-process `_jobs` dict,
  polled via GET /rag/ingest/{job_id})
                             |
                             v
  loader.py
  parse files and create Document(page_content, metadata)
  add section metadata for Markdown and page_number for PDF (+ layout/OCR)
                             |
                             v
  chunking.py
  split text into child chunks and build parent context groups
  returns (child_chunks, parent_docs); the graph node awaits
  MongoParentStore().save(parent_docs) -> `parent_docs` collection
                             |
                             v
  embeddings.py -> shared.llm_gateway.GatewayEmbeddings
  embed chunk.page_content
                             |
                             v
  vector_store.py
  delete stale content_hash records, then store vector + page_content + metadata
  in the `chunks` collection (indexes created by ensure_indexes())
                             |
                             v
  graph_store.py  (optional — RAG_GRAPH_RETRIEVAL_ENABLED)
  store Document / Chunk / Entity nodes with PART_OF, MENTIONS and
  ADJACENT_TO relationships
  (backend: `mongo` graph_chunks collection, or `neo4j` via Cypher)


                   QUERY TIME  (POST /rag/query)

  user question
        |
        v
  graph.py: embed_query  (GatewayEmbeddings)
  NOTE: the resulting vector is stored in state but not consumed —
  semantic_search re-embeds the question itself (see §12)
        |
        v
  vector_store.py
  metadata-filtered Atlas $vectorSearch + in-memory BM25 keyword search
        |
        v
  graph_store.py  (optional)
  entity and relationship based graph search
        |
        v
  web_search.py  (optional — RAG_WEB_SEARCH_ENABLED)
  live Tavily search; empty on missing key/failure, never fabricated
        |
        v
  (authorization.py filters EVERY leg above before fusion)
        |
        v
  hybrid_fusion  (reciprocal rank fusion across all legs)
        |
        v
  parent-child expansion  (parent_docs collection)
        |
        v
  rerank.py
  local rerank or cross-encoder rerank + knee-point selection (min_k=3, max_k=8)
        |
        v
  security.py
  prompt-injection filtering, async: heuristic rules + optional LLM classifier
  (internal chunks AND web results)
        |
        v
  llm.py -> shared.llm_gateway.LLMClient
  build augmented prompt and generate answer (fallback chain, tracking,
  local extractive answer as last resort)
        |
        v
  citations.py + evaluation.py
  validate citations and compute faithfulness/context precision/relevance
```

---

## Latest Implemented Enhancements

Platform integration changes (2026-07, superseding the practice-era stack):

- **MongoDB Atlas vector store**: Chroma was replaced by Atlas `$vectorSearch` on the shared app database (`chunks` collection). `ensure_indexes()` creates the four regular indexes *and* attempts programmatic Atlas vector-index creation; `RAG_MONGO_URI`/`RAG_MONGO_DB_NAME` fall back to the app-wide `MONGO_URI`/`MONGO_DB_NAME`.
- **Fully async pipeline**: both LangGraph graphs are async end to end, so MongoDB and LLM Gateway calls never block the FastAPI event loop. `VectorStore`, `MongoParentStore`, `GraphStore`, `WebSearchStore`, and the safety filter are all coroutines.
- **LLM Gateway everywhere**: embeddings go through `GatewayEmbeddings` (`LLM_EMBEDDING_*` config, `LocalHashEmbeddings` offline fallback) and generation through `LLMClient` (primary model → Groq fallback chain, retries, MongoDB call tracking). RAG holds no provider SDKs or keys of its own. The optional prompt-injection classifier (`rag.security.injection_classifier`) and LLM-as-judge evaluator (`rag.evaluation.llm_judge`) route through the same gateway under their own `caller` tags.
- **FastAPI surface** (`router.py`): async ingestion jobs (`POST /rag/ingest`, `POST /rag/upload`, poll `GET /rag/ingest/{job_id}`), `POST /rag/query`, `DELETE /rag/documents/{source}`, `GET /rag/files|stats`. All of those require Portless platform staff; `GET /rag/health` is intentionally ungated for liveness probes. This powers the Knowledge Base UI page.
- **Lazy module exports** (`__init__.py`): `query`, `ingest`, the graph builders, `settings`, `router`, and `startup` are resolved through PEP 562 `__getattr__`, so importing `shared.rag.src` (or any single submodule) does not drag in LangGraph and the optional retrieval dependencies.
- **GridFS file persistence** (`file_store.py`): raw uploaded knowledge files are stored durably in MongoDB GridFS (`rag_files` bucket) — the app's only blob store; they survive restarts and can be re-ingested or audited. A `FileStore` Protocol fronts the single `MongoGridFSFileStore` implementation.
- **Pluggable graph backends** (`graph_store.py`): the local JSON graph store became `mongo` (a `graph_chunks` collection in the same shared DB — recommended, zero new infrastructure) or `neo4j` (Cypher over the async driver), selected by `RAG_GRAPH_DB_BACKEND` and gated by `RAG_GRAPH_RETRIEVAL_ENABLED`; a safe no-op backend applies when disabled or misconfigured. Both backends share one graph shape (`Document` / `Chunk` / `Entity` with `PART_OF`, `MENTIONS`, `ADJACENT_TO`) and one entity extractor, so switching backends changes no retrieval semantics.
- **Live web-search retrieval leg** (`web_search.py`): optional 4th leg (Tavily, `RAG_WEB_SEARCH_ENABLED` + `TAVILY_API_KEY`) fused by RRF with the other candidates. Fails open to an empty list — a retrieval source must never fabricate stub evidence. Web results are fetched fresh per query, never persisted, and pass the same prompt-injection safety filter.
- **Cascading delete** (`DELETE /rag/documents/{source}`): fans out concurrently (`asyncio.gather`) to the vector store, parent store, *and* graph store, so a removed source leaves no orphaned parent docs or graph nodes.
- **Negotiation Agent integration**: `agents/negotiation_agent/rag.py::load_rag_context()` calls this pipeline with a wall-clock budget (`RAG_TIMEOUT_SECONDS`) and an escape hatch (`NEGOTIATION_RAG_ENABLED=false`), falling back to a static playbook stub on any failure; the agent's per-round market intel reuses the same web-search adapter and safety filter.

> **Behaviours that commonly surprise readers** — all verified against the source on 2026-07-29. Each is expanded in the section noted:
>
> | Behaviour | Where |
> |---|---|
> | Generation **does** silently degrade to a local extractive answer when the whole gateway chain fails | §20 |
> | Ingestion job status lives in a process-local dict — lost on restart, wrong under multiple workers | §37 |
> | Only `authorized_users`, `source`, and `content_hash` are Atlas pre-filterable; a `section=`/`page_number=` filter makes `$vectorSearch` fail and return zero semantic hits | §13, §37 |
> | Graph entity extraction only matches **capitalised** tokens, so an all-lowercase question retrieves nothing from the graph leg | §15 |
> | Every `RAG_*` setting needs the `RAG_` prefix — `CHUNK_SIZE` alone is ignored | §36.4 |
> | The `embed_query` node's vector is never consumed; `semantic_search` re-embeds the question | §12 |

The following features are implemented in the modular `shared/rag/src/` flow:

- **PDF parsing with page numbers**: `.pdf` files are parsed page by page in `loader.py`, and every page document gets `page_number`.
- **PDF layout extraction**: `pdfplumber` can extract page text, tables as Markdown, and figure counts.
- **OCR fallback**: scanned PDF pages can be OCRed with `pdf2image` and `pytesseract` when enabled.
- **Markdown section metadata**: Markdown files are split by headings, and every section document gets `section`.
- **Metadata filters during retrieval**: both the HTTP API (`metadata_filter` in the request body) and `practice-rag ask --filter key=value` pass filters into semantic and keyword retrieval. Keys are auto-prefixed with `metadata.`. See §13 for the Atlas pre-filter limitation.
- **Authorization filtering**: the HTTP API accepts `user_id`/`team_id`, `practice-rag ask` accepts `--user`/`--team`, and **all four** retrieval legs are filtered by authorization metadata before fusion.
- **Parent-child retrieval**: child chunks are grouped into parent context blocks during chunking, parent text is stored outside the `chunks` collection (in `parent_docs`), and retrieval expands matched child chunks to parent context before reranking.
- **Optional cross-encoder reranking**: enable with `RAG_USE_CROSS_ENCODER_RERANKER=true`; the local reranker remains the fallback.
- **Citation validation**: generated answers are checked for `[1]`, `[2]` style citations that map to selected chunks.
- **Prompt-injection filtering**: selected chunks are scanned with a rule classifier and optional LLM classifier before prompt augmentation.
- **Deletion and re-indexing by `content_hash`**: `VectorStore.upsert()` deletes existing records with matching content hashes before inserting fresh chunks.
- **Evaluation metrics**: each query returns local heuristic metrics and can attach RAGAS, DeepEval, or LLM-as-judge evaluation notes.
- **Graph-hybrid RAG**: ingestion writes `Document` / `Chunk` / `Entity` nodes with `PART_OF`, `MENTIONS`, and `ADJACENT_TO` relationships to the configured graph backend, and query retrieval fuses graph candidates with semantic and keyword candidates.

---

## 2. LangGraph Ingestion Flow

The ingestion graph is defined in `shared/rag/src/graph.py`.

```text
parse_and_load -> chunk -> embed_and_insert -> graph_insert -> END
```

### 2.1 Ingestion State

The ingestion graph passes a shared state object between nodes:

```python
class IngestionState(TypedDict, total=False):
    input_paths: list[str]
    chunk_strategy: ChunkStrategy
    raw_documents: list[Document]
    chunks: list[Document]
    inserted_count: int
    graph_inserted_count: int
```

Meaning:

- `input_paths`: files or folders to ingest.
- `chunk_strategy`: `recursive` or `semantic`.
- `raw_documents`: loaded LangChain documents before chunking.
- `chunks`: split documents ready for vector insertion.
- `inserted_count`: number of chunks inserted into the Mongo `chunks` collection.
- `graph_inserted_count`: number of chunks inserted into the graph database.

---

## 3. Flow 1: Data Parsing and Loading

### Code Location

- `shared/rag/src/graph.py`
- `shared/rag/src/loader.py`

### Graph Node

```text
parse_and_load
```

### What Happens

The graph starts with `parse_and_load()`.

It reads the input paths from graph state:

```text
state["input_paths"]
```

If no path is passed, `load_documents([])` simply returns an empty list — there
is **no** implicit `data/` folder default. The CLI enforces this explicitly
(`practice-rag ingest` requires at least one `--path`), and the HTTP API always
supplies paths (`/rag/ingest` from the request body, `/rag/upload` from the temp
files it just materialised).

Then it calls:

```python
load_documents(paths)
```

`load_documents()` accepts:

- a single file,
- multiple files,
- a folder,
- nested folders.

It currently supports:

- `.md`
- `.txt`
- `.pdf` (page-by-page, with optional layout extraction and OCR — see §28/§29)

Unsupported files are skipped.

### Output Shape

Each file becomes **one or more** LangChain `Document`s — Markdown is split by
heading into one document per section, PDFs into one document per page, and
`.txt` into a single document:

```python
Document(
    page_content="full text from file / section / page",
    metadata={
        "source": "...",
        "file_name": "...",
        "file_type": "...",
        "title": "...",
        "section": "...",          # heading, or "Page N", or the title
        "content_hash": "...",      # SHA-256 of THIS section/page text
        "loaded_at": "...",
        "authorized_users": "*",   # default public
        "authorized_teams": "*",
        # PDFs additionally: page_number, extraction_method,
        # table_count, figure_count, has_tables, has_figures
    }
)
```

Note that `content_hash` is computed per section/page, not per file, so editing
one Markdown section re-indexes only that section's chunks.

### Why This Step Matters

This is where raw files become structured RAG inputs.

The `page_content` becomes the text that will later be chunked and embedded.

The `metadata` becomes the traceability layer that follows every chunk into the vector database.

---

## 4. Flow 2: Metadata and Content Formation

### Code Location

```text
shared/rag/src/loader.py
```

### Functions

```python
format_text_documents(path, text) -> list[Document]   # .md / .txt entry point
format_document(path, text) -> Document               # single-document helper
_format_document(path, text, section=None,
                 page_number=None, extra_metadata=None) -> Document
```

`_format_document()` is the one place every file type converges on, so Markdown,
plain text, and PDF pages all share an identical metadata contract.
`format_document()` is the thin public wrapper used by tests and callers that
have an in-memory string.

### Document Metadata Formed Here

```python
metadata = {
    "source": str(path),
    "file_name": path.name,
    "file_type": path.suffix.lower().lstrip("."),
    "title": title,                     # first H1, else prettified file stem
    "section": section or title,        # always present
    "content_hash": content_hash,       # SHA-256 of this section/page text
    "loaded_at": datetime.now(timezone.utc).isoformat(),
    "authorized_users": "*",            # public by default
    "authorized_teams": "*",
}
# page_number added when not None; extra_metadata merged last
```

Two fields are easy to miss: `section` is **always** set (falling back to the
title) rather than being PDF/Markdown-only, and the authorization defaults are
applied at load time — which is why an un-tagged corpus is readable by everyone
until you deliberately narrow it.

### Important Point

At this stage:

```text
metadata is stored with the document
metadata is not embedded
```

Only `page_content` is later embedded by default.

### Why Metadata Matters

Metadata is used for:

- citations,
- source tracking,
- filtering,
- debugging,
- deduplication,
- access control,
- freshness checks,
- reranking boosts,
- answer explainability.

### Should Page Number and Section Be Included?

Yes.

For production RAG, metadata should include:

```python
{
    "source": "...",
    "file_name": "...",
    "file_type": "...",
    "title": "...",
    "section": "Release Gates",
    "page_number": 12,
    "content_hash": "...",
    "loaded_at": "...",
}
```

In this practice project, `.md` and `.txt` files do not have real page numbers.

For PDFs, DOCX, and scanned documents, `page_number` should be extracted during parsing/loading.

For Markdown, `section` can be extracted from headings like:

```text
# Main Title
## Section
### Subsection
```

---

## 5. Flow 3: Chunking

### Code Location

```text
shared/rag/src/chunking.py
```

### Graph Node

```text
chunk
```

### Router Function

```python
chunk_documents(
    documents, embeddings, strategy
) -> tuple[list[Document], dict[str, Document]]
```

This function chooses between:

```text
recursive chunking
semantic chunking
```

**Return shape matters**: every chunking entry point returns a
`(child_chunks, parent_docs)` tuple, where `parent_docs` is a
`{parent_id: Document}` mapping. Chunking itself stays pure and CPU-bound — it
never touches MongoDB. The `chunk` graph node is what awaits
`MongoParentStore().save(parent_docs)` before the chunks move on to embedding.
Treating the return value as a plain list is the most common integration
mistake here.

---

## 6. Flow 4: Recursive Character Text Splitting

### Function

```python
recursive_chunk_documents(documents)
```

### What Happens

The project uses LangChain's `RecursiveCharacterTextSplitter`.

It tries separators in this order:

```python
[
    "\n## ",
    "\n### ",
    "\n\n",
    "\n",
    ". ",
    " ",
    ""
]
```

This means it tries to split by:

- Markdown section headings,
- Markdown subsection headings,
- paragraphs,
- lines,
- sentence-like boundaries,
- words,
- characters.

### Why This Is Useful

Recursive splitting is a strong default because it is:

- deterministic,
- fast,
- simple,
- cheap,
- good enough for most structured text.

### Output

The output is a list of smaller `Document` chunks.

Each chunk still carries original document metadata.

Then `_number_chunks()` adds chunk-specific metadata.

---

## 7. Flow 5: Semantic Chunking

### Function

```python
semantic_chunk_documents(documents, embeddings)
```

### What Happens

Semantic chunking tries to split by meaning, not only by character count.

The flow is:

```text
document text
  -> split into semantic units
  -> embed each unit
  -> compare adjacent unit embeddings
  -> find similarity drops
  -> create chunk boundary
```

### Step 1: Split into Semantic Units

The helper `_split_semantic_units()` first splits by paragraphs.

If a paragraph is too large, it falls back to sentence splitting.

### Step 2: Embed Units

Each unit is embedded:

```python
vectors = embeddings.embed_documents(units)
```

### Step 3: Compare Neighboring Units

The function compares adjacent vectors:

```python
similarities = [
    _cosine(vectors[i], vectors[i + 1])
    for i in range(len(vectors) - 1)
]
```

### Step 4: Find Breakpoint Threshold

It computes a percentile threshold:

```python
threshold = np.percentile(similarities, settings.semantic_breakpoint_percentile)
```

If similarity drops below this threshold, the code treats that point as a topic change.

### Step 5: Create Chunks

The function creates a new chunk when:

```python
should_break = similarity < threshold and len(current_text) >= settings.chunk_size // 3
too_large    = len(current_text) >= settings.chunk_size
```

The `chunk_size // 3` guard on the semantic break is the part worth
remembering: a similarity dip alone is not enough to cut. Without that minimum,
a document with choppy short paragraphs would shatter into many tiny chunks and
destroy retrieval precision.

### Why This Is Useful

Semantic chunking helps when:

- paragraphs are too long,
- one file contains multiple topics,
- headings are missing,
- recursive splitting cuts through meaningful sections,
- retrieval quality matters more than ingestion cost.

### Trade-Off

Semantic chunking requires embedding calls during ingestion.

It is usually slower and more expensive than recursive splitting.

---

## 8. Flow 6: Chunk Metadata Formation

### Code Location

```text
shared/rag/src/chunking.py
```

### Function

```python
_number_chunks(chunks, strategy) -> tuple[list[Document], dict[str, Document]]
```

### What Happens

Chunks are first bucketed by a **group key** so numbering restarts per
source/section/page rather than running globally across a file:

```python
group_key = f"{source}::{safe_section}::page-{page}"
# safe_section = section with non [A-Za-z0-9_.-] runs replaced by "-"
```

Each chunk then gets chunk-level metadata:

```python
{
    **chunk.metadata,
    "chunk_id": f"{group_key}::{strategy}::child::{chunk_index}",
    "chunk_index": chunk_index,
    "chunk_strategy": chunk.metadata.get("chunk_strategy", strategy),
    "chunk_chars": len(chunk.page_content),
    "parent_id": f"{group_key}::{strategy}::parent::{parent_index}",
    "parent_index": chunk_index // settings.child_chunks_per_parent,
}
```

### Final Chunk Shape

Each chunk looks like:

```python
Document(
    page_content="chunk text",
    metadata={
        "source": "...",
        "file_name": "...",
        "file_type": "...",
        "title": "...",
        "section": "Retrieval Flow",
        "content_hash": "...",
        "loaded_at": "...",
        "authorized_users": "*",
        "authorized_teams": "*",
        "chunk_id": "design.md::Retrieval-Flow::page-no-page::recursive::child::4",
        "chunk_index": 4,
        "chunk_strategy": "recursive",
        "chunk_chars": 842,
        "parent_id": "design.md::Retrieval-Flow::page-no-page::recursive::parent::1",
        "parent_index": 1,
    }
)
```

The corresponding parent document carries the same metadata plus
`parent_chunk_count` (the configured `RAG_CHILD_CHUNKS_PER_PARENT` fan-in).

Because the section name is baked into `chunk_id`, renaming a heading produces
new chunk IDs. Stale rows are still cleaned up correctly — deletion keys off
`content_hash`, not `chunk_id` (§10).

### Important Point

Every chunk has metadata.

That metadata is stored with the chunk in the Mongo `chunks` collection.

By default, only `chunk.page_content` is embedded.

### What Parent-Child Chunk Metadata Means

Parent-child chunk metadata means each small retrieval chunk keeps a link back to a larger parent context block.

In this project:

- **Child chunk**: the smaller chunk inserted into the `chunks` collection and embedded for precise search.
- **Parent chunk**: a larger grouped context block stored in the `parent_docs` collection (the parent store).
- **Metadata link**: fields such as `parent_id`, `parent_index`, and `child_index` connect the child chunk to its parent context.

Example child chunk metadata:

```python
{
    "chunk_id": "design.md::Retrieval-Flow::page-no-page::recursive::child::4",
    "chunk_index": 4,
    "parent_id": "design.md::Retrieval-Flow::page-no-page::recursive::parent::1",
    "parent_index": 1,
    "source": "design.md",
    "section": "Retrieval Flow",
}
```

(There is no separate `child_index` field — `chunk_index` serves that role, and
`parent_index` is simply `chunk_index // RAG_CHILD_CHUNKS_PER_PARENT`.)

Why this helps:

1. Retrieval stays precise because embeddings are created from small child chunks.
2. The system uses `parent_id` after retrieval to load the larger parent text.
3. Reranking and generation receive enough surrounding context without embedding very large chunks.

So the vector database stores the child chunk text plus metadata, while the parent store keeps the bigger parent text. During query time, `parent_child_expand()` uses the metadata link to replace or enrich the precise child match with broader parent context.

---

## 9. Flow 7: Embedding

### Code Location

```text
shared/rag/src/embeddings.py
```

### Main Function

```python
get_embeddings()
```

This chooses the embedding backend.

### Gateway Mode (current implementation)

`get_embeddings()` delegates entirely to the **LLM Gateway**:

```python
from shared.llm_gateway.embeddings import GatewayEmbeddings
return GatewayEmbeddings()
```

The RAG module holds no embedding-model config or provider SDK of its own —
model, provider, key, and vector dimensions are configured once in
`shared/llm_gateway/.env`:

```env
LLM_EMBEDDING_PROVIDER=openai
LLM_EMBEDDING_MODEL=text-embedding-3-small
LLM_EMBEDDING_API_KEY=...
LLM_EMBEDDING_DIMENSIONS=1536   # must match the Atlas vector index numDimensions
```

### Offline Fallback

When no embedding key is configured, the gateway falls back to
`LocalHashEmbeddings` — a deterministic local vectorizer that keeps the whole
pipeline runnable in dev/CI without any API key. It is not a semantic model;
retrieval quality is placeholder-grade in this mode.

### What Gets Embedded?

By default:

```text
chunk.page_content is embedded
chunk.metadata is stored but not embedded
```

### Should Metadata Be Embedded?

Usually, metadata should not be blindly embedded.

Better approach:

- Store metadata separately for filtering and citations.
- Embed selected metadata only when it improves semantic retrieval.

Example:

```text
Title: RAG Architecture Notes
Section: Retrieval

<chunk text>
```

Good metadata to include in embedding text:

- title,
- section heading,
- subsection heading,
- document type,
- short summary.

Bad metadata to embed:

- long file paths,
- UUIDs,
- timestamps,
- access-control labels,
- raw hashes.

---

## 10. Flow 8: Insert into Vector DB

### Code Location

```text
shared/rag/src/vector_store.py
```

### Function

```python
VectorStore.upsert(chunks)
```

### What Happens

The ingestion graph calls:

```python
vector_store.upsert(chunks)
```

Inside `upsert()` the chunks are embedded via the LLM Gateway and written to
the **MongoDB Atlas `chunks` collection** — one document per chunk:

```text
{
  "chunk_id":   "...",          # unique id (upsert key)
  "embedding":  [0.01, ...],    # vector, searched by $vectorSearch
  "content":    "chunk text",
  "metadata":   { ... }         # source, section, page_number, parent_id, ...
}
```

Writes go through `bulk_write` with one `ReplaceOne(..., upsert=True)` per
`chunk_id`, so re-ingesting is idempotent. The returned count is
`upserted_count + modified_count`.

Before insert, existing records with the same `content_hash` are deleted
(re-index on document change).

### Index Creation — `ensure_indexes()`

The function is called `ensure_indexes()` (not `ensure_vector_index()`), lives in
`vector_store.py`, and is invoked from `router.startup()` during the app
lifespan. It is idempotent and safe on every boot. It creates two different
kinds of index:

**1. Regular MongoDB indexes**

```text
chunk_id_unique      unique on chunk_id           (the upsert key)
content_hash_idx     on metadata.content_hash     (re-index deletes)
source_idx           on metadata.source           (delete-by-source)
content_text_idx     text index on content
```

**2. The Atlas Vector Search index** (`vector_index`, cosine similarity,
`numDimensions` = `LLM_EMBEDDING_DIMENSIONS`), created via the
`createSearchIndexes` command:

```json
{ "fields": [
    { "type": "vector", "path": "embedding",
      "numDimensions": 1536, "similarity": "cosine" },
    { "type": "filter", "path": "metadata.authorized_users" },
    { "type": "filter", "path": "metadata.source" },
    { "type": "filter", "path": "metadata.content_hash" }
]}
```

**Only those three `filter` paths are pre-filterable at query time.** That is a
hard constraint on `metadata_filter` — see §13.

Programmatic creation of the vector index requires an Atlas tier that permits
it. On free-tier clusters the command fails; `ensure_indexes()` logs a warning
with the full index definition to paste into the Atlas UI and **continues
startup** rather than raising. `GET /rag/health` reports index presence, though
it currently checks only for the regular `chunk_id_unique` index, so a healthy
response does not by itself prove the *vector* index exists.

### Important Point

The embedding is computed explicitly through `GatewayEmbeddings` before the
Mongo write. The metadata is stored as structured fields, not as vector values.
`_serialise_metadata()` coerces any non-`str/int/float/bool/list` value to a
string first, so `Path` objects and similar never reach MongoDB.

### Graph DB Insert After Vector Insert

When graph retrieval is enabled (`RAG_GRAPH_RETRIEVAL_ENABLED=true`), the
ingestion graph then calls:

```python
GraphStore.upsert_chunks(chunks)
```

This writes the same chunks into the configured graph backend
(`RAG_GRAPH_DB_BACKEND`):

```text
mongo  -> `graph_chunks` collection in the same shared MongoDB (recommended;
          no extra service or credentials)
neo4j  -> Cypher over the async Neo4j driver (RAG_GRAPH_DB_URI/USER/PASSWORD)
arangodb / falkordb -> reserved names; currently resolve to the no-op store
no-op  -> when disabled or misconfigured: skips ingestion, returns empty
          retrieval results, and the rest of the pipeline is unaffected
```

**Config default is `neo4j`.** Setting only `RAG_GRAPH_RETRIEVAL_ENABLED=true`
without also setting `RAG_GRAPH_DB_BACKEND` leaves you on the Neo4j path, which
additionally requires `RAG_GRAPH_DB_URI` — with no URI the resolver logs a
warning and hands back the no-op store. For the zero-infrastructure path set
both:

```env
RAG_GRAPH_RETRIEVAL_ENABLED=true
RAG_GRAPH_DB_BACKEND=mongo
```

### Actual Graph Schema

Both real backends store the same shape:

```text
Nodes:
  (:Document {source, title})
  (:Chunk    {chunk_id, content, source, section, page_number})
  (:Entity   {name})

Edges:
  (Chunk)-[:PART_OF]->(Document)
  (Chunk)-[:MENTIONS]->(Entity)
  (Chunk)-[:ADJACENT_TO]->(Chunk)   -- consecutive chunks of the same source
```

Section, page number, and parent ID are **properties on the `Chunk` node**, not
separate nodes with their own relationships. Only entity mentions and
document/adjacency links are true edges.

Example:

```text
(chunk:abc)-[:PART_OF]->(document:security-policy.pdf)
(chunk:abc)-[:MENTIONS]->(entity:veracode)
(chunk:abc)-[:ADJACENT_TO]->(chunk:abd)
```

The Mongo backend models this as one document per chunk with array fields
(`entities: [str]`, `adjacent_to: [chunk_id]`) rather than separate nodes and
edges. That is sufficient because the only traversal this pipeline performs is a
single-hop shared-entity count, which `$setIntersection` answers natively — no
`$graphLookup` is needed unless multi-hop traversal is added later. Its indexes
(`graph_chunk_id_unique`, `graph_entities_idx`, `graph_source_idx`) are created
by `ensure_graph_indexes()`, which no-ops unless the backend is `mongo`.

The graph DB does not replace the vector DB. It adds a relationship index that query-time graph search can use alongside vector and keyword search.

---

## 11. LangGraph Query Flow

The query graph is defined in `shared/rag/src/graph.py`.

```text
embed_query
  -> semantic_search
  -> keyword_search
  -> graph_search
  -> web_search
  -> hybrid_fusion
  -> parent_child_expand
  -> rerank
  -> knee_point_select
  -> safety_filter
  -> augment
  -> generate
  -> validate_citations
  -> evaluate
  -> END
```

### Query State

All nodes run sequentially — the four retrieval legs are chained edges, not
parallel branches, so a slow web-search leg adds latency to the whole query.

```python
class QueryState(TypedDict, total=False):
    question: str
    metadata_filter: dict[str, Any]
    user_id: str
    team_id: str
    query_embedding: list[float]
    semantic_results: list[ScoredDocument]
    keyword_results: list[ScoredDocument]
    graph_results: list[ScoredDocument]
    web_results: list[ScoredDocument]
    hybrid_chunks: list[ScoredDocument]
    parent_chunks: list[ScoredDocument]
    reranked_chunks: list[ScoredDocument]
    selected_chunks: list[Document]
    augmented_prompt: str
    answer: str
    citation_validation: CitationValidation
    evaluation_metrics: EvaluationMetrics
```

---

## 12. Flow 9: Embedding the Query

### Graph Node

```text
embed_query
```

### What Happens

The user question is embedded:

```python
embeddings.embed_query(state["question"])
```

### Known Redundancy

The vector this node writes to `state["query_embedding"]` is **never read**.
`VectorStore.semantic_search()` takes the raw question string and calls
`embed_query()` again internally, so every query pays for two embedding calls.
It is correct but wasteful — with a remote embedding provider it doubles the
embedding latency and cost of the retrieval phase; with `LocalHashEmbeddings`
it is free. Either have `semantic_search()` accept a precomputed vector or drop
the node; until then, do not assume `query_embedding` is what Atlas actually
searched with.

### Why This Matters

Semantic search needs the query and chunks to be in the same vector space.

This means:

```text
chunk text embedding
query embedding
```

can be compared using vector similarity.

---

## 13. Flow 10: Semantic Search

### Graph Node

```text
semantic_search
```

### Code Location

```text
shared/rag/src/vector_store.py
```

### What Happens

The project runs an **Atlas `$vectorSearch` aggregation** on the `chunks` collection:

```python
pipeline = [
    {"$vectorSearch": {
        "index": settings.vector_index_name,
        "path": "embedding",
        "queryVector": query_embedding,
        "numCandidates": max(settings.retrieval_num_candidates, top_k * 10),
        "limit": top_k,
        # optional: "filter": <Atlas pre-filter built from metadata_filter>
    }},
    {"$project": {"chunk_id": 1, "content": 1, "metadata": 1,
                  "score": {"$meta": "vectorSearchScore"}}},
]
```

This returns chunks that are semantically close to the question.

### Metadata Pre-Filter Limitation (important)

If a metadata filter is supplied it is applied as an Atlas **pre-filter**, and
Atlas only accepts fields declared as `filter` type in the vector index. In this
deployment that is exactly three paths (§10):

```text
metadata.authorized_users
metadata.source
metadata.content_hash
```

Filtering on anything else — `section`, `page_number`, `file_type`, `hs_code` —
makes the `$vectorSearch` aggregation fail. The failure is caught and logged,
and the method **returns an empty list**, so the symptom is not an error
response but a query that silently loses its entire semantic leg and answers
from keyword/graph hits alone.

Two consequences worth internalising:

1. `practice-rag ask --filter section=Retrieval` still works overall, because
   `keyword_search()` uses a regular MongoDB filter (which can match any
   metadata field) — but the results are BM25-only.
2. The Negotiation Agent calls `query(..., metadata_filter={"hs_code": hs_code})`.
   `hs_code` is neither an indexed filter path nor a field any loader writes, so
   that call currently contributes no semantic hits and typically falls through
   to the static playbook stub. See §37.

To filter semantically on a new field, add it as a `filter` path in
`_VECTOR_INDEX_DEFINITION` and rebuild the Atlas index.

Detailed runtime flow:

1. The query text is embedded via `GatewayEmbeddings` — the same model used for chunk embeddings.
2. Atlas computes vector similarity between the query embedding and stored chunk embeddings (`numCandidates` ≈ 10–20× top-K for good recall).
3. Optional metadata pre-filters narrow the candidate pool before ranking.
4. Top-K nearest neighbors are returned with `vectorSearchScore` relevance scores.
5. Results are wrapped as `ScoredDocument(..., source="semantic")`.
6. Authorization filtering is applied in the query graph before fusion.

There is no exact keyword matching requirement in this branch. A chunk can be returned even when it does not share exact tokens with the query, as long as the semantic meaning is close in embedding space.

Example:

```text
Query: "How do we block production release when severe security issues exist?"
Possible semantic hit: "Go/No-Go gate fails when critical findings remain open."
```

### How Similarity Is Computed

Conceptually:

```text
chunk_text --embed--> chunk_vector
query_text --embed--> query_vector
similarity(query_vector, chunk_vector) -> relevance score
```

Higher similarity means the chunk meaning is closer to the question intent.

### Why It Is Needed

Semantic retrieval is what enables robust recall for:

1. paraphrased questions,
2. synonym-heavy wording,
3. conceptual questions where exact vocabulary differs,
4. natural-language phrasing that does not include internal identifier names.

### Best For

Semantic search is good for:

- meaning-based questions,
- paraphrased queries,
- conceptual retrieval,
- questions where exact words differ from source text.

Metadata filters are good for narrowing by:

- `section`,
- `page_number`,
- `file_type`,
- `file_name`,
- any other stored metadata key.

### Output

```python
ScoredDocument(
    document=Document(...),
    score=0.83,
    source="semantic"
)
```

### How It Fits Into Hybrid Retrieval

`semantic_search` is one branch of retrieval. Its candidates are fused with:

1. BM25 keyword candidates,
2. graph-retrieval candidates (optional),
3. live web-search candidates (optional).

Fusion strengthens chunks that are supported by multiple retrieval signals and reduces dependence on any single retriever.

---

## 14. Flow 11: Keyword Search

### Graph Node

```text
keyword_search
```

### Code Location

```text
shared/rag/src/vector_store.py
```

### What Happens

The code:

1. Reads stored chunk text and metadata from the Mongo `chunks` collection (optionally pre-filtered by metadata; capped at `RAG_KEYWORD_SEARCH_FETCH_LIMIT`, default 500 documents).
2. Tokenizes all chunk text with a simple lowercase regex tokenizer.
3. Builds an in-memory BM25 index.
4. Tokenizes the query with the same tokenizer.
5. Scores chunks by lexical relevance (BM25).
6. Returns top positive matches (`score > 0`) as `source="keyword"`.

There is no separate NLP model that first "extracts keywords." In this implementation, query keywords are simply the tokens produced from the query text.

Example:

```text
Query: "How to fix SQL injection in OrderRepository"
Tokens: [how, to, fix, sql, injection, in, orderrepository]
```

BM25 naturally gives stronger contribution to informative terms (for example `sql`, `injection`, `orderrepository`) than very common words.

### Why Chunks Must Be Persisted With Embeddings

Even when using a vector DB, embeddings alone are not enough. The system must keep chunk text + metadata alongside vectors because:

1. BM25 keyword search runs on raw chunk text.
2. Prompt augmentation needs human-readable context text.
3. Citation rendering and validation need source metadata.
4. Parent-child expansion needs IDs and relationship metadata.
5. Authorization and metadata filters need stored metadata fields.

In short:

```text
Vector similarity needs embeddings.
Keyword search and grounded responses need chunk text + metadata.
```

### Best For

Keyword search is good for:

- exact terms,
- product names,
- error codes,
- CVE IDs,
- API names,
- class names,
- acronyms.

### Why It Is Needed

Embeddings may blur exact tokens.

For example, a semantic model might understand "vulnerability scanner", but keyword search is better for exact terms like:

```text
CVE-2026-1234
Rapid7
Black Duck
chunk_id
```

### How It Fits Into Hybrid Retrieval

`keyword_search` is one branch of retrieval. The query graph combines:

1. semantic search results,
2. keyword/BM25 results,
3. graph relationship results (optional),
4. live web-search results (optional).

These are merged in `hybrid_fusion` (reciprocal-rank style), then sent to parent-child expansion, reranking, knee-point selection, and safety filtering before generation.

---

## 15. Flow 12: Graph DB Search

### Graph Node

```text
graph_search
```

### Code Location

```text
shared/rag/src/graph_store.py
shared/rag/src/graph.py
```

### Function

```python
GraphStore.retrieve(query, top_k, metadata_filter)
```

### What Happens

Graph search adds relationship-aware retrieval.

The flow is:

```text
query
  -> extract query entities  (_extract_entities)
  -> match chunks that MENTION any of those entities
  -> score by shared-entity count / number of query entities
  -> apply the `source` metadata filter (the only one supported here)
  -> apply authorization filters in graph.py
  -> return graph ScoredDocument candidates
```

Detailed runtime flow:

1. The query is run through `_extract_entities()` (see below).
2. If it yields **no** entities the store returns `[]` immediately — there is
   nothing to traverse, and vector/keyword carry the query.
3. Chunks mentioning any query entity are matched — a single hop over
   `MENTIONS`. `PART_OF` and `ADJACENT_TO` are written at ingestion time but are
   **not** traversed by the current retrieval query; they exist for future
   multi-hop expansion.
4. Each candidate is scored:

   ```python
   score = min(shared_entity_count / len(query_entities), 1.0)
   ```

   This is a plain overlap ratio. There is **no** inverse-entity-frequency (IDF)
   weighting — a rare, high-signal entity currently counts exactly as much as a
   common one. Adding IDF would be a genuine precision improvement.
5. Only `source` is honoured as a metadata filter (read from either `source` or
   `metadata.source`); other keys are ignored on this leg.
6. Authorization filtering is applied in `graph.py`, as for every other leg.
7. Results are returned as `ScoredDocument(..., source="graph")`, capped at
   `RAG_GRAPH_RETRIEVAL_TOP_K` (default 8).

### Entity Extraction — and its case-sensitivity trap

`_extract_entities()` is a deliberately dependency-free heuristic shared by both
backends, so the same phrase maps to the same entity string in a chunk and in a
query:

```python
_ENTITY_RE = r"\b([A-Z][A-Za-z]{2,})\b"   # Capitalised words, 3+ chars
_CODE_RE   = r"\b(\d{4,10})\b"            # numeric codes (HS codes, IDs)
# minus a stopword list, deduplicated, lowercased, capped at 25 entities
```

**The regex requires an initial capital letter.** A question typed in all
lowercase — `"what does the black duck scanner check?"` — extracts zero
word-entities and the graph leg returns nothing at all. Numeric codes still
match, since digits have no case. This is the single most likely reason for
"graph retrieval is enabled but never contributes anything."

Swap `_extract_entities` for spaCy or an LLM NER call for production-grade
recall; no other code needs to change.

### Why It Is Needed

Graph retrieval finds evidence that vector similarity and BM25 can both miss, especially when a query implies connected context instead of direct term overlap.

It is particularly useful for:

1. multi-hop context (entity in one chunk, remediation in another),
2. section/page/source-linked evidence,
3. parent-child relationship expansion paths,
4. related chunks that share the same domain entity but use different wording.

### Scoring Intuition

Graph scoring is relationship-driven, not embedding-distance-driven:

```text
more shared entities with the query => higher graph score
```

Note this is unweighted overlap in the current implementation — every entity
counts the same, whether it is `Veracode` or `Report`. The stopword list is the
only defence against common words dominating.

### Best For

Graph search is useful for:

- related chunks that share the same entity,
- shared section, page, source, or parent relationships,
- technical identifiers,
- concepts spread across multiple chunks,
- questions where semantic similarity alone misses related evidence.

### Example

If the query mentions `Black Duck`, extraction yields `["black", "duck"]` and
graph search retrieves every chunk that `MENTIONS` either entity:

```text
entity:black -> chunk:abc, chunk:def
entity:duck  -> chunk:abc
=> chunk:abc scores 2/2 = 1.0, chunk:def scores 1/2 = 0.5
```

Written lowercase as `black duck`, the same query extracts nothing and this leg
returns `[]`.

Those graph candidates are fused with vector and keyword candidates.

### How It Fits Into Hybrid Retrieval

`graph_search` adds relationship evidence into `hybrid_fusion` alongside semantic and keyword candidates. This improves recall for cross-chunk, connected facts before reranking and knee-point selection decide final prompt context.

---

## 15b. Flow 12b: Live Web Search (4th Retrieval Leg)

### Graph Node

```text
web_search
```

### Code Location

```text
shared/rag/src/web_search.py
```

### What Happens

When `RAG_WEB_SEARCH_ENABLED=true` and `TAVILY_API_KEY` is set (root `.env`,
like the other third-party connector keys), the query is POSTed to
`https://api.tavily.com/search` (`search_depth: "basic"`, `include_answer:
false`, 10 s timeout) and the top `RAG_WEB_SEARCH_TOP_K` (default 3) results are
wrapped as `ScoredDocument(..., source="web")`. Results with empty `content` are
dropped.

The key is read from the process environment (`os.getenv`) at construction
time — it is deliberately *not* a `RAG_*` setting, so it lives alongside the
other third-party connector credentials.

### Result Metadata Shape

Web results are given synthetic metadata so they render through the same
citation machinery as internal chunks:

```python
{
    "source":         "<result url>",
    "title":          "<result title>" or "untitled",
    "section":        "web",
    "page_number":    "n/a",
    "chunk_id":       "web-<idx>-<8 hex chars of url hash>",
    "chunk_index":    idx,
    "published_date": "<tavily published_date, may be empty>",
}
```

The synthetic `chunk_id` is what lets RRF deduplicate web results against each
other. Note there is no `parent_id`, so `expand_parent_context()` finds no
parent document and keeps the web snippet's own text (§16). Web chunks also
carry no `authorized_users`/`authorized_teams`, which means the authorization
filter treats them as public — correct for public web content, but worth knowing
if you tighten the default policy.

### Degradation Rule (different from booking connectors)

Unlike `shared/connectors/` (SGS, Cogoport, …), this leg does **NOT** return a
fabricated stub when the key is absent — a fake "search result" would be
presented to the LLM as real retrieved evidence and could be cited in the
answer. Silence (an empty list) is the only safe degradation for a retrieval
source. Real HTTP failures degrade to an empty list for the same reason.

### Safety and Persistence

Web results are fetched fresh per query and never written to the vector
store. They flow through the same fusion → parent-expand → rerank →
knee-point → **prompt-injection safety filter** pipeline as internal chunks,
so instruction-like text on a web page is stripped before it can reach the
LLM prompt.

### Reuse

The Negotiation Agent's per-round market intelligence
(`agents/negotiation_agent/market_intel.py`) reuses this same adapter and
safety filter for its live market-color fetch.

---

## 16. Flow 13: Hybrid Retrieval

### Graph Node

```text
hybrid_fusion  (called from fuse_results node in graph.py)
```

### Code Locations

```text
shared/rag/src/vector_store.py  ->  hybrid_fusion()
shared/rag/src/graph.py         ->  fuse_results()  (the LangGraph node)
```

### Function Signature

```python
hybrid_fusion(
    semantic_results: list[ScoredDocument],
    keyword_results:  list[ScoredDocument],
    top_k: int,
    rrf_k: int = 60,
    graph_results: list[ScoredDocument] | None = None,
    web_results: list[ScoredDocument] | None = None,
) -> list[ScoredDocument]
```

---

### How It Works Internally

#### Step 1 — Collect ranked lists from all retrievers

Before fusion, up to four independent retrieval branches have already run:

```text
semantic_results  -> ranked by Atlas $vectorSearch similarity score
keyword_results   -> ranked by BM25 lexical score
graph_results     -> ranked by relationship / entity-overlap score (optional)
web_results       -> ranked by Tavily search relevance (optional)
```

Each list contains up to `top_k` `ScoredDocument` objects, each with its own
score and source label (`"semantic"`, `"keyword"`, `"graph"`, `"web"`).

#### Step 2 — Assign a stable document identity key

Every document needs a stable ID so the same chunk found by multiple retrievers
can be recognised and merged.

```python
def _doc_id(document):
    return (
        document.metadata.get("chunk_id")     # preferred: set at ingestion time
        or document.metadata.get("source")    # fallback: file/source name
        or hash(document.page_content)        # last resort: content hash
    )
```

This means two `ScoredDocument` objects from different retrievers that point to
the same chunk will produce the same key and will be collapsed into one fused entry.

#### Step 3 — Compute RRF score for every document in every list

The algorithm iterates over all four ranked lists and accumulates a score for
each unique document using **Reciprocal Rank Fusion (RRF)**:

```python
fused_scores[doc_id] += 1.0 / (rrf_k + rank)
```

`rank` starts at 1 (best) for each list. `rrf_k` defaults to `60`.

Why `rrf_k = 60`?  It is a smoothing constant that prevents the rank-1 document
from dominating too strongly. With `rrf_k = 60`:

```text
rank 1  ->  1 / (60 + 1)  =  0.01639
rank 2  ->  1 / (60 + 2)  =  0.01613
rank 5  ->  1 / (60 + 5)  =  0.01538
rank 10 ->  1 / (60 + 10) =  0.01429
rank 50 ->  1 / (60 + 50) =  0.00909
```

The contribution drops gradually, so even rank-10 candidates still count.

#### Step 4 — Accumulate across retrievers (deduplication bonus)

If the same chunk appears in more than one ranked list, its score receives
contributions from each appearance.

Worked example with three retrievers:

```text
Chunk A  appears as rank-1 in semantic,  rank-3 in keyword,  absent in graph
Chunk B  appears as rank-2 in semantic,  absent in keyword,  rank-1 in graph
Chunk C  appears absent  in semantic,    rank-1 in keyword,  rank-2 in graph

Chunk A fused score = 1/(60+1) + 1/(60+3)          = 0.01639 + 0.01587 = 0.03226
Chunk B fused score = 1/(60+2) + 1/(60+1)           = 0.01613 + 0.01639 = 0.03252
Chunk C fused score = 1/(60+1) + 1/(60+2)           = 0.01639 + 0.01613 = 0.03252
```

Chunk B and C both outscore Chunk A even though Chunk A was rank-1 in semantic,
because B and C had agreement across two retrievers.

This is the core property of RRF: **cross-retriever agreement lifts rank**.

#### Step 5 — Sort and select top-K fused candidates

```python
ranked = sorted(fused_scores.items(), key=lambda item: item[1], reverse=True)[:top_k]
```

Only the top `top_k` unique chunks survive into the next stage.

**Important**: the `fuse_results` graph node widens the candidate pool before
passing to reranking:

```python
top_k = settings.retrieval_top_k * 2
```

This gives the reranker a broader set to choose from, improving final
answer quality without blowing up prompt cost (the knee-point selector
narrows it back down after reranking).

#### Step 6 — Return fused ScoredDocuments

Each surviving chunk is returned with:

```python
ScoredDocument(
    document = <original Document from whichever retriever last saw it>,
    score    = <accumulated RRF fused score>,
    source   = "hybrid"
)
```

The original embedding or BM25 scores are discarded at this point — only the
RRF rank-based fused score is carried forward.

---

### Visual Summary

```text
semantic_results  [ranked by cosine similarity]  ─────┐
keyword_results   [ranked by BM25 score]         ─────┤
graph_results     [ranked by entity overlap]     ─────┤──► hybrid_fusion (RRF)
web_results       [ranked by Tavily relevance]   ─────┘         │
                                                                 ▼
                                          fused + deduplicated top-(K×2) chunks
                                                                 │
                                                         parent_child_expand
                                                                 │
                                                            rerank
                                                                 │
                                                       knee_point_select
                                                                 │
                                                          safety_filter
                                                                 │
                                                         final context chunks
                                                         → augment → generate
```

---

### Why This Approach

| Property | RRF behaviour |
|---|---|
| Different score scales | RRF uses rank position, not raw scores, so BM25 scores and cosine similarity scores never need to be normalised to the same scale |
| Deduplication | Same chunk found by two retrievers gets a combined score; no duplicates in the prompt |
| Robustness | No single retriever can dominate; recall improves across query types |
| Simplicity | No learned weights; one hyperparameter (`rrf_k`) |

### Why This Helps

Hybrid retrieval improves recall.

It catches:

- chunks that match by meaning,
- chunks that match by exact words,
- chunks related through graph entities or metadata relationships,
- chunks that multiple retrievers agree are important.

### Output

```python
ScoredDocument(
    document=Document(...),
    score=0.0317,
    source="hybrid"
)
```

### Parent-Child Expansion After Hybrid Retrieval

Graph node:

```text
parent_child_expand
```

Code location:

```text
shared/rag/src/vector_store.py
```

Function:

```python
expand_parent_context(candidates)
```

What happens:

1. Retrieval finds precise child chunks.
2. Each child chunk carries a `parent_id` in its metadata (falling back to
   `chunk_id` when absent, as with web results).
3. The function deduplicates by `parent_id`, keeping only the **first**
   (highest-fused-score) candidate of each parent group and discarding the rest.
4. `MongoParentStore().load(parent_id)` fetches the parent text from the
   `parent_docs` collection. If no parent document exists, the child's own
   content is kept unchanged — never an error.
5. The parent's metadata is merged over the child's, and
   `expanded_from_child_id` records which child matched.
6. The result is re-emitted as `ScoredDocument(..., source="parent_child")`
   carrying the original fused score.

Note the deduplication happens *before* reranking, so if three sibling chunks of
one parent all rank highly, they collapse into a single context block — this is
what keeps the prompt from repeating near-identical text. It also means the
candidate count entering `rerank` is often smaller than the `top_k × 2` that
fusion emitted.

Why this helps:

- child chunks keep retrieval precise,
- parent chunks give the LLM enough surrounding context,
- prompt quality improves without embedding very large chunks.

---

## 17. Flow 14: Top-K

### Where It Is Configured

```text
shared/rag/src/config.py
```

Default:

```python
retrieval_top_k = 8
```

### How It Is Used

Semantic search retrieves top `k` (`RAG_RETRIEVAL_TOP_K`, default 8).

Keyword search retrieves top `k` — but only after fetching up to
`RAG_KEYWORD_SEARCH_FETCH_LIMIT` (500) documents to build the in-memory BM25
index, so on a corpus larger than that its recall is bounded by whichever 500
documents MongoDB happens to return.

Graph search retrieves `RAG_GRAPH_RETRIEVAL_TOP_K` (default 8).

Web search retrieves `RAG_WEB_SEARCH_TOP_K` (default 3).

Hybrid fusion keeps a wider pool:

```python
top_k=settings.retrieval_top_k * 2      # 16 by default
```

Parent-child expansion then collapses siblings sharing a `parent_id`, so the
reranker usually sees fewer than 16, and knee-point selection finally clamps the
prompt to 3–8 chunks.

### Why Not Send Top-K Directly to the LLM?

Top-k retrieval is only the first filter.

Some retrieved chunks may still be noisy.

That is why the next steps are:

```text
reranking -> knee-point selection
```

---

## 17b. Flow 14b: Reranking

### Graph Node

```text
rerank
```

### Code Location

```text
shared/rag/src/rerank.py  ->  rerank(), cross_encoder_rerank(), local_rerank()
```

### Why Reranking Is Needed

Hybrid fusion (RRF) uses **rank position** across retrievers to score candidates.
It does not directly judge whether a chunk actually answers the specific question.
Reranking does — it re-scores every candidate by looking at the query and the
chunk together, then re-orders them so the most relevant chunks appear first.

---

### Two Reranking Modes

```text
rerank()
  │
  ├── RAG_USE_CROSS_ENCODER_RERANKER=true?
  │       └── YES  ->  cross_encoder_rerank()
  │                        │
  │                        └── model unavailable? -> local_rerank() (fallback)
  │
  └── NO  ->  local_rerank()
```

Both modes are **synchronous** and CPU-bound. `rerank` is an async graph node,
but the work inside it is not awaited off the loop — a cross-encoder pass over
16 candidates blocks the event loop for its duration. That is acceptable for
the local reranker (pure string ops) and worth knowing before enabling the
cross-encoder under concurrent load.

---

### Mode 1 — Cross-Encoder Reranking (production quality)

#### How a cross-encoder differs from an embedding model

| | Bi-encoder (embedding) | Cross-encoder |
|---|---|---|
| Input | query and chunk embedded **separately** | query and chunk fed **together** as a single sequence |
| Output | two vectors compared by cosine distance | single relevance score for the pair |
| Speed | fast (chunk embeddings cached) | slower (every pair scored fresh) |
| Accuracy | good recall | higher precision |

A bi-encoder does not see both texts at once; it cannot capture token-level
interactions between the query and the chunk. A cross-encoder does, making it
much better at judging whether a chunk truly answers the question.

#### Step-by-step internal flow

```text
1.  candidates  <- hybrid_fusion top-(K×2) chunks
2.  pairs       <- [(query, chunk1.text), (query, chunk2.text), ...]
3.  model       <- CrossEncoder(settings.cross_encoder_model)
4.  raw_scores  <- model.predict(pairs)          # one float per pair
5.  normalized  <- min-max scale to [0, 1]
6.  sorted      <- descending by normalized score
7.  source      <- "cross_encoder"
```

In code:

```python
pairs = [(query, candidate.document.page_content) for candidate in candidates]
raw_scores = [float(score) for score in model.predict(pairs)]
```

`model.predict(pairs)` runs the cross-encoder transformer on each `(query, chunk)`
pair and returns a raw logit or probability that the chunk is relevant to the query.
These scores are on the model's own scale, so they are min-max normalised before sorting.

#### Normalization formula

```text
normalized_score = (raw - min) / (max - min)

edge case: if all raw scores are equal  ->  all normalized scores = 1.0
```

#### Default model

```env
RAG_CROSS_ENCODER_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2
```

The model is constructed fresh on **every** rerank call
(`CrossEncoder(settings.cross_encoder_model)`) — there is no module-level cache,
so the first query after enabling it pays the download and each subsequent query
pays model load. Cache the instance before using this in a latency-sensitive
path.

This is a small, fast cross-encoder fine-tuned on MS MARCO passage ranking,
which gives good out-of-the-box relevance quality for document retrieval.

#### Fallback on failure

```python
try:
    return cross_encoder_rerank(query, candidates)
except Exception:
    return local_rerank(query, candidates)
```

If the model cannot be loaded (offline environment, missing package), the
pipeline silently falls back to the local reranker so the system never breaks.

---

### Mode 2 — Local Reranker (lightweight, offline)

When cross-encoder is disabled or unavailable, the local reranker computes a
blended score from three signals without any model download.

#### Signals and weights

```text
final_score = (0.70 × term_overlap) + (0.25 × hybrid_score) + metadata_boost
```

| Signal | Weight | What it measures |
|---|---|---|
| Term overlap | 0.70 | Fraction of query tokens that appear in the chunk text |
| Hybrid retrieval score | 0.25 | RRF score carried from the fusion step |
| Metadata boost | +0.05 | Fixed bonus if chunk has a `title` metadata field |

#### Step-by-step internal flow

```text
1.  query_terms <- tokenize(query)       # e.g. {sql, injection, fix, order}
2.  for each candidate:
        doc_terms   <- tokenize(chunk.text)
        overlap     <- |query_terms ∩ doc_terms| / |query_terms|
        raw_score   <- (0.70 × overlap) + (0.25 × candidate.score) + boost
3.  normalize raw scores to [0, 1] with min-max
4.  sort descending
5.  source <- "reranked"
```

Example:

```text
Query tokens:  {sql, injection, fix, orderrepository}  -> 4 terms

Chunk A text tokens include {sql, injection, orderrepository}
  overlap = 3/4 = 0.75,  hybrid_score = 0.032,  boost = 0.0
  raw = 0.70×0.75 + 0.25×0.032 = 0.525 + 0.008 = 0.533

Chunk B text tokens include {injection, fix}
  overlap = 2/4 = 0.50,  hybrid_score = 0.031,  boost = 0.05 (has title)
  raw = 0.70×0.50 + 0.25×0.031 + 0.05 = 0.350 + 0.008 + 0.05 = 0.408
```

Chunk A wins because it matches more query terms.

---

### Score Normalization (both modes)

Both rerankers apply the same min-max normalization before returning results:

```python
normalized = [
    1.0 if max_score == min_score
    else (score - min_score) / (max_score - min_score)
    for score in raw_scores
]
```

This scales all scores to `[0, 1]`, making them interpretable and stable for
the downstream knee-point algorithm.

---

### Visual Summary

```text
hybrid_fusion output  (K×2 candidates, RRF scores)
         │
         ▼
    rerank()
         │
         ├─ cross_encoder_rerank()      (if USE_CROSS_ENCODER_RERANKER=true)
         │       model.predict([(query, chunk), ...])
         │       -> one relevance score per pair
         │       -> normalize -> sort
         │
         └─ local_rerank()              (default / fallback)
                 term_overlap + hybrid_score + metadata_boost
                 -> normalize -> sort
         │
         ▼
  reranked_chunks  (same K×2 chunks, better order)
         │
         ▼
  knee_point_select  (picks natural cutoff, reduces to final K)
```

---

### Why This Helps

Initial retrieval is broad — designed for high recall.
Reranking is precise — designed for high relevance at the top positions.

It ensures:
- the best chunks surface to the top before knee-point selection,
- the knee-point cutoff discards truly irrelevant chunks rather than just
  the lowest-retrieved ones,
- the LLM prompt contains the most useful context for grounded generation.

### Production Alternatives

In production, replace local reranking with:

- Cohere Rerank API,
- BGE reranker (open-source, strong performance),
- any larger cross-encoder (e.g. `cross-encoder/ms-marco-electra-base`),
- LLM-as-judge relevance scoring,
- Azure AI Search semantic ranker.

---

## 18. Flow 15: Knee-Point Algorithm

### Graph Node

```text
knee_point_select
```

### Code Location

```text
shared/rag/src/rerank.py
```

### Function

```python
select_by_knee_point(ranked, min_k: int = 3, max_k: int = 8)
```

### Problem It Solves

If you always pass top 8 chunks to the LLM:

- you may include irrelevant chunks,
- prompt cost goes up,
- answer quality can go down,
- citations become noisy.

### What the Algorithm Does

It looks at reranked scores as a curve:

```text
rank 1 -> score 0.98
rank 2 -> score 0.91
rank 3 -> score 0.87
rank 4 -> score 0.41
rank 5 -> score 0.39
```

The sharp drop after rank 3 is the knee.

So the algorithm selects chunks before the quality flattens.

### How the Knee Is Actually Found

It is a Kneedle-style maximum-distance-from-chord computation, not a
first-derivative threshold:

1. If `len(ranked) <= min_k`, return everything — no cutoff to find.
2. If all scores are effectively equal (`np.allclose`), return the first
   `max_k` — a flat curve has no knee.
3. Normalise ranks to `x ∈ [0, 1]` and scores to `y ∈ [0, 1]`.
4. Draw the chord from the first point to the last, and measure each point's
   perpendicular distance from it (via a 2-D cross product).
5. The farthest point is the knee: `knee_index = argmax(distances) + 1`.
6. Clamp into bounds: `selected_k = max(min_k, min(knee_index, max_k, len(ranked)))`.

### Bounds

`min_k=3` and `max_k=8` are hard defaults in the function signature, not
settings — they are not configurable through `RAG_*` env vars. So the final
context window is always 3–8 chunks regardless of what the curve suggests,
which bounds prompt cost predictably.

### Output

```text
selected_chunks
```

These are the chunks that go into the final augmented prompt (after safety
filtering, which can shrink the list further — potentially below `min_k`).

### Prompt-Injection Filtering Before Augmentation

Graph node:

```text
safety_filter
```

Code location:

```text
shared/rag/src/security.py
```

Function (the graph node calls the **async** variant):

```python
await afilter_prompt_injection_chunks(chunks)
```

What happens:

1. Each selected chunk goes through `aclassify_prompt_injection()`.
2. The deterministic regex rules run first — attempts to ignore previous
   instructions, reveal or override the system prompt, `you are now …`,
   `developer message`, `exfiltrate`.
3. A rule hit short-circuits as risky without spending an LLM call.
4. Only if the rules pass *and* `RAG_USE_LLM_PROMPT_INJECTION_CLASSIFIER=true`
   is the LLM classifier consulted.
5. Risky chunks are dropped (with a warning log naming the classifier and
   reason) before prompt construction.

A sync `filter_prompt_injection_chunks()` also exists for callers that cannot
await; it applies the heuristic rules only and never invokes the LLM.

Why this helps:

Retrieved content is untrusted input. This filtering step reduces the chance that malicious document text influences the generation step.

---

## 19. Flow 16: Query + Content Augmentation

### Graph Node

```text
augment
```

### Code Location

```text
shared/rag/src/llm.py
```

### Function

```python
build_augmented_prompt(question, chunks)
```

### What Happens

The function builds a prompt containing:

- grounding instructions,
- user question,
- retrieved context,
- citation numbers,
- title,
- source,
- chunk index,
- chunk text.

Exact prompt shape:

```text
You are a grounded RAG assistant.

Answer the question using only the context below.
If the context is insufficient, say what evidence is missing.
Include citations like [1] or [2] for important claims.

Question:
<question>

Retrieved context:
[1] title=...; section=...; page=...; source=...; chunk=<chunk_index>
<chunk text>

---

[2] title=...; section=...; page=...; source=...; chunk=<chunk_index>
<chunk text>

Answer:
```

Blocks are joined by `\n\n---\n\n`. Missing metadata degrades to literal
placeholders (`untitled`, `unknown`, `n/a`) rather than being omitted, so the
citation numbering stays dense and `validate_citations()` can map `[n]` back to
position `n` in `selected_chunks`.

### Why This Helps

This is the "A" in RAG: augmentation.

The original question is augmented with retrieved evidence before generation.

The LLM does not need to rely on memory.

It answers from the selected chunks.

---

## 20. Flow 17: Pass to LLM and Get Response

### Graph Node

```text
generate
```

### Code Location

```text
shared/rag/src/llm.py
```

### Function

```python
generate_answer(question, chunks)
```

### LLM Gateway Mode (current implementation)

The augmented prompt is sent through `shared.llm_gateway.LLMClient` — the same
gateway every other module in the app uses. This inherits:

- the fallback chain (primary model → Groq `qwen/qwen3-32b` → `qwen/qwen3.6-27b` → `openai/gpt-oss-safeguard-20b`),
- per-model retries with exponential backoff,
- full request tracking (MongoDB or JSONL),
- generation parameters from RAG settings (`RAG_RAG_TEMPERATURE` default 0.0, `RAG_RAG_MAX_TOKENS` default 2048).

RAG holds no provider SDK or API key of its own — model/provider/key live in
`shared/llm_gateway/.env` (`LLM_PRIMARY_*`, `LLM_GROQ_API_KEY`).

### Failure Behaviour — it degrades, it does not raise

`generate_answer()` wraps the whole gateway call in a broad `except`. If every
model in the chain fails (or no key is configured at all), it logs a warning and
returns `_local_extractive_answer()` — a deterministic, non-LLM summary built
from the retrieved chunks:

```text
Local grounded answer (LLM Gateway unavailable):

Question: <question>

Most relevant evidence:
- [1] <first 500 chars of chunk>  (source: ..., section: ..., page: ...)
- [2] ...

Configure LLM_PRIMARY_API_KEY or LLM_GROQ_API_KEY for a fluent LLM-generated response.
```

Two consequences to plan for:

- `POST /rag/query` returns **200 with an answer** even when no LLM is reachable.
  The router's 500 handler only fires for errors raised *outside* generation
  (for example an unhandled retrieval fault). Do not use HTTP status as an
  LLM-health signal — the `"Local grounded answer (LLM Gateway unavailable)"`
  prefix is the actual tell.
- The fallback text contains no `[n]`-style inline citations in prose, so
  `validate_citations()` will report `missing_citations` and `faithfulness`
  drops to its overlap-only component. Degraded LLM availability therefore shows
  up as a metrics dip, which is a useful alert.

If the retrieved-chunk list is empty, the fallback returns
`"I do not have enough retrieved evidence to answer this question."`

### Output

The graph returns:

```python
{
    "answer": "...",
    "citation_validation": {...},
    "evaluation_metrics": {...}
}
```

### Citation Validation

Graph node:

```text
validate_citations
```

Code location:

```text
shared/rag/src/citations.py
```

Function:

```python
validate_citations(answer, chunks)
```

What happens:

1. The answer is scanned for citation markers like `[1]` and `[2]`.
2. The citation numbers are compared against selected chunk positions.
3. The graph reports whether citations are valid, missing, or out of range.

Example output:

```python
{
    "valid": True,
    "cited_numbers": [1, 2],
    "missing_citations": False,
    "invalid_citations": [],
    "available_citations": [1, 2, 3]
}
```

### Evaluation Metrics

Graph node:

```text
evaluate
```

Code location:

```text
shared/rag/src/evaluation.py
```

Function:

```python
evaluate_answer(question, answer, chunks, citation_validation)
```

The implementation computes local heuristic metrics and returns five fields:

```python
{
    "faithfulness":      0.0-1.0,   # 0.6 × citations_valid + 0.4 × answer/context overlap
    "context_precision": 0.0-1.0,   # fraction of selected chunks overlapping the question
    "answer_relevance":  0.0-1.0,   # question→answer token overlap
    "evaluator":         "local",   # echoes RAG_ADVANCED_EVAL_PROVIDER
    "advanced_notes":    "Local lexical metrics only.",
}
```

All three scores are rounded to 3 decimals. Note the weighting inside
`faithfulness`: citation validity is a binary 0/1 worth 60% of the score, so an
otherwise-grounded answer that forgets to write `[1]` markers scores at most
0.4.

These are lightweight practice metrics, not a replacement for a production evaluator such as RAGAS, DeepEval, or LLM-as-judge evaluation.

---

## 21. Complete Ingestion Data Transformation

```text
raw file
  |
  v
text
  |
  v
Document(
  page_content = full text,
  metadata = document metadata + section/page metadata
)
  |
  v
Document(
  page_content = chunk text,
  metadata = document metadata + chunk metadata + parent metadata
)
  |
  v
Mongo `chunks` document:
  chunk_id  = chunk_id (upsert key)
  embedding = embed(chunk.page_content)   # searched by Atlas $vectorSearch
  content   = chunk.page_content
  metadata  = chunk.metadata
```

Before insert, the vector store deletes existing records with the same `content_hash`.

---

## 22. Complete Query Data Transformation

```text
question
  |
  v
metadata filters
  |
  v
query embedding
  |
  v
semantic results
  |
  +---- keyword results
  |
  +---- graph results (optional)
  |
  +---- web results (optional, Tavily)
            |
            v
vector-keyword-graph-web fused candidates (RRF)
  |
  v
parent-expanded candidates
  |
  v
reranked candidates
  |
  v
knee-point selected chunks
  |
  v
prompt-injection filtered chunks
  |
  v
augmented prompt
  |
  v
LLM or local generator
  |
  v
final answer
  |
  v
citation validation + evaluation metrics
```

---

## 23. Module Map

| Flow | Module | Notes |
|---|---|---|
| HTTP API (ingest/upload/query/files/stats/health) | `shared/rag/src/router.py` | Mounted at `/rag/*`; Portless-staff only except `/health`; job status in an in-process `_jobs` dict; exports `startup()` for the app lifespan |
| Lazy public exports | `shared/rag/src/__init__.py` | PEP 562 `__getattr__` for `query`/`ingest`/graph builders/`settings`/`router`/`startup` |
| CLI (local experimentation) | `shared/rag/src/cli.py` | `practice-rag ingest / ask`; `--path` required for ingest |
| Graph orchestration | `shared/rag/src/graph.py` | `build_ingestion_graph()`, `build_query_graph()`, `ingest()`, `query()` — all async |
| Configuration | `shared/rag/src/config.py` | `RAG_*` env prefix; root `.env` then `shared/rag/.env` (module wins) |
| Parse/load + document metadata | `shared/rag/src/loader.py` | `.md`/`.txt`/`.pdf`, section + page metadata, layout/OCR options |
| Recursive / semantic chunking + chunk & parent-child metadata | `shared/rag/src/chunking.py` | Returns `(child_chunks, parent_docs)` |
| Embeddings | `shared/rag/src/embeddings.py` | Delegates to `shared.llm_gateway.GatewayEmbeddings` (offline hash fallback) |
| Vector DB (Atlas `$vectorSearch`), BM25 keyword search, metadata filters, content-hash re-index, RRF fusion, parent expansion | `shared/rag/src/vector_store.py` | `chunks` collection; **`ensure_indexes()`** |
| Uploaded-file persistence | `shared/rag/src/file_store.py` | MongoDB GridFS (`rag_files` bucket); `FileStore` Protocol + `MongoGridFSFileStore` |
| Parent document store | `shared/rag/src/parent_store.py` | Mongo `parent_docs` collection; `MongoParentStore` |
| Graph DB ingestion + retrieval | `shared/rag/src/graph_store.py` | Backends: `mongo` (`graph_chunks`) / `neo4j` / no-op; `ensure_graph_indexes()` |
| Live web search leg | `shared/rag/src/web_search.py` | Tavily `WebSearchStore`; empty on missing key/failure; reused by negotiation market intel |
| Reranking (local + cross-encoder) and knee-point | `shared/rag/src/rerank.py` | Sync; knee-point bounded to 3–8 chunks |
| Prompt-injection filtering | `shared/rag/src/security.py` | `afilter_prompt_injection_chunks()` — rules + optional LLM classifier |
| Prompt augmentation + generation | `shared/rag/src/llm.py` | Via `shared.llm_gateway.LLMClient`; local extractive fallback |
| Citation validation | `shared/rag/src/citations.py` | |
| Evaluation metrics | `shared/rag/src/evaluation.py` | Local metrics + RAGAS/DeepEval/LLM-judge hooks |
| Authorization filtering | `shared/rag/src/authorization.py` | `authorized_users` / `authorized_teams` metadata; applied to all four legs |

---

## 24. What Happens When You Run Ingest

Command (`--path` is required — there is no default folder):

```powershell
practice-rag ingest --path ./data --strategy recursive
```

Flow:

```text
cli.py main()
  -> build_ingestion_graph()
  -> asyncio.run(graph.ainvoke(...))
  -> parse_and_load()
  -> load_documents()
  -> format_text_documents() / load_pdf_documents() -> _format_document()
  -> chunk()
  -> chunk_documents()
  -> recursive_chunk_documents()
  -> _number_chunks()  -> (child_chunks, parent_docs)
  -> await MongoParentStore().save(parent_docs)   # `parent_docs` collection
  -> embed_and_insert()
  -> VectorStore.upsert()
  -> delete_by_content_hashes() for the batch's hashes
  -> GatewayEmbeddings.embed_documents() + Mongo chunks bulk upsert
  -> graph_insert()
  -> GraphStore.upsert_chunks()
  -> Document/Chunk/Entity nodes + PART_OF/MENTIONS/ADJACENT_TO
     (mongo `graph_chunks` collection or Neo4j;
      no-op when RAG_GRAPH_RETRIEVAL_ENABLED is false)
```

It then prints both counters:

```text
Inserted vector chunks: <inserted_count>
Inserted graph chunks:  <graph_inserted_count>
```

In the running backend the same ingestion graph is triggered by
`POST /rag/upload` (files persisted to GridFS first) or `POST /rag/ingest`
(server paths), running as a background job whose status is polled at
`GET /rag/ingest/{job_id}`.

With semantic chunking:

```powershell
practice-rag ingest --strategy semantic
```

The chunking part changes:

```text
chunk_documents()
  -> semantic_chunk_documents()
  -> _split_semantic_units()
  -> embeddings.embed_documents()
  -> _cosine()
  -> _number_chunks()
```

---

## 25. What Happens When You Ask a Question

Command:

```powershell
practice-rag ask "What metadata should be stored with chunks?"
```

With metadata filtering:

```powershell
practice-rag ask "What is covered in this section?" --filter section=Retrieval
practice-rag ask "What is on this PDF page?" --filter page_number=3
```

Flow:

```text
cli.py main()
  -> build_query_graph()
  -> asyncio.run(graph.ainvoke(...))
  -> _embed_query()            [node: embed_query]
  -> _semantic_search()        [node: semantic_search]
       VectorStore.semantic_search() + filter_authorized_results()
  -> _keyword_search()         [node: keyword_search]
       VectorStore.keyword_search() + filter_authorized_results()
  -> _graph_search()           [node: graph_search]
       GraphStore.retrieve() + filter_authorized_results()
  -> _web_search()             [node: web_search]
       WebSearchStore.retrieve() (empty when disabled / key absent)
  -> _fuse_results()           [node: hybrid_fusion]
       hybrid_fusion(top_k = retrieval_top_k * 2)
  -> _parent_child_expand()    [node: parent_child_expand]
       expand_parent_context()
  -> _rerank_results()         [node: rerank]
       rerank()
  -> _knee_point_select()      [node: knee_point_select]
       select_by_knee_point()
  -> _safety_filter()          [node: safety_filter]
       afilter_prompt_injection_chunks()
  -> _augment()                [node: augment]
       build_augmented_prompt()
  -> _generate()               [node: generate]
       generate_answer()
  -> _validate_citations()     [node: validate_citations]
       validate_citations()
  -> _evaluate()               [node: evaluate]
       evaluate_answer()
```

The node names are the LangGraph identifiers (useful when reading traces); the
`_`-prefixed functions are their implementations. Note the node named
`hybrid_fusion` is implemented by `_fuse_results`, which is the one place the
two naming schemes diverge.

---

## 26. Key Design Decisions

### Metadata Stored Separately

The project stores metadata beside the chunk instead of embedding all metadata.

Each chunk has two parts:

```python
Document(
    page_content="Black Duck scans open source dependencies for license risk.",
    metadata={
        "source": "security_tools.md",
        "section": "SCA Tools",
        "page_number": 4,
        "file_type": ".md",
        "authorized_teams": ["security"],
    },
)
```

The `page_content` is the natural-language evidence. The `metadata` is structured information about the evidence.

Metadata answers questions such as:

- where the chunk came from,
- which section or page it belongs to,
- which file type produced it,
- who is allowed to see it,
- how the system should trace or cite it.

Storing metadata separately is good because:

- metadata can be used for filters,
- metadata can be shown in citations,
- metadata can enforce access rules,
- metadata does not pollute semantic meaning.

### Only Content Embedded by Default

The embedding represents:

```text
chunk.page_content
```

For the example above, the embedding represents the meaning of:

```text
Black Duck scans open source dependencies for license risk.
```

That vector is used for semantic search. If the user asks:

```text
Which tool checks open source license issues?
```

the vector search can find this chunk because the meaning is similar, even though the exact words differ.

The metadata is not converted into the vector by default. It stays attached to the chunk as structured data:

```python
{
    "source": "security_tools.md",
    "section": "SCA Tools",
    "page_number": 4,
    "file_type": ".md",
    "authorized_teams": ["security"],
}
```

### Why Not Embed All Metadata?

If the system embedded every metadata field together with the chunk text, the embedding input might become:

```text
source: security_tools.md
section: SCA Tools
page_number: 4
authorized_teams: security
Black Duck scans open source dependencies for license risk.
```

This can add noise. A query about `license risk` should match chunks about license risk, not chunks that happen to share the same file path, page number, team name, timestamp, hash, or access-control label.

Metadata such as file paths, timestamps, hashes, user IDs, team IDs, and page numbers usually should not affect semantic meaning.

### How Metadata Is Used Separately

Metadata is still very useful, but it is used through structured logic instead of vector similarity.

1. **Filters**

   A CLI query can restrict retrieval to a section or page:

   ```powershell
   practice-rag ask "What does this section explain?" --filter section="SCA Tools"
   ```

   The system checks metadata exactly:

   ```python
   metadata["section"] == "SCA Tools"
   ```

2. **Citations**

   Retrieved metadata can be shown with the answer:

   ```text
   [1] security_tools.md, section: SCA Tools, page: 4
   ```

3. **Authorization**

   Access metadata can remove chunks before generation:

   ```python
   {
       "authorized_teams": ["security"],
       "authorized_users": ["alice"],
   }
   ```

   This should be strict application logic, not semantic guessing.

4. **Traceability**

   Metadata lets the system explain where evidence came from and how it was processed.

Short rule:

```text
page_content -> embeddings / semantic search
metadata -> filters / citations / authorization / traceability
```

Sometimes it is useful to intentionally include selected metadata, such as a title or section heading, in the text that gets embedded. But the project does not blindly embed all metadata by default.

### Hybrid Search

The system uses up to four retrieval signals:

- vector semantic search,
- BM25 keyword search,
- graph relationship search (optional),
- live web search (optional).

This gives better recall than any single method alone.

### Graph-Hybrid Retrieval

The graph store adds entity and relationship recall.

It helps when:

- the query mentions an entity that appears across multiple chunks,
- related evidence is connected by section, page, source, or parent metadata,
- exact keywords are sparse,
- the answer needs relationship context.

The final candidate pool is:

```text
semantic + keyword + graph + web candidates -> reciprocal rank fusion
```

### Reranking After Retrieval

Retrieval casts a wide net.

Reranking narrows it.

### Knee-Point Selection

The system avoids always sending a fixed number of chunks.

It dynamically selects a reasonable cutoff based on relevance scores.

### Parent-Child Retrieval

The system embeds child chunks for precision but expands retrieved matches to parent context before reranking and generation.

### Metadata Filtering

The system supports exact metadata filters during semantic and keyword retrieval. This is useful for scoping by `section`, `page_number`, `file_type`, or source.

### Re-Indexing by Content Hash

The system deletes existing chunks with the same `content_hash` before inserting new chunks. This prevents duplicate stale records after a document changes.

### Safety, Citation, and Evaluation

The system filters prompt-injection-like chunks before augmentation, validates generated citations, and returns lightweight RAG quality metrics after each answer.

---

## 27. Implemented Production Enhancements

Implemented enhancement set:

- PDF parsing with `page_number`.
- Markdown heading-aware `section` metadata.
- Metadata filters during retrieval.
- Parent-child chunk retrieval.
- Optional cross-encoder reranking.
- Citation validation.
- Prompt-injection filtering for retrieved chunks.
- Deletion and re-indexing by `content_hash`.
- Evaluation metrics for faithfulness, context precision, and answer relevance.
- Richer PDF layout extraction for tables and figures.
- OCR fallback for scanned PDFs.
- Metadata authorization filters for users and teams.
- Production-style prompt-injection classifier hook.
- RAGAS, DeepEval, or LLM-as-judge evaluation hooks.
- Parent document storage in a dedicated Mongo collection (`parent_docs`) for large parent chunks.
- Graph DB based hybrid retrieval over `Document` / `Chunk` / `Entity` nodes, with pluggable Mongo and Neo4j backends.
- Optional live web-search retrieval leg (Tavily) fused by RRF, failing open to empty results.
- Fully async ingestion and query pipelines, backed by MongoDB Atlas and the LLM Gateway.
- Durable GridFS persistence for uploaded knowledge files.
- Cascading delete across the vector, parent, and graph stores.

---

## 28. Rich PDF Layout Extraction

### Code Location

```text
shared/rag/src/loader.py
```

### Functions

```python
load_pdf_documents(path)
load_pdf_documents_with_layout(path)
_table_to_markdown(table)
ocr_pdf_page(path, page_number)
```

### What Was Implemented

`load_pdf_documents()` tries layout-aware parsing first when:

```env
RAG_ENABLE_PDF_LAYOUT_EXTRACTION=true   # this is the DEFAULT
```

Unlike most optional features here, this one is **on by default**
(`enable_pdf_layout_extraction: bool = True`). Set it to `false` to force the
plain `pypdf` path.

The layout path uses `pdfplumber` when available; if the import fails it returns
an empty list and `load_pdf_documents()` silently falls through to `pypdf`.

For every page, it extracts:

- normal page text,
- detected tables,
- figure/image counts,
- extraction method,
- page number.

Tables are converted into Markdown through `_table_to_markdown()`, so table evidence can be chunked, embedded, retrieved, and cited like normal text.

Metadata added:

```python
{
    "page_number": 3,
    "section": "Page 3",
    "extraction_method": "pdfplumber",
    "table_count": 2,
    "figure_count": 1,
    "has_tables": True,
    "has_figures": True
}
```

### How It Helps the Flow

Before this change, PDF parsing mainly extracted plain text.

Now the ingestion flow preserves more evidence:

```text
PDF page -> text + tables + figure metadata -> Document -> chunks -> vector DB
```

This improves retrieval for documents where important information is stored in tables.

---

## 29. OCR for Scanned PDFs

### Code Location

```text
shared/rag/src/loader.py
```

### Function

```python
ocr_pdf_page(path, page_number)
```

### What Was Implemented

If a PDF page has no extractable text and OCR is enabled:

```env
RAG_ENABLE_PDF_OCR=true   # default false
```

the loader tries to:

1. Render the page with `pdf2image`.
2. Run OCR with `pytesseract`.
3. Store the OCR text as normal `page_content`.
4. Mark metadata with:

```python
{
    "extraction_method": "ocr"
}
```

### Important Runtime Note

OCR requires Python packages plus system tools:

- `pdf2image`
- `pytesseract`
- Poppler installed locally
- Tesseract OCR installed locally

If OCR tools are missing, the function returns empty text and ingestion continues safely.

### How It Helps the Flow

Scanned PDF pages can now enter the same RAG pipeline:

```text
scanned PDF page -> OCR text -> chunking -> embeddings -> retrieval -> citation
```

---

## 30. Authorization Metadata Filters

### Code Location

```text
shared/rag/src/authorization.py
shared/rag/src/graph.py
shared/rag/src/cli.py
```

### Functions

```python
filter_authorized_results(results, user_id, team_id)
is_authorized_document(document, user_id, team_id)
_metadata_values(value)
```

### What Was Implemented

Documents now get default authorization metadata:

```python
{
    "authorized_users": "*",
    "authorized_teams": "*"
}
```

The query CLI accepts:

```powershell
practice-rag ask "question" --user alice --team appsec
```

and the HTTP API takes the same values as `user_id` / `team_id` in the
`POST /rag/query` body.

**Every** retrieval leg — semantic, keyword, graph, and web — is filtered
before fusion, by:

- `authorized_users`
- `authorized_teams`
- public wildcard `*`

Values may be a list or a comma-separated string; `_metadata_values()`
normalises both. A document is visible if either field contains `*`, or if it
names the requesting user/team.

Because `loader.py` stamps `authorized_users: "*"` and `authorized_teams: "*"`
on every document at load time, an un-tagged corpus is fully public and passing
`--user`/`--team` narrows nothing. Restriction requires writing real values into
those metadata fields at ingestion time.

### How It Helps the Flow

Unauthorized chunks are removed before:

```text
hybrid fusion -> reranking -> prompt augmentation -> generation
```

This is important because access control must happen before evidence reaches the LLM prompt.

---

## 31. Production-Style Prompt-Injection Classifier

### Code Location

```text
shared/rag/src/security.py
```

### Functions

```python
has_prompt_injection_risk(text)            # sync regex rules
aclassify_prompt_injection(text)           # async: rules, then optional LLM
_llm_injection_assessment(text)            # async: LLM-as-judge via the gateway
afilter_prompt_injection_chunks(chunks)    # async — used by the graph node
filter_prompt_injection_chunks(chunks)     # sync, rules-only convenience
```

### What Was Implemented

The prompt-injection filter has two layers:

1. Deterministic rule classifier (`PROMPT_INJECTION_PATTERNS`).
2. Optional LLM classifier, consulted only when the rules find nothing.

Enable the LLM classifier with:

```env
RAG_USE_LLM_PROMPT_INJECTION_CLASSIFIER=true   # default false
```

The LLM leg sends at most 4000 characters of the chunk with `max_tokens=10` and
`temperature=0.0`, asking for a bare `SAFE` / `RISKY` verdict, tagged
`caller="rag.security.injection_classifier"` in gateway tracking.

**It fails open**: if the gateway is unavailable the assessment returns
`is_risky=False` with `classifier="llm-unavailable"`, so an LLM outage lets
chunks through rather than emptying the context. The deterministic rules always
run first and are unaffected, so the baseline protection never depends on model
availability.

The classifier returns a structured assessment:

```python
{
    "is_risky": True,
    "classifier": "rules",
    "reason": "Matched a known prompt-injection pattern."
}
```

### How It Helps the Flow

Retrieved chunks are untrusted input.

The safety filter runs before prompt augmentation:

```text
knee_point_select -> safety_filter -> augment
```

This prevents suspicious retrieved text from becoming part of the LLM context.

---

## 32. Advanced Evaluation Hooks

### Code Location

```text
shared/rag/src/evaluation.py
```

### Functions

```python
evaluate_answer(question, answer, chunks, citation_validation)
advanced_evaluation_notes(question, answer, chunks)
_ragas_notes()
_deepeval_notes()
_llm_judge_notes(question, answer, chunks)
```

### What Was Implemented

The system still computes local metrics:

- `faithfulness`
- `context_precision`
- `answer_relevance`

It now also supports advanced evaluator modes:

```env
RAG_ADVANCED_EVAL_PROVIDER=local      # default
RAG_ADVANCED_EVAL_PROVIDER=ragas
RAG_ADVANCED_EVAL_PROVIDER=deepeval
RAG_ADVANCED_EVAL_PROVIDER=llm
```

Behavior:

- `local`: uses only local lexical metrics.
- `ragas`: checks RAGAS availability and reports integration readiness.
- `deepeval`: checks DeepEval availability and reports integration readiness.
- `llm`: runs a compact LLM-as-judge prompt **through the LLM Gateway**
  (`caller="rag.evaluation.llm_judge"`, `max_tokens=200`) — not a direct OpenAI
  call. Chunks are truncated to 700 characters each for the judge prompt.
- any other value: falls back to local metrics and says so in `advanced_notes`.

The result of whichever branch runs is returned as the `advanced_notes` string;
it never raises, reporting `"LLM judge unavailable: …"` instead.

### Sync-in-async caveat

`evaluate_answer()` is synchronous but is called from the async `evaluate` node.
The `llm` branch therefore has to bridge back into async: it detects a running
event loop and, when it finds one (as under FastAPI), runs the judge coroutine
in a one-off `ThreadPoolExecutor` with a 30-second timeout rather than nesting
loops. This works, but it means enabling `llm` evaluation adds a blocking wait
of up to 30 s to every query. Leave it on `local` for interactive paths and use
it for offline evaluation runs.

### How It Helps the Flow

Evaluation now supports both fast local development and production evaluation extension points:

```text
generate -> validate_citations -> evaluate -> local metrics + optional advanced notes
```

---

## 33. External Parent Document Storage

### Code Location

```text
shared/rag/src/parent_store.py
shared/rag/src/chunking.py
shared/rag/src/vector_store.py
```

### Functions

```python
MongoParentStore().save(parent_documents)   # dict[parent_id, Document]
MongoParentStore().load(parent_id)          # -> Document | None
MongoParentStore().delete(parent_ids)       # -> int
MongoParentStore().delete_by_source(source) # -> int, used by DELETE /rag/documents

# thin async wrappers kept for backward compatibility:
save_parent_documents(parent_documents)
load_parent_document(parent_id)
```

(All of these are coroutines. There is no `delete_parent_documents()` helper —
use `MongoParentStore().delete()`.)

### What Was Implemented

Parent text is no longer stored inside every child chunk's metadata.

Instead:

1. `chunking.py` groups child chunks into parent context blocks.
2. `save_parent_documents()` writes parent documents to the MongoDB
   **`parent_docs`** collection (unique `parent_id` index, plus indexes on
   `metadata.source` and `metadata.content_hash` for deletion/re-index):

```text
{
  "parent_id": str,   # unique, derived from source+section+strategy+index
  "content":   str,
  "metadata":  dict
}
```

3. Child chunks keep only:

```python
{
    "parent_id": "...",
    "parent_index": 0
}
```

4. Query-time `expand_parent_context()` loads the full parent text with:

```python
await MongoParentStore().load(parent_id)
```

Indexes on the `parent_docs` collection (`parent_id_unique`,
`parent_source_idx`, `parent_hash_idx`) are ensured lazily on **every**
`_get_collection()` call rather than once at startup — correct and idempotent,
but it adds an `ensure_indexes` round trip per parent operation.

### Why This Is Better

Large parent context can exceed practical metadata limits.

External parent storage keeps the vector-searched `chunks` collection lean:

```text
chunks collection:
  child chunk text
  child embedding
  small metadata with parent_id

parent_docs collection:
  parent_id
  larger parent text
  parent metadata
```

### How It Helps the Flow

The system still retrieves precise child chunks, but expands them to larger parent context before reranking:

```text
hybrid child results -> parent_child_expand -> load parent document -> rerank
```

This gives the LLM more context without embedding oversized parent documents.

---

## 34. Azure Component Mapping for This RAG App

The current project is intentionally provider-neutral and local-friendly. The same architecture can be implemented on Azure by replacing or augmenting each local component with the corresponding Azure service.

### 34.1 Current Component to Azure Equivalent

| Current Implementation | Purpose | Azure Equivalent |
|---|---|---|
| `loader.py` with `.md`, `.txt`, `.pdf` parsing | Parse documents and form `Document(page_content, metadata)` | Azure AI Document Intelligence, Azure Functions, Azure Container Apps jobs |
| GridFS uploads (`file_store.py`) + server paths | Source documents | Azure Blob Storage, Azure Data Lake Storage Gen2, SharePoint connector, OneLake |
| `pdfplumber`, `pypdf`, `pytesseract` | PDF text/layout/OCR extraction | Azure AI Document Intelligence Read/Layout models |
| `chunking.py` | Recursive and semantic chunking | Azure Functions/Container Apps worker running LangChain splitters, or Azure AI Search integrated vectorization skillsets |
| `embeddings.py` → LLM Gateway `GatewayEmbeddings` | Embedding (offline hash fallback) | Azure OpenAI embedding deployment |
| MongoDB Atlas `$vectorSearch` in `vector_store.py` | Vector DB | Azure AI Search vector index, optionally Cosmos DB for MongoDB vCore vector search |
| BM25 in `VectorStore.keyword_search()` | Keyword search | Azure AI Search BM25 lexical search |
| `graph_store.py` (mongo `graph_chunks` / Neo4j backends) | Document/Chunk/Entity relationship retrieval (`PART_OF`, `MENTIONS`, `ADJACENT_TO`) | Azure Cosmos DB Gremlin API, Azure Cosmos DB for NoSQL adjacency model, Azure SQL graph tables, Neo4j on Azure Marketplace |
| `web_search.py` (Tavily) | Live web retrieval leg | Bing Search API / Azure AI Agent Service grounding with Bing |
| `hybrid_fusion()` | Semantic + keyword + graph + web fusion | Azure AI Search hybrid search plus graph DB retrieval fused with Reciprocal Rank Fusion |
| `rerank.py` local/cross-encoder reranking | Improve top retrieved chunks | Azure AI Search Semantic Ranker, cross-encoder hosted on Azure ML/AKS/Container Apps |
| `parent_store.py` (Mongo `parent_docs` collection) | Store large parent context outside vector metadata | Azure Blob Storage, Cosmos DB, Azure SQL, or Azure Table Storage |
| `authorization.py` | User/team metadata filtering | Microsoft Entra ID, Azure AI Search security trimming, app-level RBAC/ABAC |
| `security.py` | Prompt-injection filtering | Azure AI Content Safety, Azure OpenAI classifier prompt, custom classifier on Azure ML |
| `llm.py` → LLM Gateway `LLMClient` (primary → Groq chain) | Generate grounded answer | Azure OpenAI GPT model deployment |
| `citations.py` | Validate citation IDs | Application logic, Azure Functions, Container Apps |
| `evaluation.py` | Local/RAGAS/DeepEval/LLM evaluation | Azure AI Foundry evaluation, Prompt Flow evaluation, RAGAS/DeepEval on Azure ML |
| `.env` settings | Local configuration | Azure App Configuration, Key Vault, managed identity |
| CLI | Local interface | API Management + FastAPI/Container Apps, Azure Functions HTTP API, Web App |
| Local logs | Debugging | Azure Monitor, Application Insights, Log Analytics |

### 34.2 Azure Reference Architecture

```text
Source Documents
  Azure Blob Storage / SharePoint / Data Lake
        |
        v
Parsing and Enrichment
  Azure AI Document Intelligence
  Azure Functions or Container Apps ingestion worker
        |
        v
Chunking and Metadata Formation
  LangGraph ingestion graph
  Recursive / semantic / layout-aware chunking
        |
        v
Embedding
  Azure OpenAI text-embedding deployment
        |
        v
Indexing
  Azure AI Search vector + keyword + metadata index
  Parent documents in Blob Storage or Cosmos DB
  Graph relationships in Cosmos DB Gremlin / Azure SQL graph / Neo4j
        |
        v
Query Runtime
  API Management / Container Apps / Functions
  LangGraph query graph
        |
        v
Retrieval
  Azure AI Search hybrid search + graph DB retrieval + filters + semantic ranker
        |
        v
Safety and Generation
  Azure AI Content Safety
  Azure OpenAI chat model
        |
        v
Evaluation and Observability
  Azure AI Foundry / Prompt Flow / App Insights / Log Analytics
```

### 34.3 Azure AI Search Index Design

If you replace MongoDB Atlas Vector Search with Azure AI Search, create an index with fields similar to this:

```text
id: string key
chunk_id: string filterable sortable
parent_id: string filterable
content: string searchable
content_vector: Collection(Edm.Single) vector searchable
source: string filterable facetable
file_name: string filterable facetable
file_type: string filterable facetable
title: string searchable filterable
section: string searchable filterable facetable
page_number: int filterable sortable facetable
content_hash: string filterable
chunk_index: int filterable sortable
chunk_strategy: string filterable facetable
authorized_users: Collection(Edm.String) filterable
authorized_teams: Collection(Edm.String) filterable
loaded_at: DateTimeOffset filterable sortable
extraction_method: string filterable facetable
table_count: int filterable sortable
figure_count: int filterable sortable
```

Use:

- vector search over `content_vector`,
- keyword/BM25 search over `content`, `title`, and `section`,
- filters over metadata,
- semantic ranker for reranking,
- security trimming with `authorized_users` and `authorized_teams`.

### 34.3.1 Azure Graph DB Design

If you replace `graph_store.py`'s Mongo/Neo4j backends with an Azure graph-style
store, model the graph exactly as the current backends do:

```text
Nodes:
  Document(source, title)
  Chunk(chunk_id, content, source, section, page_number)
  Entity(name)

Edges:
  Chunk -[:PART_OF]-> Document
  Chunk -[:MENTIONS]-> Entity
  Chunk -[:ADJACENT_TO]-> Chunk
```

Section and page number are Chunk **properties**, not nodes — promote them to
nodes only if you intend to traverse them, which this pipeline does not.

Azure options:

- **Azure Cosmos DB Gremlin API**: best fit when you want graph traversals with Gremlin.
- **Azure Cosmos DB for NoSQL adjacency model**: good when you want a simple managed document database and can implement traversal in application code.
- **Azure SQL graph tables**: useful when your enterprise already standardizes on SQL Server or Azure SQL.
- **Neo4j on Azure**: useful when graph traversal and graph algorithms are central to the product.

In this app, graph retrieval does this (a single `MENTIONS` hop — no multi-hop
traversal today):

```text
query entities -> Entity nodes -> connected Chunk nodes -> graph candidates
```

Then graph candidates are fused with Azure AI Search vector and keyword candidates.

### 34.4 Azure OpenAI Model Roles

Use Azure OpenAI for two different model roles:

1. Embedding model:
   - Converts chunk text and query text into vectors.
   - Used during ingestion and retrieval.

2. Chat/generation model:
   - Receives the augmented prompt.
   - Generates the final grounded answer with citations.

Do not use the chat model for embedding. Do not use the embedding model for generation.

### 34.5 Azure Deployment Options

For a production version of this project:

- Run ingestion as Azure Container Apps jobs, Azure Functions, or Azure Batch if document volume is high.
- Run query APIs on Azure Container Apps, App Service, AKS, or Azure Functions.
- Store secrets in Azure Key Vault and access them through managed identity.
- Store raw files and parent documents in Azure Blob Storage.
- Store vectors and searchable metadata in Azure AI Search.
- Use Entra ID for user authentication.
- Use Application Insights for latency, errors, retrieval traces, and prompt/response observability.

### 34.6 When to Use Azure AI Search Instead of MongoDB Atlas Vector Search

Use Azure AI Search when you need:

- built-in hybrid keyword + vector retrieval in one service,
- the managed semantic ranker,
- security trimming,
- Azure-native RBAC/monitoring integration.

Stay on MongoDB Atlas `$vectorSearch` (the current implementation) when:

- the app already runs on a single shared MongoDB (this project's model —
  vectors, parents, GridFS files, and every service's collections live in
  one database),
- you want zero additional infrastructure or credentials,
- BM25 in-process over a bounded corpus is sufficient for keyword recall,
- graceful in-memory/dev fallback matters more than managed-search features.

---

## 35. Choosing the Correct Embedding Model

Embedding model choice strongly affects retrieval quality. Choose based on document type, language, domain vocabulary, latency, cost, and vector store constraints.

### 35.1 Selection Criteria

| Criterion | Why It Matters | What to Check |
|---|---|---|
| Domain fit | Security, legal, medical, code, and finance terms may need stronger semantic understanding | Benchmark on your real questions and documents |
| Language support | Multilingual corpora need multilingual embeddings | Test English-only vs multilingual models |
| Max input tokens | Long chunks may be truncated by small-context embedding models | Align chunk size with embedding model limits |
| Vector dimension | Higher dimensions can improve quality but increase storage and memory | Match Azure AI Search vector field dimensions |
| Latency | Ingestion and query speed depend on embedding throughput | Measure p95 embedding latency |
| Cost | Embedding every chunk can be expensive at scale | Estimate cost by number of chunks and re-index frequency |
| Stability | Re-embedding is required if the model changes | Version embedding model in metadata |

### 35.2 Practical Azure Choices

For Azure OpenAI, common choices are:

- `text-embedding-3-small`: good default for cost-sensitive RAG, support bots, internal docs, and moderate-scale retrieval.
- `text-embedding-3-large`: better quality for complex semantic retrieval, technical documents, legal/compliance content, and high-value enterprise search.

Use `text-embedding-3-small` when:

- documents are mostly straightforward,
- latency and cost matter,
- the corpus is large,
- queries are simple,
- exact metadata filters and hybrid search already narrow the search space.

Use `text-embedding-3-large` when:

- documents are dense or highly technical,
- queries are abstract,
- answers require nuanced semantic matching,
- retrieval quality is more important than embedding cost,
- you have many paraphrased questions.

### 35.3 Embedding Model by Scenario

| Scenario | Recommended Embedding Choice | Reason |
|---|---|---|
| Internal policy Q&A | `text-embedding-3-small` | Policies usually have clear wording and metadata filters help |
| Security findings and remediation | `text-embedding-3-large` or benchmark both | Technical terms, CWE/CVE language, and remediation text can be nuanced |
| Legal or compliance contracts | `text-embedding-3-large` | Clause-level meaning and cross-references need better semantic precision |
| Source code search | Code-aware embeddings if available, otherwise hybrid search with `text-embedding-3-large` | Exact symbols matter, so combine with keyword search |
| Product documentation | `text-embedding-3-small` | Usually structured and heading-rich |
| Multilingual support | Multilingual embedding model or benchmark Azure OpenAI embeddings on target languages | Language coverage is the deciding factor |
| High-volume logs/tickets | `text-embedding-3-small` plus strong metadata filters | Cost and volume dominate |
| Executive summaries and strategy docs | `text-embedding-3-large` | Queries are often conceptual and paraphrased |

### 35.4 What About Metadata in Embeddings?

By default, this project embeds:

```text
chunk.page_content
```

and stores metadata separately.

In Azure AI Search, the same pattern is recommended:

- store metadata in filterable/facetable fields,
- embed the content field,
- optionally enrich the embedded text with selected metadata.

Good metadata to include in embedded text:

- title,
- section heading,
- page heading,
- short document summary,
- table caption,
- figure caption.

Avoid embedding:

- access-control fields,
- timestamps,
- content hashes,
- long raw file paths,
- internal IDs that do not help semantic meaning.

Example enriched embedding text:

```text
Title: Security Release Policy
Section: Critical Vulnerability Release Gate
Page: 12

<chunk text>
```

This often improves retrieval because the embedding sees the semantic frame of the chunk.

### 35.5 Benchmark Before Finalizing

Do not choose an embedding model by reputation alone.

Create a small evaluation set:

```text
50-100 real user questions
expected source document
expected section/page
expected answer points
```

Measure:

- Recall@K,
- Precision@K,
- MRR,
- NDCG,
- answer faithfulness,
- citation correctness,
- latency,
- cost.

Choose the cheapest model that meets quality targets.

---

## 36. Choosing the Correct Chunking Strategy

Chunking strategy should follow the shape of the source documents and the type of questions users ask.

### 36.1 Core Trade-Off

Small chunks:

- improve precise retrieval,
- reduce irrelevant context,
- help exact citations,
- but may lose surrounding context.

Large chunks:

- preserve context,
- help synthesis,
- reduce chunk count,
- but can dilute embedding relevance and increase prompt cost.

Parent-child retrieval gives a good compromise:

```text
retrieve small child chunk
expand to larger parent chunk
send parent context to LLM
```

### 36.2 Chunking Strategy by File Format

| File Type | Recommended Strategy | Why |
|---|---|---|
| Markdown | Heading-aware recursive chunking | Markdown has useful `#`, `##`, `###` structure |
| Plain text | Recursive splitter with paragraph and sentence separators | Usually lacks reliable structure |
| PDF text documents | Page-aware parsing + recursive chunking | Page citations matter and PDF text can be noisy |
| PDF with tables | Layout extraction + table-to-Markdown + recursive chunking | Tables must be preserved as text |
| Scanned PDF | OCR + page-aware chunking | OCR text needs page traceability |
| DOCX/PPTX | Section/slide-aware parsing + recursive chunking | Preserve document hierarchy and slide boundaries |
| Source code | Language-aware splitting by class/function | Avoid splitting functions in the middle |
| API docs | Header-aware chunking + metadata for endpoint/method | Users often ask by endpoint or method |
| Tickets/logs | One ticket/log event per parent, smaller child chunks if long | IDs, timestamps, and status are important metadata |
| Legal/contracts | Clause-aware or semantic chunking | Clauses and definitions must stay intact |
| Research papers | Section-aware + semantic chunking | Sections are useful but concepts can span paragraphs |

### 36.3 Chunking Strategy by Query Type

| Query Type | Better Chunking Strategy | Reason |
|---|---|---|
| Fact lookup | Smaller recursive chunks | Precise retrieval and citations |
| "Summarize this document" | Parent-child or larger parent chunks | Needs broader context |
| "Compare sections" | Section-aware chunks | Metadata filters and section names help |
| "What changed?" | Chunk by version/section and store timestamps | Enables diff-style retrieval |
| "Find exact error/CVE/API" | Smaller chunks + keyword search | Exact term matching matters |
| "Explain this concept" | Semantic chunks or parent-child | Concept may span paragraphs |
| "What does page 10 say?" | Page-aware chunks | Page metadata is required |
| "Which policy blocks release?" | Section-aware chunks + metadata filters | Policy sections map well to metadata |

### 36.4 Recommended Defaults

For this project (note the mandatory `RAG_` prefix — `config.py` sets
`env_prefix="RAG_"`, so an unprefixed `CHUNK_SIZE` is silently ignored and the
default is used):

```env
RAG_CHUNK_SIZE=900
RAG_CHUNK_OVERLAP=120
RAG_CHILD_CHUNKS_PER_PARENT=3
RAG_SEMANTIC_BREAKPOINT_PERCENTILE=25
```

Use recursive chunking by default:

```powershell
practice-rag ingest --path ./data --strategy recursive
```

Use semantic chunking when:

- documents mix multiple topics,
- headings are weak or missing,
- retrieval returns neighboring but not exact content,
- paragraphs are long and concept boundaries matter.

```powershell
practice-rag ingest --path ./data --strategy semantic
```

### 36.5 When to Tune Chunk Size

Increase chunk size when:

- answers need more surrounding context,
- chunks feel too fragmented,
- the LLM misses definitions or prerequisites,
- source documents are narrative or conceptual.

Decrease chunk size when:

- retrieval returns too much irrelevant text,
- citations are too broad,
- chunks contain multiple unrelated topics,
- exact lookup quality is poor.

Increase overlap when:

- answers often depend on text at chunk boundaries,
- sections are split mid-thought,
- definitions appear just before the retrieved text.

Decrease overlap when:

- too many duplicate chunks are retrieved,
- vector DB size is growing too fast,
- repeated context appears in prompts.

### 36.6 Azure-Specific Chunking Choices

With Azure AI Search, you have two common options:

1. Application-controlled chunking:
   - Keep chunking in LangGraph/LangChain.
   - Push chunks and embeddings into Azure AI Search.
   - Best when you need custom metadata, parent-child retrieval, or specialized chunk logic.

2. Azure AI Search integrated vectorization:
   - Use Azure AI Search indexers and skillsets.
   - Let Azure handle document cracking, chunking, embedding, and indexing.
   - Best when you want managed ingestion and simpler operations.

Choose application-controlled chunking for this project because:

- it demonstrates the full RAG flow,
- parent-child logic is custom,
- section/page metadata is explicit,
- security and evaluation steps are easy to inspect.

Choose integrated vectorization when:

- your ingestion flow is standard,
- your documents are mostly PDFs/Office files,
- you want less custom code,
- Azure AI Search indexers can reach your data source directly.

### 36.7 Validation Checklist

After choosing a chunking strategy, inspect:

- Are retrieved chunks specific enough?
- Do chunks include enough context to answer?
- Are citations useful?
- Do page and section metadata survive chunking?
- Does hybrid search find exact terms?
- Does reranking improve the top results?
- Does parent-child expansion add useful context without too much noise?

If not, tune:

- `RAG_CHUNK_SIZE`,
- `RAG_CHUNK_OVERLAP`,
- `RAG_CHILD_CHUNKS_PER_PARENT`,
- recursive separators (code-level, in `chunking.py`),
- `RAG_SEMANTIC_BREAKPOINT_PERCENTILE`,
- metadata enrichment before embedding.

---

## 37. Known Gaps and Operational Caveats

Verified against `shared/rag/src/` on 2026-07-29. None of these are bugs that
break the happy path — they are behaviours that will mislead you if you assume
otherwise, listed roughly by how likely they are to bite.

### 37.1 Atlas pre-filter fields are limited to three paths

Only `metadata.authorized_users`, `metadata.source`, and
`metadata.content_hash` are declared as `filter` type in the vector index. Any
other `metadata_filter` key makes `$vectorSearch` fail; the exception is caught
and logged, and `semantic_search()` returns `[]`. The query still answers — from
keyword and graph hits alone — so the failure is invisible in the response.

*Symptom*: filtered queries return noticeably worse answers than unfiltered ones.
*Check*: grep the logs for `Atlas $vectorSearch failed`.
*Fix*: add the field to `_VECTOR_INDEX_DEFINITION` and rebuild the Atlas index.

### 37.2 The Negotiation Agent's `hs_code` filter matches nothing

`agents/negotiation_agent/rag.py::load_rag_context()` calls:

```python
rag_query(question=..., metadata_filter={"hs_code": hs_code})
```

`hs_code` is neither an indexed Atlas filter path (37.1) nor a field any loader
writes — `loader.py` stamps `source`, `file_name`, `file_type`, `title`,
`section`, `content_hash`, `loaded_at`, the authorization defaults, and PDF page
fields, and nothing else. So the semantic leg fails to `[]` and the keyword leg
matches zero documents, meaning the agent almost always lands on its static
playbook fallback regardless of what is indexed.

Closing this needs both halves: tag documents with `hs_code` at ingestion time
*and* declare it as an Atlas filter path.

### 37.3 Ingestion job status is process-local

`router.py` keeps jobs in a module-level `_jobs: dict`. Consequences:

- job status is lost on restart — a poll after a redeploy 404s,
- with more than one uvicorn worker, `GET /rag/ingest/{job_id}` only finds the
  job if the poll lands on the worker that accepted it,
- completed jobs are never evicted, so the dict grows for the process lifetime.

`RAG_INGESTION_JOBS_COLLECTION` (`"ingestion_jobs"`) is already declared in
`config.py` but is **not referenced anywhere** — the intended MongoDB-backed
store is the obvious fix and the config slot is waiting for it.

### 37.4 Generation degrades silently

Covered in §20: a total LLM Gateway failure yields a 200 response containing a
local extractive summary, not a 5xx. Alert on the
`"Local grounded answer (LLM Gateway unavailable)"` prefix or on
`citation_validation.missing_citations`, not on HTTP status.

### 37.5 The graph leg is case-sensitive

`_extract_entities()` only matches capitalised words (plus 4–10 digit numeric
codes), so an all-lowercase question contributes nothing from the graph leg
(§15). Combined with the `neo4j` config default requiring a URI, this is why
graph retrieval is frequently enabled but silently inert.

### 37.6 The query is embedded twice

The `embed_query` node's output is never consumed; `semantic_search()`
re-embeds the question string (§12). Harmless with `LocalHashEmbeddings`,
but it doubles embedding cost and latency against a paid provider.

### 37.7 Unprefixed env vars are ignored

`config.py` sets `env_prefix="RAG_"`. Every setting needs it — `CHUNK_SIZE`,
`USE_CROSS_ENCODER_RERANKER`, `ENABLE_PDF_OCR`, `ADVANCED_EVAL_PROVIDER` and
friends do nothing on their own. Two exceptions that are genuinely unprefixed
because they are not RAG settings: `TAVILY_API_KEY` (read via `os.getenv` in
`web_search.py`) and the `LLM_EMBEDDING_*` / `LLM_PRIMARY_*` / `LLM_GROQ_API_KEY`
gateway variables. Note also that `rag_temperature` and `rag_max_tokens` become
the doubled `RAG_RAG_TEMPERATURE` / `RAG_RAG_MAX_TOKENS`.

### 37.8 Blocking work inside async nodes

`rerank` (especially with the cross-encoder, which reloads the model per call),
`select_by_knee_point`, and `evaluate_answer` are synchronous CPU-bound
functions called from async nodes. The `llm` evaluation provider additionally
blocks up to 30 s on a thread-pool bridge (§32). Fine at low concurrency;
consider `asyncio.to_thread` before scaling.

### 37.9 Stale references in code comments — resolved 2026-07-29

Previously, docstrings in `loader.py` and `cli.py` described inserting into
**Chroma** (replaced by MongoDB Atlas), and `config.py` / `vector_store.py`
named a nonexistent `ensure_vector_index()`. Those comments have been corrected,
along with the same stale name in `technical_doc.md` and
`workflow-implementation.md`. The code was always correct; only the comments
were wrong.

If you find `Chroma`, `ensure_vector_index`, or `workflow_rag.md` anywhere else,
they are leftovers — the current names are the MongoDB `chunks` collection,
`ensure_indexes()`, and this file.

---

## 38. Settings Reference (`config.py`)

Every field carries the `RAG_` prefix. Load order: `backend/.env`, then
`backend/shared/rag/.env` (module file wins on conflict).

| Setting | Default | Notes |
|---|---|---|
| `RAG_MONGO_URI` | `""` | Falls back to `MONGO_URI` |
| `RAG_MONGO_DB_NAME` | `portless` | Falls back to `MONGO_DB_NAME` |
| `RAG_CHUNKS_COLLECTION` | `chunks` | Vector-searched collection |
| `RAG_PARENTS_COLLECTION` | `parent_docs` | Parent context blocks |
| `RAG_INGESTION_JOBS_COLLECTION` | `ingestion_jobs` | **Declared but unused** (§37.3) |
| `RAG_MONGO_MAX_POOL_SIZE` | `50` | |
| `RAG_MONGO_CONNECT_TIMEOUT_MS` | `5000` | |
| `RAG_MONGO_SERVER_SELECTION_TIMEOUT_MS` | `5000` | |
| `RAG_VECTOR_INDEX_NAME` | `vector_index` | Must exist before queries run |
| `RAG_VECTOR_SIMILARITY` | `cosine` | `cosine` \| `euclidean` \| `dotProduct` |
| `RAG_CHUNK_SIZE` | `900` | |
| `RAG_CHUNK_OVERLAP` | `120` | |
| `RAG_SEMANTIC_BREAKPOINT_PERCENTILE` | `25` | Semantic strategy only |
| `RAG_CHILD_CHUNKS_PER_PARENT` | `3` | Parent fan-in |
| `RAG_RETRIEVAL_TOP_K` | `8` | Fusion widens to `× 2` |
| `RAG_RETRIEVAL_NUM_CANDIDATES` | `100` | Atlas `numCandidates`; 10–20× top-k |
| `RAG_KEYWORD_SEARCH_FETCH_LIMIT` | `500` | Docs pulled for in-memory BM25 |
| `RAG_USE_CROSS_ENCODER_RERANKER` | `false` | |
| `RAG_CROSS_ENCODER_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Reloaded per call |
| `RAG_RAG_TEMPERATURE` | `0.0` | Note the doubled prefix |
| `RAG_RAG_MAX_TOKENS` | `2048` | Note the doubled prefix |
| `RAG_ENABLE_PDF_LAYOUT_EXTRACTION` | **`true`** | On by default |
| `RAG_ENABLE_PDF_OCR` | `false` | Needs Poppler + Tesseract |
| `RAG_FILE_STORE_GRIDFS_BUCKET` | `rag_files` | GridFS bucket |
| `RAG_USE_LLM_PROMPT_INJECTION_CLASSIFIER` | `false` | Fails open |
| `RAG_ADVANCED_EVAL_PROVIDER` | `local` | `local` \| `ragas` \| `deepeval` \| `llm` |
| `RAG_GRAPH_RETRIEVAL_ENABLED` | `false` | |
| `RAG_GRAPH_RETRIEVAL_TOP_K` | `8` | |
| `RAG_GRAPH_DB_BACKEND` | **`neo4j`** | Set to `mongo` for the zero-infra path |
| `RAG_GRAPH_DB_URI` / `_USER` / `_PASSWORD` / `_DATABASE` | `""` | Neo4j only; URI required |
| `RAG_GRAPH_CHUNKS_COLLECTION` | `graph_chunks` | Mongo backend only |
| `RAG_WEB_SEARCH_ENABLED` | `false` | |
| `RAG_WEB_SEARCH_TOP_K` | `3` | |
| `TAVILY_API_KEY` | — | **Unprefixed**, read from the process env |

Not configurable via env, despite looking like they should be: the knee-point
`min_k=3` / `max_k=8` bounds, the RRF constant `rrf_k=60`, the local reranker's
`0.70/0.25/+0.05` weights, the Tavily 10 s timeout, and the recursive splitter's
separator list.

### 38.1 Variables that are NOT RAG settings

These appear in older notes under a `RAG_` prefix but do not exist. Embedding
and generation model config lives entirely in the LLM Gateway — RAG declares
none of its own:

| Wrong (does nothing) | Correct |
|---|---|
| `RAG_EMBEDDING_PROVIDER` | `LLM_EMBEDDING_PROVIDER` |
| `RAG_EMBEDDING_MODEL` | `LLM_EMBEDDING_MODEL` |
| `RAG_EMBEDDING_API_KEY` | `LLM_EMBEDDING_API_KEY` |
| `RAG_EMBEDDING_DIMENSIONS` | `LLM_EMBEDDING_DIMENSIONS` (must match the Atlas index `numDimensions`) |
| `RAG_TEMPERATURE` | `RAG_RAG_TEMPERATURE` |
| `RAG_MAX_TOKENS` | `RAG_RAG_MAX_TOKENS` |

---

## 39. Implementation Status

Legend: ✅ implemented · ⚠️ partial / optional / needs infra · ❌ not implemented

### 39.1 Where RAG sits (callers ↔ callees)

```text
Callers                        RAG                     Callees
───────                        ───                     ───────
negotiation_agent   ──▶  query() / ingest()   ──▶  MongoDB Atlas ($vectorSearch
(any service)       ──▶                            + doc fetch — shared DB)
Knowledge Base UI   ──▶  POST /rag/query      ──▶  LLM Gateway (generation
                                                    + embeddings)
POST /rag/upload    ──▶  file_store           ──▶  MongoDB GridFS (raw files,
                                                    same shared DB)
                                               ──▶  MongoDB `graph_chunks`
                                                    or Neo4j (optional graph)
                                               ──▶  Tavily (optional web leg)
```

Any service can call `from shared.rag.src import query`.

### 39.2 Core retrieval + generation — no hollow stubs in this path

| Feature | Status | File |
|---|---|---|
| Query pipeline (**14**-node LangGraph) | ✅ | `graph.py` |
| Ingestion pipeline (4-node) | ✅ | `graph.py` |
| Vector store — Atlas `$vectorSearch` | ✅ | `vector_store.py` |
| Keyword search — in-memory BM25 | ✅ | `vector_store.py` (`rank_bm25` lazy-imported) |
| Hybrid fusion — Reciprocal Rank Fusion | ✅ | `vector_store.py` |
| Parent-child context expansion | ✅ | `parent_store.py`, `vector_store.py` |
| Reranking + knee-point selection | ✅ | `rerank.py` |
| Prompt-injection safety filter | ✅ | `security.py` |
| Citation validation | ✅ | `citations.py` |
| Answer evaluation metrics | ✅ | `evaluation.py` |
| Authorization (user/team ACL) filtering | ✅ | `authorization.py` — all four legs |
| Embeddings — gateway + local-hash fallback | ✅ | `shared.llm_gateway.GatewayEmbeddings`; `embeddings.py` is a thin adapter importing no provider SDK |
| Chunking (recursive/semantic) + parent docs | ✅ | `chunking.py` |
| Document loading (PDF/text/Markdown) | ✅ | `loader.py` |
| FastAPI router + CLI | ✅ | `router.py`, `cli.py` |
| Web search — 4th retrieval leg (Tavily) | ✅ | `web_search.py`; off by default, fails open, never persisted |

### 39.3 Backends

| Backend | Status | Notes |
|---|---|---|
| Graph store — **MongoDB** | ✅ | `MongoGraphStore` — `graph_chunks` collection in the shared DB, `$setIntersection` entity-overlap retrieval. **The recommended backend**: no extra service or credentials. Set `RAG_GRAPH_DB_BACKEND=mongo` explicitly; the config default is `neo4j`. |
| Graph store — **Neo4j** | ✅ | `Neo4jGraphStore` — Cypher over the async driver; same Document/Chunk/Entity schema. Needs `RAG_GRAPH_DB_URI` (+ user/password) and `pip install neo4j`. |
| Graph store — arangodb / falkordb | ❌ | Reserved names; resolve to the no-op store. |
| Graph store — no-op | ✅ | Automatic when disabled, misconfigured, or the driver is missing. Returns empty results so the rest of the pipeline is unaffected. |
| File store — **MongoDB GridFS** | ✅ | Sole backend (`file_store.py`). S3/blob support was implemented and then **removed** — the app is MongoDB-only for every persistence need (documents, vectors, files). `test_file_store_has_no_s3_backend` guards against it returning. |

### 39.4 Gaps / partial

| Item | Status | Notes |
|---|---|---|
| Cross-encoder reranker | ⚠️ | Present but off by default; also reloads the model per call (§17b). |
| Graph entity extraction | ⚠️ | Heuristic `_extract_entities` — capitalised tokens + numeric codes, so lowercase queries retrieve nothing (§15). Real NER is the upgrade. |
| Graph scoring | ⚠️ | Unweighted entity overlap; no IDF (§15). |
| PDF OCR | ⚠️ | Off by default; needs Poppler + Tesseract system binaries. |
| Advanced eval (RAGAS / DeepEval / LLM) | ⚠️ | `local` by default; the LLM branch blocks up to 30 s per query (§32). |
| Ingestion job persistence | ⚠️ | In-process dict; config slot exists but is unwired (§37.3). |
| Atlas pre-filter coverage | ⚠️ | Three filter paths only; other filters silently void the semantic leg (§37.1). |
| Tests for the core pipeline | ❌ | `tests/test_rag_backends.py` (24 tests) covers the graph store, GridFS file store, and gateway embeddings. Fusion, knee-point, citations, evaluation, and the query graph end-to-end remain **untested**. |

---

## 40. Operational Readiness

### 40.1 "Is RAG workable if I just set the configs?"

Necessary but not sufficient. Four things must all be true:

1. **Dependencies installed** — `pip install -r shared/rag/requirements.txt`
   (`rank_bm25`, `langchain-text-splitters`, `pypdf`, …), plus `neo4j` only if
   you choose that graph backend. Note this file is **not** pulled in by the
   top-level `backend/requirements.txt`.
2. **Atlas reachable and the vector index provisioned** — `ensure_indexes()`
   runs at startup, but on clusters that reject programmatic search-index
   creation you must create it in the Atlas UI from the definition it logs.
   Missing index is the single most common "RAG returns nothing" cause.
3. **Env configured** — `MONGO_URI` at minimum. Without an embedding key the
   pipeline still runs on `LocalHashEmbeddings`, but retrieval quality is
   placeholder-grade, not semantic.
4. **Documents ingested** — the store starts empty; `ingest([...])` or
   `POST /rag/upload` must run before any query returns evidence.

### 40.2 Smoke test

The fastest end-to-end proof:

```text
install deps → set MONGO_URI + LLM_EMBEDDING_API_KEY → start app (index auto-created)
→ POST /rag/upload  (or practice-rag ingest --path ./data)
→ poll GET /rag/ingest/{job_id} until completed
→ GET /rag/stats     (total_chunks > 0 confirms ingestion landed)
→ POST /rag/query    (check the answer is not the local-extractive fallback)
```

`GET /rag/stats` is the useful diagnostic here: it reports the **active**
embedding provider/model, file-store backend, and whether graph retrieval and
web search are on — reflecting whichever `.env` actually won (root vs.
module-local), rather than what a config file appears to say.

### 40.3 Notes

- The core retrieval + generation path has **no hollow stubs**. The graph store
  is genuinely implemented on two backends; arangodb/falkordb and advanced eval
  are deliberate, gated future work.
- **MongoDB is the only persistence, vector, and blob technology in this app.**
  Vector search, keyword-search documents, parent docs, graph nodes, and
  uploaded raw files (GridFS) all live in the one shared database.
- **Verification, not code, is the real gap.** The pipeline is fully coded but
  the core query nodes are untested and unproven against live Atlas +
  embeddings.
- `__init__.py` is **lazy (PEP 562)** so submodules import without dragging in
  the whole pipeline and its optional deps — keep it that way when adding
  exports.

---

## 41. Roadmap / Nice-to-Haves

- **Real NER for graph retrieval** — replace heuristic `_extract_entities`
  (spaCy or an LLM NER call) for much higher graph recall, and fix the
  lowercase-query blind spot in one stroke.
- **IDF weighting in graph scoring** — so rare, high-signal entities outrank
  common ones.
- **MongoDB-backed ingestion job store** — wire up the already-declared
  `RAG_INGESTION_JOBS_COLLECTION` (§37.3).
- **Tests for the core query pipeline** — fusion, knee-point, citations,
  evaluation, embedding fallback.
- **Cache the cross-encoder model** and consider enabling it by default.
- **ArangoDB / FalkorDB graph backends** behind the same `GraphStore` interface.
- **RAGAS / DeepEval** as real evaluation providers rather than readiness checks.
- **Per-HS-code playbook ingestion UI** — operators upload/edit trade playbooks
  per product; pairs with fixing the `hs_code` filter gap (§37.2).
- **Dedicated intraday commodity-price feed** — the web-search leg already
  injects live pricing color; a structured exchange API (CME / Volza) would be
  more precise, but is separate, not-yet-built work.
- **Streaming answers** — once the LLM Gateway exposes `stream_chat()`.
