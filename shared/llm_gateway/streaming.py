"""Streaming chat — placeholder, NOT YET IMPLEMENTED.

Why this is needed:
    Negotiation memos can be 1-2K tokens. Waiting for the full response
    before returning makes the UI feel slow for the sourcing ops team.
    Streaming lets the frontend render tokens as they arrive via SSE or
    WebSocket, giving immediate visual feedback.

Hook point:
    Add stream_chat() as a sibling to LLMClient.chat(). It does NOT go
    through the LangGraph graph — streaming is inherently stateful and
    cannot be cleanly interrupted/resumed by the graph router.  Instead
    it calls the first healthy endpoint directly, then falls back
    synchronously if the stream fails before any tokens arrive.

Interface once implemented::

    async for chunk in client.stream_chat(messages, caller="agent"):
        print(chunk.content, end="", flush=True)

    # Or with an SSE adapter:
    async def sse_endpoint(request: Request):
        async def _gen():
            async for chunk in client.stream_chat(messages, caller="agent"):
                yield f"data: {json.dumps({'content': chunk.content})}\n\n"
        return StreamingResponse(_gen(), media_type="text/event-stream")

Implementation plan:
    1. StreamChunk dataclass: content str, model str, finish_reason str,
       is_final bool, usage (prompt/completion tokens, only on last chunk).
    2. Groq connector: ChatGroq.astream() yields AIMessageChunk objects.
    3. OpenAI / Anthropic: similar .astream() pattern.
    4. http connector: httpx AsyncClient with stream=True, parse SSE lines.
    5. First-chunk timeout: if no token arrives within
       settings.stream_first_chunk_timeout_seconds → fall back to next model.
    6. Tracking: start_request before stream, complete_request on StopIteration
       (or on exception if stream fails mid-way).
    7. Config fields on LLMGatewaySettings (already added as placeholders):
       LLM_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import AsyncGenerator, Any

logger = logging.getLogger(__name__)


@dataclass
class StreamChunk:
    """One token chunk from a streaming LLM response."""

    content: str
    model: str
    finish_reason: str = ""
    is_final: bool = False
    prompt_tokens: int = 0        # populated only on the final chunk
    completion_tokens: int = 0    # populated only on the final chunk


async def stream_chat(
    messages: list[dict[str, str]],
    caller: str = "unknown",
    temperature: float | None = None,
    max_tokens: int | None = None,
    **kwargs: Any,
) -> AsyncGenerator[StreamChunk, None]:
    """Stream a chat response token by token.

    Yields:
        StreamChunk objects as tokens arrive from the LLM.
        The last chunk has is_final=True and populated token counts.

    Raises:
        NotImplementedError: until this is implemented.
    """
    # TODO: accept LLMClient instance (or FallbackChain + settings) as arg
    # TODO: iterate endpoints in chain; call ChatGroq/ChatOpenAI .astream()
    # TODO: yield StreamChunk per AIMessageChunk
    # TODO: catch first-chunk timeout and advance to next endpoint
    # TODO: track request with RequestTracker
    raise NotImplementedError(
        "stream_chat() is not yet implemented. "
        "See streaming.py for the implementation plan."
    )
    # needed to make this a generator at the type level
    yield StreamChunk(content="", model="", is_final=True)  # pragma: no cover


class SSEAdapter:
    """Converts stream_chat() output to Server-Sent Events for FastAPI.

    Usage once implemented::

        from fastapi import Request
        from fastapi.responses import StreamingResponse

        @app.post("/v1/chat/stream")
        async def chat_stream(request: Request):
            body = await request.json()
            adapter = SSEAdapter(client)
            return adapter.to_streaming_response(body["messages"], caller="ui")
    """

    def to_streaming_response(
        self,
        messages: list[dict[str, str]],
        caller: str = "unknown",
    ) -> Any:
        """Return a FastAPI StreamingResponse that pushes SSE events.

        Raises:
            NotImplementedError: until this is implemented.
        """
        # TODO: wrap stream_chat() in an async generator that formats SSE lines
        # TODO: return StreamingResponse(..., media_type="text/event-stream")
        raise NotImplementedError(
            "SSEAdapter.to_streaming_response() is not yet implemented."
        )
