"""Event publishing abstraction.

Inter-service communication is REST today; domain events still flow through a
single ``EventPublisher`` seam so the move to a real bus (NATS JetStream →
Kafka/Redpanda, per LLD 4.4) is a drop-in swap, not a refactor.
"""

from .publisher import (
    EventPublisher,
    InProcessEventBus,
    NoopPublisher,
    get_publisher,
    set_publisher,
)

__all__ = [
    "EventPublisher",
    "InProcessEventBus",
    "NoopPublisher",
    "get_publisher",
    "set_publisher",
]
