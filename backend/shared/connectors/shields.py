"""Third-party "shield" connectors: QC, freight and customs (CHA).

See HLD 3.5 and Appendix 8.1. Adapters are skeletons.
"""
from __future__ import annotations

from typing import Any

from .base import Connector


class SgsConnector(Connector):
    """Quality-control inspection (SGS / Bureau Veritas / Intertek class)."""

    name = "sgs"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError


class CogoportConnector(Connector):
    name = "cogoport"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError


class FreightosConnector(Connector):
    name = "freightos"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError


class IcegateConnector(Connector):
    """Customs filing via ICEGATE EDI / registered CHA."""

    name = "icegate"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, ext_ref: str) -> dict[str, Any]:
        raise NotImplementedError

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        raise NotImplementedError
