"""Money modules to 100 % (part 4): the remaining trade-compliance branches."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mongomock_motor import AsyncMongoMockClient

from shared.orchestration import compliance as c


def test_collection_with_a_mongo_uri_and_a_failing_client():
    fake_settings = SimpleNamespace(mongo_uri="mongodb://x", mongo_db_name="portless")
    client = AsyncMongoMockClient()
    with patch("shared.config.settings", new=fake_settings), patch("shared.mongo.get_client", return_value=client):
        assert c._collection("compliance_checks") is not None
    with patch("shared.config.settings", new=fake_settings), \
            patch("shared.mongo.get_client", side_effect=RuntimeError("bad uri")):
        assert c._collection("compliance_checks") is None


async def test_hs_policy_not_in_mongo_falls_back_to_memory():
    col = AsyncMongoMockClient()["t"]["compliance_hs_policy"]
    with patch.object(c, "_collection", return_value=col):
        assert await c.get_hs_policy("999999") is None


def test_exact_product_with_a_broken_catalogue():
    with patch("shared.catalogue.line_for", side_effect=RuntimeError("x")):
        assert c.exact_product("070310") == ("070310", None)


async def test_dgft_findings(monkeypatch):
    assert await c._dgft_list_findings(SimpleNamespace(parties=[])) == []
    deal = SimpleNamespace(parties=[SimpleNamespace(role="supplier", legal_name="Acme", iec="0123456789")])
    monkeypatch.setenv("COMPLIANCE_DGFT_LIST", "review")
    with patch("shared.orchestration.denied_list.check", new=AsyncMock(return_value={"list_stale": True, "list_age_days": 90})):
        assert (await c._dgft_list_findings(deal))[0].code == "dgft_list_stale"
    with patch("shared.orchestration.denied_list.check", new=AsyncMock(return_value={})):
        assert await c._dgft_list_findings(deal) == []
    with patch.object(c, "_dgft_list_findings", new=AsyncMock(side_effect=RuntimeError("list unreadable"))):
        assert await c.check_supplier_record(SimpleNamespace(deal_id="D", winning_supplier_id=None)) == []


async def test_trade_check_adds_destination_and_related_party_findings():
    deal = SimpleNamespace(deal_id="D", hs_code="999999", market="IR", parties=[])
    rel = c.Finding(code="related_parties", severity="review", message="same domain")
    with patch.object(c, "check_screening", return_value=[]), \
            patch.object(c, "check_related_parties", return_value=rel), \
            patch.object(c, "check_supplier_record", new=AsyncMock(return_value=[])), \
            patch.object(c, "check_prices", new=AsyncMock(return_value=[])):
        result = await c.check_trade(deal, stage="before_pi", persist=False)
    codes = {f.code for f in result.findings}
    assert "destination_embargoed" in codes and "related_parties" in codes
