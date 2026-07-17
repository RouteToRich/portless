"""Fallback chain: ordered list of LLM endpoints tried in sequence on failure.

Chain assembly (from_settings):
  slot 0  — primary model     (optional; added only when primary_api_key is set)
  slot 1  — qwen/qwen3-32b   via Groq
  slot 2  — qwen/qwen3.6-27b via Groq
  slot 3  — openai/gpt-oss-safeguard-20b via Groq

Error classification (classify_error):
  "rate_limit"        — 429 request/token-per-minute burst → retry after a long,
                        quota-aware delay (see get_retry_delay_seconds) then advance chain
  "request_too_large" — 413 single request exceeds the model's TPM ceiling on its
                        own (e.g. "Requested 6140, Limit 6000") → waiting never
                        helps, since the request doesn't shrink between attempts;
                        skip retries and advance to the next model immediately
  "transient"         — timeout, connection drop, 5xx → retry with normal exponential backoff
  "fatal"             — bad request, auth error, other 4xx → skip retries, advance chain
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Sequence

import httpx

from .config import llm_gateway_settings

logger = logging.getLogger(__name__)


@dataclass
class ModelEndpoint:
    """Single LLM endpoint — provider-agnostic descriptor."""

    model: str
    api_key: str
    connector: str = "http"  # "groq" | "openai" | "anthropic" | "http"
    base_url: str = ""       # required only for connector="http"
    priority: int = 0        # lower = higher priority in chain


@dataclass
class FallbackChain:
    """Ordered sequence of endpoints to try when a model call fails."""

    endpoints: list[ModelEndpoint] = field(default_factory=list)

    # ---------------------------------------------------------------- factory

    @classmethod
    def from_settings(cls) -> FallbackChain:
        """Build the fallback chain from environment configuration.

        Always includes the three Groq fallback models.
        Prepends a primary model endpoint when LLM_PRIMARY_API_KEY is set.
        """
        endpoints: list[ModelEndpoint] = []

        if llm_gateway_settings.primary_api_key and llm_gateway_settings.primary_model:
            endpoints.append(
                ModelEndpoint(
                    model=llm_gateway_settings.primary_model,
                    api_key=llm_gateway_settings.primary_api_key,
                    connector=llm_gateway_settings.primary_connector,
                    base_url=llm_gateway_settings.primary_base_url,
                    priority=0,
                )
            )

        groq_key = llm_gateway_settings.groq_api_key
        groq_fallbacks = [
            (
                llm_gateway_settings.fallback_1_model,
                llm_gateway_settings.fallback_1_connector,
                llm_gateway_settings.fallback_1_api_key or groq_key,
            ),
            (
                llm_gateway_settings.fallback_2_model,
                llm_gateway_settings.fallback_2_connector,
                llm_gateway_settings.fallback_2_api_key or groq_key,
            ),
            (
                llm_gateway_settings.fallback_3_model,
                llm_gateway_settings.fallback_3_connector,
                llm_gateway_settings.fallback_3_api_key or groq_key,
            ),
        ]

        for i, (model, connector, api_key) in enumerate(groq_fallbacks, start=1):
            if model and api_key:
                endpoints.append(
                    ModelEndpoint(
                        model=model,
                        api_key=api_key,
                        connector=connector,
                        base_url="",
                        priority=i,
                    )
                )

        if not endpoints:
            logger.error(
                "LLM Gateway: fallback chain is empty — set LLM_GROQ_API_KEY "
                "or configure a primary model."
            )

        logger.info(
            "LLM Gateway chain: %s",
            " → ".join(f"{e.model}({e.connector})" for e in endpoints),
        )
        return cls(endpoints=endpoints)

    @classmethod
    def custom(cls, endpoints: list[ModelEndpoint]) -> FallbackChain:
        """Build a chain from an explicit list (useful for tests)."""
        return cls(endpoints=sorted(endpoints, key=lambda e: e.priority))

    # ------------------------------------------------------------ accessors

    def get_ordered_endpoints(self) -> Sequence[ModelEndpoint]:
        return self.endpoints

    def get_primary(self) -> ModelEndpoint | None:
        return self.endpoints[0] if self.endpoints else None

    def __len__(self) -> int:
        return len(self.endpoints)


# ---------------------------------------------------------------- error helpers


def classify_error(exc: Exception) -> str:
    """Classify an exception as "rate_limit", "request_too_large", "transient"
    (all retry in some form), or "fatal" (skip straight to next model).

    Rate-limit (429): a burst against the per-minute request/token quota —
                worth a long, quota-aware wait (see get_retry_delay_seconds)
                since the window rolls forward and frees up capacity.
    Request-too-large (413): Groq reports "request too large ... tokens per
                minute (TPM): Limit 6000, Requested 6140" as a 413, not a 429.
                Unlike a 429 burst, retrying changes nothing — the same
                request is still over the ceiling every time — so this must
                skip retries and advance to the next model immediately,
                exactly like a fatal error.
    Transient: timeouts, network drops, 5xx — worth retrying almost immediately.
    Fatal: bad request, wrong auth, model-level refusal — retrying the same
           model won't help; advance to the next one immediately.
    """
    # ---- Groq SDK exceptions (groq package, installed with langchain-groq) ----
    try:
        import groq

        if isinstance(exc, groq.RateLimitError):
            status_code = getattr(exc, "status_code", None)
            return "request_too_large" if status_code == 413 else "rate_limit"

        if isinstance(
            exc,
            (
                groq.APIConnectionError,
                groq.InternalServerError,
                groq.APITimeoutError,
            ),
        ):
            return "transient"

        if isinstance(
            exc,
            (
                groq.BadRequestError,
                groq.AuthenticationError,
                groq.PermissionDeniedError,
            ),
        ):
            # groq.BadRequestError covers HTTP 400 AND 413 — Groq's "request too
            # large for ... tokens per minute" quota error is reported as 413,
            # so it lands here even though it's a rate-limit condition, not a
            # malformed request.
            status_code = getattr(exc, "status_code", None)
            if status_code == 413:
                return "request_too_large"
            if status_code == 429:
                return "rate_limit"
            return "fatal"
    except ImportError:
        pass

    # ---- httpx exceptions (raw HTTP connector) ----
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return "transient"

    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code == 413:
            return "request_too_large"
        if status_code == 429:
            return "rate_limit"
        return "transient" if status_code in (500, 502, 503, 504) else "fatal"

    # ---- Generic fallback: inspect status_code attribute if present ----
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        resp = getattr(exc, "response", None)
        status_code = getattr(resp, "status_code", None)

    if status_code is not None:
        if status_code == 413:
            return "request_too_large"
        if status_code == 429:
            return "rate_limit"
        if 500 <= status_code <= 599:
            return "transient"
        # Any other 4xx (400/401/403 auth/bad-request, but also 404 model-not-
        # found, 405, 409, 422, …) is a permanent condition for THIS endpoint —
        # retrying the identical request never changes the outcome, so skip
        # retries and advance to the next model immediately.
        if 400 <= status_code <= 499:
            return "fatal"

    # Unknown errors with no status code: treat as transient — give the model
    # one more chance (a bare timeout/connection hiccup with no HTTP response).
    return "transient"


_RETRY_AFTER_RE = re.compile(r"retry.{0,5}after\D{0,10}([\d.]+)", re.IGNORECASE)

# Groq's actual wording is "try again in <duration>", not "retry after <n>", and
# the duration itself is a composite "1h1m44.832s" / "20.5s" / "3m2s" format, not
# a bare number — the pattern above never matches it, silently discarding the
# server's real wait time (a TPD quota's multi-hour figure is exactly the case
# that most needs to be read correctly, since it drives the fallback-ceiling
# decision). Each unit group is optional and independently captured.
_TRY_AGAIN_RE = re.compile(
    r"try\s+again\s+in\s+"
    r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?!s))?\s*(?:([\d.]+)\s*s)?",
    re.IGNORECASE,
)


def _parse_try_again_duration(text: str) -> float | None:
    m = _TRY_AGAIN_RE.search(text)
    if not m or not any(m.groups()):
        return None
    hours, minutes, seconds = (float(g) if g else 0.0 for g in m.groups())
    total = hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


def get_retry_delay_seconds(exc: Exception, base_delay: float, attempt: int) -> float:
    """Compute the retry delay for a "rate_limit"-classified error.

    Prefers a server-provided wait time (Groq's ``Retry-After`` header, or the
    same figure echoed in the error message body) over a fixed guess — Groq's
    TPM/RPM windows reset on a rolling per-minute basis, so a fixed exponential
    backoff (e.g. 1s, 2s) almost always retries before the quota has room
    again. Falls back to ``rate_limit_retry_floor_seconds`` (default 20s) when
    no explicit wait time is available.
    """
    retry_after = _extract_retry_after_seconds(exc)
    if retry_after is not None:
        return max(retry_after, 1.0)
    floor = llm_gateway_settings.rate_limit_retry_floor_seconds
    return max(floor, base_delay * (2 ** attempt))


def _extract_retry_after_seconds(exc: Exception) -> float | None:
    # groq/openai SDK exceptions expose the raw httpx.Response on .response
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers:
        for key in ("retry-after", "Retry-After", "x-ratelimit-reset-tokens"):
            value = headers.get(key) if hasattr(headers, "get") else None
            if value:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    pass

    # Fall back to parsing the error message text. Two Groq wordings observed:
    #   "retry after 20.5s"          (older / other-provider style)
    #   "try again in 1h1m44.832s"   (Groq's actual TPD/TPM quota wording)
    text = str(exc)
    match = _RETRY_AFTER_RE.search(text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    duration = _parse_try_again_duration(text)
    if duration is not None:
        return duration
    return None
