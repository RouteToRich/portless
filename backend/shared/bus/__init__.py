"""Event publishing abstraction.

Domain events flow through a single ``EventPublisher`` seam. Three backends:

  NoopPublisher      — events vanish (unit tests, bare service mode)
  InProcessEventBus  — async pub/sub inside one process (dev monolith default)
  NatsJetStreamBus   — durable pub/sub over NATS JetStream (Synadia Cloud);
                       survives restarts, redelivers on handler failure

Backend selection + lifecycle live in ``factory.init_event_bus()`` /
``factory.close_event_bus()`` — call those from an app lifespan; everything
else keeps calling ``get_publisher().publish(...)``.
"""

from .factory import close_event_bus, init_event_bus
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
    "close_event_bus",
    "get_publisher",
    "init_event_bus",
    "set_publisher",
]
