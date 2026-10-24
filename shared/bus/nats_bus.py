"""NATS JetStream event bus — the durable, cross-process transport.

Fills the ``EventPublisher`` seam with a real broker (Synadia Cloud / any NATS
server with JetStream). Call sites are untouched: services keep calling
``get_publisher().publish(DomainEvent(...))``.

Topology
--------
  Stream    : one stream (default ``PORTLESS_EVENTS``) capturing every subject
              under ``<prefix>.>`` (default prefix ``portless``).
  Subject   : ``<prefix>.<EventTopic.value>`` — e.g. ``portless.negotiation.agreed``.
  Consumers : one durable *pull* consumer per subscribed topic
              (``portless-cont-negotiation-agreed``), so redelivery cursors
              survive process restarts — events published while the consumer
              host is down are replayed on the next boot.

Delivery semantics
------------------
  • At-least-once. Handlers are already required to be idempotent (see
    shared/orchestration/continuations.py design rules); the ``Nats-Msg-Id``
    header (= ``event_id``) additionally lets JetStream drop duplicate
    *publishes* inside the stream's dedupe window.
  • Unlike ``InProcessEventBus`` (fire-and-forget tasks), handlers here run
    to completion before the message is acked — that is what makes redelivery
    meaningful.

Failure handling (the whole point of this module)
-------------------------------------------------
  publish() NEVER raises — a broker outage must not roll back the state
  change that triggered the event:
    • broker unreachable / publish error → loud log, then the event is
      dispatched to locally subscribed handlers (degraded in-process
      delivery). A publish-only process (no local handlers) logs that the
      event was dropped.
  Consumer side:
    • handler raises           → NAK with exponential backoff → redelivered.
    • max deliveries exhausted → TERM (stop redelivering) + CRITICAL log +
      a ``deal.exception`` event (unless the failing topic already IS
      ``deal.exception`` — no self-amplifying loops).
    • unparseable message      → TERM immediately (poison message).
    • long-running handler     → ``in_progress`` heartbeats keep the ack
      deadline from expiring mid-handler (the exception-resolver LLM run
      can exceed ack_wait).
    • fetch/connection errors  → logged, retried with backoff; the nats
      client itself reconnects automatically.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from shared.domain.events import DomainEvent, EventTopic

from .publisher import EventHandler

logger = logging.getLogger(__name__)

# Redelivery backoff schedule (seconds) indexed by delivery attempt.
_NAK_DELAYS = (5.0, 15.0, 30.0, 60.0)

_DEFAULT_NATS_PORT = 4222


def _nak_delay(attempt: int) -> float:
    return _NAK_DELAYS[min(max(attempt - 1, 0), len(_NAK_DELAYS) - 1)]


def _normalize_url(url: str) -> str:
    """Ensure the server URL carries an explicit port.

    nats-py mishandles a port-less ``tls://host`` URL (hangs / WinError 10049
    loop instead of defaulting to 4222) — seen live against Synadia Cloud.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme and parsed.hostname and parsed.port is None:
        return f"{parsed.scheme}://{parsed.netloc}:{_DEFAULT_NATS_PORT}{parsed.path}"
    return url


class NatsJetStreamBus:
    """Durable pub/sub over NATS JetStream implementing ``EventPublisher``.

    Same ``subscribe(topic, handler)`` surface as ``InProcessEventBus`` so
    ``register_continuations()`` works on either.
    """

    def __init__(
        self,
        *,
        url: str,
        creds_file: str | None = None,
        stream_name: str = "PORTLESS_EVENTS",
        subject_prefix: str = "portless",
        durable_prefix: str = "portless-cont",
        connect_timeout: float = 10.0,
        publish_timeout: float = 5.0,
        ack_wait: float = 120.0,
        max_deliver: int = 5,
        max_age_seconds: float = 14 * 24 * 3600,
        max_bytes: int = 128 * 1024 * 1024,  # Synadia requires a bound; 128 MiB ≫ event volume
        fetch_batch: int = 10,
        fetch_timeout: float = 5.0,
    ) -> None:
        self._url = _normalize_url(url)
        self._creds_file = creds_file
        self._stream_name = stream_name
        self._subject_prefix = subject_prefix
        self._durable_prefix = durable_prefix
        self._connect_timeout = connect_timeout
        self._publish_timeout = publish_timeout
        self._ack_wait = ack_wait
        self._max_deliver = max_deliver
        self._max_age_seconds = max_age_seconds
        self._max_bytes = max_bytes
        self._fetch_batch = fetch_batch
        self._fetch_timeout = fetch_timeout

        self._handlers: dict[EventTopic, list[EventHandler]] = defaultdict(list)
        self._nc = None
        self._js = None
        self._consumer_tasks: list[asyncio.Task] = []
        self._local_tasks: set[asyncio.Task] = set()
        self._closing = False

    # ------------------------------------------------------------------ #
    # Wiring                                                              #
    # ------------------------------------------------------------------ #

    def subscribe(self, topic: EventTopic, handler: EventHandler) -> None:
        self._handlers[topic].append(handler)
        logger.info("NatsBus: subscribed %s → %s", topic.value, getattr(handler, "__name__", handler))

    def _subject(self, topic: EventTopic) -> str:
        return f"{self._subject_prefix}.{topic.value}"

    def _durable(self, topic: EventTopic) -> str:
        # Durable names must not contain dots.
        return f"{self._durable_prefix}-{topic.value.replace('.', '-')}"

    async def connect(self) -> None:
        """Connect + ensure the stream exists. Raises on failure — the
        factory catches and falls back to the in-process bus."""
        import nats

        async def _on_error(exc: Exception) -> None:
            logger.error("NatsBus: client error: %s", exc)

        async def _on_disconnect() -> None:
            logger.warning("NatsBus: disconnected from %s — client will auto-reconnect", self._url)

        async def _on_reconnect() -> None:
            logger.info("NatsBus: reconnected to %s", self._url)

        async def _on_close() -> None:
            level = logging.INFO if self._closing else logging.ERROR
            logger.log(level, "NatsBus: connection closed (closing=%s)", self._closing)

        # Bound the INITIAL connect hard (wait_for) so a misconfigured URL /
        # unreachable broker fails fast and the factory can fall back to the
        # in-process bus instead of blocking app startup. Reconnects after an
        # established connection are generous but finite.
        self._nc = await asyncio.wait_for(
            nats.connect(
                servers=[self._url],
                user_credentials=self._creds_file,
                name="portless-backend",
                connect_timeout=self._connect_timeout,
                max_reconnect_attempts=120,
                error_cb=_on_error,
                disconnected_cb=_on_disconnect,
                reconnected_cb=_on_reconnect,
                closed_cb=_on_close,
            ),
            timeout=self._connect_timeout * 3,
        )
        self._js = self._nc.jetstream()
        await self._ensure_stream()
        logger.info(
            "NatsBus: connected to %s (stream=%s, subjects=%s.>)",
            self._url, self._stream_name, self._subject_prefix,
        )

    async def _ensure_stream(self) -> None:
        from nats.js.errors import NotFoundError

        wanted = f"{self._subject_prefix}.>"
        try:
            info = await self._js.stream_info(self._stream_name)
            subjects = list(info.config.subjects or [])
            if wanted not in subjects:
                info.config.subjects = [*subjects, wanted]
                await self._js.update_stream(info.config)
                logger.info("NatsBus: added subject %s to existing stream %s", wanted, self._stream_name)
        except NotFoundError:
            await self._js.add_stream(
                name=self._stream_name,
                subjects=[wanted],
                max_age=self._max_age_seconds,
                # Synadia Cloud rejects unbounded streams
                # ("account requires a stream config to have max bytes set").
                max_bytes=self._max_bytes,
            )
            logger.info("NatsBus: created stream %s (subjects=[%s])", self._stream_name, wanted)

    async def start_consumers(self) -> None:
        """Create one durable pull consumer + fetch loop per subscribed topic."""
        from nats.js.api import ConsumerConfig

        for topic in self._handlers:
            subject = self._subject(topic)
            durable = self._durable(topic)
            config = ConsumerConfig(
                durable_name=durable,
                filter_subject=subject,
                ack_wait=self._ack_wait,
                max_deliver=self._max_deliver,
            )
            psub = await self._js.pull_subscribe(
                subject, durable=durable, stream=self._stream_name, config=config,
            )
            task = asyncio.create_task(
                self._consume_loop(topic, psub), name=f"nats-consumer-{durable}",
            )
            self._consumer_tasks.append(task)
            logger.info("NatsBus: durable consumer %s started (subject=%s)", durable, subject)

    # ------------------------------------------------------------------ #
    # Publish                                                             #
    # ------------------------------------------------------------------ #

    async def publish(self, event: DomainEvent) -> None:
        """Publish to JetStream. Never raises — see module docstring."""
        subject = self._subject(event.topic)
        try:
            if self._js is None:
                raise RuntimeError("JetStream context not connected")
            await self._js.publish(
                subject,
                event.model_dump_json().encode("utf-8"),
                timeout=self._publish_timeout,
                # Stream-level dedupe: republishing the same event_id inside
                # the dedupe window is dropped by the server.
                headers={"Nats-Msg-Id": event.event_id},
            )
            logger.info(
                "NatsBus: published %s (deal=%s, event_id=%s)",
                event.topic.value, event.deal_id, event.event_id,
            )
        except Exception as exc:
            logger.error(
                "NatsBus: publish FAILED for %s (deal=%s, event_id=%s): %s — "
                "falling back to in-process delivery",
                event.topic.value, event.deal_id, event.event_id, exc,
            )
            self._dispatch_locally(event)

    def _dispatch_locally(self, event: DomainEvent) -> None:
        """Degraded delivery when the broker is unreachable: run locally
        subscribed handlers fire-and-forget, exactly like InProcessEventBus."""
        handlers = self._handlers.get(event.topic, [])
        if not handlers:
            logger.critical(
                "NatsBus: event %s (deal=%s, event_id=%s) DROPPED — broker "
                "unreachable and no local handlers in this process",
                event.topic.value, event.deal_id, event.event_id,
            )
            return
        for handler in handlers:
            task = asyncio.create_task(self._run_local(handler, event))
            self._local_tasks.add(task)
            task.add_done_callback(self._local_tasks.discard)

    @staticmethod
    async def _run_local(handler: EventHandler, event: DomainEvent) -> None:
        try:
            await handler(event)
        except Exception:
            logger.exception(
                "NatsBus: local-fallback handler %s failed for %s (deal=%s)",
                getattr(handler, "__name__", handler), event.topic.value, event.deal_id,
            )

    # ------------------------------------------------------------------ #
    # Consume                                                             #
    # ------------------------------------------------------------------ #

    async def _consume_loop(self, topic: EventTopic, psub) -> None:
        from nats.errors import TimeoutError as NatsTimeoutError

        while not self._closing:
            try:
                msgs = await psub.fetch(self._fetch_batch, timeout=self._fetch_timeout)
            except NatsTimeoutError:
                continue  # no messages — normal
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._closing:
                    return
                logger.warning(
                    "NatsBus: fetch failed for %s (%s) — retrying in %.0fs",
                    topic.value, exc, self._fetch_timeout,
                )
                await asyncio.sleep(self._fetch_timeout)
                continue
            for msg in msgs:
                await self._process(topic, msg)

    async def _process(self, topic: EventTopic, msg) -> None:
        """Parse → run handlers → ack / nak / term. Never raises."""
        try:
            event = DomainEvent.model_validate_json(msg.data)
        except Exception:
            logger.exception(
                "NatsBus: POISON message on %s — terminating (data=%r)",
                topic.value, msg.data[:500],
            )
            await self._safe(msg.term)
            return

        try:
            await self._run_handlers_with_heartbeat(topic, event, msg)
        except Exception as exc:
            attempt = self._delivery_attempt(msg)
            if attempt >= self._max_deliver:
                logger.critical(
                    "NatsBus: handler for %s FAILED on final attempt %d/%d "
                    "(deal=%s, event_id=%s): %s — terminating message",
                    topic.value, attempt, self._max_deliver,
                    event.deal_id, event.event_id, exc,
                )
                await self._safe(msg.term)
                await self._emit_dead_letter(topic, event, exc)
            else:
                delay = _nak_delay(attempt)
                logger.warning(
                    "NatsBus: handler for %s failed on attempt %d/%d "
                    "(deal=%s, event_id=%s): %s — redelivery in %.0fs",
                    topic.value, attempt, self._max_deliver,
                    event.deal_id, event.event_id, exc, delay,
                )
                await self._safe(lambda: msg.nak(delay=delay))
            return

        await self._safe(msg.ack)

    async def _run_handlers_with_heartbeat(self, topic: EventTopic, event: DomainEvent, msg) -> None:
        """Run every handler sequentially while a heartbeat task extends the
        ack deadline — long handlers (LLM calls) must not be redelivered
        mid-flight."""
        interval = max(self._ack_wait / 3.0, 1.0)

        async def _heartbeat() -> None:
            while True:
                await asyncio.sleep(interval)
                try:
                    await msg.in_progress()
                except Exception as exc:  # noqa: BLE001 — heartbeat is best-effort
                    logger.warning("NatsBus: in_progress heartbeat failed: %s", exc)

        hb = asyncio.create_task(_heartbeat())
        try:
            for handler in self._handlers.get(topic, []):
                await handler(event)
        finally:
            hb.cancel()

    async def _emit_dead_letter(self, topic: EventTopic, event: DomainEvent, exc: Exception) -> None:
        """Surface an exhausted message as a deal.exception so it reaches the
        Exception Resolver / UI instead of dying only in a log line."""
        if topic == EventTopic.DEAL_EXCEPTION:
            return  # never let deal.exception failures emit more deal.exceptions
        try:
            await self.publish(DomainEvent(
                topic=EventTopic.DEAL_EXCEPTION,
                deal_id=event.deal_id,
                payload={
                    "category": "CALLBACK_FAILED",
                    "source": "nats_bus.dead_letter",
                    "failed_topic": topic.value,
                    "failed_event_id": event.event_id,
                    "error": str(exc),
                    "action_required": "manual_retry_or_intervention",
                },
            ))
        except Exception:
            logger.exception("NatsBus: could not emit dead-letter deal.exception for %s", event.event_id)

    @staticmethod
    def _delivery_attempt(msg) -> int:
        try:
            return int(msg.metadata.num_delivered)
        except Exception:  # noqa: BLE001 — metadata parse must never break ack flow
            return 1

    @staticmethod
    async def _safe(op) -> None:
        """Run an ack/nak/term op; a broken connection here must not raise."""
        try:
            await op()
        except Exception as exc:  # noqa: BLE001
            logger.warning("NatsBus: ack/nak/term failed (message will redeliver): %s", exc)

    # ------------------------------------------------------------------ #
    # Shutdown                                                            #
    # ------------------------------------------------------------------ #

    async def close(self) -> None:
        self._closing = True
        for task in self._consumer_tasks:
            task.cancel()
        for task in self._consumer_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._consumer_tasks.clear()
        if self._nc is not None:
            try:
                await self._nc.drain()
            except Exception as exc:  # noqa: BLE001
                logger.warning("NatsBus: drain on close failed: %s", exc)
            self._nc = None
            self._js = None
        logger.info("NatsBus: closed")
