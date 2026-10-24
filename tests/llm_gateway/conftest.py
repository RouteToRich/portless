"""Shared fixtures for LLM Gateway tests.

No real gateway traffic: chat calls go to an httpx.MockTransport (test_remote.py)
or a patched ``LLMClient._send`` (test_client.py). File I/O goes to pytest's
tmp_path so tests are hermetic.
"""
from __future__ import annotations

import pytest

from shared.llm_gateway.client import LLMResponse
from shared.llm_gateway.tracker import RequestTracker


@pytest.fixture(autouse=True)
def _fresh_gateway_client():
    """The gateway client and credential are process singletons — isolate tests."""
    from shared.llm_gateway import remote

    remote.reset_remote_client()
    yield
    remote.reset_remote_client()


@pytest.fixture()
def mem_tracker() -> RequestTracker:
    """In-memory only tracker (no file I/O)."""
    return RequestTracker(enabled=False)


@pytest.fixture()
def file_tracker(tmp_path) -> RequestTracker:
    """Tracker that writes to a temp file."""
    return RequestTracker(enabled=True, tracking_file=tmp_path / "tracking.txt")


# ──────────────────────────────────────────── response factory

def ok_response(content: str = "ok", model: str = "model-a",
                prompt_tokens: int = 10, completion_tokens: int = 5) -> LLMResponse:
    return LLMResponse(
        content=content,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason="stop",
        raw={},
    )
