"""DGFT denied-entity list loaded from DGFT's document, not a stub (DEF-17)."""
from __future__ import annotations

import pytest

from shared.orchestration import denied_list as dl

CSV = """name,iec,address,order_ref,notified_on
M/s Shree Rice Mills Pvt. Ltd.,0400012345,"Karnal, Haryana",DEL-2025-17,2025-08-01
Bad Exports LLP,,Mumbai,DEL-2025-21,2025-09-12
"""


@pytest.fixture(autouse=True)
def _mem(monkeypatch):
    monkeypatch.setattr(dl, "_collection", lambda: None)
    dl.reset_for_tests()
    yield
    dl.reset_for_tests()


async def test_without_a_list_the_check_is_unchecked_not_clear():
    out = await dl.check("Any Mill", "0400099999")
    assert out["on_denied_list"] is False and out["unchecked"] is True and out["list_loaded"] is False


async def test_iec_match_is_certain_name_match_asks_a_person():
    meta = await dl.load(CSV, by="co@portless.in")
    assert meta["count"] == 2
    by_iec = await dl.check("Different Name", "04000-12345")
    assert by_iec["on_denied_list"] and by_iec["match"]["by"] == "iec" and by_iec["match"]["order_ref"] == "DEL-2025-17"
    by_name = await dl.check("BAD EXPORTS", None)
    assert by_name["on_denied_list"] and by_name["match"]["by"] == "name" and by_name["match"]["confirm"] is True
    clean = await dl.check("Good Mills Pvt Ltd", "0400000001")
    assert clean["on_denied_list"] is False and "unchecked" not in clean and clean["list_stale"] is False


async def test_bad_csv_is_refused():
    with pytest.raises(ValueError):
        await dl.load("foo,bar\n1,2\n", by="x")


async def test_the_connector_uses_the_list():
    from shared.connectors.regulatory import DgftConnector
    await dl.load(CSV, by="co@portless.in")
    out = await DgftConnector().book({"supplier_id": "S1", "iec": "0400012345", "entity_name": "x"})
    assert out["on_denied_list"] is True


async def test_compliance_reviews_without_a_list_and_blocks_on_an_iec_match(monkeypatch):
    from types import SimpleNamespace

    from shared.orchestration import compliance
    monkeypatch.setenv("COMPLIANCE_DGFT_LIST", "review")
    deal = SimpleNamespace(deal_id="D1", winning_supplier_id=None, parties=[
        SimpleNamespace(role="supplier", legal_name="Shree Rice Mills Pvt Ltd", iec="0400012345")])
    assert [f.code for f in await compliance.check_supplier_record(deal)] == ["dgft_list_not_loaded"]
    await dl.load(CSV, by="co@portless.in")
    (block,) = await compliance.check_supplier_record(deal)
    assert block.code == "supplier_dgft_denied" and block.severity == "block"
    deal.parties[0].iec = None
    (review,) = await compliance.check_supplier_record(deal)
    assert review.code == "supplier_dgft_name_match" and review.severity == "review"
    monkeypatch.setenv("COMPLIANCE_DGFT_LIST", "allow")
    dl.reset_for_tests()
    assert await compliance.check_supplier_record(deal) == []
