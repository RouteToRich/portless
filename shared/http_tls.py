"""One TLS context for the outbound HTTP clients.

``httpx.AsyncClient()`` builds a new SSL context each time it is created, and that loads the
certificate store — about 0.2 s on Windows, synchronously, so the whole event loop stops. A
radar scan creates a client per connector call (hundreds per scan), so the scan connectors
pass this shared context instead: ``httpx.AsyncClient(timeout=30, verify=ssl_context())``.
An ``SSLContext`` is safe to share between clients.
"""
from __future__ import annotations

import functools
import ssl

import httpx


@functools.lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """The process-wide default TLS context (certifi roots, same as httpx's default)."""
    return httpx.create_ssl_context()
