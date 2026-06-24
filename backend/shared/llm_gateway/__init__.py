"""LLM Gateway: authenticated, resilient LLM access with model fallback and request tracking.

Provides a shared gateway layer for all AI agents to call LLM providers.
Features:
- JWT token validation on inbound requests
- Model fallback chains (primary → secondary → tertiary)
- Request/response tracking with latency, token usage, and error logging
"""

from .config import LLMGatewaySettings, llm_gateway_settings
from .auth import JWTValidator, require_valid_token
from .client import LLMClient
from .fallback import FallbackChain, ModelEndpoint
from .tracker import RequestTracker, RequestRecord

__all__ = [
    "LLMGatewaySettings",
    "llm_gateway_settings",
    "JWTValidator",
    "require_valid_token",
    "LLMClient",
    "FallbackChain",
    "ModelEndpoint",
    "RequestTracker",
    "RequestRecord",
]
