"""Tests for JWT authentication and the require_valid_token dependency."""
from __future__ import annotations

import time

import pytest

from shared.llm_gateway.auth import (
    InvalidTokenError,
    JWTValidator,
    TokenClaims,
    require_valid_token,
)
from shared.llm_gateway.config import LLMGatewaySettings


# ─────────────────────────────────── JWTValidator — create_token / validate


def test_token_round_trip():
    v = JWTValidator(secret="test-secret", issuer="portless", audience="portless-llm-gateway")
    token = v.create_token("user-42")
    claims = v.validate(token)
    assert claims.sub == "user-42"


def test_validate_returns_token_claims():
    v = JWTValidator(secret="test-secret", issuer="portless", audience="portless-llm-gateway")
    token = v.create_token("alice", ttl_seconds=3600)
    claims = v.validate(token)
    assert isinstance(claims, TokenClaims)
    assert claims.iss == "portless"
    assert claims.aud == "portless-llm-gateway"


def test_extra_claims_preserved():
    v = JWTValidator(secret="test-secret", issuer="portless", audience="portless-llm-gateway")
    token = v.create_token("user", role="admin", org="portless")
    claims = v.validate(token)
    assert claims.extra["role"] == "admin"
    assert claims.extra["org"] == "portless"


def test_expired_token_raises():
    v = JWTValidator(secret="test-secret", issuer="portless", audience="portless-llm-gateway")
    token = v.create_token("user", ttl_seconds=-1)  # already expired
    with pytest.raises(InvalidTokenError, match="expired"):
        v.validate(token)


def test_wrong_secret_raises():
    signer = JWTValidator(secret="correct-secret", issuer="portless", audience="portless-llm-gateway")
    verifier = JWTValidator(secret="wrong-secret", issuer="portless", audience="portless-llm-gateway")
    token = signer.create_token("user")
    with pytest.raises(InvalidTokenError):
        verifier.validate(token)


def test_wrong_issuer_raises():
    signer = JWTValidator(secret="s", issuer="portless", audience="portless-llm-gateway")
    verifier = JWTValidator(secret="s", issuer="other-issuer", audience="portless-llm-gateway")
    token = signer.create_token("user")
    with pytest.raises(InvalidTokenError, match="issuer"):
        verifier.validate(token)


def test_wrong_audience_raises():
    signer = JWTValidator(secret="s", issuer="portless", audience="portless-llm-gateway")
    verifier = JWTValidator(secret="s", issuer="portless", audience="other-audience")
    token = signer.create_token("user")
    with pytest.raises(InvalidTokenError, match="audience"):
        verifier.validate(token)


def test_malformed_token_raises():
    v = JWTValidator(secret="s", issuer="portless", audience="portless-llm-gateway")
    with pytest.raises(InvalidTokenError, match="decode"):
        v.validate("not.a.valid.jwt.at.all")


def test_empty_secret_raises_before_decoding():
    v = JWTValidator(secret="")
    with pytest.raises(InvalidTokenError, match="not configured"):
        v.validate("any.token.here")


def test_token_exp_is_in_the_future():
    v = JWTValidator(secret="s", issuer="portless", audience="portless-llm-gateway")
    token = v.create_token("user", ttl_seconds=3600)
    claims = v.validate(token)
    assert claims.exp > int(time.time())


# ─────────────────────────────────── require_valid_token dependency


async def test_passthrough_when_auth_disabled(monkeypatch):
    """When auth_enabled=False the dependency returns anonymous without a token."""
    settings = LLMGatewaySettings(auth_enabled=False)
    monkeypatch.setattr(
        "shared.llm_gateway.auth.llm_gateway_settings", settings
    )
    dep = require_valid_token()
    claims = await dep(authorization="")
    assert claims.sub == "anonymous"


async def test_passthrough_ignores_bearer_when_disabled(monkeypatch):
    settings = LLMGatewaySettings(auth_enabled=False)
    monkeypatch.setattr("shared.llm_gateway.auth.llm_gateway_settings", settings)
    dep = require_valid_token()
    # Even with a bearer token present, returns anonymous when disabled
    claims = await dep(authorization="Bearer some-token")
    assert claims.sub == "anonymous"


async def test_enabled_valid_token_returns_claims(monkeypatch):
    from fastapi import HTTPException

    settings = LLMGatewaySettings(
        auth_enabled=True,
        jwt_secret="test-secret",
        jwt_issuer="portless",
        jwt_audience="portless-llm-gateway",
    )
    monkeypatch.setattr("shared.llm_gateway.auth.llm_gateway_settings", settings)

    validator = JWTValidator(
        secret="test-secret", issuer="portless", audience="portless-llm-gateway"
    )
    token = validator.create_token("user-99")
    dep = require_valid_token(validator)
    claims = await dep(authorization=f"Bearer {token}")
    assert claims.sub == "user-99"


async def test_enabled_missing_bearer_raises_401(monkeypatch):
    from fastapi import HTTPException

    settings = LLMGatewaySettings(auth_enabled=True, jwt_secret="s")
    monkeypatch.setattr("shared.llm_gateway.auth.llm_gateway_settings", settings)
    dep = require_valid_token()
    with pytest.raises(HTTPException) as exc_info:
        await dep(authorization="")
    assert exc_info.value.status_code == 401


async def test_enabled_bad_token_raises_401(monkeypatch):
    from fastapi import HTTPException

    settings = LLMGatewaySettings(
        auth_enabled=True,
        jwt_secret="test-secret",
        jwt_issuer="portless",
        jwt_audience="portless-llm-gateway",
    )
    monkeypatch.setattr("shared.llm_gateway.auth.llm_gateway_settings", settings)
    validator = JWTValidator(
        secret="test-secret", issuer="portless", audience="portless-llm-gateway"
    )
    dep = require_valid_token(validator)
    with pytest.raises(HTTPException) as exc_info:
        await dep(authorization="Bearer this.is.garbage")
    assert exc_info.value.status_code == 401


async def test_enabled_expired_token_raises_401(monkeypatch):
    from fastapi import HTTPException

    settings = LLMGatewaySettings(
        auth_enabled=True,
        jwt_secret="test-secret",
        jwt_issuer="portless",
        jwt_audience="portless-llm-gateway",
    )
    monkeypatch.setattr("shared.llm_gateway.auth.llm_gateway_settings", settings)
    validator = JWTValidator(
        secret="test-secret", issuer="portless", audience="portless-llm-gateway"
    )
    token = validator.create_token("user", ttl_seconds=-1)
    dep = require_valid_token(validator)
    with pytest.raises(HTTPException) as exc_info:
        await dep(authorization=f"Bearer {token}")
    assert exc_info.value.status_code == 401
