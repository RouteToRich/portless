"""Trade-intelligence connectors (global demand & buyer discovery).

See Appendix 8.1 of the master specification. All adapters implement the uniform
``Connector`` interface and are intentionally unimplemented in this skeleton.
"""
from __future__ import annotations

from typing import Any

from .base import Connector


class ComtradeConnector(Connector):
    name = "comtrade"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError


class VolzaConnector(Connector):
    name = "volza"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError


class IndiamartConnector(Connector):
    name = "indiamart"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError


class TradeIndiaConnector(Connector):
    name = "tradeindia"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError
