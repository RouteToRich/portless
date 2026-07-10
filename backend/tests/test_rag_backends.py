"""Tests for the RAG graph-DB (Neo4j) and S3 file-store backends.

The real drivers (neo4j, boto3) are mocked, so these run offline. They cover:
  • entity extraction heuristic
  • GraphStore backend resolution (disabled → no-op; configured → Neo4j; no URI → no-op)
  • Neo4jGraphStore upsert / retrieve / delete Cypher wiring
  • S3FileStore save / read + factory selection and validation
"""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.documents import Document

from shared.rag.src.file_store import (
    MongoGridFSFileStore,
    get_file_store,
)
from shared.rag.src.graph_store import (
    GraphStore,
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
    monkeypatch.setattr("shared.rag.src.graph_store.settings.graph_retrieval_enabled", True)
    monkeypatch.setattr("shared.rag.src.graph_store.settings.graph_db_uri", "")
    assert isinstance(GraphStore()._impl, NoopGraphStore)


def test_graph_store_unimplemented_backend_falls_back_to_noop(monkeypatch):
    monkeypatch.setattr("shared.rag.src.graph_store.settings.graph_retrieval_enabled", True)
    monkeypatch.setattr("shared.rag.src.graph_store.settings.graph_db_backend", "arangodb")
    monkeypatch.setattr("shared.rag.src.graph_store.settings.graph_db_uri", "http://localhost:8529")
    assert isinstance(GraphStore()._impl, NoopGraphStore)


# ---------------------------------------------------------------------------
# S3 file store
# ---------------------------------------------------------------------------

def _install_fake_boto3(monkeypatch, client):
    fake_boto3 = MagicMock()
    fake_boto3.client = MagicMock(return_value=client)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)


def test_s3_file_store_save_and_read(monkeypatch):
    body = MagicMock()
    body.read = MagicMock(return_value=b"hello world")
    client = MagicMock()
    client.put_object = MagicMock(return_value={})
    client.get_object = MagicMock(return_value={"Body": body})
    _install_fake_boto3(monkeypatch, client)

    monkeypatch.setattr("shared.rag.src.file_store.settings.file_store_backend", "s3")
    monkeypatch.setattr("shared.rag.src.file_store.settings.s3_bucket", "my-bucket")
    monkeypatch.setattr("shared.rag.src.file_store.settings.s3_prefix", "rag/")
    monkeypatch.setattr("shared.rag.src.file_store.settings.s3_access_key_id", "")
    monkeypatch.setattr("shared.rag.src.file_store.settings.s3_secret_access_key", "")

    store = get_file_store()
    assert store.backend == "s3"

    stored = run(store.save("quote.txt", b"hello world", content_type="text/plain"))
    assert stored.backend == "s3"
    assert stored.size == 11
    client.put_object.assert_called_once()
    kwargs = client.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "my-bucket"
    assert kwargs["Key"].startswith("rag/")
    assert kwargs["Key"].endswith("quote.txt")

    data = run(store.read(stored.file_id))
    assert data == b"hello world"


def test_s3_file_store_requires_bucket(monkeypatch):
    _install_fake_boto3(monkeypatch, MagicMock())
    monkeypatch.setattr("shared.rag.src.file_store.settings.file_store_backend", "s3")
    monkeypatch.setattr("shared.rag.src.file_store.settings.s3_bucket", "")
    with pytest.raises(ValueError):
        get_file_store()


def test_file_store_factory_defaults_to_mongodb(monkeypatch):
    monkeypatch.setattr("shared.rag.src.file_store.settings.file_store_backend", "mongodb")
    store = get_file_store()
    assert isinstance(store, MongoGridFSFileStore)
    assert store.backend == "mongodb"
