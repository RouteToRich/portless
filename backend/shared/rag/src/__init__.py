"""RAG module — MongoDB Atlas vector store + LLM Gateway generation.

The public names (``query``, ``ingest``, the graph builders, ``settings``,
``router``, ``startup``) are loaded lazily via PEP 562 ``__getattr__``. This keeps
``import shared.rag.src`` — and importing any single submodule (e.g. the graph or
file-store backends) — cheap: the heavy pipeline in ``graph.py`` (and its optional
dependencies) is only imported the first time one of those names is actually used.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = [
    "build_ingestion_graph",
    "build_query_graph",
    "ingest",
    "query",
    "settings",
    "router",
    "startup",
]

# name → relative module that defines it
_LAZY_EXPORTS = {
    "build_ingestion_graph": ".graph",
    "build_query_graph": ".graph",
    "ingest": ".graph",
    "query": ".graph",
    "settings": ".config",
    "router": ".router",
    "startup": ".router",
}


def __getattr__(name: str):
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(module_path, __name__)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(__all__)


if TYPE_CHECKING:  # import-time only for type checkers, never at runtime
    from .config import settings
    from .graph import build_ingestion_graph, build_query_graph, ingest, query
    from .router import router, startup
