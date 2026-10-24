"""Per-action autonomy policy (shared/orchestration/autonomy_policy.py).

  • built-in defaults per action class
  • env override AUTONOMY_<ACTION>
  • runtime override (set_policy) beats env; None clears
  • deal profile only for classes that follow it
  • money ceiling clamps payout / tranche release
  • effective_policy reports source
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from shared.orchestration import autonomy_policy as ap


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ap.reset_for_tests()
    monkeypatch.setattr(ap, "_mongo_collection", lambda: None)
    for cls in ap.ACTION_CLASSES:
        monkeypatch.delenv(f"AUTONOMY_{cls.action}", raising=False)
    monkeypatch.delenv("AUTONOMY_MONEY_CEILING", raising=False)
    yield
    ap.reset_for_tests()


def _deal(level: str):
    return SimpleNamespace(autonomy_profile=SimpleNamespace(value=level))


class TestDefaults:
    def test_builtin_defaults(self):
        assert ap.resolve_level("SEND_NEGOTIATION_EMAIL") == "L0"
        assert ap.resolve_level("CONFIRM_NEGOTIATION_AGREEMENT") == "L0"
        assert ap.resolve_level("DISPATCH_RFQ") == "L2"
        assert ap.resolve_level("SEND_DEAL_NOTIFICATION") == "L2"
        assert ap.resolve_level("ISSUE_PO") == "L0"

    def test_unknown_action_is_l0(self):
        assert ap.resolve_level("SOMETHING_NEW") == "L0"

    def test_every_approval_action_has_a_class(self):
        from shared.domain.enums import ApprovalAction
        classes = {c.action for c in ap.ACTION_CLASSES}
        assert {a.value for a in ApprovalAction} == classes


class TestDealProfile:
    def test_po_follows_deal_profile(self):
        assert ap.resolve_level("ISSUE_PO", _deal("L2")) == "L2"
        assert ap.resolve_level("BOOK_FREIGHT", _deal("L3")) == "L3"

    def test_email_ignores_deal_profile(self):
        assert ap.resolve_level("SEND_NEGOTIATION_EMAIL", _deal("L3")) == "L0"

    def test_plain_string_profile(self):
        assert ap.resolve_level("FILE_CUSTOMS", SimpleNamespace(autonomy_profile="L1")) == "L1"


class TestOverrides:
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("AUTONOMY_SEND_NEGOTIATION_EMAIL", "l2")
        assert ap.resolve_level("SEND_NEGOTIATION_EMAIL") == "L2"

    def test_env_beats_deal_profile(self, monkeypatch):
        monkeypatch.setenv("AUTONOMY_ISSUE_PO", "L1")
        assert ap.resolve_level("ISSUE_PO", _deal("L3")) == "L1"

    @pytest.mark.asyncio
    async def test_runtime_override_beats_env_and_clears(self, monkeypatch):
        monkeypatch.setenv("AUTONOMY_DISPATCH_RFQ", "L0")
        await ap.set_policy({"DISPATCH_RFQ": "L3"})
        assert ap.resolve_level("DISPATCH_RFQ") == "L3"
        await ap.set_policy({"DISPATCH_RFQ": None})
        assert ap.resolve_level("DISPATCH_RFQ") == "L0"  # env applies again

    @pytest.mark.asyncio
    async def test_invalid_inputs_rejected(self):
        with pytest.raises(ValueError):
            await ap.set_policy({"NOT_AN_ACTION": "L2"})
        with pytest.raises(ValueError):
            await ap.set_policy({"DISPATCH_RFQ": "L9"})

    @pytest.mark.asyncio
    async def test_load_policy_without_mongo_is_noop(self):
        assert await ap.load_policy() == {}


class TestMoneyCeiling:
    def test_payout_clamped_to_default_ceiling(self):
        assert ap.resolve_level("RELEASE_SUPPLIER_PAYOUT", _deal("L3")) == "L1"
        assert ap.resolve_level("RELEASE_ESCROW_TRANCHE", _deal("L2")) == "L1"

    def test_ceiling_can_be_raised_explicitly(self, monkeypatch):
        monkeypatch.setenv("AUTONOMY_MONEY_CEILING", "L3")
        assert ap.resolve_level("RELEASE_SUPPLIER_PAYOUT", _deal("L3")) == "L3"

    @pytest.mark.asyncio
    async def test_override_still_clamped(self):
        await ap.set_policy({"RELEASE_SUPPLIER_PAYOUT": "L3"})
        assert ap.resolve_level("RELEASE_SUPPLIER_PAYOUT") == "L1"

    def test_non_money_actions_not_clamped(self, monkeypatch):
        monkeypatch.setenv("AUTONOMY_BOOK_FREIGHT", "L3")
        assert ap.resolve_level("BOOK_FREIGHT") == "L3"


class TestEffectivePolicy:
    @pytest.mark.asyncio
    async def test_reports_sources(self, monkeypatch):
        monkeypatch.setenv("AUTONOMY_BOOK_FREIGHT", "L2")
        await ap.set_policy({"DISPATCH_RFQ": "L0", "RELEASE_SUPPLIER_PAYOUT": "L3"})
        view = ap.effective_policy()
        assert view["DISPATCH_RFQ"]["effective"] == "L0"
        assert view["DISPATCH_RFQ"]["source"] == "override"
        assert view["BOOK_FREIGHT"]["effective"] == "L2"
        assert view["BOOK_FREIGHT"]["source"] == "env"
        assert view["SEND_NEGOTIATION_EMAIL"]["source"] == "default"
        assert view["RELEASE_SUPPLIER_PAYOUT"]["effective"] == "L1"
        assert view["RELEASE_SUPPLIER_PAYOUT"]["source"] == "money_ceiling"
        assert view["RELEASE_SUPPLIER_PAYOUT"]["money_moving"] is True
