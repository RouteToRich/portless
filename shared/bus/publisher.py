"""Event publisher seam + the in-process event bus.

The transactional-outbox pattern (LLD 4.4) will eventually sit behind this
interface: a service writes its state + an outbox row atomically, and a relay
drains the outbox to the bus via a concrete ``EventPublisher``.

Today two publishers exist:

  NoopPublisher      — default; events vanish (unit tests, bare service mode).
  InProcessEventBus  — wired by ``app.py``'s lifespan in the combined monolith;
                       fans each event out to subscribed async handlers as
                       fire-and-forget tasks. This is what makes the pipeline
                       event-driven (e.g. NEGOTIATION_AGREED → PI issuance)
                       instead of relying on a long-lived poller task that can
                       die and leave deals parked.

Delivery guarantee is best-effort in-process (no persistence, no retry across
restarts). The durable outbox + relay remains the production upgrade path —
handlers must therefore already be idempotent so the swap is behavioral no-op.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Awaitable, Callable, Protocol

from shared.domain.events import DomainEvent, EventTopic

logger = logging.getLogger(__name__)

EventHandler = Callable[[DomainEvent], Awaitable[None]]


class EventPublisher(Protocol):
    """Anything that can publish a :class:`DomainEvent`."""

    async def publish(self, event: DomainEvent) -> None: ...


class NoopPublisher:
    """Default no-op publisher used until a real bus is wired up."""

    async def publish(self, event: DomainEvent) -> None:
        # Intentionally does nothing in the skeleton.
        return None


class InProcessEventBus:
    """Async in-process pub/sub.

    Handlers run as detached tasks so publishing never blocks the caller
    (finalize_agreement should not wait for PI rendering). Handler exceptions
    are logged loudly and swallowed — a failing continuation must never crash
    the state change that triggered it; recovery is the handler's job
    (idempotent re-trigger or deal exception).
    """

    def __init__(self) -> None:
        self._handlers: dict[EventTopic, list[EventHandler]] = defaultdict(list)
        self._tasks: set[asyncio.Task] = set()

    def subscribe(self, topic: EventTopic, handler: EventHandler) -> None:
        self._handlers[topic].append(handler)
        logger.info("EventBus: subscribed %s → %s", topic.value, getattr(handler, "__name__", handler))

    async def publish(self, event: DomainEvent) -> None:
        handlers = self._handlers.get(event.topic, [])
        logger.info(
            "EventBus: %s (deal=%s, event_id=%s) → %d handler(s)",
            event.topic.value, event.deal_id, event.event_id, len(handlers),
        )
        for handler in handlers:
            task = asyncio.create_task(self._run_handler(handler, event))
            # Keep a strong reference until done — otherwise the task can be
            # garbage-collected mid-flight.
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _run_handler(handler: EventHandler, event: DomainEvent) -> None:
        try:
            await handler(event)
        except Exception:
            logger.exception(
                "EventBus: handler %s failed for %s (deal=%s, event_id=%s)",
                getattr(handler, "__name__", handler),
                event.topic.value, event.deal_id, event.event_id,
            )


_publisher: EventPublisher = NoopPublisher()


def get_publisher() -> EventPublisher:
    """Return the process-wide event publisher (swap-in point for the bus)."""
    return _publisher


def set_publisher(publisher: EventPublisher) -> None:
    """Install the process-wide publisher (called once from app startup)."""
    global _publisher
    _publisher = publisher
