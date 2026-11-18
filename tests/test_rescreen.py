"""Re-screening of verified counterparties linked to open prospects / deals once their check
is older than TRADE_MIN_VALIDATION_MAX_AGE_DAYS (services/trade_min/rescreen.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.deals.schemas import BuyerCandidate, DealRecord, DealStatus
from services.trade_min import directory as dr
from services.trade_min import relationships as rl
from services.trade_min import rescreen
from services.trade_min import validation as val
from services.trade_min.schemas import CounterpartyRecord, CounterpartyValidation

NOW = datetime.now(UTC)


def _deal(deal_id, status, website):
    host = website.split("//")[-1]
    return DealRecord(deal_id=deal_id, operator_id="op", org_id="o", hs_code="090422", market="AE", status=status,
                      created_at=NOW, updated_at=NOW,
                      buyer_candidates=[BuyerCandidate(candidate_id=host, legal_name=host, country="AE",
                                                       email=f"desk@{host}", website=website, status="contacted")])


async def _add(name, website, *, status="verified", age_days=40) -> CounterpartyRecord:
    store = await dr.get_directory()
    rec = CounterpartyRecord(
        id=dr.record_key("buyer", name, website), role="buyer", name=name, website=website, country="AE",
        email=f"desk@{website.split('//')[-1]}", source="apify_company_search", hs_codes=["090422"], markets=["AE"],
        first_seen_at=NOW, last_seen_at=NOW,
        validation=CounterpartyValidation(status=status, sanctions="clear", email="ok",
                                          checked_at=NOW - timedelta(days=age_days)))
    await store.upsert(rec)
    await store.set_validation(rec)
    return rec


@pytest.fixture
def world(monkeypatch):
    dr.reset_directory()
    state = SimpleNamespace(deals=[], verdict="clear", calls=[], raised=[])
    monkeypatch.setattr(rl, "_deals", AsyncMock(side_effect=lambda: state.deals))
    monkeypatch.setattr(rescreen, "_screening_available", lambda: True)
    monkeypatch.setenv("TRADE_MIN_RESCREEN_MAX_PER_RUN", "20")

    async def fake_validate(rec):
        state.calls.append(rec.name)
        v = CounterpartyValidation(sanctions=state.verdict, email="ok",
                                   sanctions_reason="OFAC SDN match" if state.verdict == "block" else None)
        rec.validation = val.decide(v.model_copy(update={"checked_at": NOW}), role="buyer", country="AE")
        return rec

    monkeypatch.setattr(val, "validate_record", fake_validate)

    async def fake_raise(deal_id, rec, reasons):
        state.raised.append((deal_id, rec.name, reasons))

    monkeypatch.setattr(rescreen, "_raise", fake_raise)
    yield state
    dr.reset_directory()


async def test_only_old_verified_companies_of_open_deals_are_due(world):
    await _add("Gulf Foods", "https://gulf.ae")                         # old, open prospect → due
    await _add("Fresh Co", "https://fresh.ae", age_days=2)             # still fresh
    await _add("Closed Co", "https://closed.ae")                        # only in a closed deal
    await _add("Unlinked Co", "https://unlinked.ae")                    # in no deal
    await _add("Unverified Co", "https://unv.ae", status="unverified")  # not verified
    world.deals = [_deal("D1", DealStatus.MATCHING, "https://gulf.ae"),
                   _deal("D2", DealStatus.MATCHING, "https://fresh.ae"),
                   _deal("D3", DealStatus.CANCELLED, "https://closed.ae"),
                   _deal("D4", DealStatus.MATCHING, "https://unv.ae")]
    items = await rescreen.due()
    assert [(r.name, linked) for r, linked in items] == [("Gulf Foods", {"D1"})]


async def test_a_clear_rescreen_refreshes_the_date(world):
    rec = await _add("Gulf Foods", "https://gulf.ae")
    world.deals = [_deal("D1", DealStatus.MATCHING, "https://gulf.ae")]
    summary = await rescreen.run()
    assert summary["checked"] == 1 and summary["still_verified"] == 1 and world.raised == []
    stored = await (await dr.get_directory()).get(rec.id)
    assert stored.validation.checked_at >= NOW - timedelta(seconds=5)
    assert await rescreen.due() == []


async def test_a_company_that_now_fails_raises_on_each_open_deal(world):
    await _add("Gulf Foods", "https://gulf.ae")
    world.deals = [_deal("D1", DealStatus.MATCHING, "https://gulf.ae"), _deal("D2", DealStatus.MATCHING, "https://gulf.ae")]
    world.verdict = "block"
    summary = await rescreen.run()
    assert summary["now_failed"] == 1 and summary["failed_ids"]
    assert sorted(d for d, _, _ in world.raised) == ["D1", "D2"]
    assert "OFAC SDN match" in world.raised[0][2][0]


async def test_review_goes_to_the_queue(world):
    await _add("Gulf Foods", "https://gulf.ae")
    world.deals = [_deal("D1", DealStatus.MATCHING, "https://gulf.ae")]
    world.verdict = "review"
    assert (await rescreen.run())["now_review"] == 1
    from services.trade_min import screening_review
    assert [r.name for r in await screening_review.queue()] == ["Gulf Foods"]


async def test_a_check_that_could_not_run_keeps_the_old_result(world):
    rec = await _add("Gulf Foods", "https://gulf.ae")
    world.deals = [_deal("D1", DealStatus.MATCHING, "https://gulf.ae")]
    world.verdict = "unchecked"
    assert (await rescreen.run())["checked"] == 0
    assert (await (await dr.get_directory()).get(rec.id)).validation.status == "verified"


async def test_nothing_runs_without_a_token_or_when_switched_off(world, monkeypatch):
    await _add("Gulf Foods", "https://gulf.ae")
    world.deals = [_deal("D1", DealStatus.MATCHING, "https://gulf.ae")]
    monkeypatch.setattr(rescreen, "_screening_available", lambda: False)
    assert "no Apify token" in (await rescreen.run())["skipped"]
    monkeypatch.setattr(rescreen.settings, "trade_min_validation_enabled", False)
    assert "switched off" in (await rescreen.run())["skipped"]
    assert world.calls == []


async def test_the_run_is_capped(world, monkeypatch):
    for i in range(4):
        await _add(f"Buyer {i}", f"https://b{i}.ae", age_days=40 + i)
    world.deals = [_deal(f"D{i}", DealStatus.MATCHING, f"https://b{i}.ae") for i in range(4)]
    monkeypatch.setenv("TRADE_MIN_RESCREEN_MAX_PER_RUN", "2")
    summary = await rescreen.run()
    assert summary["due"] == 4 and summary["checked"] == 2
    assert world.calls == ["Buyer 3", "Buyer 2"]  # oldest result first
