"""Tests for the durable event bus (2026-07-17):

  NatsJetStreamBus — publish fallback, ack/nak/term consumer semantics,
                     poison messages, dead-letter emission + loop guard
  factory          — backend resolution, NATS-failure fallback, idempotency
  on_deal_exception — DEAL_EXCEPTION → Exception Resolver routing (loop-safe)

All NATS interactions use fakes — no broker required.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.bus import factory
from shared.bus.nats_bus import NatsJetStreamBus, _nak_delay
from shared.bus.publisher import InProcessEventBus, NoopPublisher, get_publisher
from shared.domain.events import DomainEvent, EventTopic
from shared.orchestration.continuations import on_deal_exception


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeMsg:
    def __init__(self, data: bytes, num_delivered: int = 1):
        self.data = data
        self.metadata = SimpleNamespace(num_delivered=num_delivered)
        self.ack = AsyncMock()
        self.nak = AsyncMock()
        self.term = AsyncMock()
        self.in_progress = AsyncMock()


def _bus(**overrides) -> NatsJetStreamBus:
    defaults = dict(url="nats://fake:4222", creds_file=None, max_deliver=3, ack_wait=30.0)
    defaults.update(overrides)
    return NatsJetStreamBus(**defaults)


def _event(topic: EventTopic = EventTopic.NEGOTIATION_AGREED, **payload) -> DomainEvent:
    return DomainEvent(topic=topic, deal_id="DEAL-1", payload=payload)


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------

class TestNatsPublish:
    @pytest.mark.asyncio
    async def test_publish_uses_subject_and_msg_id_header(self):
        bus = _bus()
        bus._js = SimpleNamespace(publish=AsyncMock())
        event = _event(agreed_price=1200.0)
        await bus.publish(event)
        args, kwargs = bus._js.publish.await_args
        assert args[0] == "portless.negotiation.agreed"
        assert kwargs["headers"] == {"Nats-Msg-Id": event.event_id}
        assert b"1200" in args[1]

    @pytest.mark.asyncio
    async def test_publish_failure_falls_back_to_local_handlers(self):
        bus = _bus()
        bus._js = SimpleNamespace(publish=AsyncMock(side_effect=ConnectionError("down")))
        seen: list[DomainEvent] = []

        async def handler(event: DomainEvent) -> None:
            seen.append(event)

        bus.subscribe(EventTopic.NEGOTIATION_AGREED, handler)
        await bus.publish(_event())  # must not raise
        await asyncio.sleep(0.01)
        assert len(seen) == 1 and seen[0].deal_id == "DEAL-1"

    @pytest.mark.asyncio
    async def test_publish_never_raises_when_disconnected_and_no_handlers(self):
        bus = _bus()  # _js is None, no subscribers
        await bus.publish(_event())  # dropped with a critical log — no raise

    @pytest.mark.asyncio
    async def test_local_fallback_handler_exception_is_isolated(self):
        bus = _bus()
        bus._js = None
        seen: list[str] = []

        async def bad(event: DomainEvent) -> None:
            raise RuntimeError("boom")

        async def good(event: DomainEvent) -> None:
            seen.append("ok")

        bus.subscribe(EventTopic.QC_PASSED, bad)
        bus.subscribe(EventTopic.QC_PASSED, good)
        await bus.publish(_event(EventTopic.QC_PASSED))
        await asyncio.sleep(0.01)
        assert seen == ["ok"]


# ---------------------------------------------------------------------------
# Consume — ack / nak / term
# ---------------------------------------------------------------------------

class TestNatsConsume:
    @pytest.mark.asyncio
    async def test_success_acks(self):
        bus = _bus()
        handled: list[DomainEvent] = []

        async def handler(event: DomainEvent) -> None:
            handled.append(event)

        bus.subscribe(EventTopic.NEGOTIATION_AGREED, handler)
        msg = _FakeMsg(_event().model_dump_json().encode())
        await bus._process(EventTopic.NEGOTIATION_AGREED, msg)
        assert len(handled) == 1
        msg.ack.assert_awaited_once()
        msg.nak.assert_not_awaited()
        msg.term.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handler_failure_naks_with_backoff(self):
        bus = _bus(max_deliver=3)
        bus.subscribe(EventTopic.NEGOTIATION_AGREED, AsyncMock(side_effect=RuntimeError("boom")))
        msg = _FakeMsg(_event().model_dump_json().encode(), num_delivered=1)
        await bus._process(EventTopic.NEGOTIATION_AGREED, msg)
        msg.nak.assert_awaited_once_with(delay=_nak_delay(1))
        msg.ack.assert_not_awaited()
        msg.term.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_final_attempt_terms_and_dead_letters(self):
        bus = _bus(max_deliver=3)
        bus.subscribe(EventTopic.NEGOTIATION_AGREED, AsyncMock(side_effect=RuntimeError("boom")))
        bus.publish = AsyncMock()  # capture the dead-letter emit
        msg = _FakeMsg(_event().model_dump_json().encode(), num_delivered=3)
        await bus._process(EventTopic.NEGOTIATION_AGREED, msg)
        msg.term.assert_awaited_once()
        msg.nak.assert_not_awaited()
        dead = bus.publish.await_args.args[0]
        assert dead.topic == EventTopic.DEAL_EXCEPTION
        assert dead.payload["failed_topic"] == "negotiation.agreed"
        assert dead.payload["category"] == "CALLBACK_FAILED"

    @pytest.mark.asyncio
    async def test_deal_exception_failure_never_dead_letters(self):
        """A failing deal.exception handler must not emit more deal.exceptions."""
        bus = _bus(max_deliver=3)
        bus.subscribe(EventTopic.DEAL_EXCEPTION, AsyncMock(side_effect=RuntimeError("boom")))
        bus.publish = AsyncMock()
        msg = _FakeMsg(_event(EventTopic.DEAL_EXCEPTION).model_dump_json().encode(), num_delivered=3)
        await bus._process(EventTopic.DEAL_EXCEPTION, msg)
        msg.term.assert_awaited_once()
        bus.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_poison_message_is_termed_without_handler_call(self):
        bus = _bus()
        handler = AsyncMock()
        bus.subscribe(EventTopic.NEGOTIATION_AGREED, handler)
        msg = _FakeMsg(b"not json at all")
        await bus._process(EventTopic.NEGOTIATION_AGREED, msg)
        msg.term.assert_awaited_once()
        handler.assert_not_awaited()
        msg.ack.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ack_failure_is_swallowed(self):
        bus = _bus()
        bus.subscribe(EventTopic.NEGOTIATION_AGREED, AsyncMock())
        msg = _FakeMsg(_event().model_dump_json().encode())
        msg.ack = AsyncMock(side_effect=ConnectionError("gone"))
        await bus._process(EventTopic.NEGOTIATION_AGREED, msg)  # no raise

    @pytest.mark.asyncio
    async def test_missing_delivery_metadata_defaults_to_first_attempt(self):
        bus = _bus(max_deliver=3)
        bus.subscribe(EventTopic.NEGOTIATION_AGREED, AsyncMock(side_effect=RuntimeError("x")))
        msg = _FakeMsg(_event().model_dump_json().encode())
        msg.metadata = None  # metadata parse failure path
        await bus._process(EventTopic.NEGOTIATION_AGREED, msg)
        msg.nak.assert_awaited_once()  # treated as attempt 1, not termed


# ---------------------------------------------------------------------------
# Factory — backend selection + fallback
# ---------------------------------------------------------------------------

@pytest.fixture
async def clean_bus(monkeypatch, tmp_path):
    """Isolate the factory singleton and point creds discovery at an empty dir."""
    await factory.close_event_bus()
    monkeypatch.setattr(factory, "_BACKEND_DIR", tmp_path)
    monkeypatch.delenv("NATS_CREDS_FILE", raising=False)
    yield tmp_path
    await factory.close_event_bus()


class TestFactory:
    @pytest.mark.asyncio
    async def test_noop_backend(self, clean_bus, monkeypatch):
        monkeypatch.setenv("EVENT_BUS_BACKEND", "noop")
        publisher = await factory.init_event_bus(consume=False)
        assert isinstance(publisher, NoopPublisher)
        assert get_publisher() is publisher

    @pytest.mark.asyncio
    async def test_auto_without_creds_is_inprocess(self, clean_bus, monkeypatch):
        monkeypatch.setenv("EVENT_BUS_BACKEND", "auto")
        publisher = await factory.init_event_bus(consume=False)
        assert isinstance(publisher, InProcessEventBus)

    @pytest.mark.asyncio
    async def test_nats_without_creds_falls_back(self, clean_bus, monkeypatch):
        monkeypatch.setenv("EVENT_BUS_BACKEND", "nats")
        publisher = await factory.init_event_bus(consume=False)
        assert isinstance(publisher, InProcessEventBus)

    @pytest.mark.asyncio
    async def test_nats_connect_failure_falls_back(self, clean_bus, monkeypatch):
        (clean_bus / "test.creds").write_text("-----BEGIN NATS USER JWT-----\nfake\n")
        monkeypatch.setenv("EVENT_BUS_BACKEND", "nats")
        with patch.object(NatsJetStreamBus, "connect",
                          AsyncMock(side_effect=ConnectionError("unreachable"))):
            publisher = await factory.init_event_bus(consume=False)
        assert isinstance(publisher, InProcessEventBus)

    @pytest.mark.asyncio
    async def test_auto_with_creds_selects_nats(self, clean_bus, monkeypatch):
        (clean_bus / "test.creds").write_text("fake")
        monkeypatch.setenv("EVENT_BUS_BACKEND", "auto")
        with (
            patch.object(NatsJetStreamBus, "connect", AsyncMock()),
            patch.object(NatsJetStreamBus, "start_consumers", AsyncMock()) as start,
        ):
            publisher = await factory.init_event_bus(consume=False)
        assert isinstance(publisher, NatsJetStreamBus)
        start.assert_not_awaited()  # consume=False → publish-only

    @pytest.mark.asyncio
    async def test_consume_true_registers_continuations_and_starts_consumers(
        self, clean_bus, monkeypatch,
    ):
        (clean_bus / "test.creds").write_text("fake")
        monkeypatch.setenv("EVENT_BUS_BACKEND", "nats")
        with (
            patch.object(NatsJetStreamBus, "connect", AsyncMock()),
            patch.object(NatsJetStreamBus, "start_consumers", AsyncMock()) as start,
        ):
            publisher = await factory.init_event_bus(consume=True)
        assert isinstance(publisher, NatsJetStreamBus)
        start.assert_awaited_once()
        assert EventTopic.NEGOTIATION_AGREED in publisher._handlers
        assert EventTopic.DEAL_EXCEPTION in publisher._handlers

    @pytest.mark.asyncio
    async def test_init_is_idempotent(self, clean_bus, monkeypatch):
        monkeypatch.setenv("EVENT_BUS_BACKEND", "inprocess")
        first = await factory.init_event_bus(consume=False)
        second = await factory.init_event_bus(consume=False)
        assert first is second

    @pytest.mark.asyncio
    async def test_explicit_creds_path_missing_falls_back(self, clean_bus, monkeypatch):
        monkeypatch.setenv("EVENT_BUS_BACKEND", "nats")
        monkeypatch.setenv("NATS_CREDS_FILE", str(clean_bus / "nope.creds"))
        publisher = await factory.init_event_bus(consume=False)
        assert isinstance(publisher, InProcessEventBus)


# ---------------------------------------------------------------------------
# DEAL_EXCEPTION → Exception Resolver continuation
# ---------------------------------------------------------------------------

class TestDealExceptionContinuation:
    @pytest.fixture(autouse=True)
    def _continuations_enabled(self, monkeypatch):
        """Exercises on_deal_exception directly — must not inherit whatever
        EVENT_CONTINUATION_ENABLED happens to be set to in the environment
        (e.g. backend/.env sets it to "false" to avoid racing auto_flow.py in
        dev). The one test below that verifies the disabled path sets it to
        "false" itself, overriding this default."""
        monkeypatch.setenv("EVENT_CONTINUATION_ENABLED", "true")

    @pytest.mark.asyncio
    async def test_routes_to_resolver_with_mapped_fields(self):
        result = SimpleNamespace(
            exception_id="EXC-1", status=SimpleNamespace(value="ESCALATED"),
        )
        raise_exc = AsyncMock(return_value=result)
        with patch("services.exception_resolver.service.raise_exception", new=raise_exc):
            await on_deal_exception(DomainEvent(
                topic=EventTopic.DEAL_EXCEPTION, deal_id="DEAL-1",
                payload={
                    "category": "CALLBACK_FAILED",
                    "stage": "PO_ISSUED→QC_BOOKED",
                    "source": "shield_link",
                    "error": "connector timeout",
                },
            ))
        raise_exc.assert_awaited_once()
        req = raise_exc.await_args.args[0]
        assert req.deal_id == "DEAL-1"
        assert req.source_event == "PO_ISSUED→QC_BOOKED"
        assert req.source_service == "shield_link"
        assert req.detail == "connector timeout"
        assert req.context["category"] == "CALLBACK_FAILED"

    @pytest.mark.asyncio
    async def test_skips_resolver_lifecycle_echoes(self):
        """Events the resolver itself emits carry exception_id — never re-raise."""
        raise_exc = AsyncMock()
        with patch("services.exception_resolver.service.raise_exception", new=raise_exc):
            await on_deal_exception(DomainEvent(
                topic=EventTopic.DEAL_EXCEPTION, deal_id="DEAL-1",
                payload={"exception_id": "EXC-1", "status": "OPEN"},
            ))
        raise_exc.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_without_deal_id(self):
        raise_exc = AsyncMock()
        with patch("services.exception_resolver.service.raise_exception", new=raise_exc):
            await on_deal_exception(DomainEvent(
                topic=EventTopic.DEAL_EXCEPTION, payload={"error": "x"},
            ))
        raise_exc.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolver_failure_is_contained(self):
        """A resolver crash must neither raise nor publish more events."""
        emit = AsyncMock()
        with (
            patch("services.exception_resolver.service.raise_exception",
                  new=AsyncMock(side_effect=RuntimeError("resolver down"))),
            patch("shared.bus.publisher._publisher",
                  new=SimpleNamespace(publish=emit)),
        ):
            await on_deal_exception(DomainEvent(
                topic=EventTopic.DEAL_EXCEPTION, deal_id="DEAL-1",
                payload={"error": "x"},
            ))
        emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_respects_disabled_flag(self, monkeypatch):
        monkeypatch.setenv("EVENT_CONTINUATION_ENABLED", "false")
        raise_exc = AsyncMock()
        with patch("services.exception_resolver.service.raise_exception", new=raise_exc):
            await on_deal_exception(DomainEvent(
                topic=EventTopic.DEAL_EXCEPTION, deal_id="DEAL-1",
                payload={"error": "x"},
            ))
        raise_exc.assert_not_awaited()
