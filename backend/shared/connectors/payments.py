"""Payment / escrow connectors (Stripe Treasury, Payoneer).

See HLD 3.5 / 3.6. Adapters are skeletons implementing the uniform interface.
"""
from __future__ import annotations

from typing import Any

from .base import Connector


class StripeTreasuryConnector(Connector):
    name = "stripe_treasury"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError


class PayoneerConnector(Connector):
    name = "payoneer"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError
