# Practice RAG with LangGraph

> ## ⚠️ This README describes the upstream practice project, not this module
>
> It was carried over when RAG was vendored into Portless and has **not** been
> rewritten. It is inaccurate in structural ways — it describes a standalone
> `practice-RAG/` project with a `single_file_langgraph_rag.py`, a
> `src/rag_practice/` package, Chroma as the vector DB, and `USE_OPENAI_*`
> env vars. In this repo the code lives in `src/`, the vector store is
> **MongoDB Atlas `$vectorSearch`**, and all model config comes from the
> shared LLM Gateway (`LLM_EMBEDDING_*` / `LLM_PRIMARY_*`).
>
> **Authoritative reference: [`RAG_IMPLEMENTATION_FLOWS.md`](RAG_IMPLEMENTATION_FLOWS.md)**
> — architecture, every pipeline flow, the full settings table (§38),
> implementation status (§39), and known gaps (§37).
>
> The conceptual material below (why metadata is stored separately, why hybrid
> retrieval helps, what reranking buys you) is still sound. Treat every file
> path, env var, and vector-DB reference in it as wrong.

This project is a practical Retrieval-Augmented Generation system using **LangGraph** for both:

1. **Ingestion pipeline**: parse/load data -> form content and metadata -> chunk -> embed -> insert into vector DB.
2. **Query pipeline**: embed query -> keyword search -> semantic search -> hybrid retrieval -> rerank -> knee-point cutoff -> augment prompt -> generate answer.

It contains two versions:

- `single_file_langgraph_rag.py`: complete RAG implementation in one file.
- `src/rag_practice/`: modular folder-based implementation.

The project works without an API key by using local deterministic hash embeddings and a local extractive answer generator. If you add an OpenAI API key, it can use OpenAI embeddings and chat generation.

---

## 1. Project Structure

```text
practice-RAG/
  README.md
  requirements.txt
  pyproject.toml
  .env.example
  single_file_langgraph_rag.py
  data/
    devsecops_control_plane.md
    rag_architecture_notes.md
  src/
    rag_practice/
      cli.py
      config.py
      embeddings.py
      loader.py
      chunking.py
      vector_store.py
      rerank.py
      llm.py
      graph.py
```

---

## 2. Setup

```powershell
cd c:\workArea\New-Interview\practice-RAG
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Optional OpenAI mode:

```env
OPENAI_API_KEY=your-key
USE_OPENAI_EMBEDDINGS=true
USE_OPENAI_LLM=true
```

---

## 3. Run the Single-File Version

Ingest with recursive character splitting:

```powershell
python single_file_langgraph_rag.py ingest --strategy recursive
```

Ingest with semantic chunking:

```powershell
python single_file_langgraph_rag.py ingest --strategy semantic
```

Ask a question:

```powershell
python single_file_langgraph_rag.py ask "How should a RAG system combine keyword and semantic search?"
```

---

## 4. Run the Modular Folder Version

Install the package in editable mode:

```powershell
pip install -e .
```

Ingest:

```powershell
practice-rag ingest --strategy recursive
practice-rag ingest --strategy semantic
```

Ask:

```powershell
practice-rag ask "When should a release be blocked?"
```

You can also run the module directly:

```powershell
python -m rag_practice.cli ingest --strategy recursive
python -m rag_practice.cli ask "What metadata should be stored with chunks?"
```

---

## 5. LangGraph Ingestion Pipeline

Implemented in:

- Single file: `single_file_langgraph_rag.py`
- Modular: `src/rag_practice/graph.py`

Flow:

```text
parse_and_load -> chunk -> embed_and_insert -> END
```

### Data Parsing and Loading

Implemented in:

- Single file: `load_documents()`
- Modular: `src/rag_practice/loader.py`

Supported file types:

- `.md`
- `.txt`

The loader creates a `Document` with:

- `page_content`: normalized text content.
- `metadata`: source path, file name, file type, title, content hash, and load timestamp.

### Metadata and Content Formation

Each loaded document stores metadata before chunking. Each chunk inherits document metadata and adds:

- `chunk_id`
- `chunk_index`
- `chunk_strategy`
- `chunk_chars`

This matters because vector databases should not store only embeddings. They should also store metadata for filtering, citations, access control, freshness checks, and debugging retrieval quality.

### Chunking

Implemented in:

- Single file: `recursive_chunks()` and `semantic_chunks()`
- Modular: `src/rag_practice/chunking.py`

Supported strategies:

1. **Recursive character text splitter**
   - Uses `RecursiveCharacterTextSplitter`.
   - Separators include headings, paragraphs, lines, sentences, spaces, and characters.
   - Good default for most text corpora.

2. **Semantic chunking**
   - Splits text into semantic units.
   - Embeds neighboring units.
   - Computes cosine similarity between adjacent units.
   - Creates a breakpoint when similarity falls below a percentile threshold.
   - Useful when topic boundaries do not match paragraph boundaries.

### Embedding Data

Implemented in:

- Single file: `LocalHashEmbeddings` and `get_embeddings()`
- Modular: `src/rag_practice/embeddings.py`

The system supports:

- Local hash embeddings for practice without API keys.
- OpenAI embeddings when `USE_OPENAI_EMBEDDINGS=true`.

### What About Metadata?

Embeddings represent the semantic meaning of chunk text. Metadata is stored beside each chunk in Chroma.

Metadata is used for:

- Citations.
- Source traceability.
- Filtering by file, type, owner, date, or access control.
- Debugging retrieval.
- Avoiding stale or unauthorized context.

### Insert into Vector DB

Implemented in:

- Single file: `vector_store()` and `upsert_chunks()`
- Modular: `src/rag_practice/vector_store.py`

The project uses **Chroma** as the local vector database.

---

## 6. LangGraph Retrieval and Generation Pipeline

Implemented in:

- Single file: `build_query_graph()`
- Modular: `src/rag_practice/graph.py`

Flow:

```text
embed_query
  -> semantic_search
  -> keyword_search
  -> hybrid_fusion
  -> rerank
  -> knee_point_select
  -> augment
  -> generate
  -> END
```

### Embedding Query

The query is embedded before retrieval. In the modular version, this is the `embed_query` node in `src/rag_practice/graph.py`.

### Search: Keyword and Semantic

Implemented in:

- Modular: `src/rag_practice/vector_store.py`

Search types:

- **Semantic search**: uses query embedding against Chroma.
- **Keyword search**: uses BM25 over stored chunk text.

### Hybrid Chunks

Hybrid retrieval uses reciprocal rank fusion to combine semantic and keyword results.

This helps because:

- Semantic search catches meaning.
- Keyword search catches exact terms, product names, IDs, CVEs, APIs, and acronyms.

### Top-K

`RETRIEVAL_TOP_K` controls how many candidates each retriever returns.

Default:

```env
RETRIEVAL_TOP_K=8
```

### Reranking

Implemented in:

- Single file: `rerank()`
- Modular: `src/rag_practice/rerank.py`

The local reranker combines:

- lexical overlap with the query,
- original hybrid retrieval score,
- small metadata boost.

In a production system, replace this with:

- Cohere Rerank,
- `bge-reranker`,
- a cross-encoder,
- or an LLM relevance scorer.

### Knee-Point Algorithm

Implemented in:

- Single file: `knee_point()`
- Modular: `select_by_knee_point()`

Purpose:

- Sort reranked chunks by relevance score.
- Plot rank vs score.
- Find the point where relevance starts flattening.
- Select chunks up to that point.

This avoids blindly passing a fixed number of chunks to the LLM.

### Query + Content Augmentation

Implemented in:

- Single file: `build_prompt()`
- Modular: `src/rag_practice/llm.py`

The augmented prompt includes:

- user query,
- selected chunks,
- source metadata,
- chunk indices,
- grounding instructions,
- citation requirement.

### Pass to LLM and Get Response

Implemented in:

- Single file: `generate()`
- Modular: `generate_answer()`

By default, the project returns a local grounded response from retrieved evidence.

When `USE_OPENAI_LLM=true`, the augmented prompt is sent to the configured OpenAI chat model.

---

## 7. End-to-End Example

```powershell
cd c:\workArea\New-Interview\practice-RAG
pip install -r requirements.txt

python single_file_langgraph_rag.py ingest --strategy recursive
python single_file_langgraph_rag.py ask "What metadata should be stored for RAG chunks?"
```

Expected behavior:

1. Documents from `data/` are parsed and loaded.
2. Content and metadata are formed.
3. Text is chunked.
4. Chunks are embedded.
5. Chunks are inserted into Chroma.
6. Query is embedded.
7. Semantic and keyword retrieval run.
8. Hybrid results are fused.
9. Results are reranked.
10. Knee-point logic selects the best chunks.
11. Prompt is augmented with selected chunks.
12. The answer is generated.

---

## 8. Production Improvements

For production, add:

- stronger document parsers for PDF, HTML, DOCX, code, and tables,
- metadata-based access control filters,
- persistent ingestion job tracking,
- deduplication by content hash,
- deletion and re-indexing workflows,
- production embedding models,
- cross-encoder reranking,
- observability for retrieval quality,
- prompt and answer evaluation,
- citation validation,
- guardrails against prompt injection in retrieved content.
