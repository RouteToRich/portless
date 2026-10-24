"""Event-bus selection + lifecycle (called from every app lifespan).

Resolves which ``EventPublisher`` backs ``get_publisher()`` for this process:

  EVENT_BUS_BACKEND=auto        (default) NATS when credentials are found,
                                otherwise the in-process bus
  EVENT_BUS_BACKEND=nats        require NATS; falls back (loudly) if it
                                cannot connect
  EVENT_BUS_BACKEND=inprocess   in-process async bus only (single process)
  EVENT_BUS_BACKEND=noop        events vanish (unit tests / bare mode)

Credentials resolution: ``NATS_CREDS_FILE`` when set; otherwise the first
``*.creds`` file in the repository root (so dropping a Synadia credentials file in
place is enough — same graceful-degradation philosophy as the connectors).

``init_event_bus()`` NEVER raises: any NATS failure logs loudly and degrades
to the in-process bus so the app always boots.

Roles
-----
  consume=True   (combined monolith ``app.py``) — publish + durable consumers
                 running the pipeline continuations. Consumers live ONLY here:
                 continuations call peer services via in-process imports, which
                 is correct solely in the monolith.
  consume=False  (standalone service mode) — publish-only. Events persist in
                 JetStream and are replayed to the monolith's durable
                 consumers whenever it next runs.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .publisher import EventPublisher, InProcessEventBus, NoopPublisher, set_publisher

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_lock = asyncio.Lock()
_active_publisher: EventPublisher | None = None
_active_nats_bus = None  # NatsJetStreamBus | None — kept for close()


def _resolve_creds_file() -> str | None:
    explicit = os.getenv("NATS_CREDS_FILE", "").strip()
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = _REPO_ROOT / path
        if path.is_file():
            return str(path)
        logger.error("EventBus: NATS_CREDS_FILE=%s does not exist", explicit)
        return None
    found = sorted(_REPO_ROOT.glob("*.creds"))
    if found:
        logger.info("EventBus: discovered NATS credentials file %s", found[0].name)
        return str(found[0])
    return None


def _resolve_backend() -> tuple[str, str | None]:
    """→ ("nats"|"inprocess"|"noop", creds_file_or_None)."""
    backend = os.getenv("EVENT_BUS_BACKEND", "auto").strip().lower()
    if backend == "noop":
        return "noop", None
    if backend == "inprocess":
        return "inprocess", None
    creds = _resolve_creds_file()
    if backend == "nats":
        if creds is None:
            logger.error(
                "EventBus: EVENT_BUS_BACKEND=nats but no credentials file found "
                "(set NATS_CREDS_FILE or drop a *.creds in the repository root) — "
                "falling back to the in-process bus"
            )
            return "inprocess", None
        return "nats", creds
    # auto
    if backend != "auto":
        logger.warning("EventBus: unknown EVENT_BUS_BACKEND=%r — treating as auto", backend)
    return ("nats", creds) if creds else ("inprocess", None)


def _register_continuations(bus) -> None:
    # Lazy import: continuations pull in service modules.
    from shared.orchestration.continuations import register_continuations

    register_continuations(bus)


async def init_event_bus(*, consume: bool = True) -> EventPublisher:
    """Install the process-wide publisher. Idempotent; never raises."""
    global _active_publisher, _active_nats_bus

    async with _lock:
        if _active_publisher is not None:
            return _active_publisher

        try:
            backend, creds = _resolve_backend()
        except Exception:  # noqa: BLE001 — resolution must never block boot
            logger.exception("EventBus: backend resolution failed — using in-process bus")
            backend, creds = "inprocess", None

        if backend == "noop":
            publisher: EventPublisher = NoopPublisher()
            logger.info("EventBus: NoopPublisher active (EVENT_BUS_BACKEND=noop)")

        elif backend == "nats":
            publisher = await _init_nats(creds, consume)

        else:
            publisher = _init_inprocess(consume)

        set_publisher(publisher)
        _active_publisher = publisher
        return publisher


def _init_inprocess(consume: bool) -> EventPublisher:
    bus = InProcessEventBus()
    if consume:
        _register_continuations(bus)
    logger.info("EventBus: InProcessEventBus active (consume=%s)", consume)
    return bus


async def _init_nats(creds: str | None, consume: bool) -> EventPublisher:
    global _active_nats_bus
    from .nats_bus import NatsJetStreamBus

    bus = NatsJetStreamBus(
        url=os.getenv("NATS_URL", "tls://connect.ngs.global:4222"),
        creds_file=creds,
        stream_name=os.getenv("NATS_STREAM_NAME", "PORTLESS_EVENTS"),
        subject_prefix=os.getenv("NATS_SUBJECT_PREFIX", "portless"),
        ack_wait=float(os.getenv("NATS_ACK_WAIT_SECONDS", "120")),
        max_deliver=int(os.getenv("NATS_MAX_DELIVER", "5")),
        max_bytes=int(os.getenv("NATS_STREAM_MAX_BYTES", str(128 * 1024 * 1024))),
    )
    if consume:
        _register_continuations(bus)
    try:
        await bus.connect()
        if consume:
            await bus.start_consumers()
    except Exception:
        logger.exception(
            "EventBus: NATS init FAILED (url=%s) — falling back to the "
            "in-process bus; durable delivery is OFF until this is fixed",
            os.getenv("NATS_URL", "tls://connect.ngs.global:4222"),
        )
        try:
            await bus.close()
        except Exception:  # noqa: BLE001
            pass
        return _init_inprocess(consume)

    _active_nats_bus = bus
    logger.info("EventBus: NatsJetStreamBus active (consume=%s)", consume)
    return bus


async def close_event_bus() -> None:
    """Tear down the active bus and reset to Noop. Never raises."""
    global _active_publisher, _active_nats_bus

    async with _lock:
        if _active_nats_bus is not None:
            try:
                await _active_nats_bus.close()
            except Exception:  # noqa: BLE001
                logger.exception("EventBus: error closing NATS bus")
            _active_nats_bus = None
        _active_publisher = None
        set_publisher(NoopPublisher())
