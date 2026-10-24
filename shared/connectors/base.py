"""Uniform external-adapter interface: ``quote() / book() / status() / webhook_verify()``."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Connector(ABC):
    """Base class for all external provider adapters.

    Concrete adapters live in their own modules (e.g. ``connectors/sgs.py``) and
    are intentionally unimplemented in this skeleton.
    """

    name: str = "base"

    @abstractmethod
    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Fetch a price / availability quote from the provider."""
        raise NotImplementedError

    @abstractmethod
    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        """Place a booking / order with the provider."""
        raise NotImplementedError

    @abstractmethod
    async def status(self, ext_ref: str) -> dict[str, Any]:
        """Poll the status of a previously placed order."""
        raise NotImplementedError

    @abstractmethod
    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        """Verify the signature/authenticity of an inbound provider webhook."""
        raise NotImplementedError
