"""Event publisher seam (skeleton).

The transactional-outbox pattern (LLD 4.4) will sit behind this interface: a
service writes its state + an outbox row atomically, and a relay drains the
outbox to the bus via a concrete ``EventPublisher``.
"""
from __future__ import annotations

from typing import Protocol

from shared.domain.events import DomainEvent


class EventPublisher(Protocol):
    """Anything that can publish a :class:`DomainEvent`."""

    async def publish(self, event: DomainEvent) -> None: ...


class NoopPublisher:
    """Default no-op publisher used until a real bus is wired up.

    TODO: replace with a NATS JetStream / Kafka backed publisher and an
    outbox relay that guarantees at-least-once delivery.
    """

    async def publish(self, event: DomainEvent) -> None:
        # Intentionally does nothing in the skeleton.
        return None


_publisher: EventPublisher = NoopPublisher()


def get_publisher() -> EventPublisher:
    """Return the process-wide event publisher (swap-in point for the bus)."""
    return _publisher
