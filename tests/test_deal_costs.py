"""What each deal costs to run: LLM, Apify, operator decisions (review board B-24)."""
from __future__ import annotations

import asyncio

import pytest
from mongomock_motor import AsyncMongoMockClient

from shared import deal_costs as dc
from shared.domain.events import DomainEvent, EventTopic


@pytest.fixture(autouse=True)
def _memory():
    dc.set_collection_for_tests(None)
    yield
    dc.set_collection_for_tests(None)


async def test_scope_books_to_the_deal_and_nothing_outside_it():
    dc.add(llm_calls=1)  # no deal: ignored
    with dc.deal_scope("D1"):
        dc.add(llm_calls=1, llm_tokens=1200, llm_usd=dc.llm_usd(1200))
        with dc.deal_scope(None):  # an inner call without a deal keeps the outer one
            dc.add(apify_runs=1, apify_usd=0.35)
    assert dc.current() is None
    s = await dc.summary("D1")
    assert s["llm_calls"] == 1 and s["llm_tokens"] == 1200 and s["apify_runs"] == 1
    assert s["tool_usd"] == pytest.approx(round(dc.llm_usd(1200) + 0.35, 2))
    assert (await dc.summary("D2"))["llm_calls"] == 0


async def test_event_handlers_run_inside_the_events_deal():
    from shared.bus.publisher import InProcessEventBus
    seen = []

    async def handler(event):
        seen.append(dc.current())
        dc.add(apify_runs=1)
    await InProcessEventBus._run_handler(handler, DomainEvent(topic=EventTopic.DEAL_CREATED, deal_id="D7", payload={}))
    assert seen == ["D7"] and (await dc.summary("D7"))["apify_runs"] == 1


async def test_apify_run_and_llm_call_are_counted(monkeypatch):
    from shared.connectors import apify
    with dc.deal_scope("D3"):
        apify._meter_run({"usageTotalUsd": 0.42})
    assert (await dc.summary("D3"))["apify_usd"] == pytest.approx(0.42)


async def test_mongo_counters_increment():
    col = AsyncMongoMockClient()["db"]["deal_costs"]
    dc.set_collection_for_tests(col)
    dc.add("D5", decisions=1, decision_wait_hours=2.5)
    dc.add("D5", decisions=1, decision_wait_hours=0.5)
    await asyncio.gather(*list(dc._tasks))
    s = await dc.summary("D5")
    assert s["decisions"] == 2 and s["decision_wait_hours"] == pytest.approx(3.0)


def test_add_without_a_loop_keeps_memory():
    dc.add("D6", llm_calls=2)
    assert dc._memory["D6"]["llm_calls"] == 2
