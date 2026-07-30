# RAG Worked Examples — Input/Output Trace

> Companion to [`RAG_IMPLEMENTATION_FLOWS.md`](RAG_IMPLEMENTATION_FLOWS.md) (architecture reference).
> This file traces **two concrete documents** through the entire pipeline — ingestion
> (parse → chunk → embed → graph-insert) and query (embed → semantic/keyword/graph
> search → fusion → parent expansion → rerank → knee-point → safety filter → augment
> → generate) — showing the actual shape of data at every node boundary. Numbers are
> illustrative but structurally accurate to the code in `src/`.
>
> Code referenced: [`chunking.py`](src/chunking.py), [`vector_store.py`](src/vector_store.py),
> [`graph_store.py`](src/graph_store.py), [`graph.py`](src/graph.py), [`rerank.py`](src/rerank.py),
> [`llm.py`](src/llm.py).

---

## 0. Tools, libraries & models used in this flow (+ alternatives)

Every stage below is tagged with **[Tool: …]** at the point it happens in the
worked examples. This table is the reference key for those tags — what's
actually wired up today (`backend/requirements.txt`, `shared/llm_gateway/`,
`shared/rag/src/config.py`) versus what the code already supports as a
drop-in alternative versus what would need new code.

| Stage | Used today | Alternatives already wired (config flag) | Alternatives not wired (would need new code) |
|---|---|---|---|
| **PDF parsing** | [`pypdf`](https://pypi.org/project/pypdf/) (plain text) — upgraded to [`pdfplumber`](https://pypi.org/project/pdfplumber/) when `RAG_ENABLE_PDF_LAYOUT_EXTRACTION=true` (default), which adds table→Markdown extraction + figure counts | `pypdf` fallback if `pdfplumber` import fails or returns nothing (`loader.py:167-175`) | Unstructured.io, Azure Document Intelligence, LlamaParse |
| **OCR (scanned PDFs)** | none by default | `pdf2image` + `pytesseract` (Tesseract) when `RAG_ENABLE_PDF_OCR=true` — both are optional/commented in `requirements.txt`, install manually (`loader.py:289-322`) | AWS Textract, Google Document AI OCR |
| **Markdown/text parsing** | Python stdlib (`pathlib`, regex heading matcher) — no external library | — | — |
| **Chunking — recursive** | [`langchain-text-splitters`](https://pypi.org/project/langchain-text-splitters/) `RecursiveCharacterTextSplitter` (`chunk_size=900`, `chunk_overlap=120`) | — (this *is* the default strategy) | — |
| **Chunking — semantic** | Custom cosine-breakpoint splitter (`chunking.py::semantic_chunk_documents`) using `numpy` for cosine similarity over `Embeddings.embed_documents()` output. **Quality depends entirely on the embedding backend** — needs a real semantic model (`LLM_EMBEDDING_PROVIDER=openai`/`gemini`) to produce meaningful breakpoints; under the zero-cost `LocalHashEmbeddings` default it degrades to bag-of-words similarity. Worked example: A.3c. | Selected via `chunk_strategy="semantic"` param to `ingest()` — same embedding backend as everything else, no separate model | LangChain's own `SemanticChunker` (langchain-experimental), unstructured's chunk-by-title |
| **Embeddings** | Routed through `shared/llm_gateway/embeddings.py::GatewayEmbeddings` — provider selected by `LLM_EMBEDDING_PROVIDER`. Default when no key is set: **`LocalHashEmbeddings`** (scikit-learn `HashingVectorizer`, 1536-dim, deterministic, zero cost, not semantically meaningful) | `LLM_EMBEDDING_PROVIDER=openai` → `langchain_openai.OpenAIEmbeddings` (`text-embedding-3-small`, 1536-dim, real semantic vectors); `LLM_EMBEDDING_PROVIDER=gemini` → `google-genai` SDK direct (`GeminiEmbeddings`, MRL-truncatable dimensions, asymmetric RETRIEVAL_DOCUMENT/RETRIEVAL_QUERY task types) | Cohere embed, Voyage AI, self-hosted `sentence-transformers` bi-encoder |
| **Vector store / index** | **MongoDB Atlas `$vectorSearch`** (native aggregation stage, cosine similarity by default — `RAG_VECTOR_SIMILARITY`) — same shared cluster as every other collection, no separate vector DB service | euclidean / dotProduct similarity via `RAG_VECTOR_SIMILARITY` (same Atlas index, just a different metric) | Pinecone, Weaviate, Qdrant, pgvector, Chroma (note: the *stale* top-level `README.md` in this folder documents an old Chroma-based practice version — see its banner) |
| **Keyword search** | In-memory **BM25Okapi** via [`rank-bm25`](https://pypi.org/project/rank-bm25/), run over up to `RAG_KEYWORD_SEARCH_FETCH_LIMIT=500` Mongo-fetched docs per query — no separate search index | — | MongoDB Atlas Search (`$search` full-text stage) — called out in `vector_store.py`'s module docstring as the natural upgrade path past ~500k chunks; Elasticsearch/OpenSearch BM25 |
| **Graph store** | Off by default (`RAG_GRAPH_RETRIEVAL_ENABLED=false`). When enabled: **`RAG_GRAPH_DB_BACKEND=mongo`** (recommended) — plain Mongo documents + `$setIntersection` aggregation, same shared cluster, no new service | `RAG_GRAPH_DB_BACKEND=neo4j` — real graph DB via the async [`neo4j`](https://pypi.org/project/neo4j/) driver + Cypher (`MERGE`/`MATCH` statements in `graph_store.py`) | `arangodb` / `falkordb` — reserved in config, resolve to a no-op stub until backend code is written |
| **Entity extraction (graph ingest)** | Zero-dependency regex heuristic — capitalised tokens (`_ENTITY_RE`) + 4–10 digit numeric codes (`_CODE_RE`), minus a hardcoded stopword list (`graph_store.py:47-84`). **No model required; $0 cost.** See A.5.1 for the full cost/tradeoff comparison. | spaCy NER (local, still free, adds latency) | An LLM-based NER call via `LLMClient` (≈$0.00007/chunk on the Groq fallback chain — see A.5.1) — both explicitly called out in the module docstring as swap-in replacements for `_extract_entities`, no other code would need to change |
| **Web search (4th retrieval leg)** | Off by default (`RAG_WEB_SEARCH_ENABLED=false`). When enabled: **[Tavily](https://tavily.com/) Search API** via raw `httpx` POST (no SDK) — free tier | — | Bing Search API, SerpAPI, Google Programmable Search |
| **Fusion (semantic+keyword+graph+web → one ranked list)** | **Reciprocal Rank Fusion (RRF)**, `rrf_k=60`, hand-implemented in `vector_store.py::hybrid_fusion` — no library | — | Weighted linear score combination, Cohere Rerank-as-fusion |
| **Parent-child expansion** | Custom `MongoParentStore` lookup by `parent_id` (`parent_store.py`) — plain MongoDB collection, no library | — | LangChain's own `ParentDocumentRetriever` (same concept, different storage — would replace this custom store) |
| **Reranking** | **Local lexical reranker** by default (`rerank.py::local_rerank` — term overlap + retrieval score blend, `numpy` only, no model download) | `RAG_USE_CROSS_ENCODER_RERANKER=true` → [`sentence-transformers`](https://pypi.org/project/sentence-transformers/) `CrossEncoder` (default model `cross-encoder/ms-marco-MiniLM-L-6-v2`, config `RAG_CROSS_ENCODER_MODEL`) | Cohere Rerank API, Voyage rerank, `bge-reranker` served via an inference endpoint |
| **Context-window sizing** | Custom **knee-point heuristic** (`rerank.py::select_by_knee_point`) — max-distance-from-chord over the normalized score curve, `numpy` only, `min_k=3`/`max_k=8` | — | Fixed top-k cutoff, score-threshold cutoff, LLM-judged relevance cutoff |
| **Prompt-injection safety filter** | Regex heuristics by default (`security.py::PROMPT_INJECTION_PATTERNS`, 7 patterns, zero cost) | `RAG_USE_LLM_PROMPT_INJECTION_CLASSIFIER=true` → LLM-as-judge via the same `LLMClient` gateway (SAFE/RISKY classification call) | A dedicated moderation-model endpoint (e.g. Llama Guard) |
| **Answer generation (augmentation → LLM)** | `shared/llm_gateway/client.py::LLMClient` — provider selected by `LLM_PRIMARY_CONNECTOR` (`groq`\|`openai`\|`anthropic`\|`http`; default `http`). Falls through a fixed Groq chain when the primary is unavailable/unset: `qwen/qwen3-32b` → `qwen/qwen3.6-27b` → `openai/gpt-oss-safeguard-20b` | Any of `groq` / `openai` / `anthropic` as `LLM_PRIMARY_CONNECTOR` | Any other LangChain chat-model integration (would need a new connector in the gateway) |
| **Local fallback answer** | Plain-Python extractive summary (`llm.py::_local_extractive_answer`) — fires only if the entire LLM Gateway chain fails; no library, guarantees the pipeline never raises on generation | — | — |
| **Citation validation** | Regex citation-marker extraction (`citations.py`) — no library | — | — |
| **Answer evaluation** | Local heuristic metrics by default (`RAG_ADVANCED_EVAL_PROVIDER=local` — grounding overlap, citation coverage) | `RAG_ADVANCED_EVAL_PROVIDER` also accepts `ragas` \| `deepeval` \| `llm` in config (their actual SDK integration is not shown in the files traced here — check `evaluation.py` before relying on the non-`local` paths) | Human eval, RAGAS end-to-end (if not already fully wired) |
| **Orchestration (both pipelines)** | [`langgraph`](https://pypi.org/project/langgraph/) `StateGraph` — every node in this document (`_chunk`, `_embed_and_insert`, `_semantic_search`, etc.) is a graph node | — | Plain async function chaining (no framework), LlamaIndex query pipelines |
| **Doc/vector object model** | [`langchain-core`](https://pypi.org/project/langchain-core/) `Document` / `Embeddings` — the common type every stage passes around | — | — |
| **MongoDB driver** | [`motor`](https://pypi.org/project/motor/) (async PyMongo) via `shared/llm_gateway/mongo_connection.py`'s shared connection pool — one client reused by vector store, parent store, graph store | — | — |

**Reading the tags below:** `[Tool: X]` next to a step means *this is the
concrete library/service doing that step in the trace that follows* — not a
hypothetical. Config-gated alternatives are noted inline where relevant.

---

## 0.1 Function-level call map — which function of which tool runs at each stage

§0 answers *"which library"*. This section answers *"which call"* — the exact
API surface this pipeline touches, so a stage can be traced from our node
function down to the third-party method that does the work. Everything below is
read off `shared/rag/src/` and `shared/llm_gateway/`; nothing is illustrative.

### Ingestion pipeline (`build_ingestion_graph()`)

| Node | Our function (`shared/rag/src/`) | Third-party / gateway call it makes |
|---|---|---|
| *(orchestration)* | `graph.py::build_ingestion_graph()`, `ingest()` | `langgraph`: `StateGraph(IngestionState)`, `.add_node()`, `.add_edge()`, `.set_entry_point()`, `.compile()`, `.ainvoke()` |
| `parse_and_load` | `graph.py::_parse_and_load` → `loader.py::load_documents()` | **PDF (layout):** `load_pdf_documents_with_layout()` → `pdfplumber.open()`, `page.extract_text()`, `page.extract_tables()`<br>**PDF (fallback):** `load_pdf_documents()` → `pypdf.PdfReader()`, `page.extract_text()`<br>**OCR (opt-in):** `ocr_pdf_page()` → `pdf2image.convert_from_path()`, `pytesseract.image_to_string()`<br>**MD/TXT:** stdlib only — `pathlib.Path.read_text()`, `_split_markdown_sections()`, `_extract_title()` |
| `chunk` | `graph.py::_chunk` → `chunking.py::chunk_documents()` | **recursive:** `RecursiveCharacterTextSplitter(chunk_size, chunk_overlap, separators).split_documents()`<br>**semantic:** `Embeddings.embed_documents(units)` + `numpy` `np.percentile` / `np.dot` / `np.linalg.norm`<br>**both:** `_number_chunks()` → `_chunk_group_key()`, `_build_parent_groups()` (stdlib) |
| `chunk` (parent persist) | `parent_store.py::MongoParentStore.save()` | `motor`: `collection.bulk_write([ReplaceOne(…, upsert=True)])` |
| `embed_and_insert` | `graph.py::_embed_and_insert` → `vector_store.py::VectorStore.upsert()` | `GatewayEmbeddings.embed_documents(texts)` → one of `OpenAIEmbeddings` / `GeminiEmbeddings` / `LocalHashEmbeddings.embed_documents()`<br>`pymongo.ReplaceOne`, `motor` `collection.bulk_write(ops, ordered=False)`<br>dedupe: `delete_by_content_hashes()` → `collection.delete_many()` |
| `graph_insert` | `graph.py::_graph_insert` → `graph_store.py::GraphStore.upsert_chunks()` → `_chunk_fields()` → `_extract_entities()` | **extraction:** stdlib `re` only — `_ENTITY_RE.finditer()`, `_CODE_RE.finditer()` (**no model** — A.5.1)<br>**mongo:** `pymongo.UpdateOne`, `collection.bulk_write(ordered=False)`<br>**neo4j:** `AsyncGraphDatabase.driver()`, `session.run(_UPSERT_CHUNKS_CYPHER / _UPSERT_ENTITIES_CYPHER / _ADJ_CYPHER)` |
| *(startup, once)* | `vector_store.py::ensure_indexes()`, `graph_store.py::ensure_graph_indexes()` | `motor`: `conn.ensure_indexes()` → `create_index()` for regular indexes; the Atlas `$vectorSearch` index goes through a raw `col.database.command("createSearchIndexes", …)` — free-tier clusters reject it, which is logged as a warning and never blocks startup |

### Query pipeline (`build_query_graph()`)

| Node | Our function (`shared/rag/src/`) | Third-party / gateway call it makes |
|---|---|---|
| *(orchestration)* | `graph.py::build_query_graph()`, `query()` | `langgraph` `StateGraph(QueryState)` → `.compile()`, `.ainvoke()` |
| `embed_query` | `graph.py::_embed_query` → `embeddings.py::get_embeddings()` | `GatewayEmbeddings.embed_query(question)` |
| `semantic_search` | `vector_store.py::VectorStore.semantic_search()` | `embed_query()`, then `motor` `collection.aggregate([{"$vectorSearch": …}])` |
| `keyword_search` | `vector_store.py::VectorStore.keyword_search()` | `motor` `collection.find()`, then `rank_bm25.BM25Okapi(tokenized)` + `bm25.get_scores(_tokenize(query))` |
| `graph_search` | `graph_store.py::GraphStore.retrieve()` | `_extract_entities(question)` (stdlib `re`), then `collection.aggregate([$match, $project{$setIntersection}, $sort, $limit])` — or `session.run(_RETRIEVE_CYPHER)` on Neo4j |
| *(all 3 legs)* | `authorization.py::filter_authorized_results()` | none — plain Python filtering |
| `web_search` | `web_search.py::WebSearchStore.retrieve()` | `httpx.AsyncClient().post()` → Tavily REST (no SDK) |
| `hybrid_fusion` | `vector_store.py::hybrid_fusion()` | none — RRF hand-implemented, `1/(k+rank)` |
| `parent_child_expand` | `vector_store.py::expand_parent_context()` → `MongoParentStore.load()` | `motor` `collection.find_one({"parent_id": …})` |
| `rerank` | `rerank.py::rerank()` | **default:** `local_rerank()` → `_tokenize()` + `numpy`<br>**opt-in:** `cross_encoder_rerank()` → `sentence_transformers.CrossEncoder(model)`, `model.predict(pairs)` |
| `knee_point_select` | `rerank.py::select_by_knee_point()` | `numpy`: `np.asarray`, `np.allclose`, `np.linspace`, `np.cross`, `np.linalg.norm`, `np.argmax` |
| `safety_filter` | `security.py::afilter_prompt_injection_chunks()` | **default:** `has_prompt_injection_risk()` — stdlib `re` over `PROMPT_INJECTION_PATTERNS`<br>**opt-in:** `aclassify_prompt_injection()` → `_llm_injection_assessment()` → `LLMClient().chat(caller="rag.security.injection_classifier")` |
| `augment` | `llm.py::build_augmented_prompt()` | none — string assembly |
| `generate` | `llm.py::generate_answer()` | `LLMClient().chat(messages=[{"role": "user", …}], caller="rag.generate", temperature=settings.rag_temperature, max_tokens=settings.rag_max_tokens)` → `.content`<br>on any exception: `_local_extractive_answer()` (stdlib, cannot fail) |
| `validate_citations` | `citations.py::validate_citations()` | none — stdlib `re` |
| `evaluate` | `evaluation.py::evaluate_answer()` | **default:** `_faithfulness()`, `_context_precision()`, `_term_overlap()`, `_tokenize()` — stdlib<br>**opt-in:** `advanced_evaluation_notes()` → `_ragas_notes()` / `_deepeval_notes()` / `_llm_judge_notes()` → `LLMClient().chat(caller="rag.evaluation.llm_judge")` |

### Shared infrastructure (every Mongo-touching stage)

| Concern | Function | Underlying call |
|---|---|---|
| Connection pool | `shared/llm_gateway/mongo_connection.py::get_connection(uri, db_name)` | `motor.motor_asyncio.AsyncIOMotorClient` — **one client** reused by vector store, parent store, and graph store |
| Collection handle | `conn.get_collection(name)` | `motor` database indexing |
| Index bootstrap | `conn.ensure_indexes(collection, specs)` | `motor` `create_index()` (idempotent, safe on every startup) |

### Two things this map makes obvious

**There are exactly three LLM call sites in the whole pipeline, and only one is
on by default.** All go through the same `LLMClient().chat()` and are
distinguished by their `caller` tag, which is what the gateway's MongoDB request
tracker records:

| `caller` tag | Where | Default |
|---|---|---|
| `rag.generate` | `llm.py::generate_answer` | **on** — the only unavoidable LLM call |
| `rag.security.injection_classifier` | `security.py::_llm_injection_assessment` | off (`RAG_USE_LLM_PROMPT_INJECTION_CLASSIFIER`) |
| `rag.evaluation.llm_judge` | `evaluation.py::_llm_judge_notes` | off (`RAG_ADVANCED_EVAL_PROVIDER=llm`) |

Everything else — chunking, entity extraction, fusion, knee-point selection,
citation validation, default reranking, default safety filtering, default
evaluation — is stdlib, `numpy`, or a small pure-Python function. That is why
the pipeline still returns an answer with **zero API keys configured**:
`LocalHashEmbeddings` covers embedding and `_local_extractive_answer()` covers
generation.

**The embedding model is called at exactly three points**, and only one of them
is unconditional:

| Call | Where | When |
|---|---|---|
| `embed_documents(units)` | `chunking.py::semantic_chunk_documents` | only when `chunk_strategy="semantic"` |
| `embed_documents(texts)` | `vector_store.py::VectorStore.upsert` | every ingestion |
| `embed_query(question)` | `_embed_query` + `VectorStore.semantic_search` | every query |

Note the last row: `_embed_query` stores an embedding in `QueryState`, and
`semantic_search()` independently calls `embed_query()` again on the same
question — so a query embeds twice per run. Harmless under
`LocalHashEmbeddings`, but it is a duplicated billable call on a real provider.

---

## Example A — Negotiation playbook chunk (semantic + graph retrieval both fire)

*Orchestration for the whole ingestion trace below (A.1–A.5) — [Tool: `langgraph` `StateGraph`, `build_ingestion_graph()` — see §0].*

### A.1 Source document

A markdown knowledge file ingested for the Negotiation Agent's RAG playbook:

`backend/shared/rag/data/negotiation_playbook.md`
```markdown
## Counter-offer strategy for HS Code 8471.30

When a supplier's counter-offer on HS Code 8471.30 (laptops) exceeds the target
FOB price by more than 6%, anchor the next round at the midpoint between our
last offer and their counter, and cite the Freightos freight index as
justification for holding firm on Incoterms.

Suppliers in the Shenzhen region typically concede 2-3% per round when the
buyer references a competing quote from Guangzhou-based manufacturers.
```

### A.2 Loading (`loader.py`) — [Tool: Python stdlib `pathlib`/regex; PDFs use `pypdf`/`pdfplumber` — see §0]

`load_documents([Path("negotiation_playbook.md")])` returns one LangChain `Document`:

```json
{
  "page_content": "## Counter-offer strategy for HS Code 8471.30\n\nWhen a supplier's counter-offer on HS Code 8471.30 (laptops) exceeds the target FOB price by more than 6%, anchor the next round at the midpoint between our last offer and their counter, and cite the Freightos freight index as justification for holding firm on Incoterms.\n\nSuppliers in the Shenzhen region typically concede 2-3% per round when the buyer references a competing quote from Guangzhou-based manufacturers.",
  "metadata": {
    "source": "negotiation_playbook.md",
    "file_name": "negotiation_playbook.md",
    "file_type": ".md",
    "title": "negotiation_playbook",
    "content_hash": "9f2a1c...e77b",
    "loaded_at": "2026-07-28T09:12:00Z"
  }
}
```

### A.3 Chunking (`chunking.py::recursive_chunk_documents`) — [Tool: `langchain-text-splitters` `RecursiveCharacterTextSplitter` — see §0]

`RecursiveCharacterTextSplitter(chunk_size=900, chunk_overlap=120)` splits on the `\n## ` heading separator first. This document is short enough (≈520 chars) to stay as **one child chunk**. `_number_chunks()` then attaches chunk metadata and builds the parent group:

**Output — 1 child chunk:**
```json
{
  "page_content": "## Counter-offer strategy for HS Code 8471.30\n\nWhen a supplier's counter-offer on HS Code 8471.30 (laptops) exceeds the target FOB price by more than 6%, anchor the next round at the midpoint between our last offer and their counter, and cite the Freightos freight index as justification for holding firm on Incoterms.\n\nSuppliers in the Shenzhen region typically concede 2-3% per round when the buyer references a competing quote from Guangzhou-based manufacturers.",
  "metadata": {
    "source": "negotiation_playbook.md",
    "title": "negotiation_playbook",
    "content_hash": "9f2a1c...e77b",
    "chunk_id": "negotiation_playbook.md::unknown-section::page-no-page::recursive::child::0",
    "chunk_index": 0,
    "chunk_strategy": "recursive",
    "chunk_chars": 520,
    "parent_id": "negotiation_playbook.md::unknown-section::page-no-page::recursive::parent::0",
    "parent_index": 0
  }
}
```

**Output — parent doc map** (persisted separately to MongoDB `parent_docs` by `MongoParentStore().save()`, keyed by `parent_id`; since only one child exists, the parent content is identical here — with a longer source doc the parent would bundle `child_chunks_per_parent=3` children into one wider context block):
```json
{
  "negotiation_playbook.md::unknown-section::page-no-page::recursive::parent::0": {
    "page_content": "<same 520-char text as above>",
    "metadata": { "parent_id": "...parent::0", "parent_index": 0, "parent_chunk_count": 3 }
  }
}
```

### A.3a How child→parent grouping actually works (the mechanism)

Example A above is a degenerate case — 1 child chunk maps 1:1 to its parent, so
the grouping logic never really gets exercised. Before the multi-child examples
in A.3b, here is the mechanism itself, precisely as implemented in
`_number_chunks()` / `_chunk_group_key()` / `_build_parent_groups()`
(`chunking.py:123-191`). It runs in two independent stages.

**Stage 1 — bucket chunks by document identity (`_chunk_group_key`).**
Every chunk carries metadata inherited from its source `Document` *before*
splitting. `_chunk_group_key()` derives a bucket key from three of those
fields only:

```python
group_key = f"{source}::{safe_section}::page-{page}"
```

- `source` — the file path/name
- `section` — a metadata field (`"unknown-section"` if the loader never set one)
- `page` — page number for PDFs, `"no-page"` for flat text/markdown

Chunks only ever compete for the same parent if they share **all three** —
same file, same section, same page. This is a hard partition: it is why, in
Example B (the QC SOP PDF), the page-3 "Sampling" chunk and the page-4
"Escalation" chunk never share a parent even though they come from the same
file — `page` differs, so `_chunk_group_key` puts them in two separate
buckets before grouping even starts.

Within `_number_chunks()`, chunks are walked in splitter output order, and
`chunk_index` is a **per-bucket** counter (`total_by_group[group_key]`) — so
numbering restarts at 0 independently inside each bucket, not globally across
the whole document.

**Stage 2 — sub-group by position within the bucket (integer division).**
Within one bucket, children are grouped strictly by their **position**, not by
topic or content:

```python
parent_index = chunk_index // settings.child_chunks_per_parent   # default 3
```

| `chunk_index` | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|---|
| `parent_index` | 0 | 0 | 0 | 1 | 1 | 1 | 2 |

Every run of 3 consecutive children (by `chunk_index`, i.e. by the order the
splitter produced them in) becomes one parent. `parent_id` is then just
`f"{group_key}::{strategy}::parent::{parent_index}"` — deterministic and
reproducible from the bucket key plus the position alone, so re-ingesting the
same file regenerates identical `parent_id`s (which is what lets the upsert-by-hash
dedup in `VectorStore.upsert` and the parent store's `ReplaceOne` upserts stay
consistent across re-indexing).

**Building the parent's actual text (`_build_parent_groups`).** For each
`parent_index`, the function collects every member child's `page_content`, in
`chunk_index` order, and joins them with `"\n\n"`:

```python
parent_groups[parent_index] = "\n\n".join(child.page_content for child in members)
```

That joined string becomes the parent `Document`'s `page_content` — built once
per `parent_id`, the first time `_number_chunks()` encounters a child
belonging to it (`if parent_id not in parent_documents: ...`).

**Net effect / properties worth knowing:**

1. **Grouping is purely positional, not semantic.** Three consecutive children
   always land in one parent regardless of whether they're topically related —
   it's a fixed-size window over splitter order, not a clustering algorithm.
   Semantic chunking (`semantic_chunk_documents`, worked example in A.3c)
   only changes *where the child boundaries fall*; it still hands its output
   to this same positional stage-2 grouping — semantic mode does not group
   parents by topic either.
2. **Grouping never crosses a stage-1 bucket boundary.** Stage 1 is a hard
   partition; stage 2 only operates inside one partition, so a parent group
   can never mix chunks from two different sources, sections, or pages.
3. **The last group in a bucket can be smaller than `child_chunks_per_parent`.**
   A 7-child bucket with size 3 produces groups of 3, 3, 1 — never a
   wraparound borrowing children from the next bucket (worked out in A.3b.2).

### A.3b Parent-child grouping with multiple children per parent

The two examples below use a longer source file — a 7-chunk supplier-contract
clause doc — all landing in the **same** `_chunk_group_key()` bucket
(`supplier_contract_terms.md::Payment Terms::page-no-page`) because they share
`source`, `section`, and `page_number`. That single bucket is what gets split
3-children-per-parent.

#### A.3b.1 Even split — 6 children → 2 full parents

Source: `supplier_contract_terms.md`, section "Payment Terms", split by the
recursive splitter into exactly 6 child chunks (`chunk_index` 0–5).

| `chunk_index` | `parent_index` (`idx // 3`) | `parent_id` |
|---|---|---|
| 0 | 0 | `...::recursive::parent::0` |
| 1 | 0 | `...::recursive::parent::0` |
| 2 | 0 | `...::recursive::parent::0` |
| 3 | 1 | `...::recursive::parent::1` |
| 4 | 1 | `...::recursive::parent::1` |
| 5 | 1 | `...::recursive::parent::1` |

**Child chunks (abbreviated, only the fields `_number_chunks` sets shown):**
```json
[
  { "chunk_id": "...::child::0", "chunk_index": 0, "parent_id": "...::parent::0", "parent_index": 0,
    "page_content": "### 4.1 Payment schedule\n\n30% advance on PI acceptance, 70% against BL copy within 5 business days of shipment." },
  { "chunk_id": "...::child::1", "chunk_index": 1, "parent_id": "...::parent::0", "parent_index": 0,
    "page_content": "### 4.2 Currency and FX\n\nAll invoices are denominated in USD; buyer bears FX conversion risk on the 70% BL-triggered tranche." },
  { "chunk_id": "...::child::2", "chunk_index": 2, "parent_id": "...::parent::0", "parent_index": 0,
    "page_content": "### 4.3 Late payment\n\nPayments overdue by more than 10 days accrue 1.5% monthly interest, compounding." },
  { "chunk_id": "...::child::3", "chunk_index": 3, "parent_id": "...::parent::1", "parent_index": 1,
    "page_content": "### 4.4 Escrow release conditions\n\nThe 70% tranche releases from escrow only after SGS QC_PASSED and BL copy upload." },
  { "chunk_id": "...::child::4", "chunk_index": 4, "parent_id": "...::parent::1", "parent_index": 1,
    "page_content": "### 4.5 Disputed invoices\n\nA disputed line item does not delay payment of undisputed amounts on the same invoice." },
  { "chunk_id": "...::child::5", "chunk_index": 5, "parent_id": "...::parent::1", "parent_index": 1,
    "page_content": "### 4.6 Refund handling\n\nRefunds for QC_FAILED lots route back through the original payment rail within 3 business days." }
]
```

**Resulting parent doc map** — `_build_parent_groups()` joins each parent's member
children with `"\n\n".join(parts)`, in `chunk_index` order:
```json
{
  "supplier_contract_terms.md::Payment Terms::page-no-page::recursive::parent::0": {
    "page_content": "### 4.1 Payment schedule\n\n30% advance on PI acceptance, 70% against BL copy within 5 business days of shipment.\n\n### 4.2 Currency and FX\n\nAll invoices are denominated in USD; buyer bears FX conversion risk on the 70% BL-triggered tranche.\n\n### 4.3 Late payment\n\nPayments overdue by more than 10 days accrue 1.5% monthly interest, compounding.",
    "metadata": { "parent_id": "...parent::0", "parent_index": 0, "parent_chunk_count": 3 }
  },
  "supplier_contract_terms.md::Payment Terms::page-no-page::recursive::parent::1": {
    "page_content": "### 4.4 Escrow release conditions\n\nThe 70% tranche releases from escrow only after SGS QC_PASSED and BL copy upload.\n\n### 4.5 Disputed invoices\n\nA disputed line item does not delay payment of undisputed amounts on the same invoice.\n\n### 4.6 Refund handling\n\nRefunds for QC_FAILED lots route back through the original payment rail within 3 business days.",
    "metadata": { "parent_id": "...parent::1", "parent_index": 1, "parent_chunk_count": 3 }
  }
}
```
Every child in `parent::0` expands, at query time, to the *same* 3-clause block —
so a retrieval hit on child 1 (currency/FX) still gives the LLM the sibling
context about the payment schedule (child 0) and late-payment interest (child 2)
without needing a separate retrieval pass for either.

#### A.3b.2 Remainder split — 7 children → 3 parents, last parent partial

Same document, one more clause appended (`chunk_index` 0–6, 7 total). `parent_index
= idx // 3` now produces a **3rd, partial** parent group with only 1 member:

| `chunk_index` | `parent_index` | Group size |
|---|---|---|
| 0, 1, 2 | 0 | 3 (full) |
| 3, 4, 5 | 1 | 3 (full) |
| 6 | 2 | **1 (partial)** |

```json
{
  "supplier_contract_terms.md::Payment Terms::page-no-page::recursive::parent::2": {
    "page_content": "### 4.7 Force majeure payment suspension\n\nIf a force majeure event under Section 9 suspends shipment, all pending payment obligations pause without penalty until the event is resolved or the contract is terminated.",
    "metadata": { "parent_id": "...parent::2", "parent_index": 2, "parent_chunk_count": 3 }
  }
}
```
`parent_chunk_count` is always set to the *configured* `child_chunks_per_parent`
(3) regardless of how many children actually landed in that group — it records
the grouping target, not the realized member count. A caller that wants the
true member count must count matching children by `parent_id` rather than
trust this field. This partial-group behavior is expected and harmless: at
query time `expand_parent_context()` (`vector_store.py:341-377`) only needs
`parent_id` to look up one row from `MongoParentStore`; a 1-member parent just
means expansion returns exactly that member's own content, i.e. no broader
context is available for clause 4.7 beyond itself.

#### A.3b.3 Two children, two different parents (different `chunk_size` cutoff)

A more common case in practice: the splitter doesn't cleanly break every clause
onto its own chunk — long clauses can span the `chunk_size=900` boundary. If
`4.1`–`4.2` together are 850 chars (chunk 0) and `4.3` alone is 950 chars,
`RecursiveCharacterTextSplitter` cuts chunk 1 mid-clause, continuing into chunk 2 via `chunk_overlap=120`:

```json
[
  { "chunk_index": 0, "parent_index": 0, "chunk_chars": 850,
    "page_content": "### 4.1 Payment schedule\n\n30% advance...\n\n### 4.2 Currency and FX\n\n...FX conversion risk on the 70% BL-triggered tranche." },
  { "chunk_index": 1, "parent_index": 0, "chunk_chars": 900,
    "page_content": "...on the 70% BL-triggered tranche. ### 4.3 Late payment\n\nPayments overdue by more than 10 days accrue 1.5%..." },
  { "chunk_index": 2, "parent_index": 0, "chunk_chars": 210,
    "page_content": "...monthly interest, compounding." }
]
```
The last 120 characters of chunk 0 (the FX sentence) reappear at the start of
chunk 1 — this is `chunk_overlap` in action, so a semantic boundary that falls
inside a sentence doesn't lose context on either side of the cut. All three
still land under `parent_index=0` here since `idx // 3` groups by *position*,
not by clause boundary — the parent's joined text simply contains a
duplicated overlap span, which is harmless for LLM context (redundant text,
not contradictory).

### A.3c Semantic chunking — worked example (`chunking.py::semantic_chunk_documents`) — [Tool: custom cosine-breakpoint splitter, `numpy` + the configured `Embeddings` backend — see §0]

Everything in A.3/A.3a/A.3b above uses the **recursive** strategy (the
default — `RAG_CHUNK_STRATEGY` unset or `chunk_strategy="recursive"` passed
to `ingest()`). This section runs the **same source concept** — a
multi-clause document — through `semantic_chunk_documents()` instead, so the
two strategies can be compared side by side on comparable input.

**Source document** — `backend/shared/rag/data/qc_provider_notes.md`, a
single flowing paragraph block (no `##` headings) covering two genuinely
different topics back to back:

```markdown
SGS offers AQL 2.5 sampling for garment shipments and typically turns around
inspection reports within 48 hours of the physical check. QIMA's pricing runs
roughly 15% lower than SGS for the same AQL tier but with a 72-hour report
turnaround. Bureau Veritas is the strongest option for electronics HS codes
because of their in-house EMC testing lab.

Freight rates from Freightos spiked 9% in the last quarter due to Red Sea
rerouting, pushing most Shenzhen-to-Nhava-Sheva quotes above $2,400 per FEU.
Cogoport's consolidated LCL rates are still the cheapest option for shipments
under 8 CBM. Buyers should lock freight quotes at PI acceptance, not at PO
issuance, to avoid this volatility.
```

Because this file has no `##` heading, `_split_markdown_sections()` (loader)
treats the whole thing as one `Document` — the topic shift between "QC
providers" (paragraph 1) and "freight rates" (paragraph 2) exists only in the
prose, not in the document structure. This is exactly the case semantic
chunking is meant to catch and recursive chunking (which only looks at
headings/blank lines/character count) would miss unless it happened to land a
900-char boundary near the topic shift by coincidence.

**Step 1 — `_split_semantic_units()`:** splits on blank lines (`\n\s*\n`) into
paragraph-level units. Both paragraphs here are well under `chunk_size=900`
chars, so neither gets further split into sentences:

```python
units = [
  "SGS offers AQL 2.5 sampling for garment shipments...EMC testing lab.",   # unit 0, ~340 chars
  "Freight rates from Freightos spiked 9%...PO issuance, to avoid this volatility.",  # unit 1, ~360 chars
]
```

**Step 2 — embed every unit, compute adjacent-pair cosine similarity:**

```python
vectors = embeddings.embed_documents(units)     # 2 vectors, 1536-dim each
similarities = [_cosine(vectors[0], vectors[1])]   # only 1 pair for 2 units
# → similarities = [0.31]
```
With a real semantic embedding model (`LLM_EMBEDDING_PROVIDER=openai`), 0.31
is a plausible low cosine similarity for two topically unrelated paragraphs
(QC inspection vendors vs. freight pricing) — well below the ~0.7–0.9 range
typically seen between sentences of the *same* topic. (Under the zero-cost
`LocalHashEmbeddings` default — a `HashingVectorizer` bag-of-words hash —
this number reflects vocabulary overlap rather than true meaning; the two
paragraphs still score low here because they share almost no words, but this
is a coincidence of word choice, not semantic understanding — see the
callout at the end of this section.)

**Step 3 — compute the breakpoint threshold:**

```python
threshold = np.percentile(similarities, settings.semantic_breakpoint_percentile)  # percentile=25
# → with only one value in the list, percentile(any_percentile) == that value == 0.31
threshold = 0.31
```

**Step 4 — walk units, breaking where similarity drops below threshold AND
the accumulated chunk has reached the minimum break size (`chunk_size // 3 =
300` chars):**

```python
current = [units[0]]                     # current_text = unit 0, ~340 chars
# idx=1, similarity=0.31
should_break = (0.31 < 0.31) and (340 >= 300)   # 0.31 < 0.31 is False → should_break = False
too_large = 340 >= 900                          # False
# → neither condition fires; unit 1 is appended to the SAME chunk
current.append(units[1])
```

This is the sharp edge worth knowing: **`should_break` uses a strict `<`, not
`<=`.** Because the threshold was computed as the percentile *of this exact
similarity list*, and there's only one similarity value, the threshold
literally equals the one similarity being compared against it — `0.31 < 0.31`
is `False`, so **no break fires** and both paragraphs land in **one child
chunk** despite being the maximally-dissimilar pair available. This is a
direct, illustrative consequence of `np.percentile` on a single-element (or
generally very short) similarity list: the percentile-of-N-items heuristic
needs enough adjacent-pair samples to produce a threshold that's genuinely
*below* at least one real similarity score. A 2-unit document is the
degenerate case; A.3c.1 below shows a longer document where the same
algorithm does produce a real split.

**Output — 1 child chunk** (semantic strategy still routes through the same
`_number_chunks()` grouping logic as recursive — see A.3a):
```json
{
  "page_content": "SGS offers AQL 2.5 sampling for garment shipments...EMC testing lab.\n\nFreight rates from Freightos spiked 9%...to avoid this volatility.",
  "metadata": {
    "source": "qc_provider_notes.md",
    "chunk_id": "qc_provider_notes.md::unknown-section::page-no-page::semantic::child::0",
    "chunk_index": 0,
    "chunk_strategy": "semantic",
    "chunk_chars": 705,
    "parent_id": "qc_provider_notes.md::unknown-section::page-no-page::semantic::parent::0",
    "parent_index": 0
  }
}
```
Note the `::semantic::` segment in `chunk_id`/`parent_id` — `strategy` is
threaded through `_number_chunks(chunks, "semantic")`, so semantic-strategy
IDs never collide with recursive-strategy IDs even for the same source file
and section, which is what lets both strategies' output coexist in the same
`chunks` collection if a document is ever re-ingested under a different
strategy.

#### A.3c.1 A longer document where a semantic breakpoint actually fires

Take the same two-topic document but with a third, related-to-freight
paragraph appended (freight-adjacent, so it should **stay merged** with
paragraph 2 rather than triggering another break):

```
unit 0: QC providers paragraph (~340 chars)
unit 1: Freight rates paragraph (~360 chars)
unit 2: "ICEGATE shipping bill filing typically completes within 24 hours of
         vessel departure, and LEO (Let Export Order) issuance follows
         automatically once customs clears the filing." (~190 chars, freight/customs-adjacent)
```

Now there are **2 similarity pairs**: `sim(0,1)` (QC → freight, topically
distant) and `sim(1,2)` (freight → customs filing, topically close):

```python
similarities = [0.31, 0.74]   # illustrative: unit 1→2 is much more similar than unit 0→1
threshold = np.percentile([0.31, 0.74], 25)   # 25th percentile of a 2-value list ≈ 0.4075
```

Walking the units:

```python
current = [unit0]                                  # current_text ≈ 340 chars
# idx=1, similarity=0.31
should_break = (0.31 < 0.4075) and (340 >= 300)     # True and True → BREAK
# → chunk A = unit0 alone; current resets to [unit1]
current = [unit1]                                   # current_text ≈ 360 chars
# idx=2, similarity=0.74
should_break = (0.74 < 0.4075) and (...)            # False → no break
# → unit2 appended to the same chunk as unit1
current.append(unit2)
# loop ends; flush remaining `current`
chunk B = unit1 + "\n\n" + unit2
```

**Output — 2 child chunks, split exactly at the real topic boundary:**
```json
[
  {
    "chunk_id": "...::semantic::child::0",
    "chunk_index": 0,
    "chunk_chars": 340,
    "page_content": "SGS offers AQL 2.5 sampling for garment shipments...EMC testing lab."
  },
  {
    "chunk_id": "...::semantic::child::1",
    "chunk_index": 1,
    "chunk_chars": 554,
    "page_content": "Freight rates from Freightos spiked 9%...to avoid this volatility.\n\nICEGATE shipping bill filing typically completes...follows automatically once customs clears the filing."
  }
]
```
This is the payoff semantic chunking is designed for: chunk 0 is purely about
QC providers, chunk 1 bundles freight-rate and customs-filing content
together because they scored as topically related — a boundary the recursive
splitter could only have found by accident (it has no concept of "topic" at
all; it only reacts to headings, blank lines, and the fixed `chunk_size`).

**One important caveat, stated plainly:** semantic chunking's quality is
entirely bounded by the embedding backend actually producing *meaningful*
similarity scores. Under the zero-cost `LocalHashEmbeddings` default (see
§0), similarity is a bag-of-words hash comparison, not a learned semantic
representation — it will correlate with shared vocabulary, not shared
meaning, so breakpoints can land in the wrong place for prose that shares
words across topics or uses different words for the same topic. Getting the
example above's clean topic-aligned split in production requires
`LLM_EMBEDDING_PROVIDER=openai` (or `gemini`) configured — i.e. semantic
chunking has a **real dependency on a real embedding model** to deliver on
its premise, unlike recursive chunking, which needs no model at all.

### A.4 Embedding + vector upsert (`vector_store.py::VectorStore.upsert`) — [Tool: `GatewayEmbeddings` → `LocalHashEmbeddings` (scikit-learn) by default, or `OpenAIEmbeddings`/Gemini if a key is set; storage = MongoDB Atlas `$vectorSearch` — see §0]

`embeddings.embed_documents([chunk.page_content])` → one vector of length `LLM_EMBEDDING_DIMENSIONS` (1536 for `text-embedding-3-small`, or 1536-dim deterministic hash vector via `LocalHashEmbeddings` if no embedding key is configured).

**Document written to MongoDB `chunks` collection:**
```json
{
  "_id": "ObjectId(...)",
  "chunk_id": "negotiation_playbook.md::unknown-section::page-no-page::recursive::child::0",
  "content": "## Counter-offer strategy for HS Code 8471.30\n\nWhen a supplier's counter-offer...",
  "embedding": [0.0142, -0.0087, 0.0231, "... 1533 more floats ..."],
  "metadata": {
    "source": "negotiation_playbook.md",
    "title": "negotiation_playbook",
    "content_hash": "9f2a1c...e77b",
    "chunk_index": 0,
    "parent_id": "negotiation_playbook.md::unknown-section::page-no-page::recursive::parent::0",
    "authorized_teams": ["negotiation-agent", "sourcing"]
  }
}
```
`content_hash` drives dedup: re-ingesting the same file first deletes every chunk sharing that hash, so a stale re-index never leaves orphan vectors.

### A.5 Graph insert (`graph_store.py::MongoGraphStore.upsert_chunks`) — [Tool: MongoDB (backend=`mongo`, default) or Neo4j driver (backend=`neo4j`) for storage; zero-dependency regex heuristic for entity extraction — see §0]

**Yes — graph insertion always operates on child chunks, never on the raw
document.** This isn't a design choice made independently for the graph leg;
it falls out of how the ingestion `StateGraph` is wired in `graph.py`:

```
parse_and_load → chunk → embed_and_insert → graph_insert → END
```

Each node's output feeds the next node's input via shared `IngestionState`.
Concretely (`graph.py:143-173`):

```python
async def _chunk(state):
    chunks, parent_docs = chunk_documents(state["raw_documents"], embeddings, strategy)
    ...
    return {"chunks": chunks}          # ← written into state["chunks"]

async def _embed_and_insert(state):
    ... vector_store.upsert(state["chunks"])   # ← reads the SAME state["chunks"]

async def _graph_insert(state):
    ... graph_store.upsert_chunks(state["chunks"])  # ← reads the SAME state["chunks"] again
```

`_graph_insert` never touches `state["raw_documents"]` — it isn't even in its
node signature's relevant fields. Both `_embed_and_insert` (vector store) and
`_graph_insert` (graph store) consume the *exact same* `chunks` list that
`_chunk` produced; they're siblings reading one shared upstream output, not
two independent ingestion paths. Practically, this means:

- **Chunking happens once, upstream of both stores.** There's no
  "graph-specific" chunking pass — whatever recursive/semantic split
  `chunking.py` produced is what both `MongoGraphStore.upsert_chunks()` and
  `VectorStore.upsert()` see.
- **`_chunk_fields()` in `graph_store.py` operates per-chunk, not per-document.**
  `_extract_entities()` (below) runs once per `Document` in the `chunks` list
  — for a document that split into 6 children, that's **6 separate entity
  extractions**, one per child's `page_content`, not one extraction over the
  whole source file. This is *why* Example B's two chunks (page 3 "Sampling",
  page 4 "Escalation") get two disjoint entity sets instead of one combined
  set — each chunk's `_extract_entities()` call only ever sees its own
  `page_content`.
- **Chunk-level graph nodes, not document-level.** The graph schema's
  `(:Chunk {chunk_id, ...})` node is keyed by `chunk_id`, and `ADJACENT_TO`
  edges link consecutive *chunks* (`graph_store.py`'s `prev_by_source`
  tracking over the `chunks` list), not consecutive source documents. A
  `(:Document {source, title})` node exists too (`MERGE (d:Document
  {source: row.source})` in the Neo4j Cypher), but it's a coarse grouping
  node derived from each chunk's `source` field — one `Document` node ends up
  referenced by every `Chunk` that shares that `source`, rather than the
  document itself ever being embedded, entity-extracted, or graph-inserted as
  a single unit.
- **If graph retrieval were fed raw (unchunked) documents instead**, a single
  entity-extraction pass over an entire multi-page file would blur together
  entities from unrelated sections (e.g. "Sampling" and "Escalation" would
  share one bloated entity list), and `graph_search`'s single-hop
  shared-entity-count scoring (C.4 below) would lose its ability to
  distinguish which *part* of a document actually matched the query — the
  whole point of keying graph nodes by `chunk_id` is to retrieve at the same
  granularity the LLM prompt ultimately gets built from.

#### A.5.1 What tool converts a chunk into graph entity nodes — and does it need a model?

**Tool:** `_extract_entities()` in `graph_store.py:62-84` — **[Tool: Python
stdlib `re` only. No model, no ML library, no external call, no cost.]**

```python
_ENTITY_RE = re.compile(r"\b([A-Z][A-Za-z]{2,})\b")   # capitalised words, ≥3 chars
_CODE_RE   = re.compile(r"\b(\d{4,10})\b")             # 4–10 digit numeric codes
```

It runs two regex scans over `chunk.page_content`, lowercases every match,
drops anything in a ~30-word hardcoded stopword list (`the`, `and`, `please`,
`regards`, …), dedupes, and truncates to `max_entities=25`. That's the entire
algorithm — no tokenizer, no embedding, no inference call of any kind.

**Do you need a model to support this?** No — not for the code as shipped.
The module docstring is explicit that this is deliberate:

> "Deliberately simple and deterministic; replace with real NER for
> production-grade recall." (`graph_store.py:49-50`)
> "Entity extraction is a lightweight, dependency-free heuristic... Swap
> `_extract_entities` for spaCy / an LLM NER call for higher precision — no
> other code needs to change." (module docstring, `graph_store.py:28-31`)

So a model is an *optional upgrade path*, not a requirement. The regex
approach is what makes `RAG_GRAPH_DB_BACKEND=mongo` genuinely "no new
service, no new credentials" (per `config.py`'s comment) — swapping in a
model-based extractor would add a new dependency/cost axis that today's
default deployment doesn't have at all.

**How costly is it — today vs. the alternatives?**

| Approach | Wired in this codebase? | Cost per chunk | Cost for a 500-chunk ingestion run | Precision/recall tradeoff |
|---|---|---|---|---|
| **Regex heuristic (current)** | Yes — this is what runs today | **$0** (µs of CPU, no network call) | **$0**, sub-second total | Coarse: only catches capitalised tokens and digit runs; misses lowercase domain terms, multi-word entities ("Bureau Veritas" → two separate entities "bureau"/"veritas"), and anything not capitalised in source text |
| **spaCy NER** (mentioned as a swap-in, not implemented) | No — named as a suggestion only, no spaCy import anywhere in `shared/rag/` | $0 (local inference), but real latency cost: tens of ms per chunk on CPU (small model, e.g. `en_core_web_sm`) | Roughly seconds, still local/free — no per-token billing | Better recall on real-world entity types (ORG, GPE, PRODUCT), still no domain tuning for HS codes / Incoterms out of the box |
| **LLM-based NER call** (mentioned as a swap-in, not implemented) | No — no such call exists in `shared/rag/`; would reuse `shared/llm_gateway/client.py::LLMClient` the same way `security.py`'s optional LLM injection classifier does | Priced per token via whichever model handles it. Using this repo's own Groq fallback-chain pricing (`shared/llm_gateway/cost_estimator.py:34-45`, prices as of 2026-06) as the realistic default route: **`qwen/qwen3-32b` — $0.29/1M input, $0.59/1M output tokens.** A ~500-char chunk is roughly ~150 input tokens; a short structured-entity-list response is ~50 output tokens → **≈$0.000073 per chunk** | ≈500 chunks × ~200 tokens ≈ 100K tokens → **≈$0.037** for a 500-chunk ingestion batch (negligible in absolute terms, but non-zero and now billed, network-latency-bound, and subject to the same fallback chain / rate limits as every other LLM Gateway call) | Best recall and can be prompted for domain-specific entities (HS codes, Incoterms, supplier names) directly, but non-deterministic across restarts, adds gateway-chain latency (network round trip vs. µs) and a new failure mode (LLM Gateway outage → `_graph_insert` would need its own fallback, unlike the regex path which cannot fail) |

**Why the regex approach is the actual default, not a stopgap being phased
out:** `_extract_entities()` is explicitly **deterministic** — the module
docstring calls this out directly: *"the same phrase in a chunk and in a
query maps to the same `Entity` node — that overlap is what graph retrieval
matches on."* An LLM- or model-based extractor risks non-determinism (the
same input text producing slightly different entity casing/phrasing on
re-ingestion, or the same question phrased differently at query time not
mapping onto entities the same way it did at ingestion time), which would
silently degrade the exact single-hop shared-entity-count matching that
`MongoGraphStore.retrieve()` (C.4) depends on. Swapping extractors is a
precision/recall-for-cost-and-determinism tradeoff, not a strict upgrade.

**Document written to MongoDB `graph_chunks` collection** (Mongo backend — the recommended default; same shape would be Cypher `MERGE` statements on Neo4j):
```json
{
  "chunk_id": "negotiation_playbook.md::unknown-section::page-no-page::recursive::child::0",
  "source": "negotiation_playbook.md",
  "title": "negotiation_playbook",
  "content": "## Counter-offer strategy for HS Code 8471.30\n\n...",
  "section": "",
  "page_number": null,
  "entities": ["counter", "code", "when", "fob", "freightos", "incoterms", "suppliers", "shenzhen", "guangzhou", "8471"],
  "adjacent_to": ["negotiation_playbook.md::...::child::1"]
}
```
`adjacent_to` links this chunk to the *next* chunk sharing the same `source` (empty here since this is the only chunk — populated when a document produces multiple sequential chunks, e.g. Example B).

Two things in that `entities` array are worth pausing on, because they are the
extractor's behaviour rather than typos — **`hs` is absent** even though the
chunk says "HS Code" twice, and **`when` is present** even though it carries no
meaning. Both fall out of the regex; A.5.2 explains why and catalogues the rest.

Ingestion for this file is now complete: `{inserted_count: 1, graph_inserted_count: 1}`.

#### A.5.2 Known limitations of the regex extractor and the persisted shape

Every entity list in this document was produced by running `_ENTITY_RE` /
`_CODE_RE` / `_STOPWORDS` (verbatim from `graph_store.py:51-84`) over the exact
example text. The behaviours below are what that produces — they are
consequences of the heuristic being case- and boundary-driven, and they are the
practical reason `RAG_GRAPH_RETRIEVAL_ENABLED` defaults to `false`.

**Extraction-side limits**

| # | Limitation | Why | Verified example |
|---|---|---|---|
| 1 | **Two-character acronyms are never extracted** | `[A-Z][A-Za-z]{2,}` needs ≥3 chars total | `HS`, `PI`, `PO`, `QC`, `CI`, `PL`, `BL` — much of this domain's vocabulary — are invisible. A.5's chunk says "HS Code" twice and yields `code`, never `hs` |
| 2 | **`SNAKE_CASE` tokens are entirely invisible** | `_` is a word character, so `\b` never fires beside it — neither `QC` nor `FAILED` in `QC_FAILED` has a word boundary on the underscore side | `"Deal moved to ESCROW_FUNDED then PO_ISSUED."` → `['deal']`. Every `DealStatus` value, `AUTO_FIX`, `QC_FAILED`, `RESOLVE_EXCEPTION` extract as **nothing** |
| 3 | **Lowercase text is skipped entirely** | The first character must be `A-Z` | B.1's page-3 text contains "(cotton knit garments)" → `cotton` and `garments` are **not** entities. Users typing lowercase queries lose nearly all signal: `"what is the aql level for hs code 100630?"` → `['100630']` |
| 4 | **A query with no capitalised word and no 4–10 digit run yields `[]`** | `retrieve()` returns early at `graph_store.py:262-263` | `"how long do suppliers have to respond?"` → `[]` → the graph leg contributes nothing to RRF for that question |
| 5 | **Sentence-initial capitals become noise entities** | Not in `_STOPWORDS`, structurally indistinguishable from a real entity | A.5 yields `when`; B.3's page-3 chunk yields `reject` and `minor`; C.4's question yields `what`. These inflate the score denominator (see #8) |
| 6 | **Multi-word entities split into unrelated tokens** | Each match is independent; no phrase detection | `"Bureau Veritas inspected the lot."` → `['bureau', 'veritas']`, two separate `Entity` nodes with no link between them |
| 7 | **Numeric matches are context-free** | `\b(\d{4,10})\b` cannot tell an HS code from a price, quantity, or year | `8471` (HS code) and `1250` (a unit price) are indistinguishable entity strings |
| 8 | **Noise entities silently deflate every score** | Score is `shared / len(query_entities)` (`graph_store.py:293`), and noise counts in the denominator | In C.4, `what` makes a perfect 3-of-3 content match score `0.75` instead of `1.0` |

Note that #1/#2/#3 are *symmetric* — they suppress the term at ingestion **and**
at query time — so they cause misses, not false matches. That symmetry is the
same property A.5.1 argues makes the regex safer than a non-deterministic
extractor; the cost is that whole classes of domain vocabulary never enter the
graph at all.

**Persistence-side limits**

- **`adjacent_to` is written but never read.** `retrieve()` matches only on
  `entities`; no query path in either backend touches `adjacent_to`. The Neo4j
  backend is identical — `_ADJ_CYPHER` creates the `ADJACENT_TO` edge, but
  `_RETRIEVE_CYPHER` (`graph_store.py:129-138`) traverses only `MENTIONS`. As
  B.3 notes, it is stored for a future multi-hop query; today it is dead weight
  on every write.
- **Adjacency only forms within a single ingestion batch.** `prev_by_source` is
  a local dict rebuilt per `upsert_chunks()` call (`graph_store.py:218-225`), so
  ingesting a document one chunk per call produces **zero** adjacency edges. A
  re-upsert also `$set`s `adjacent_to` wholesale, overwriting whatever the
  previous batch computed.
- **There are no entity-to-entity edges.** The graph is bipartite (chunk ↔
  entity), not a knowledge graph: there is no `(SGS)-[:INSPECTS]->(Garments)`.
  It cannot answer relational questions — only "which chunks mention the same
  terms as this query," which is closer to a keyword search over an
  auto-curated term list than to graph reasoning.
- **Chunk content is stored twice.** Full `content` lives in `graph_chunks`
  *and* in the `chunks` collection alongside its embedding (A.4), doubling
  corpus text storage when the graph leg is enabled.
- **The `chunk_id` fallback is not stable across restarts.** When metadata has
  no `chunk_id`, `_chunk_fields` synthesizes one from `hash(chunk.page_content)`
  (`graph_store.py:91`) — Python randomizes string hashing per process unless
  `PYTHONHASHSEED` is set, so the same chunk re-ingested in a new process
  upserts under a *different* `_id` instead of replacing the old row. Chunks
  arriving from `chunking.py` always carry a deterministic `chunk_id`, so this
  only bites on hand-constructed `Document`s.

**What would actually fix the recall gaps.** Swapping `_extract_entities` for a
model (A.5.1's upgrade path) addresses #3 and #6 but not #1/#2, and it forfeits
the determinism the matching depends on. The cheaper, determinism-preserving fix
for the biggest offenders is to keep the two regexes and add (a) a
case-insensitive gazetteer of known domain vocabulary — HS/PI/PO/QC, Incoterms,
provider names, `DealStatus` values — and (b) an `_` → space normalisation pass
before matching, which alone recovers every `SNAKE_CASE` enum value. Both apply
identically at ingestion and query time, so the symmetry property survives.

#### A.5.3 How a start-of-document chunk relates to an end-of-document chunk

A natural question once `adjacent_to` is on the page: if chunk 0 and chunk 47 of
the same file are related, how does the graph express that across 47 hops?

**The chain is complete; nothing walks it.** `prev_by_source` is keyed on
**`source`**, not on `_chunk_group_key()`, so the adjacency chain deliberately
crosses section and page boundaries. Combined with `_number_chunks()` emitting
chunks in document order (dict insertion order over group keys, then
`chunk_index` within each group), one unbroken linked list is written per source
file:

```
sgs_qc_sop.pdf::Sampling::page-3::...::child::0
  → sgs_qc_sop.pdf::Sampling::page-3::...::child::1
  → sgs_qc_sop.pdf::Escalation::page-4::...::child::0
  → ...                                              (start of file → end of file)
```

So the structure linking first chunk to last chunk genuinely exists in Mongo. It
is simply never queried (A.5.2, persistence limits).

**The retrieval traversal is depth-1 from the query.** This is the part that
makes the distance question moot. What `retrieve()` does:

```
query ──_extract_entities──> entity strings ──$in──> chunks     (one hop, stop)
```

What it never does:

```
chunk ──MENTIONS──> entity <──MENTIONS── other chunk            (not implemented)
```

Chunks are therefore never related **to each other** by any query in this
codebase — not distant ones, and not even adjacent ones. Two chunks that both
contain `freightos` are not linked; they independently match a query that
contains `freightos`. There is no path between them that anything traverses.

The practical consequence: **document distance is not a factor, because
retrieval has no notion of distance.** A start chunk and an end chunk stand in
exactly the same relation as two chunks from unrelated files — none, beyond
independently matching the same query terms. Functionally, `graph_chunks` is an
inverted index (term → chunks) that happens to be shaped like a graph.

Nor does anything else in the pipeline bridge the gap. Parent expansion (C.7)
cannot: `_chunk_group_key()` partitions on `source::section::page` and parents
group 3 *consecutive* children inside one partition, so a parent never spans
pages. RRF (C.6) and the reranker (C.8) score independent candidates. If a start
chunk and an end chunk both reach the prompt, each earned its place separately —
Example C is exactly this, with Sampling and Escalation arriving via different
legs.

**Activating the chain is the cheapest available improvement to this leg,**
because the data is already written on every ingest. Post-expanding each graph
hit with its ±1 neighbours would deliver the "pull the next section too"
behaviour B.3 promises but does not implement — in Mongo a `$graphLookup` over
`adjacent_to` with `maxDepth: 1`, or simply a second `$in` query against the
returned `adjacent_to` arrays; in Neo4j a variable-length `MATCH` on
`ADJACENT_TO`. One caveat to settle first: per A.5.2 the chain is only complete
when a document is ingested in a single `upsert_chunks()` call, so that
invariant needs asserting before anything depends on it.

#### A.5.4 Would a model be better for entity *relationships*?

Worth separating two jobs that are easy to conflate — the answer differs sharply
between them:

| Job | Status in this codebase | Can a regex do it? |
|---|---|---|
| **Entity extraction** — which strings are entities | Implemented (`_extract_entities`) | Yes, imperfectly (A.5.2) — and the gaps are fixable *deterministically* |
| **Entity relationships** — typed edges between entities | **Does not exist at all** | **No** |

All three edge types in the schema are structural: `PART_OF` from the `source`
field, `MENTIONS` from regex output, `ADJACENT_TO` from list position. None is
derived from reading the text. There is no `(SGS)-[:INSPECTS]->(Garments)`
anywhere. So for genuine relationship extraction the answer is unambiguous: **a
model is the only option** — "SGS shall inspect garment shipments" cannot be
turned into a typed triple by pattern matching. That is what GraphRAG is.

**The argument against doing it here** is not cost, it is signal correlation.
The four retrieval legs are orthogonal by construction: vector = fuzzy semantic
similarity, keyword/BM25 = lexical frequency, graph = exact shared-term overlap,
web = external. RRF (C.6) derives its lift from that independence — a chunk
ranking well across *decorrelated* signals is genuinely strong. Make entity
extraction semantic and model-driven and the graph leg begins surfacing the same
chunks as the embedding leg, for the same reasons. The cost is per-token
latency, a new failure mode (`_graph_insert` today cannot fail), and loss of
determinism; the return is a third vote correlated with a vote already counted.

Three further practicalities:

- **The retrieval query would need rewriting regardless.** `$setIntersection`
  over a flat string array cannot express typed triples — A.5.1's "swap
  `_extract_entities`, no other code changes" only holds for extractors that
  still emit a flat entity list.
- **Full GraphRAG is much costlier than A.5.1's table suggests.** That
  ≈$0.037/500-chunk figure covers per-chunk entity extraction only. Community
  detection plus community summarisation — what makes GraphRAG worth having —
  is 10–100× those tokens.
- **Corpus shape matters.** This store serves negotiation playbooks and QC SOPs
  for the negotiation agent, where answers live *inside* chunks. GraphRAG earns
  its cost on corpus-wide sensemaking ("what themes recur across all supplier
  contracts?"), which is not a query this pipeline is asked to serve.

**Recommended order of work**, cheapest and most certain first:

1. **Fix extraction deterministically** (A.5.2's gazetteer + `_`→space
   normalisation). Zero cost, zero latency, recovers most of the current miss
   rate, preserves ingest/query symmetry.
2. **Activate the adjacency already being written** (A.5.3). Also free, and the
   only mechanism in the current design that encodes document locality across
   page boundaries.
3. **Only then consider a model** — and scope it to relationship extraction as a
   *separate* leg rather than replacing `_extract_entities`, keeping the
   deterministic exact-match leg intact so decorrelation survives.

Steps 1 and 2 are hours of work on a leg that ships disabled by default
(`RAG_GRAPH_RETRIEVAL_ENABLED=false`). Worth seeing it enabled, measured, and
still falling short before an LLM call enters the ingestion path.

---

## Example B — Multi-chunk PDF (QC inspection SOP) → keyword + parent-expansion path

### B.1 Source document

`backend/shared/rag/data/sgs_qc_sop.pdf` (2 pages), loaded via the PDF loader with layout extraction (`RAG_ENABLE_PDF_LAYOUT_EXTRACTION=true`). [Tool: `pdfplumber` — table/figure-aware extraction, falling back to plain `pypdf` if unavailable — see §0.] Loader output — **two** `Document`s, one per page:

```json
[
  {
    "page_content": "SGS QC Inspection SOP — Section 3: Sampling\n\nFor shipments under HS Code 6109.10 (cotton knit garments), apply AQL 2.5 sampling per ANSI/ASQ Z1.4. Reject the lot if defects exceed the AQL threshold on major defects. Minor defects up to AQL 4.0 are acceptable with a filed exception.",
    "metadata": { "source": "sgs_qc_sop.pdf", "page_number": 3, "section": "Sampling", "title": "SGS QC Inspection SOP" }
  },
  {
    "page_content": "SGS QC Inspection SOP — Section 4: Escalation\n\nIf a QC_FAILED result is issued, the Exception Resolver categorizes it under QC_FAILED and routes to AUTO_FIX only when the defect is a labeling error; all other cases escalate to HITL at L0.",
    "metadata": { "source": "sgs_qc_sop.pdf", "page_number": 4, "section": "Escalation", "title": "SGS QC Inspection SOP" }
  }
]
```

### B.2 Chunking

Each page is short enough to remain one child chunk, but because both pages share the same `source` they land in **different `_chunk_group_key()` buckets** (page number differs), so each gets its own parent group:

```
chunk_id = "sgs_qc_sop.pdf::Sampling::page-3::recursive::child::0"
chunk_id = "sgs_qc_sop.pdf::Escalation::page-4::recursive::child::0"
```

### B.3 Graph insert — adjacency in action

Because both chunks share `source: "sgs_qc_sop.pdf"` and are processed in page order, `MongoGraphStore.upsert_chunks()`'s `prev_by_source` tracking links them:

```json
[
  {
    "chunk_id": "sgs_qc_sop.pdf::Sampling::page-3::recursive::child::0",
    "source": "sgs_qc_sop.pdf",
    "entities": ["sgs", "inspection", "sop", "section", "sampling", "code", "aql", "ansi", "asq", "reject", "minor", "6109"],
    "adjacent_to": ["sgs_qc_sop.pdf::Escalation::page-4::recursive::child::0"]
  },
  {
    "chunk_id": "sgs_qc_sop.pdf::Escalation::page-4::recursive::child::0",
    "source": "sgs_qc_sop.pdf",
    "entities": ["sgs", "inspection", "sop", "section", "escalation", "exception", "resolver", "hitl"],
    "adjacent_to": []
  }
]
```

These two lists are a compact illustration of A.5.2's limits — worth reading
against the page text in B.1 rather than skimming:

- **`hs` and `qc` are missing from both**, despite "HS Code" and "QC Inspection"
  appearing in the text — both are 2-character acronyms (A.5.2 #1).
- **`cotton` and `garments` are missing** from the Sampling chunk: the source
  writes them lowercase inside "(cotton knit garments)" (A.5.2 #3).
- **`auto`, `fix`, and `failed` are missing** from the Escalation chunk even
  though `AUTO_FIX` and `QC_FAILED` are its most distinctive terms — the
  underscore suppresses the word boundary (A.5.2 #2).
- **`reject` and `minor` appear** in the Sampling chunk purely because they
  start sentences (A.5.2 #5).

The net effect is that the Escalation chunk's 8 entities are almost entirely
boilerplate shared with its neighbour (`sgs`, `inspection`, `sop`, `section`),
with only `escalation`/`exception`/`resolver`/`hitl` carrying real signal — which
is exactly why it fails to match the QC-failure half of C.4's question.

`ADJACENT_TO` is what lets a future multi-hop graph query pull "the next section" even when only one page matches on entities directly — not exercised by the single-hop `retrieve()` used today, but stored for that future use. Note the chain here spans a page boundary (page 3 → page 4) because `prev_by_source` keys on `source`, not on the chunk group key; A.5.3 works through what that chain does and does not buy you, and why activating it is the cheapest available improvement to this leg.

---

## C — Query pipeline trace

*Orchestration for the whole query trace below (C.1–C.14) — [Tool: `langgraph` `StateGraph`, `build_query_graph()` — see §0].*

**Question:** *"What AQL threshold applies to HS Code 6109.10 garments, and what happens if the shipment fails QC?"*
**Caller:** negotiation-agent service, `user_id="u_442"`, `team_id="sourcing"`, no explicit `metadata_filter`.

### C.1 `embed_query` node — [Tool: same `GatewayEmbeddings` backend as ingestion — see §0]

Input: the raw question string.
Output: `query_embedding: [0.0091, -0.0334, ...]` (1536 floats) — same embedding model/dimension as ingestion, so cosine similarity is meaningful.

### C.2 `semantic_search` node (`VectorStore.semantic_search`) — [Tool: MongoDB Atlas `$vectorSearch` — see §0]

Runs Atlas `$vectorSearch` with `numCandidates=100, limit=8`, pre-filtered only on the three Atlas-declared filter fields (`metadata.authorized_users`, `metadata.source`, `metadata.content_hash` — none supplied here, so no pre-filter is applied).

**Output (`semantic_results`, top 2 of 8 shown):**
```json
[
  { "score": 0.912, "source": "semantic",
    "document": { "page_content": "SGS QC Inspection SOP — Section 3: Sampling\n\nFor shipments under HS Code 6109.10...",
                  "metadata": { "chunk_id": "sgs_qc_sop.pdf::Sampling::page-3::...::child::0", "source": "sgs_qc_sop.pdf", "page_number": 3 } } },
  { "score": 0.847, "source": "semantic",
    "document": { "page_content": "SGS QC Inspection SOP — Section 4: Escalation\n\nIf a QC_FAILED result is issued...",
                  "metadata": { "chunk_id": "sgs_qc_sop.pdf::Escalation::page-4::...::child::0", "source": "sgs_qc_sop.pdf", "page_number": 4 } } }
]
```
Then `filter_authorized_results()` drops any chunk whose `authorized_users`/`authorized_teams` metadata excludes `user_id="u_442"` / `team_id="sourcing"` — both pass here.

### C.3 `keyword_search` node (BM25) — [Tool: `rank-bm25`'s `BM25Okapi`, in-memory over a Mongo-fetched subset — see §0]

Fetches up to `keyword_search_fetch_limit=500` chunks (no filter applied), tokenizes with `[a-zA-Z0-9_]+` lowercased, builds `BM25Okapi`, scores against tokenized query `["what","aql","threshold","applies","to","hs","code","6109","10","garments","and","what","happens","if","the","shipment","fails","qc"]`.

**Output (`keyword_results`):**
```json
[
  { "score": 4.31, "source": "keyword", "document": { "metadata": { "chunk_id": "sgs_qc_sop.pdf::Sampling::page-3::...::child::0" } } },
  { "score": 1.02, "source": "keyword", "document": { "metadata": { "chunk_id": "sgs_qc_sop.pdf::Escalation::page-4::...::child::0" } } }
]
```
The Sampling chunk wins BM25 decisively because it contains the literal tokens `aql` and `6109`; the Escalation chunk only scores from `qc`.

### C.4 `graph_search` node (`MongoGraphStore.retrieve`) — [Tool: MongoDB aggregation (`$setIntersection`), or Neo4j Cypher if `RAG_GRAPH_DB_BACKEND=neo4j` — see §0]

This is the retrieval-side payoff of graph nodes being keyed per-*chunk*
(see A.5's explainer): the Mongo `$match` below returns individual `Chunk`
documents — Sampling and Escalation score and rank independently — rather
than one row for the whole `sgs_qc_sop.pdf` file. If graph nodes had been
built from unchunked documents, this query could only ever return "yes,
`sgs_qc_sop.pdf` matches" with no way to prefer the Sampling section over the
Escalation section.

`_extract_entities(question)` runs the **same** two regexes over the question that ran over the chunks at ingestion — that symmetry is the whole matching mechanism (A.5.1). For this question it yields exactly:

```python
_extract_entities("What AQL threshold applies to HS Code 6109.10 garments, and what happens if the shipment fails QC?")
# → ['what', 'aql', 'code', '6109']
```

Only four entities, and one of them is noise. Everything A.5.2 catalogues is visible in that single line: `hs` and `qc` are dropped as 2-character acronyms (#1); `threshold`, `garments`, and `shipment` are dropped as lowercase (#3); `what` survives because it starts the sentence and isn't in `_STOPWORDS` (#5). The second, lowercase `what` is deduped by the `seen` set, so it appears once.

Mongo aggregation: `$match: {entities: {$in: ["what","aql","code","6109"]}}`, then `$project` a `shared` count via `$setIntersection`, sort descending, `$limit: 8`.

**Output (`graph_results`):**
```json
[
  { "score": 0.75, "source": "graph",
    "document": { "page_content": "SGS QC Inspection SOP — Section 3: Sampling\n\n...",
                  "metadata": { "chunk_id": "sgs_qc_sop.pdf::Sampling::page-3::...::child::0", "source": "sgs_qc_sop.pdf" } } }
]
```

Score = `shared_entities / total_query_entities`. The Sampling chunk shares `aql`, `code`, and `6109` with the query → **3 / 4 = 0.75**. Note it would have scored a perfect `1.0` if `what` hadn't padded the denominator (A.5.2 #8).

**The Escalation chunk is not returned at all.** Its entity list (B.3) is `["sgs","inspection","sop","section","escalation","exception","resolver","hitl"]`, which intersects the query's four entities **zero** times — so it never passes `$match`, let alone gets scored. The question explicitly asks *"and what happens if the shipment fails QC?"*, and the chunk that answers it is invisible to this leg, because the terms that would have connected them (`QC`, `QC_FAILED`, `fails`) are all suppressed by limits #1, #2, and #3 respectively.

That is the honest picture of graph retrieval as shipped: it contributes a strong signal for the half of the question expressed in capitalised acronyms and numeric codes, and nothing at all for the half expressed in ordinary prose. The Escalation chunk is still recovered downstream — by the keyword leg (C.3) and parent expansion (C.7) — which is precisely why the graph leg is fused via RRF as one voice among four rather than trusted alone. This leg only fires when `RAG_GRAPH_RETRIEVAL_ENABLED=true`; otherwise `NoopGraphStore` returns `[]` and the pipeline is unaffected.

### C.5 `web_search` node — [Tool: Tavily Search API via raw `httpx`, when `RAG_WEB_SEARCH_ENABLED=true` — see §0]

`RAG_WEB_SEARCH_ENABLED=false` in this example → `{"web_results": []}` immediately, no Tavily call.

### C.6 `hybrid_fusion` node (Reciprocal Rank Fusion, `rrf_k=60`) — [Tool: hand-implemented RRF, no library — see §0]

Each of the 3 non-empty result lists contributes `1/(60+rank)` per chunk it contains, summed by `chunk_id`:

| chunk_id | semantic rank | keyword rank | graph rank | RRF score |
|---|---|---|---|---|
| `...Sampling::page-3...child::0` | 1 → 1/61=0.01639 | 1 → 1/61=0.01639 | 1 → 1/61=0.01639 | **0.04918** |
| `...Escalation::page-4...child::0` | 2 → 1/62=0.01613 | 2 → 1/62=0.01613 | — (absent) | **0.03226** |

Note the Escalation row's empty graph cell: per C.4 that chunk shared zero entities with the query, so it never entered `graph_results` and contributes only two of the three terms. RRF handles this natively — a list that omits a candidate simply adds nothing for it, no penalty term and no missing-value handling required. This is the mechanism that lets a weak or blind retrieval leg stay wired in without dragging the ranking down.

**Output (`hybrid_chunks`, top `retrieval_top_k*2=16` kept):** both chunks, Sampling ranked first, `source: "hybrid"`. The Escalation chunk survives into the answer purely on its semantic and keyword ranks — a concrete instance of the safe-degrade property section D describes for the graph leg as a whole.

### C.7 `parent_child_expand` node — [Tool: custom `MongoParentStore` lookup, no library — see §0]

`expand_parent_context()` looks up each chunk's `parent_id` in `MongoParentStore`, dedupes by `parent_id` (both chunks here have distinct parents — one per page since `_chunk_group_key` split on page number), and replaces `page_content` with the wider parent text (here identical to the child since each page was a single chunk; with a longer 3-child-per-parent document the parent content would span all three siblings).

**Output (`parent_chunks`):** same 2 documents, `source: "parent_child"`, `metadata.expanded_from_child_id` set to the original `chunk_id`.

### C.8 `rerank` node (`local_rerank`, cross-encoder disabled by default) — [Tool: local lexical scorer (`numpy` only); `sentence-transformers` `CrossEncoder` if `RAG_USE_CROSS_ENCODER_RERANKER=true` — see §0]

For each candidate: `score = 0.70 * lexical_overlap + 0.25 * original_score + metadata_boost(0.05 if title present)`.

- Sampling chunk: query terms `{aql, hs, code, garments, qc, threshold, 6109, 10, shipment, fails, applies}` (11 terms) ∩ chunk terms → overlap ≈ `5/11 = 0.4545`. `0.70×0.4545 + 0.25×0.04918 + 0.05 ≈ 0.3804`
- Escalation chunk: overlap ≈ `3/11 = 0.2727`. `0.70×0.2727 + 0.25×0.03226 + 0.05 ≈ 0.2490`

(The reranker tokenizes the query itself — it does **not** reuse
`_extract_entities`, which is why `hs` and `qc` appear as terms here despite
being invisible to the graph leg in C.4. The reranker is where those
two-character acronyms actually earn their keep.)

Min-max normalized to `[0,1]` → Sampling = `1.0`, Escalation = `0.0`. **Output (`reranked_chunks`):** Sampling first, `source: "reranked"`.

### C.9 `knee_point_select` node — [Tool: custom knee-point heuristic (`numpy` only) — see §0]

Only 2 candidates ≤ `min_k=3`, so `select_by_knee_point` short-circuits and returns both unchanged. **Output (`selected_chunks`):** `[Sampling doc, Escalation doc]` (plain `Document`s, scores dropped).

### C.10 `safety_filter` node — [Tool: regex heuristics (`security.py::PROMPT_INJECTION_PATTERNS`); LLM-as-judge via `LLMClient` if `RAG_USE_LLM_PROMPT_INJECTION_CLASSIFIER=true` — see §0]

`afilter_prompt_injection_chunks()` scans both chunks for injection markers (e.g. "ignore previous instructions"). Neither matches — both pass through unchanged.

### C.11 `augment` node (`build_augmented_prompt`) — [Tool: plain Python f-string templating, no library — see §0]

**Output — the actual LLM prompt string:**
```text
You are a grounded RAG assistant.

Answer the question using only the context below.
If the context is insufficient, say what evidence is missing.
Include citations like [1] or [2] for important claims.

Question:
What AQL threshold applies to HS Code 6109.10 garments, and what happens if the shipment fails QC?

Retrieved context:
[1] title=SGS QC Inspection SOP; section=Sampling; page=3; source=sgs_qc_sop.pdf; chunk=0
SGS QC Inspection SOP — Section 3: Sampling

For shipments under HS Code 6109.10 (cotton knit garments), apply AQL 2.5 sampling per ANSI/ASQ Z1.4. Reject the lot if defects exceed the AQL threshold on major defects. Minor defects up to AQL 4.0 are acceptable with a filed exception.

---

[2] title=SGS QC Inspection SOP; section=Escalation; page=4; source=sgs_qc_sop.pdf; chunk=0
SGS QC Inspection SOP — Section 4: Escalation

If a QC_FAILED result is issued, the Exception Resolver categorizes it under QC_FAILED and routes to AUTO_FIX only when the defect is a labeling error; all other cases escalate to HITL at L0.

Answer:
```

### C.12 `generate` node (`LLMClient.chat` via the LLM Gateway) — [Tool: `LLM_PRIMARY_CONNECTOR` (default `http`) with Groq fallback chain `qwen/qwen3-32b` → `qwen/qwen3.6-27b` → `openai/gpt-oss-safeguard-20b`; plain-Python extractive fallback if the whole chain fails — see §0]

**Output (`answer`):**
```text
For HS Code 6109.10 cotton knit garments, apply AQL 2.5 sampling per ANSI/ASQ Z1.4 —
lots are rejected if major defects exceed that threshold, though minor defects up to
AQL 4.0 are acceptable with a filed exception [1]. If the shipment fails QC
(QC_FAILED), the Exception Resolver classifies it under the QC_FAILED category and
only auto-fixes labeling errors; every other failure escalates to a human via HITL
at approval level L0 [2].
```

### C.13 `validate_citations` node — [Tool: regex citation-marker extraction (`citations.py`), no library — see §0]

Extracts citation markers `[1]`, `[2]` from the answer, checks both against `available_citations = [1, 2]` (one per selected chunk). **Output (`citation_validation`):**
```json
{ "valid": true, "cited_numbers": [1, 2], "missing_citations": false, "invalid_citations": [], "available_citations": [1, 2] }
```

### C.14 `evaluate` node — [Tool: local heuristic metrics (`RAG_ADVANCED_EVAL_PROVIDER=local`, default); `ragas`/`deepeval`/`llm` selectable in config — see §0]

Lightweight local metrics (grounding overlap between answer and selected chunks, citation coverage, answer length sanity). **Output (`evaluation_metrics`):**
```json
{ "grounded": true, "citation_coverage": 1.0, "num_chunks_used": 2, "answer_length_chars": 452 }
```

`query()` returns the full `QueryState` — the API layer typically surfaces just `answer`, `citation_validation`, and the `source`/`page_number` of each `selected_chunks` entry to the caller (e.g. the Negotiation Agent's market-context step, or a UI "answer with sources" panel).

---

## D — What changes when graph retrieval is OFF (default config)

With `RAG_GRAPH_RETRIEVAL_ENABLED=false` (the shipped default), `_resolve_graph_backend()` returns `NoopGraphStore(warn=False)`. Section C.4 becomes:

```json
{ "graph_results": [] }
```

`hybrid_fusion` in C.6 then only fuses `semantic_results` + `keyword_results` (2 lists instead of 3) — the RRF formula and downstream nodes (C.7–C.14) are otherwise identical. This is the "safe degrade" property: enabling the graph leg only ever *adds* candidates into fusion, never required for the pipeline to produce an answer.
