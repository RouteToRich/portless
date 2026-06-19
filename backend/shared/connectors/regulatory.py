"""Regulatory & verification connectors (GSTN, DGFT, MCA, Udyam, VIES).

See Appendix 8.1. Adapters are skeletons implementing the uniform interface.
"""
from __future__ import annotations

from typing import Any

from .base import Connector


class GstnConnector(Connector):
    name = "gstn"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError


class DgftConnector(Connector):
    name = "dgft"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError


class McaConnector(Connector):
    name = "mca"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError


class UdyamConnector(Connector):
    name = "udyam"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError


class ViesConnector(Connector):
    name = "vies"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError
