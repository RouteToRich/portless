"""Tests for the RAG graph-DB (Mongo + Neo4j) backends, the MongoDB-only file
store, and embeddings routed through the LLM Gateway.

MongoDB is the sole persistence/vector/blob-storage technology in this app — no
S3 or other object store exists anywhere (the graph store's "mongo" backend
keeps that true for graph retrieval too). The real Neo4j driver and Mongo
collection are both mocked, so these run offline. They cover:
  • entity extraction heuristic (shared by every graph backend)
  • GraphStore backend resolution (disabled → no-op; mongo; neo4j configured;
    neo4j without URI → no-op; unimplemented backend → no-op)
  • Neo4jGraphStore upsert / retrieve / delete Cypher wiring
  • MongoGraphStore upsert / retrieve / delete aggregation-pipeline wiring
  • file store factory (MongoDB GridFS — the only backend)
  • embeddings delegate to the LLM Gateway's GatewayEmbeddings
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from langchain_core.documents import Document

from shared.rag.src.file_store import (
    MongoGridFSFileStore,
    get_file_store,
)
from shared.rag.src.graph_store import (
    GraphStore,
    MongoGraphStore,
    Neo4jGraphStore,
    NoopGraphStore,
    _extract_entities,
)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Mock async Neo4j driver
# ---------------------------------------------------------------------------

class _AsyncIter:
    """Async-iterable stand-in for a Neo4j result cursor."""

    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        self._it = iter(self._items)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


def _make_driver(run_return=None):
    """Return (driver, session) where `async with driver.session(...)` yields session."""
    session = MagicMock()
    session.run = AsyncMock(return_value=run_return)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    driver = MagicMock()
    driver.session = MagicMock(return_value=cm)
    return driver, session


# ---------------------------------------------------------------------------
# Entity extraction
# ---------------------------------------------------------------------------

def test_extract_entities_basic():
    ents = _extract_entities("Thailand White Rice HS 1006 from Bangkok")
    assert "thailand" in ents
    assert "rice" in ents
    assert "1006" in ents          # numeric code captured
    assert "from" not in ents      # lowercase → not an entity


def test_extract_entities_drops_stopwords_and_dedupes():
    ents = _extract_entities("The Rice. The Rice. Please note Rice.")
    assert ents.count("rice") == 1
    assert "the" not in ents
    assert "please" not in ents


def test_extract_entities_empty_or_lowercase():
    assert _extract_entities("") == []
    assert _extract_entities("all lowercase words only") == []


# ---------------------------------------------------------------------------
# Neo4jGraphStore
# ---------------------------------------------------------------------------

def test_neo4j_upsert_runs_cypher_and_counts():
    driver, session = _make_driver()
    store = Neo4jGraphStore(driver, database=None)
    chunks = [
        Document(page_content="Thailand White Rice from Bangkok", metadata={"source": "doc1", "chunk_id": "c1"}),
        Document(page_content="Rice price benchmark in Vietnam", metadata={"source": "doc1", "chunk_id": "c2"}),
    ]
    n = run(store.upsert_chunks(chunks))
    assert n == 2
    # chunks + entities + adjacency (both share source doc1 → one adjacency edge)
    assert session.run.await_count == 3


def test_neo4j_upsert_empty_is_noop():
    driver, session = _make_driver()
    assert run(Neo4jGraphStore(driver).upsert_chunks([])) == 0
    session.run.assert_not_awaited()


def test_neo4j_retrieve_returns_scored_docs():
    records = [
        {"chunk_id": "c1", "content": "Thailand rice", "source": "doc1", "section": "", "page": None, "shared": 2},
        {"chunk_id": "c2", "content": "Vietnam rice", "source": "doc1", "section": "", "page": None, "shared": 1},
    ]
    driver, _ = _make_driver(run_return=_AsyncIter(records))
    results = run(Neo4jGraphStore(driver).retrieve("Thailand Rice benchmark", top_k=8))

    assert len(results) == 2
    assert results[0].source == "graph"
    assert results[0].document.page_content == "Thailand rice"
    assert 0.0 <= results[0].score <= 1.0
    assert results[0].document.metadata["chunk_id"] == "c1"


def test_neo4j_retrieve_empty_when_no_query_entities():
    driver, session = _make_driver()
    # all-lowercase question → no entities → short-circuits without touching the DB
    results = run(Neo4jGraphStore(driver).retrieve("what is the current price today", top_k=8))
    assert results == []
    session.run.assert_not_awaited()


def test_neo4j_delete_by_source_returns_count():
    result = MagicMock()
    result.consume = AsyncMock(return_value=MagicMock(counters=MagicMock(nodes_deleted=5)))
    driver, _ = _make_driver(run_return=result)
    assert run(Neo4jGraphStore(driver).delete_by_source("doc1")) == 5


# ---------------------------------------------------------------------------
# MongoGraphStore
# ---------------------------------------------------------------------------

def _make_mongo_store(aggregate_return=None):
    """Return (store, collection) with MongoGraphStore._collection() mocked out
    so no real MongoDB connection is touched."""
    collection = MagicMock()
    collection.bulk_write = AsyncMock()
    collection.aggregate = MagicMock(return_value=_AsyncIter(aggregate_return or []))
    collection.delete_many = AsyncMock(return_value=MagicMock(deleted_count=0))
    store = MongoGraphStore()
    store._collection = AsyncMock(return_value=collection)
    return store, collection


def test_mongo_upsert_writes_bulk_and_counts():
    store, collection = _make_mongo_store()
    chunks = [
        Document(page_content="Thailand White Rice from Bangkok", metadata={"source": "doc1", "chunk_id": "c1"}),
        Document(page_content="Rice price benchmark in Vietnam", metadata={"source": "doc1", "chunk_id": "c2"}),
    ]
    n = run(store.upsert_chunks(chunks))
    assert n == 2
    collection.bulk_write.assert_awaited_once()
    ops = collection.bulk_write.await_args.args[0]
    assert len(ops) == 2


def test_mongo_upsert_empty_is_noop():
    store, collection = _make_mongo_store()
    assert run(store.upsert_chunks([])) == 0
    collection.bulk_write.assert_not_awaited()


def test_mongo_retrieve_returns_scored_docs():
    records = [
        {"chunk_id": "c1", "content": "Thailand rice", "source": "doc1", "section": "", "page_number": None, "shared": 2},
        {"chunk_id": "c2", "content": "Vietnam rice", "source": "doc1", "section": "", "page_number": None, "shared": 1},
    ]
    store, _ = _make_mongo_store(aggregate_return=records)
    results = run(store.retrieve("Thailand Rice benchmark", top_k=8))

    assert len(results) == 2
    assert results[0].source == "graph"
    assert results[0].document.page_content == "Thailand rice"
    assert 0.0 <= results[0].score <= 1.0
    assert results[0].document.metadata["chunk_id"] == "c1"


def test_mongo_retrieve_empty_when_no_query_entities():
    store, collection = _make_mongo_store()
    # all-lowercase question → no entities → short-circuits without touching the DB
    results = run(store.retrieve("what is the current price today", top_k=8))
    assert results == []
    collection.aggregate.assert_not_called()


def test_mongo_delete_by_source_returns_count():
    store, collection = _make_mongo_store()
    collection.delete_many = AsyncMock(return_value=MagicMock(deleted_count=5))
    assert run(store.delete_by_source("doc1")) == 5


# ---------------------------------------------------------------------------
# GraphStore backend resolution
# ---------------------------------------------------------------------------

def test_graph_store_disabled_is_noop(monkeypatch):
    monkeypatch.setattr("shared.rag.src.graph_store.settings.graph_retrieval_enabled", False)
    store = GraphStore()
    assert isinstance(store._impl, NoopGraphStore)
    assert run(store.retrieve("Thailand Rice", 8)) == []
    assert run(store.upsert_chunks([Document(page_content="x")])) == 0


def test_graph_store_resolves_neo4j_when_configured(monkeypatch):
    monkeypatch.setattr("shared.rag.src.graph_store.settings.graph_retrieval_enabled", True)
    monkeypatch.setattr("shared.rag.src.graph_store.settings.graph_db_backend", "neo4j")
    monkeypatch.setattr("shared.rag.src.graph_store.settings.graph_db_uri", "bolt://localhost:7687")
    monkeypatch.setattr("shared.rag.src.graph_store._build_neo4j_driver", lambda *a, **k: MagicMock())
    store = GraphStore()
    assert isinstance(store._impl, Neo4jGraphStore)


def test_graph_store_enabled_without_uri_falls_back_to_noop(monkeypatch):
    # Pin backend=neo4j explicitly — mongo (now the default) needs no URI at
    # all, so this test only makes sense for the neo4j path.
    monkeypatch.setattr("shared.rag.src.graph_store.settings.graph_retrieval_enabled", True)
    monkeypatch.setattr("shared.rag.src.graph_store.settings.graph_db_backend", "neo4j")
    monkeypatch.setattr("shared.rag.src.graph_store.settings.graph_db_uri", "")
    assert isinstance(GraphStore()._impl, NoopGraphStore)


def test_graph_store_resolves_mongo_when_configured(monkeypatch):
    monkeypatch.setattr("shared.rag.src.graph_store.settings.graph_retrieval_enabled", True)
    monkeypatch.setattr("shared.rag.src.graph_store.settings.graph_db_backend", "mongo")
    assert isinstance(GraphStore()._impl, MongoGraphStore)


def test_graph_store_unimplemented_backend_falls_back_to_noop(monkeypatch):
    monkeypatch.setattr("shared.rag.src.graph_store.settings.graph_retrieval_enabled", True)
    monkeypatch.setattr("shared.rag.src.graph_store.settings.graph_db_backend", "arangodb")
    monkeypatch.setattr("shared.rag.src.graph_store.settings.graph_db_uri", "http://localhost:8529")
    assert isinstance(GraphStore()._impl, NoopGraphStore)


# ---------------------------------------------------------------------------
# File store — MongoDB GridFS is the only backend (no S3 anywhere in the app)
# ---------------------------------------------------------------------------

def test_file_store_factory_returns_mongodb_gridfs():
    store = get_file_store()
    assert isinstance(store, MongoGridFSFileStore)
    assert store.backend == "mongodb"


def test_file_store_has_no_s3_backend():
    # Guards against S3 support being re-introduced silently.
    import shared.rag.src.file_store as fs

    assert not hasattr(fs, "S3FileStore")


# ---------------------------------------------------------------------------
# Embeddings routed through the LLM Gateway
# ---------------------------------------------------------------------------

class _FakeLocalEmbeddings:
    def __init__(self, n_features: int = 1536) -> None:
        self.n = n_features

    def embed_documents(self, texts):
        return [[0.0] * self.n for _ in texts]

    def embed_query(self, text):
        return [0.0] * self.n


def test_rag_get_embeddings_routes_through_gateway():
    from shared.llm_gateway.embeddings import GatewayEmbeddings
    from shared.rag.src.embeddings import get_embeddings

    e = get_embeddings()
    assert isinstance(e, GatewayEmbeddings)      # RAG delegates to the gateway class
    assert e._impl_cache is None                 # lazy — no provider SDK built at construction


def test_gateway_embeddings_uses_openai_when_key_present():
    from shared.llm_gateway.embeddings import GatewayEmbeddings

    e = GatewayEmbeddings(provider="openai", api_key="sk-test-key", dimensions=1536)
    assert type(e._impl).__name__ == "OpenAIEmbeddings"   # routed via gateway, not local


def test_gateway_embeddings_falls_back_to_local_without_key(monkeypatch):
    import shared.llm_gateway.embeddings as emb

    monkeypatch.setattr(emb, "LocalHashEmbeddings", _FakeLocalEmbeddings)
    monkeypatch.setattr(emb.llm_gateway_settings, "primary_api_key", "")  # no gateway key either
    e = emb.GatewayEmbeddings(provider="openai", api_key="", dimensions=8)
    assert isinstance(e._impl, _FakeLocalEmbeddings)
    assert e.embed_query("hello") == [0.0] * 8


def test_gateway_embeddings_local_provider(monkeypatch):
    import shared.llm_gateway.embeddings as emb

    monkeypatch.setattr(emb, "LocalHashEmbeddings", _FakeLocalEmbeddings)
    e = emb.GatewayEmbeddings(provider="local", dimensions=16)
    assert isinstance(e._impl, _FakeLocalEmbeddings)
    assert len(e.embed_query("x")) == 16
