"""knowledge-service (the RouteToRich RAG service) — through the API gateway.

Same route MakeMeRich uses (makemerich-backend/app/agents/knowledge_client.py):
``<gateway>/api/knowledge/api/v1`` — ``POST /ingestion/upload`` puts one text
document into the vector store and the knowledge graph, ``POST /retrieval`` is
hybrid (dense + BM25) + graph retrieval. The gateway authenticates the bearer and
injects ``X-User-ID`` / ``X-Account-ID``; knowledge-service scopes everything to
that account.

PortLess calls run in background handlers with no user request, so the bearer is
PortLess's own auth-service service account — the same credential the LLM
Gateway client signs in with (``LLM_GATEWAY_URL`` + ``LLM_GATEWAY_EMAIL`` /
``_PASSWORD`` / ``_ACCOUNT_ID``, or ``LLM_GATEWAY_TOKEN``).

What goes in (shared/orchestration/memory.py): e-mails with counterparties,
operator notes, product knowledge, deal summaries. The LLM steps read from it
(negotiation drafts, reply reading, exception analysis); money decisions never
do — those stay on the database and the scoring code.

Never raises into a deal flow: ``remember()`` schedules the upload in the
background (graph ingestion makes LLM calls and can take tens of seconds),
``recall()`` returns ``[]`` on any failure. Off when the gateway is not
configured (CI pins ``LLM_GATEWAY_URL=""``) or ``KNOWLEDGE_ENABLED=false``.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None
_pending: set[asyncio.Task] = set()
_recall_cache: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}
_RECALL_TTL = 300.0


def collection() -> str:
    return os.getenv("KNOWLEDGE_COLLECTION", "portless-trade")


def _timeout() -> float:
    try:
        return float(os.getenv("KNOWLEDGE_TIMEOUT_SECONDS", "60"))
    except ValueError:
        return 60.0


def enabled() -> bool:
    if os.getenv("KNOWLEDGE_ENABLED", "true").strip().lower() in ("0", "false", "no", "off"):
        return False
    try:
        from shared.llm_gateway.config import llm_gateway_settings as s
        return bool(s.gateway_url.strip()) and bool(s.gateway_configured)
    except Exception:  # noqa: BLE001
        return False


def _base_url() -> str:
    from shared.llm_gateway.config import llm_gateway_settings as s
    path = os.getenv("KNOWLEDGE_GATEWAY_PATH", "/api/knowledge/api/v1")
    return f"{s.gateway_url.rstrip('/')}{path}"


def get_client() -> httpx.AsyncClient:
    """Process-wide client on the gateway, with the service-account bearer."""
    global _client
    if _client is None:
        from shared.llm_gateway.config import llm_gateway_settings
        from shared.llm_gateway.remote import _service_auth
        _client = httpx.AsyncClient(base_url=_base_url(), timeout=_timeout(),
                                    auth=_service_auth(llm_gateway_settings))
    return _client


async def aclose() -> None:
    global _client
    for task in list(_pending):
        task.cancel()
    if _client is not None:
        await _client.aclose()
    _client = None


def reset_for_tests() -> None:
    global _client
    _client = None
    _recall_cache.clear()


async def _acl() -> list[str]:
    """Who may read PortLess's documents: ``KNOWLEDGE_ACL`` (comma list) when set,
    else the service account's own user id (the token's ``sub``) — the account
    scope the gateway adds keeps other apps out either way."""
    explicit = [a.strip() for a in os.getenv("KNOWLEDGE_ACL", "").split(",") if a.strip()]
    if explicit:
        return explicit
    try:
        from shared.llm_gateway.config import llm_gateway_settings
        from shared.llm_gateway.remote import _service_auth
        auth = _service_auth(llm_gateway_settings)
        token = await auth._abearer()
        if token and token.count(".") == 2:
            payload = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            sub = claims.get("sub") or claims.get("user_id")
            return [str(sub)] if sub else []
    except Exception:  # noqa: BLE001
        pass
    return []


async def ingest_text(text: str, *, path: str, metadata: dict[str, str]) -> dict[str, Any]:
    """Upload ``text`` as one .txt document; re-ingesting the same ``path`` replaces it."""
    client = get_client()
    acl = await _acl()
    data = {
        "collection": collection(),
        # account_id: the gateway also sets X-Account-ID from the token and knowledge-service
        # scopes by it; stamping it on the record keeps the document explicit about the
        # application account it belongs to (same as MakeMeRich).
        "metadata": json.dumps({"app": "portless", **({"account_id": account_id()} if account_id() else {}),
                                **{k: str(v) for k, v in metadata.items() if v not in (None, "")}}),
        "targets": "vector,graph",  # graph: companies, products, ports, people as entities
        "relative_path": path,
    }
    if acl:
        data["acl"] = ",".join(acl)
    response = await client.post(
        "/ingestion/upload",
        files={"file": (path.rsplit("/", 1)[-1], text.encode("utf-8"), "text/plain")},
        data=data,
    )
    response.raise_for_status()
    return response.json()


async def retrieve(query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
    response = await get_client().post("/retrieval", json={
        "query": query[:4000], "collection": collection(), "top_k": top_k, "rag_mode": "hybrid_graph",
    })
    response.raise_for_status()
    return response.json().get("results", [])


def is_graph_fact(result: dict[str, Any]) -> bool:
    return result.get("source") == "knowledge-graph" or (result.get("metadata") or {}).get(
        "retriever") in ("graph-global", "graph-cypher")


async def _ingest_safely(text: str, path: str, metadata: dict[str, str]) -> None:
    try:
        await ingest_text(text, path=path, metadata=metadata)
        logger.info("Knowledge: remembered %s", path)
    except Exception as exc:  # noqa: BLE001 — memory is an add-on, never a blocker
        logger.warning("Knowledge: could not remember %s: %s", path, exc)


def remember(text: str, *, path: str, metadata: dict[str, Any], wait: bool = False) -> asyncio.Task | None:
    """Store one document in the background. Returns the task (tests / shutdown)."""
    if not enabled() or not text or not text.strip():
        return None
    meta = {k: str(v) for k, v in metadata.items() if v not in (None, "")}
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(_ingest_safely(text.strip()[:20000], path, meta))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


async def recall(query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
    """Relevant passages for ``query`` (graph facts first). ``[]`` when off or failing."""
    if not enabled() or not query.strip():
        return []
    key = (query.strip()[:500], top_k)
    hit = _recall_cache.get(key)
    if hit and time.monotonic() - hit[0] < _RECALL_TTL:
        return hit[1]
    try:
        results = await asyncio.wait_for(retrieve(query, top_k=top_k), timeout=min(_timeout(), 20.0))
    except Exception as exc:  # noqa: BLE001
        logger.info("Knowledge: recall failed (%s) — continuing without it", type(exc).__name__)
        return []
    results = [r for r in results if _ours(r)]
    results.sort(key=lambda r: not is_graph_fact(r))
    _recall_cache[key] = (time.monotonic(), results)
    return results


def account_id() -> str:
    """PortLess's application account (auth-service) — what every document is scoped to."""
    try:
        from shared.llm_gateway.config import llm_gateway_settings as s
        return (s.gateway_account_id or os.getenv("PORTLESS_ACCOUNT_ID", "")).strip()
    except Exception:  # noqa: BLE001
        return os.getenv("PORTLESS_ACCOUNT_ID", "").strip()


def _ours(result: dict[str, Any]) -> bool:
    """Defence in depth on top of the gateway's account scoping: a chunk stamped with
    another application's account, or another app's tag, is never handed to a PortLess
    prompt. Graph facts carry no per-document metadata and are scoped server-side."""
    meta = result.get("metadata") or {}
    mine = account_id()
    if mine and meta.get("account_id") and str(meta["account_id"]) != mine:
        return False
    if meta.get("app") and meta["app"] != "portless" and not is_graph_fact(result):
        return False
    return True


def as_context(results: list[dict[str, Any]], *, max_chars: int = 2500) -> str:
    """Passages as a compact prompt block (each line tagged with its source)."""
    lines: list[str] = []
    used = 0
    for r in results:
        text = (r.get("text") or r.get("content") or r.get("chunk") or "").strip().replace("\n", " ")
        if not text:
            continue
        meta = r.get("metadata") or {}
        tag = meta.get("kind") or ("graph" if is_graph_fact(r) else "doc")
        line = f"- [{tag}] {text[:600]}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)
