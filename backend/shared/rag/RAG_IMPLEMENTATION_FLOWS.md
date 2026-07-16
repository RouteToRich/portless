# RAG Implementation Flows
### Detailed Walkthrough of the Portless Shared RAG Module (`shared/rag/`)

This document explains every major flow in this RAG implementation. It is a companion to `README.md` and `workflow_rag.md`.

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
-> graph store nodes and relationships (Mongo or Neo4j backend, optional)

Query:
question + optional metadata filters -> query embedding
-> Atlas $vectorSearch + BM25 keyword search + graph search + live web search (Tavily, optional)
-> reciprocal-rank hybrid fusion -> parent-child expansion
-> reranking or cross-encoder reranking -> knee-point chunk selection
-> prompt-injection filtering -> augmented prompt -> answer (LLM Gateway)
-> citation validation -> evaluation metrics
```

### Platform integration at a glance (updated 2026-07)

| Concern | Implementation |
|---|---|
| Vector store | MongoDB Atlas `$vectorSearch` (`chunks` collection; `ensure_vector_index()` creates the index; `RAG_MONGO_URI` falls back to the shared `MONGO_URI`) |
| Embeddings | `shared.llm_gateway.GatewayEmbeddings` — model/provider/key configured once via `LLM_EMBEDDING_*`; offline `LocalHashEmbeddings` fallback when no key |
| Generation | `shared.llm_gateway.LLMClient` — primary model → Groq fallback chain, retries, MongoDB call tracking |
| Raw uploaded files | MongoDB **GridFS** (`rag_files` bucket) via `file_store.py` — the app's only blob store; listed at `GET /rag/files` |
| Parent documents | MongoDB `parent_docs` collection (`parent_store.py`) |
| Graph retrieval (optional) | `RAG_GRAPH_RETRIEVAL_ENABLED` + `RAG_GRAPH_DB_BACKEND=mongo` (same shared DB, recommended) or `neo4j` (Cypher over async driver); no-op backend when disabled/misconfigured |
| Live web search (optional) | 4th retrieval leg via Tavily (`RAG_WEB_SEARCH_ENABLED`, `TAVILY_API_KEY`); fails open to empty results — never fabricates hits; results pass the same safety filter |
| HTTP API | `POST /rag/ingest`, `POST /rag/upload`, `GET /rag/ingest/{job_id}` (async job polling), `POST /rag/query`, `DELETE /rag/documents/{source}`, `GET /rag/files`, `GET /rag/stats`, `GET /rag/health` — all gated to Portless platform staff (`require_portless`) |
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
  router.py: background ingestion job (status in `ingestion_jobs` collection,
  polled via GET /rag/ingest/{job_id})
                             |
                             v
  loader.py
  parse files and create Document(page_content, metadata)
  add section metadata for Markdown and page_number for PDF (+ layout/OCR)
                             |
                             v
  chunking.py
  split text into child chunks and add parent-child chunk metadata
  (parent documents stored in the `parent_docs` Mongo collection)
                             |
                             v
  embeddings.py -> shared.llm_gateway.GatewayEmbeddings
  embed chunk.page_content
                             |
                             v
  vector_store.py
  delete stale content_hash records, then store vector + page_content + metadata
  in the `chunks` collection (Atlas vector index via ensure_vector_index())
                             |
                             v
  graph_store.py  (optional — RAG_GRAPH_RETRIEVAL_ENABLED)
  store chunk/entity/source/section/page/parent nodes and relationships
  (backend: `mongo` graph_chunks collection, or `neo4j` via Cypher)


                   QUERY TIME  (POST /rag/query)

  user question
        |
        v
  graph.py: embed_query  (GatewayEmbeddings)
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
  hybrid_fusion  (reciprocal rank fusion across all legs)
        |
        v
  parent-child expansion  (parent_docs collection)
        |
        v
  rerank.py
  local rerank or cross-encoder rerank + knee-point selection
        |
        v
  security.py
  prompt-injection filtering (internal chunks AND web results)
        |
        v
  llm.py -> shared.llm_gateway.LLMClient
  build augmented prompt and generate answer (fallback chain, tracking)
        |
        v
  citations.py + evaluation.py
  validate citations and compute faithfulness/context precision/relevance
```

---

## Latest Implemented Enhancements

Platform integration changes (2026-07, superseding the practice-era stack):

- **MongoDB Atlas vector store**: Chroma was replaced by Atlas `$vectorSearch` on the shared app database (`chunks` collection). `ensure_vector_index()` creates the index; `RAG_MONGO_URI`/`RAG_MONGO_DB_NAME` fall back to the app-wide `MONGO_URI`/`MONGO_DB_NAME`.
- **LLM Gateway everywhere**: embeddings go through `GatewayEmbeddings` (`LLM_EMBEDDING_*` config, `LocalHashEmbeddings` offline fallback) and generation through `LLMClient` (primary model → Groq fallback chain, retries, MongoDB call tracking). RAG holds no provider SDKs or keys of its own.
- **FastAPI surface** (`router.py`): async ingestion jobs (`POST /rag/ingest`, `POST /rag/upload`, poll `GET /rag/ingest/{job_id}`), `POST /rag/query`, `DELETE /rag/documents/{source}`, `GET /rag/files|stats|health` — all endpoints require Portless platform staff. This powers the Knowledge Base UI page.
- **GridFS file persistence** (`file_store.py`): raw uploaded knowledge files are stored durably in MongoDB GridFS (`rag_files` bucket) — the app's only blob store; they survive restarts and can be re-ingested or audited.
- **Pluggable graph backends** (`graph_store.py`): the local JSON graph store became `mongo` (a `graph_chunks` collection in the same shared DB — recommended, zero new infrastructure) or `neo4j` (Cypher over the async driver), selected by `RAG_GRAPH_DB_BACKEND` and gated by `RAG_GRAPH_RETRIEVAL_ENABLED`; a safe no-op backend applies when disabled or misconfigured.
- **Live web-search retrieval leg** (`web_search.py`): optional 4th leg (Tavily, `RAG_WEB_SEARCH_ENABLED` + `TAVILY_API_KEY`) fused by RRF with the other candidates. Fails open to an empty list — a retrieval source must never fabricate stub evidence. Web results are fetched fresh per query, never persisted, and pass the same prompt-injection safety filter.
- **Negotiation Agent integration**: `agents/negotiation_agent/rag.py::load_rag_context()` calls this pipeline with a wall-clock budget (`RAG_TIMEOUT_SECONDS`) and an escape hatch (`NEGOTIATION_RAG_ENABLED=false`), falling back to a static playbook stub on any failure; the agent's per-round market intel reuses the same web-search adapter and safety filter.

The following features are implemented in the modular `shared/rag/src/` flow:

- **PDF parsing with page numbers**: `.pdf` files are parsed page by page in `loader.py`, and every page document gets `page_number`.
- **PDF layout extraction**: `pdfplumber` can extract page text, tables as Markdown, and figure counts.
- **OCR fallback**: scanned PDF pages can be OCRed with `pdf2image` and `pytesseract` when enabled.
- **Markdown section metadata**: Markdown files are split by headings, and every section document gets `section`.
- **Metadata filters during retrieval**: `practice-rag ask` supports repeated `--filter key=value` options, passed into semantic and keyword retrieval.
- **Authorization filtering**: `practice-rag ask` supports `--user` and `--team`, and retrieval results are filtered by authorization metadata.
- **Parent-child retrieval**: child chunks are grouped into parent context blocks during chunking, parent text is stored outside the `chunks` collection (in `parent_docs`), and retrieval expands matched child chunks to parent context before reranking.
- **Optional cross-encoder reranking**: enable with `USE_CROSS_ENCODER_RERANKER=true`; the local reranker remains the fallback.
- **Citation validation**: generated answers are checked for `[1]`, `[2]` style citations that map to selected chunks.
- **Prompt-injection filtering**: selected chunks are scanned with a rule classifier and optional LLM classifier before prompt augmentation.
- **Deletion and re-indexing by `content_hash`**: `VectorStore.upsert()` deletes existing records with matching content hashes before inserting fresh chunks.
- **Evaluation metrics**: each query returns local heuristic metrics and can attach RAGAS, DeepEval, or LLM-as-judge evaluation notes.
- **Graph-hybrid RAG**: ingestion writes chunk/entity/source/section/page/parent relationships to a local graph store, and query retrieval fuses graph candidates with semantic and keyword candidates.

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

If no path is passed, it uses the default `data/` folder.

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

Each file becomes a LangChain `Document`:

```python
Document(
    page_content="full text from file",
    metadata={
        "source": "...",
        "file_name": "...",
        "file_type": "...",
        "title": "...",
        "content_hash": "...",
        "loaded_at": "..."
    }
)
```

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

### Function

```python
format_document(path: Path, text: str) -> Document
```

### Document Metadata Formed Here

The metadata is formed at document level:

```python
metadata = {
    "source": str(path),
    "file_name": path.name,
    "file_type": path.suffix.lower().lstrip("."),
    "title": title,
    "content_hash": content_hash,
    "loaded_at": datetime.now(timezone.utc).isoformat(),
}
```

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
chunk_documents(documents, embeddings, strategy)
```

This function chooses between:

```text
recursive chunking
semantic chunking
```

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

```text
similarity is below threshold
or
current chunk is too large
```

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
_number_chunks(chunks, strategy)
```

### What Happens

After splitting, each chunk gets chunk-level metadata:

```python
{
    **chunk.metadata,
    "chunk_id": f"{source}::{strategy}::{chunk_index}",
    "chunk_index": chunk_index,
    "chunk_strategy": strategy,
    "chunk_chars": len(chunk.page_content),
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
        "content_hash": "...",
        "loaded_at": "...",
        "chunk_id": "...",
        "chunk_index": 0,
        "chunk_strategy": "recursive",
        "chunk_chars": 842
    }
)
```

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
    "chunk_id": "design.md::recursive::4",
    "chunk_index": 4,
    "child_index": 4,
    "parent_id": "design.md::parent::1",
    "parent_index": 1,
    "source": "design.md",
    "section": "Retrieval Flow",
}
```

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

Before insert, existing records with the same `content_hash` are deleted
(re-index on document change). The Atlas **vector index** (`vector_index`,
cosine similarity, `numDimensions` = `LLM_EMBEDDING_DIMENSIONS`) is created
programmatically by `ensure_vector_index()` — it must exist before queries run.

### Important Point

The embedding is computed explicitly through `GatewayEmbeddings` before the
Mongo write. The metadata is stored as structured fields, not as vector values.

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
no-op  -> when disabled or misconfigured: skips ingestion, returns empty
          retrieval results, and the rest of the pipeline is unaffected
```

The graph store creates:

- chunk nodes,
- entity-to-chunk relationships,
- metadata-to-chunk relationships for source, file name, file type, title, section, page number, parent ID, users, and teams.

Example relationship shape:

```text
(chunk:abc) -> (entity:veracode)
(chunk:abc) -> (section:Release Gates)
(chunk:abc) -> (page_number:12)
(chunk:abc) -> (source:security-policy.pdf)
(chunk:abc) -> (parent:policy-page-12)
```

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

```python
class QueryState(TypedDict, total=False):
    question: str
    metadata_filter: dict[str, Any]
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

This returns chunks that are semantically close to the question. If a metadata
filter is supplied it is applied as an Atlas **pre-filter** (only fields
declared as `filter` type in the vector index are supported).

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
2. graph-retrieval candidates.

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
3. graph relationship results.

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
  -> extract query entities
  -> find graph entity nodes
  -> traverse entity_to_chunks adjacency
  -> score related chunks by entity overlap and inverse entity frequency
  -> apply metadata filters
  -> apply authorization filters in graph.py
  -> return graph ScoredDocument candidates
```

Detailed runtime flow:

1. The query is tokenized into entity-like terms.
2. Graph entity nodes matching those terms are located.
3. Traversal expands to neighboring chunk nodes through relationships such as:
   - `entity -> chunk`,
   - `section -> chunk`,
   - `source -> chunk`,
   - `page_number -> chunk`,
   - `parent -> chunk`.
4. Candidate chunks are scored by relationship evidence (entity overlap and inverse entity frequency weighting).
5. Optional metadata filters are applied.
6. Authorization filtering is applied in `graph.py`.
7. Results are returned as `ScoredDocument(..., source="graph")`.

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
more shared important entities + stronger graph linkage
=> higher graph score
```

Rare, high-signal entities contribute more than very common entities.

### Best For

Graph search is useful for:

- related chunks that share the same entity,
- shared section, page, source, or parent relationships,
- technical identifiers,
- concepts spread across multiple chunks,
- questions where semantic similarity alone misses related evidence.

### Example

If the query mentions `Black Duck`, graph search can retrieve chunks connected to:

```text
entity:black
entity:duck
section:SCA Risk
source:devsecops_control_plane.md
```

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
like the other third-party connector keys), the query is sent to the Tavily
search API and the top `RAG_WEB_SEARCH_TOP_K` (default 3) results are wrapped
as `ScoredDocument(..., source="web")` with URL/title metadata.

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

The algorithm iterates over all three ranked lists and accumulates a score for
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
keyword_results   [ranked by BM25 score]         ──────┤──► hybrid_fusion (RRF)
graph_results     [ranked by entity overlap]     ─────┘         │
                                                                 ▼
                                          fused + deduplicated top-(K×2) chunks
                                                                 │
                                                         parent_child_expand
                                                                 │
                                                            rerank
                                                                 │
                                                       knee_point_select
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
2. Each child chunk carries `parent_id` and `parent_content` metadata.
3. The function deduplicates by `parent_id`.
4. The child result is expanded into the broader parent context before reranking.

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

Semantic search retrieves top `k`.

Keyword search retrieves top `k`.

Graph search retrieves `graph_retrieval_top_k`.

Hybrid fusion may keep a wider pool:

```python
top_k=settings.retrieval_top_k * 2
```

### Why Not Send Top-K Directly to the LLM?

Top-k retrieval is only the first filter.

Some retrieved chunks may still be noisy.

That is why the next steps are:

```text
reranking -> knee-point selection
```

---

## 17. Flow 14: Reranking

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
  ├── USE_CROSS_ENCODER_RERANKER=true?
  │       └── YES  ->  cross_encoder_rerank()
  │                        │
  │                        └── model unavailable? -> local_rerank() (fallback)
  │
  └── NO  ->  local_rerank()
```

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
CROSS_ENCODER_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2
```

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
select_by_knee_point(ranked)
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

### Output

```text
selected_chunks
```

These are the chunks that go into the final augmented prompt.

### Prompt-Injection Filtering Before Augmentation

Graph node:

```text
safety_filter
```

Code location:

```text
shared/rag/src/security.py
```

Function:

```python
filter_prompt_injection_chunks(chunks)
```

What happens:

1. Selected chunks are scanned for high-risk instruction patterns.
2. Examples include attempts to ignore previous instructions or reveal system prompts.
3. Suspicious chunks are removed before prompt construction.

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

Example prompt shape:

```text
You are a grounded RAG assistant.

Answer the question using only the context below.
If the context is insufficient, say what evidence is missing.
Include citations like [1] or [2].

Question:
...

Retrieved context:
[1] title=...; source=...; chunk=...
...
```

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

If every model in the chain is unavailable, the gateway raises and the query
endpoint reports the failure — generation is never silently faked.

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

The implementation computes local heuristic metrics:

- `faithfulness`: citation validity plus answer/context overlap.
- `context_precision`: fraction of selected chunks that overlap with the question.
- `answer_relevance`: overlap between the question and answer.

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
| HTTP API (ingest/upload/query/files/stats/health) | `shared/rag/src/router.py` | Mounted at `/rag/*`; Portless-staff only; async ingestion jobs in `ingestion_jobs` |
| CLI (local experimentation) | `shared/rag/src/cli.py` | `practice-rag ingest / ask` |
| Graph orchestration | `shared/rag/src/graph.py` | `build_ingestion_graph()`, `build_query_graph()` |
| Configuration | `shared/rag/src/config.py` | `RAG_*` env prefix; root `.env` then `shared/rag/.env` (module wins) |
| Parse/load + document metadata | `shared/rag/src/loader.py` | `.md`/`.txt`/`.pdf`, section + page metadata, layout/OCR options |
| Recursive / semantic chunking + chunk & parent-child metadata | `shared/rag/src/chunking.py` | |
| Embeddings | `shared/rag/src/embeddings.py` | Delegates to `shared.llm_gateway.GatewayEmbeddings` (offline hash fallback) |
| Vector DB (Atlas `$vectorSearch`), BM25 keyword search, metadata filters, content-hash re-index, RRF fusion, parent expansion | `shared/rag/src/vector_store.py` | `chunks` collection; `ensure_vector_index()` |
| Uploaded-file persistence | `shared/rag/src/file_store.py` | MongoDB GridFS (`rag_files` bucket) |
| Parent document store | `shared/rag/src/parent_store.py` | Mongo `parent_docs` collection |
| Graph DB ingestion + retrieval | `shared/rag/src/graph_store.py` | Backends: `mongo` (`graph_chunks`) / `neo4j` / no-op |
| Live web search leg | `shared/rag/src/web_search.py` | Tavily; empty on missing key/failure; reused by negotiation market intel |
| Reranking (local + cross-encoder) and knee-point | `shared/rag/src/rerank.py` | |
| Prompt-injection filtering | `shared/rag/src/security.py` | Rules + optional LLM classifier |
| Prompt augmentation + generation | `shared/rag/src/llm.py` | Via `shared.llm_gateway.LLMClient` (fallback chain, tracking) |
| Citation validation | `shared/rag/src/citations.py` | |
| Evaluation metrics | `shared/rag/src/evaluation.py` | Local metrics + RAGAS/DeepEval/LLM-judge hooks |
| Authorization filtering | `shared/rag/src/authorization.py` | `authorized_users` / `authorized_teams` metadata |

---

## 24. What Happens When You Run Ingest

Command:

```powershell
practice-rag ingest --strategy recursive
```

Flow:

```text
cli.py main()
  -> build_ingestion_graph()
  -> parse_and_load()
  -> load_documents()
  -> format_document()
  -> chunk()
  -> chunk_documents()
  -> recursive_chunk_documents()
  -> _number_chunks()
  -> embed_and_insert()
  -> VectorStore.upsert()
  -> GatewayEmbeddings.embed_documents() + Mongo chunks upsert
  -> graph_insert()
  -> GraphStore.upsert_chunks()
  -> graph nodes and relationships (mongo `graph_chunks` collection or Neo4j;
     no-op when RAG_GRAPH_RETRIEVAL_ENABLED is false)
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
  -> embed_query()
  -> semantic_search()
  -> VectorStore.semantic_search()
  -> keyword_search()
  -> VectorStore.keyword_search()
  -> graph_search()
  -> GraphStore.retrieve()
  -> web_search()
  -> Tavily search (empty when disabled / key absent)
  -> fuse_results()
  -> hybrid_fusion()
  -> parent_child_expand()
  -> expand_parent_context()
  -> rerank_results()
  -> rerank()
  -> knee_point_select()
  -> select_by_knee_point()
  -> safety_filter()
  -> filter_prompt_injection_chunks()
  -> augment()
  -> build_augmented_prompt()
  -> generate()
  -> generate_answer()
  -> validate_answer_citations()
  -> validate_citations()
  -> evaluate()
  -> evaluate_answer()
```

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

The system uses three retrieval signals:

- vector semantic search,
- BM25 keyword search,
- graph relationship search.

This gives better recall than either method alone.

### Graph-Hybrid Retrieval

The graph store adds entity and relationship recall.

It helps when:

- the query mentions an entity that appears across multiple chunks,
- related evidence is connected by section, page, source, or parent metadata,
- exact keywords are sparse,
- the answer needs relationship context.

The final candidate pool is:

```text
semantic candidates + keyword candidates + graph candidates -> reciprocal rank fusion
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
- Graph DB based hybrid retrieval with chunk/entity/metadata relationships.

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

`load_pdf_documents()` now tries layout-aware parsing first when:

```env
ENABLE_PDF_LAYOUT_EXTRACTION=true
```

The layout path uses `pdfplumber` when available.

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
ENABLE_PDF_OCR=true
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

After semantic and keyword retrieval, the graph filters results by:

- `authorized_users`
- `authorized_teams`
- public wildcard `*`

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
classify_prompt_injection(text)
has_prompt_injection_risk(text)
llm_prompt_injection_assessment(text)
filter_prompt_injection_chunks(chunks)
```

### What Was Implemented

The prompt-injection filter now has two layers:

1. Deterministic rule classifier.
2. Optional LLM classifier.

Enable the LLM classifier with:

```env
USE_LLM_PROMPT_INJECTION_CLASSIFIER=true
```

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
ADVANCED_EVAL_PROVIDER=local
ADVANCED_EVAL_PROVIDER=ragas
ADVANCED_EVAL_PROVIDER=deepeval
ADVANCED_EVAL_PROVIDER=llm
```

Behavior:

- `local`: uses only local lexical metrics.
- `ragas`: checks RAGAS availability and reports integration readiness.
- `deepeval`: checks DeepEval availability and reports integration readiness.
- `llm`: runs a compact LLM-as-judge prompt if OpenAI is configured.

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
save_parent_documents(parent_documents)
load_parent_document(parent_id)
delete_parent_documents(parent_ids)
```

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
load_parent_document(parent_id)
```

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
| `graph_store.py` (mongo `graph_chunks` / Neo4j backends) | Entity/chunk/source/section/page/parent relationship retrieval | Azure Cosmos DB Gremlin API, Azure Cosmos DB for NoSQL adjacency model, Azure SQL graph tables, Neo4j on Azure Marketplace |
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

If you replace the local JSON `graph_store.py` with an Azure graph-style store, model the graph like this:

```text
Nodes:
  Chunk(chunk_id, source, section, page_number, parent_id)
  Entity(value)
  Source(path)
  Section(name)
  Page(number)
  Parent(parent_id)

Edges:
  Chunk -[:MENTIONS]-> Entity
  Chunk -[:IN_SOURCE]-> Source
  Chunk -[:IN_SECTION]-> Section
  Chunk -[:ON_PAGE]-> Page
  Chunk -[:HAS_PARENT]-> Parent
```

Azure options:

- **Azure Cosmos DB Gremlin API**: best fit when you want graph traversals with Gremlin.
- **Azure Cosmos DB for NoSQL adjacency model**: good when you want a simple managed document database and can implement traversal in application code.
- **Azure SQL graph tables**: useful when your enterprise already standardizes on SQL Server or Azure SQL.
- **Neo4j on Azure**: useful when graph traversal and graph algorithms are central to the product.

In this app, graph retrieval does this:

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

For this project:

```env
CHUNK_SIZE=900
CHUNK_OVERLAP=120
CHILD_CHUNKS_PER_PARENT=3
```

Use recursive chunking by default:

```powershell
practice-rag ingest --strategy recursive
```

Use semantic chunking when:

- documents mix multiple topics,
- headings are weak or missing,
- retrieval returns neighboring but not exact content,
- paragraphs are long and concept boundaries matter.

```powershell
practice-rag ingest --strategy semantic
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

- `CHUNK_SIZE`,
- `CHUNK_OVERLAP`,
- `CHILD_CHUNKS_PER_PARENT`,
- recursive separators,
- semantic breakpoint percentile,
- metadata enrichment before embedding.
