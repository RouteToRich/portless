# RAG — Workflow & Implementation Reference

> **Module:** `shared/rag/` (code in `src/`)
> **Purpose:** production retrieval-augmented generation — MongoDB Atlas vector store + hybrid retrieval + grounded LLM answers.
> **Engine:** two async LangGraph pipelines (ingestion + query).
> **Persistence:** MongoDB is the *only* store used anywhere in RAG — vector search, keyword-search documents, parent docs, and uploaded raw files (GridFS) all live in the **single shared app database** (`MONGO_DB_NAME`, default `portless`), in their own collections. No blob store (S3, etc.) or separate vector DB is used.
> **Last reviewed:** 2026-07-10; web-search retrieval leg documented 2026-07-14 (was already implemented, previously undocumented here).
> Legend: ✅ implemented · ⚠️ partial / optional / needs infra · ❌ not implemented · 💡 nice-to-have
> See also: `README.md`, `RAG_IMPLEMENTATION_FLOWS.md` (deep dives), `workflow_llm_gateway.md` (generation backend).

---

## 1. Purpose

A self-contained RAG engine any service can call to answer a question grounded in an indexed corpus. It combines **vector + keyword + (optional) graph** retrieval with reranking, prompt-injection safety, citation validation, and an evaluation pass — all async so nothing blocks the event loop.

**Primary consumer today:** the Negotiation Agent (`load_rag_context(hs_code)` → `query(...)` for product playbooks). Any service can call `from shared.rag.src import query`.

---

## 2. Where it sits (callers ↔ callees)

```
Callers                         RAG                          Callees
───────                         ───                          ───────
negotiation_agent   ──▶  query() / ingest()   ──▶  MongoDB Atlas ($vectorSearch + doc fetch — shared DB)
(any service)       ──▶                        ──▶  LLM Gateway (answer generation + embeddings)
POST /rag/upload    ──▶  file_store            ──▶  MongoDB GridFS (raw uploaded files — same shared DB)
                                               ──▶  Neo4j (optional graph retrieval)
```

---

## 3. Workflow

### Ingestion pipeline (`ingest(paths)`)
```
parse_and_load → chunk (+persist parent docs) → embed_and_insert (Atlas) → graph_insert (Neo4j/noop) → END
```

### Query pipeline (`query(question, metadata_filter?, user_id?, team_id?)`) — 14 nodes
```
embed_query
  → semantic_search  ┐
    keyword_search   │
    graph_search      ├─ (each ACL-filtered, populates its own state key) → hybrid_fusion (RRF) → parent_child_expand
    web_search        ┘
  → rerank → knee_point_select → safety_filter (prompt-injection)
  → augment → generate (LLM Gateway) → validate_citations → evaluate → END
```
`web_search` is a fourth retrieval leg (Tavily, `src/web_search.py`) — a
no-op (empty result, not an error) when `RAG_WEB_SEARCH_ENABLED` is false or
`TAVILY_API_KEY` is unset. Unlike the internal legs, web results are fetched
fresh per query and never persisted to the vector store, but flow through
the exact same fusion → rerank → prompt-injection safety filter path as
everything else before reaching the generation prompt. (The four search
nodes execute as sequential LangGraph edges, not concurrently — each just
writes its own `*_results` state key, so `hybrid_fusion` sees the same
merged inputs regardless of execution order.)
Returns a `QueryState` dict with `answer`, `selected_chunks`, `citation_validation`, `evaluation_metrics`, and the intermediate result sets.

---

## 4. Configuration reference (`RAG_` prefix)

| Area | Vars | Notes |
|---|---|---|
| MongoDB | `RAG_MONGO_URI` / `RAG_MONGO_DB_NAME` (fall back to the app-wide `MONGO_URI` / `MONGO_DB_NAME`), `*_COLLECTION` | No hardcoded credentials — resolves from env only (fixed 2026-07-10). Writes into the single shared app database, same as every other service. |
| Vector index | `RAG_VECTOR_INDEX_NAME`, `RAG_EMBEDDING_DIMENSIONS` (1536), `RAG_VECTOR_SIMILARITY` | Index **must exist** — `ensure_vector_index()` or Atlas UI. |
| Embeddings | `RAG_EMBEDDING_PROVIDER` (openai/local), `RAG_EMBEDDING_MODEL`, `RAG_EMBEDDING_API_KEY` | Routed through the LLM Gateway's `GatewayEmbeddings`; falls back to `LocalHashEmbeddings` if no key. |
| Retrieval / rerank | `RAG_RETRIEVAL_TOP_K`, `RAG_KEYWORD_SEARCH_FETCH_LIMIT`, `RAG_USE_CROSS_ENCODER_RERANKER` | Cross-encoder off by default. |
| Generation | `RAG_TEMPERATURE`, `RAG_MAX_TOKENS` | Via LLM Gateway. |
| **Graph** | `RAG_GRAPH_RETRIEVAL_ENABLED`, `RAG_GRAPH_DB_BACKEND` (neo4j), `RAG_GRAPH_DB_URI/USER/PASSWORD/DATABASE` | Neo4j implemented; arangodb/falkordb → no-op. |
| **Web search** | `RAG_WEB_SEARCH_ENABLED` (default false), `RAG_WEB_SEARCH_TOP_K` (default 3) | Tavily free tier; provider key (`TAVILY_API_KEY`) lives in the root `.env` like other third-party connector keys, not under the `RAG_` prefix. Fails open (empty result) — never fabricates a hit. Shared by the Negotiation Agent's own market-intel fetch (`NEGOTIATION_MARKET_SEARCH_ENABLED`). |
| **File store** | `RAG_FILE_STORE_GRIDFS_BUCKET` (default `rag_files`) | **MongoDB GridFS only** — the sole backend. No S3/blob-store config exists anywhere in the app (removed 2026-07-10). |

---

## 5. Implementation status

### Core retrieval + generation (fully functional — no hollow stubs in this path)

| Feature | Status | File |
|---|---|---|
| Query pipeline (13-node LangGraph) | ✅ | `graph.py` |
| Ingestion pipeline (4-node) | ✅ | `graph.py` |
| Vector store — Atlas `$vectorSearch` | ✅ | `vector_store.py` |
| Keyword search — in-memory BM25 | ✅ | `vector_store.py` (`rank_bm25` lazy-imported) |
| Hybrid fusion — Reciprocal Rank Fusion | ✅ | `vector_store.py` |
| Parent-child context expansion | ✅ | `parent_store.py`, `vector_store.py` |
| Reranking + knee-point selection | ✅ | `rerank.py` |
| Prompt-injection safety filter | ✅ | `security.py` |
| Citation validation | ✅ | `citations.py` |
| Answer evaluation metrics | ✅ | `evaluation.py` |
| Authorization (user/team ACL) filtering | ✅ | `authorization.py` |
| Embeddings — OpenAI + local-hash fallback | ✅ | Routed through `shared.llm_gateway.GatewayEmbeddings` (2026-07-10); RAG imports no provider SDK directly. `embeddings.py` is a thin adapter. |
| Chunking (recursive/semantic) + parent docs | ✅ | `chunking.py` |
| Document loading (PDF/text) | ✅ | `loader.py` |
| FastAPI router + CLI | ✅ | `router.py`, `cli.py` |
| Web search — 4th retrieval leg (Tavily) | ✅ | `web_search.py`: gated by `RAG_WEB_SEARCH_ENABLED` (default off); fails open on missing key/HTTP error; fetched fresh per query, never persisted; screened by the same prompt-injection filter as internal chunks |

### Backends (added 2026-07-10)

| Backend | Status | Notes |
|---|---|---|
| Graph store — **Neo4j** | ✅ | `graph_store.py`: Document/Chunk/Entity nodes, PART_OF/MENTIONS/ADJACENT_TO edges; entity-overlap retrieval; delegating wrapper + graceful no-op fallback. Gated by `RAG_GRAPH_RETRIEVAL_ENABLED`. |
| Graph store — arangodb / falkordb | ❌ | Reserved; fall back to no-op. |
| File store — **MongoDB GridFS** | ✅ | Sole backend (`file_store.py`). S3/blob support was implemented then **removed 2026-07-10** — the app is MongoDB-only for every persistence need (documents, vectors, files). |

### Gaps / partial

| Item | Status | Notes |
|---|---|---|
| Cross-encoder reranker | ⚠️ | Present but off (`RAG_USE_CROSS_ENCODER_RERANKER=false`); default rerank is lighter-weight. |
| Graph entity extraction | ⚠️ | Heuristic (`_extract_entities`) — capitalised tokens + codes; recall-limited. Real NER is the upgrade. |
| PDF OCR | ⚠️ | `RAG_ENABLE_PDF_OCR=false` by default. |
| Advanced eval (RAGAS/DeepEval/LLM) | ⚠️ | `RAG_ADVANCED_EVAL_PROVIDER=local` by default; others reserved. |
| **Tests for the core pipeline** | ❌ | Only `tests/test_rag_backends.py` (15, graph + S3) exists. Fusion, knee-point, citations, evaluation, embedding fallback — **untested**. |

---

## 6. Nice-to-have features (💡)

- **Real NER for graph retrieval** — replace the heuristic `_extract_entities` (spaCy / LLM NER) for much higher graph recall.
- **Dedicated intraday commodity-price feed** — the web-search leg (above) already injects live web pricing/demand color; a structured exchange-price API (CME/Comdty/Volza) would be more precise but is separate, not-yet-built work.
- **ArangoDB / FalkorDB graph backends** — behind the same `GraphStore` interface.
- **Cross-encoder reranker on by default** — better top-k ordering (`use_cross_encoder_reranker`).
- **RAGAS / DeepEval** advanced evaluation providers.
- **Per-HS-code playbook ingestion UI** — operators upload/edit trade playbooks per product.
- **Streaming answers** — once the LLM Gateway exposes `stream_chat()`.

---

## 7. Pending tasks

1. **Install RAG's Python deps** — `pip install -r shared/rag/requirements.txt` (rank_bm25, langchain-text-splitters, scikit-learn, pypdf, …) + `neo4j` if graph retrieval is enabled. *Currently not installed in this env — the module can't run without them.*
2. **Create the Atlas vector-search index** — `ensure_vector_index()` at startup or via the Atlas UI. `$vectorSearch` fails without it (the #1 "not working" cause).
3. **Ingest documents** — the vector store starts **empty**; run `ingest([...])` before querying, or every query returns nothing.
4. **Write tests for the core pipeline** — fusion, knee-point, citation validation, evaluation, embedding fallback are all uncovered.
5. **(optional) Upgrade graph entity extraction** to real NER for recall.

> **"Is RAG workable if I set the configs?"** — Necessary but not sufficient. You also need: **(1)** deps installed, **(2)** Atlas + vector index provisioned (and Neo4j if graph retrieval is enabled), **(3)** the env configs (`MONGO_URI` at minimum), **(4)** documents ingested. All four → workable.

---

## 8. Suggestions / notes

- The **core retrieval + generation path has no hollow stubs** — the formerly-stubbed graph DB piece is now implemented (Neo4j); arangodb/falkordb and advanced eval remain deliberate, gated future work.
- **MongoDB is the only persistence/vector/blob-storage technology in this app.** Vector search, keyword-search documents, parent docs, and uploaded raw files (GridFS) all live in the one shared database. S3/blob-store support was implemented on 2026-07-10 and **removed the same day** in favor of this MongoDB-only design — there is no external object store anywhere.
- **Verification, not code, is the real gap:** the pipeline is fully coded but the core nodes are untested, and nothing has been proven against live Atlas + embeddings. A one-shot smoke run (install deps → set `MONGO_URI` + embedding key → create index → `ingest()` → `query()`) is the fastest way to confirm end-to-end.
- The package `__init__.py` is **lazy (PEP 562)** so submodules import without dragging in the whole pipeline + optional deps — keep it that way when adding exports.

---

## 9. Files

| File | Role |
|---|---|
| `src/graph.py` | Ingestion + query LangGraph pipelines; public `query()` / `ingest()` |
| `src/vector_store.py` | Atlas `$vectorSearch`, BM25 keyword, RRF fusion, parent expansion |
| `src/embeddings.py` | `GatewayEmbeddings` (OpenAI) + `LocalHashEmbeddings` fallback |
| `src/chunking.py` · `src/parent_store.py` · `src/loader.py` | Chunking, parent-doc store, document loading |
| `src/rerank.py` · `src/citations.py` · `src/evaluation.py` · `src/security.py` · `src/authorization.py` | Rerank, citations, eval, injection filter, ACL |
| `src/graph_store.py` | **Neo4j** graph store + no-op fallback + entity extraction |
| `src/web_search.py` | `WebSearchStore` — Tavily-backed 4th retrieval leg, fails open |
| `src/file_store.py` | MongoDB GridFS — the sole file-storage backend (no S3) |
| `src/config.py` | `RAGSettings` (env `RAG_`); no hardcoded credentials — resolves `MONGO_URI`/`MONGO_DB_NAME` |
| `src/router.py` · `src/cli.py` | FastAPI routes + CLI |
| `tests/test_rag_backends.py` | Graph store + GridFS file store + gateway-embeddings tests (18) |
