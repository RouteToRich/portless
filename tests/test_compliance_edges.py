"""Boundaries and failure paths of shared/orchestration/compliance.py.

tests/test_compliance.py walks the main flow; this file pins each individual
check: every seed entry, every country list member, inclusive / exclusive
limits, accepted findings, hold bookkeeping and storage failures.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared import pricebook
from shared.orchestration import compliance as cp

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in (
        "COMPLIANCE_ENABLED", "COMPLIANCE_LOADING_CHECK_REQUIRED", "COMPLIANCE_UNCHECKED_SCREENING",
        "COMPLIANCE_BLOCKED_COUNTRIES", "COMPLIANCE_REVIEW_COUNTRIES", "COMPLIANCE_PRICE_DEVIATION_PCT",
        "COMPLIANCE_MAX_SPREAD_RATIO", "COMPLIANCE_QTY_TOLERANCE_PCT", "COMPLIANCE_MIN_COMPANY_AGE_YEARS",
    ):
        monkeypatch.delenv(name, raising=False)
    cp.reset_for_tests()
    pricebook.reset_for_tests()
    monkeypatch.setattr(pricebook, "public_priors", AsyncMock(return_value={}))
    monkeypatch.setattr("services.sourcing.service.list_quotes", AsyncMock(return_value=[]))
    yield
    cp.reset_for_tests()
    pricebook.reset_for_tests()


def _party(role="buyer", **kw):
    base = {
        "role": role,
        "legal_name": "Glaconchemie GmbH" if role == "buyer" else "Swadesh India Chemical Pvt Ltd",
        "country": "DE" if role == "buyer" else "IN",
        "email": "buy@glacon.de" if role == "buyer" else "sales@swadesh.in",
        "website": None, "phone": None, "address": None, "sanctions_status": "clear",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _deal(buyer=None, supplier=None, **kw):
    base = {
        "deal_id": "DEAL-E", "market": "DE", "hs_code": "152000", "winning_supplier_id": "SUP-1",
        "parties": [buyer or _party("buyer"), supplier or _party("supplier")],
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _codes_of(findings) -> list[str]:
    return [f.code for f in findings]


def _loading(**kw):
    base = {"container_numbers": ["MSCU1234567"], "seal_numbers": ["SEAL1"], "loaded_quantity_mt": 20.0,
                "hs_code": "1520", "photos": ["p.jpg"]}
    base.update(kw)
    return cp.LoadingRecord(**base)


class _Col:
    """Minimal async Mongo collection; ``fail`` makes every call raise."""

    def __init__(self, doc=None, fail=False):
        self.doc, self.fail, self.replaced = doc, fail, []

    async def find_one(self, *_a, **_k):
        if self.fail:
            raise RuntimeError("mongo down")
        return self.doc

    async def replace_one(self, flt, doc, upsert=False):
        if self.fail:
            raise RuntimeError("mongo down")
        self.replaced.append((flt, doc, upsert))

    def find(self, *_a, **_k):
        if self.fail:
            raise RuntimeError("mongo down")

        async def _gen():
            if self.doc:
                yield self.doc

        return _gen()


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

class TestConfig:
    @pytest.mark.parametrize(("raw", "expected"), [
        (None, True), ("", True), ("   ", True), ("1", True), ("true", True), ("yes", True),
        ("0", False), ("false", False), ("FALSE", False), ("no", False), ("off", False), (" Off ", False),
    ])
    def test_enabled_flag(self, monkeypatch, raw, expected):
        if raw is not None:
            monkeypatch.setenv("COMPLIANCE_ENABLED", raw)
        assert cp.enabled() is expected

    def test_loading_check_flag(self, monkeypatch):
        assert cp.loading_check_required() is True
        monkeypatch.setenv("COMPLIANCE_LOADING_CHECK_REQUIRED", "off")
        assert cp.loading_check_required() is False

    def test_float_falls_back_on_bad_value(self, monkeypatch):
        monkeypatch.setenv("COMPLIANCE_TEST_X", "not-a-number")
        assert cp._float("COMPLIANCE_TEST_X", 7.5) == 7.5
        monkeypatch.setenv("COMPLIANCE_TEST_X", "2.5")
        assert cp._float("COMPLIANCE_TEST_X", 7.5) == 2.5

    def test_codes_list_parsing(self, monkeypatch):
        assert cp._codes("COMPLIANCE_TEST_Y", {"AA"}) == {"AA"}
        monkeypatch.setenv("COMPLIANCE_TEST_Y", " kp, ir ,,")
        assert cp._codes("COMPLIANCE_TEST_Y", {"AA"}) == {"KP", "IR"}
        monkeypatch.setenv("COMPLIANCE_TEST_Y", "")
        assert cp._codes("COMPLIANCE_TEST_Y", {"AA"}) == set()


class TestHelpers:
    @pytest.mark.parametrize(("raw", "out"), [("1520.00", "152000"), ("15 20", "1520"), (None, ""), (1520, "1520")])
    def test_hs_normalised(self, raw, out):
        assert cp._hs(raw) == out

    @pytest.mark.parametrize(("raw", "out"), [
        (" de ", "DE"), ("Iran", "IR"), ("north korea", "KP"), ("Germany", "GERMANY"), (None, ""), ("", ""),
    ])
    def test_country_normalised(self, raw, out):
        assert cp._country(raw) == out

    def test_role_reads_enum_and_string(self):
        assert cp._role(SimpleNamespace(role=SimpleNamespace(value="BUYER"))) == "buyer"
        assert cp._role(SimpleNamespace(role="Supplier")) == "supplier"
        assert cp._role(SimpleNamespace()) == ""

    def test_party_absent(self):
        assert cp._party(SimpleNamespace(parties=None), "buyer") is None

    def test_status_worst_finding_wins(self):
        info = cp.Finding(code="a", severity="info", message="m")
        review = cp.Finding(code="b", severity="review", message="m")
        block = cp.Finding(code="c", severity="block", message="m")
        assert cp._status([]) == "clear"
        assert cp._status([info]) == "clear"
        assert cp._status([info, review]) == "review"
        assert cp._status([review, block, info]) == "block"

    def test_name_core_drops_noise_and_short_words(self):
        assert cp._name_core("The Acme Trading Pvt Ltd Co") == {"acme"}
        assert cp._name_core(None) == set()

    def test_result_helpers(self):
        res = cp.CheckResult(stage="x", status="block", findings=[
            cp.Finding(code="a", severity="block", message="stop"),
            cp.Finding(code="b", severity="review", message="look"),
            cp.Finding(code="c", severity="info", message="fyi"),
        ])
        assert _codes_of(res.blocks) == ["a"]
        assert _codes_of(res.reviews) == ["b"]
        assert res.summary() == "a: stop; b: look"
        assert cp.CheckResult(stage="x", status="clear").summary() == "no findings"


# ---------------------------------------------------------------------------
# HS policy
# ---------------------------------------------------------------------------

class TestHsPolicy:
    @pytest.mark.parametrize(("prefix", "entry"), sorted(cp.HS_POLICY_SEED.items()))
    async def test_every_seed_entry_has_its_severity(self, prefix, entry):
        status, _note = entry
        finding = await cp.check_hs(prefix)
        assert finding.code == f"hs_{status}"
        assert finding.severity == cp._STATUS_SEVERITY[status]

    @pytest.mark.parametrize("status", ["prohibited", "scomet"])
    def test_prohibited_and_scomet_always_block(self, status):
        assert cp._STATUS_SEVERITY[status] == "block"

    async def test_longer_code_inherits_seed_prefix(self):
        assert (await cp.hs_policy_status("93019000"))[0] == "scomet"
        assert (await cp.hs_policy_status("440310"))[0] == "prohibited"

    async def test_longest_prefix_is_the_one_used(self):
        assert (await cp.hs_policy_status("121130"))[0] == "restricted"
        assert (await cp.hs_policy_status("1211"))[0] == "unverified"

    @pytest.mark.parametrize("code", ["", None, "9", "abc"])
    async def test_unusable_code_is_unverified_not_free(self, code):
        finding = await cp.check_hs(code)
        assert finding.code == "hs_unverified" and finding.severity == "review"

    @pytest.mark.parametrize(("status", "severity"), [
        ("free", "info"), ("licensed", "info"), ("restricted", "review"), ("state_trading", "review"),
        ("dual_use", "review"), ("prohibited", "block"), ("scomet", "block"),
    ])
    async def test_operator_status_severity(self, status, severity):
        await cp.set_hs_policy(cp.HsPolicyEntry(hs_code="1520", status=status))
        assert (await cp.check_hs("1520")).severity == severity

    async def test_operator_confirmation_beats_the_seed(self):
        await cp.set_hs_policy(cp.HsPolicyEntry(hs_code="1006", status="free", confirmed_by="ops"))
        status, note = await cp.hs_policy_status("100630")
        assert status == "free" and "ops" in note

    async def test_operator_prohibition_beats_a_free_parent(self):
        await cp.set_hs_policy(cp.HsPolicyEntry(hs_code="1520", status="free"))
        await cp.set_hs_policy(cp.HsPolicyEntry(hs_code="152000", status="prohibited", note="banned"))
        assert await cp.hs_policy_status("152000") == ("prohibited", "banned")

    @pytest.mark.parametrize("parent", ["152000", "1520"])
    async def test_parent_confirmation_covers_eight_digits(self, parent):
        await cp.set_hs_policy(cp.HsPolicyEntry(hs_code=parent, status="free"))
        assert (await cp.hs_policy_status("15200010"))[0] == "free"

    async def test_two_digit_confirmation_does_not_lift_a_seed_block(self):
        await cp.set_hs_policy(cp.HsPolicyEntry(hs_code="93", status="free"))
        assert (await cp.hs_policy_status("930190"))[0] == "scomet"

    async def test_confirmation_without_note_or_name(self):
        await cp.set_hs_policy(cp.HsPolicyEntry(hs_code="1520", status="free"))
        assert await cp.hs_policy_status("1520") == ("free", "confirmed by operator")

    async def test_code_is_normalised_when_saved(self):
        entry = await cp.set_hs_policy(cp.HsPolicyEntry(hs_code="15.20", status="free"))
        assert entry.hs_code == "1520"
        assert await cp.get_hs_policy("1520") is not None


# ---------------------------------------------------------------------------
# Destination and party countries
# ---------------------------------------------------------------------------

class TestDestination:
    @pytest.mark.parametrize("cc", sorted(cp.BLOCKED_COUNTRIES_DEFAULT))
    def test_each_embargoed_country_blocks(self, cc):
        finding = cp.check_destination(cc)
        assert finding.code == "destination_embargoed" and finding.severity == "block"

    @pytest.mark.parametrize("cc", sorted(cp.REVIEW_COUNTRIES_DEFAULT))
    def test_each_high_risk_country_reviews(self, cc):
        finding = cp.check_destination(cc)
        assert finding.code == "destination_high_risk" and finding.severity == "review"

    @pytest.mark.parametrize("name", ["Iran", "NORTH KOREA", "syria", "Cuba"])
    def test_embargoed_country_by_full_name(self, name):
        assert cp.check_destination(name).severity == "block"

    @pytest.mark.parametrize("raw", ["", None, "   "])
    def test_unknown_destination_needs_review(self, raw):
        finding = cp.check_destination(raw)
        assert finding.code == "destination_unknown" and finding.severity == "review"

    @pytest.mark.parametrize("cc", ["DE", "US", "AE", "de"])
    def test_ordinary_destination_has_no_finding(self, cc):
        assert cp.check_destination(cc) is None

    def test_lists_can_be_overridden(self, monkeypatch):
        monkeypatch.setenv("COMPLIANCE_BLOCKED_COUNTRIES", "DE")
        monkeypatch.setenv("COMPLIANCE_REVIEW_COUNTRIES", "FR")
        assert cp.check_destination("DE").severity == "block"
        assert cp.check_destination("FR").severity == "review"
        assert cp.check_destination("IR") is None

    def test_block_list_wins_over_review_list(self, monkeypatch):
        monkeypatch.setenv("COMPLIANCE_REVIEW_COUNTRIES", "IR")
        assert cp.check_destination("IR").severity == "block"

    def test_buyer_in_embargoed_country(self):
        findings = cp.check_party_countries(_deal(buyer=_party(country="IR")))
        assert _codes_of(findings) == ["buyer_country_embargoed"]
        assert findings[0].severity == "block"

    def test_supplier_in_high_risk_country(self):
        findings = cp.check_party_countries(_deal(supplier=_party("supplier", country="RU")))
        assert _codes_of(findings) == ["supplier_country_high_risk"]
        assert findings[0].severity == "review"

    def test_party_without_country_is_not_a_finding(self):
        assert cp.check_party_countries(_deal(buyer=_party(country=None))) == []

    def test_prospect_without_parties(self):
        assert cp.check_party_countries(SimpleNamespace(parties=[])) == []


# ---------------------------------------------------------------------------
# Screening and related parties
# ---------------------------------------------------------------------------

class TestScreening:
    @pytest.mark.parametrize("verdict", ["unchecked", None, "", "UNCHECKED"])
    def test_never_screened_needs_review(self, verdict):
        findings = cp.check_screening(_deal(buyer=_party(sanctions_status=verdict)))
        assert _codes_of(findings) == ["buyer_screening_unchecked"]
        assert findings[0].severity == "review"

    @pytest.mark.parametrize("verdict", ["clear", "review", "block"])
    def test_screened_party_is_not_flagged_here(self, verdict):
        assert cp.check_screening(_deal(buyer=_party(sanctions_status=verdict))) == []

    def test_block_mode(self, monkeypatch):
        monkeypatch.setenv("COMPLIANCE_UNCHECKED_SCREENING", " BLOCK ")
        findings = cp.check_screening(_deal(supplier=_party("supplier", sanctions_status=None)))
        assert _codes_of(findings) == ["supplier_screening_unchecked"]
        assert findings[0].severity == "block"

    def test_allow_mode(self, monkeypatch):
        monkeypatch.setenv("COMPLIANCE_UNCHECKED_SCREENING", "allow")
        assert cp.check_screening(_deal(buyer=_party(sanctions_status=None))) == []

    def test_missing_party_is_skipped(self):
        assert cp.check_screening(SimpleNamespace(parties=[_party(sanctions_status="clear")])) == []


class TestRelatedParties:
    def test_unrelated(self):
        assert cp.check_related_parties(_deal()) is None

    def test_missing_side(self):
        assert cp.check_related_parties(SimpleNamespace(parties=[_party()])) is None

    def test_same_company_email_domain(self):
        finding = cp.check_related_parties(_deal(
            buyer=_party(email="a@shared.com"), supplier=_party("supplier", email="b@shared.com")))
        assert finding.severity == "review"
        assert finding.data["reasons"] == ["same company domain shared.com"]

    def test_shared_free_mail_domain_is_not_a_link(self):
        assert cp.check_related_parties(_deal(
            buyer=_party(email="a@gmail.com"), supplier=_party("supplier", email="b@gmail.com"))) is None

    def test_email_domain_matches_other_sides_website(self):
        finding = cp.check_related_parties(_deal(
            buyer=_party(email="a@shared.com"),
            supplier=_party("supplier", email="b@gmail.com", website="https://www.shared.com/about")))
        assert "same company domain shared.com" in finding.data["reasons"]

    def test_same_website(self):
        finding = cp.check_related_parties(_deal(
            buyer=_party(website="http://Shared.com"), supplier=_party("supplier", website="www.shared.com/")))
        assert finding is not None

    def test_same_phone_with_different_country_prefix(self):
        finding = cp.check_related_parties(_deal(
            buyer=_party(phone="+91 98765 43210"), supplier=_party("supplier", phone="098765-43210")))
        assert finding.data["reasons"] == ["same phone number"]

    def test_short_phone_is_ignored(self):
        assert cp.check_related_parties(_deal(
            buyer=_party(phone="12345"), supplier=_party("supplier", phone="12345"))) is None

    def test_different_phone(self):
        assert cp.check_related_parties(_deal(
            buyer=_party(phone="+919876543210"), supplier=_party("supplier", phone="+919876543211"))) is None

    def test_same_address_ignores_punctuation_and_case(self):
        finding = cp.check_related_parties(_deal(
            buyer=_party(address="12, MG Road, Chennai"), supplier=_party("supplier", address="12 mg road chennai")))
        assert finding.data["reasons"] == ["same address"]

    def test_short_address_is_ignored(self):
        assert cp.check_related_parties(_deal(
            buyer=_party(address="Depot 4"), supplier=_party("supplier", address="Depot 4"))) is None

    def test_same_name_after_noise_words(self):
        finding = cp.check_related_parties(_deal(
            buyer=_party(legal_name="Acme Foods GmbH"), supplier=_party("supplier", legal_name="Acme Foods Pvt Ltd")))
        assert finding.data["reasons"] == ["same company name"]

    def test_noise_only_names_are_not_a_match(self):
        assert cp.check_related_parties(_deal(
            buyer=_party(legal_name="Trading Co"), supplier=_party("supplier", legal_name="Trading Co"))) is None

    def test_all_reasons_are_reported(self):
        finding = cp.check_related_parties(_deal(
            buyer=_party(legal_name="Acme", email="a@x.com", phone="9876543210", address="12 MG Road Chennai"),
            supplier=_party("supplier", legal_name="Acme", email="b@x.com", phone="9876543210",
                            address="12 MG Road Chennai")))
        assert len(finding.data["reasons"]) == 4


# ---------------------------------------------------------------------------
# Supplier record and prices
# ---------------------------------------------------------------------------

def _quote(supplier="SUP-1", denied=False, age=5.0, verified=True):
    verification = SimpleNamespace(dgft_denied=denied, business_age_years=age) if verified else None
    return SimpleNamespace(supplier_id=supplier, verification=verification)


class TestSupplierRecord:
    def _quotes(self, monkeypatch, *quotes):
        monkeypatch.setattr("services.sourcing.service.list_quotes", AsyncMock(return_value=list(quotes)))

    async def test_no_winning_supplier(self):
        assert await cp.check_supplier_record(_deal(winning_supplier_id=None)) == []

    async def test_quotes_unavailable(self, monkeypatch):
        monkeypatch.setattr("services.sourcing.service.list_quotes", AsyncMock(side_effect=RuntimeError("x")))
        assert await cp.check_supplier_record(_deal()) == []

    async def test_quote_without_verification_is_ignored(self, monkeypatch):
        self._quotes(monkeypatch, _quote(verified=False))
        assert await cp.check_supplier_record(_deal()) == []

    async def test_denied_entity_blocks(self, monkeypatch):
        self._quotes(monkeypatch, _quote(denied=True))
        findings = await cp.check_supplier_record(_deal())
        assert _codes_of(findings) == ["supplier_dgft_denied"] and findings[0].severity == "block"

    async def test_other_suppliers_are_ignored(self, monkeypatch):
        self._quotes(monkeypatch, _quote(supplier="SUP-2", denied=True))
        assert await cp.check_supplier_record(_deal()) == []

    async def test_latest_quote_counts(self, monkeypatch):
        self._quotes(monkeypatch, _quote(denied=True), _quote(denied=False))
        assert await cp.check_supplier_record(_deal()) == []

    @pytest.mark.parametrize(("age", "flagged"), [(0.5, True), (0.99, True), (1.0, False), (3.0, False), (0.0, False), (None, False)])
    async def test_company_age_limit(self, monkeypatch, age, flagged):
        self._quotes(monkeypatch, _quote(age=age))
        findings = await cp.check_supplier_record(_deal())
        assert _codes_of(findings) == (["supplier_new_company"] if flagged else [])

    async def test_age_limit_is_configurable(self, monkeypatch):
        monkeypatch.setenv("COMPLIANCE_MIN_COMPANY_AGE_YEARS", "4")
        self._quotes(monkeypatch, _quote(age=3.0))
        assert _codes_of(await cp.check_supplier_record(_deal())) == ["supplier_new_company"]


class TestPrices:
    def _market(self, monkeypatch, median=100.0, confidence="high", observations=()):
        obs = AsyncMock(return_value=list(observations))
        est = SimpleNamespace(median=median, confidence=confidence)
        monkeypatch.setattr(pricebook, "observations", obs)
        monkeypatch.setattr(pricebook, "estimate_from", lambda *_a, **_k: est)
        return obs

    @pytest.mark.parametrize(("price", "flagged"), [(140.0, False), (141.0, True), (60.0, False), (59.0, True), (100.0, False)])
    async def test_buyer_price_deviation_limit_is_exclusive(self, monkeypatch, price, flagged):
        self._market(monkeypatch)
        findings = await cp.check_prices(_deal(), buyer_price=price, supplier_price=None)
        assert _codes_of(findings) == (["buyer_price_off_market"] if flagged else [])

    async def test_supplier_price_is_checked_too(self, monkeypatch):
        self._market(monkeypatch)
        findings = await cp.check_prices(_deal(), buyer_price=None, supplier_price=50.0)
        assert _codes_of(findings) == ["supplier_price_off_market"]

    @pytest.mark.parametrize(("median", "confidence"), [(100.0, "none"), (0.0, "high"), (None, "high")])
    async def test_no_market_estimate_means_no_finding(self, monkeypatch, median, confidence):
        self._market(monkeypatch, median=median, confidence=confidence)
        assert await cp.check_prices(_deal(), buyer_price=500.0, supplier_price=None) == []

    @pytest.mark.parametrize("price", [None, 0.0, -5.0])
    async def test_missing_or_bad_price_is_skipped(self, monkeypatch, price):
        obs = self._market(monkeypatch)
        assert await cp.check_prices(_deal(), buyer_price=price, supplier_price=None) == []
        obs.assert_not_awaited()

    async def test_own_deal_prices_are_excluded(self, monkeypatch):
        seen = {}
        mine, other = SimpleNamespace(deal_id="DEAL-E"), SimpleNamespace(deal_id="DEAL-Z")
        monkeypatch.setattr(pricebook, "observations", AsyncMock(return_value=[mine, other]))

        def _estimate(obs, **_kw):
            seen["obs"] = obs
            return SimpleNamespace(median=100.0, confidence="high")

        monkeypatch.setattr(pricebook, "estimate_from", _estimate)
        await cp.check_prices(_deal(), buyer_price=100.0, supplier_price=None)
        assert seen["obs"] == [other]

    @pytest.mark.parametrize(("buyer", "supplier", "flagged"), [
        (180.0, 100.0, False), (181.0, 100.0, True), (100.0, 100.0, False),
        (500.0, None, False), (None, 100.0, False), (500.0, 0.0, False),
    ])
    async def test_spread_limit_is_exclusive(self, monkeypatch, buyer, supplier, flagged):
        self._market(monkeypatch, median=0.0)
        findings = await cp.check_prices(_deal(), buyer_price=buyer, supplier_price=supplier)
        assert _codes_of(findings) == (["spread_too_wide"] if flagged else [])

    async def test_price_book_failure_still_checks_the_spread(self, monkeypatch):
        monkeypatch.setattr(pricebook, "public_priors", AsyncMock(side_effect=RuntimeError("down")))
        findings = await cp.check_prices(_deal(), buyer_price=300.0, supplier_price=100.0)
        assert _codes_of(findings) == ["spread_too_wide"]

    async def test_limits_are_configurable(self, monkeypatch):
        self._market(monkeypatch)
        monkeypatch.setenv("COMPLIANCE_PRICE_DEVIATION_PCT", "10")
        monkeypatch.setenv("COMPLIANCE_MAX_SPREAD_RATIO", "1.05")
        findings = await cp.check_prices(_deal(), buyer_price=111.0, supplier_price=100.0)
        assert _codes_of(findings) == ["buyer_price_off_market", "spread_too_wide"]


# ---------------------------------------------------------------------------
# Trade check and hold bookkeeping
# ---------------------------------------------------------------------------

class TestTradeCheck:
    async def test_deal_created_skips_party_and_price_checks(self):
        deal = _deal(buyer=_party(sanctions_status=None))
        result = await cp.check_trade(deal, stage="deal_created", buyer_price=900.0, supplier_price=100.0)
        assert _codes_of(result.findings) == ["hs_unverified"]

    async def test_pi_stage_runs_party_and_price_checks(self):
        deal = _deal(buyer=_party(sanctions_status=None))
        result = await cp.check_trade(deal, stage="pi", buyer_price=900.0, supplier_price=100.0)
        assert {"hs_unverified", "buyer_screening_unchecked", "spread_too_wide"} <= set(_codes_of(result.findings))
        assert result.status == "review"

    async def test_block_outranks_review(self):
        result = await cp.check_trade(_deal(hs_code="930190"), stage="deal_created")
        assert result.status == "block"
        assert (await cp.get_record("DEAL-E")).holds == ["hs_scomet"]

    async def test_clear_trade_has_no_holds(self):
        await cp.set_hs_policy(cp.HsPolicyEntry(hs_code="1520", status="free"))
        result = await cp.check_trade(_deal(), stage="deal_created")
        assert result.status == "clear"
        assert (await cp.get_record("DEAL-E")).holds == []

    async def test_persist_false_leaves_the_record_alone(self):
        await cp.check_trade(_deal(), stage="deal_created", persist=False)
        record = await cp.get_record("DEAL-E")
        assert record.trade is None and record.holds == []

    async def test_accepted_review_is_not_asked_again(self):
        await cp.check_trade(_deal(), stage="deal_created")
        await cp.accept_findings("DEAL-E", ["hs_unverified"], by="ops")
        result = await cp.check_trade(_deal(), stage="deal_created")
        assert result.status == "clear"
        assert "accepted by operator" in result.findings[0].message

    async def test_accepting_a_block_does_not_downgrade_it(self):
        await cp.accept_findings("DEAL-E", ["hs_scomet"], by="ops")
        result = await cp.check_trade(_deal(hs_code="930190"), stage="deal_created")
        assert result.status == "block"
        assert "hs_scomet" in (await cp.get_record("DEAL-E")).holds

    async def test_recheck_replaces_trade_holds_and_keeps_shipment_ones(self):
        record = await cp.get_record("DEAL-E")
        record.holds = ["hs_unverified", "loading_not_verified", "payer_unknown", "seal_mismatch", "shipment_no_photos"]
        await cp.save_record(record)
        await cp.set_hs_policy(cp.HsPolicyEntry(hs_code="1520", status="free"))
        await cp.check_trade(_deal(), stage="deal_created")
        assert (await cp.get_record("DEAL-E")).holds == [
            "loading_not_verified", "payer_unknown", "seal_mismatch", "shipment_no_photos",
        ]

    def test_trade_holds_selection(self):
        assert cp._trade_holds(["hs_x", "shipment_y", "loading_z", "seal_a", "payer_b", "related_parties"]) == {
            "hs_x", "related_parties",
        }


class TestAcceptAndHolds:
    async def test_accept_clears_only_the_named_codes(self):
        record = await cp.get_record("D")
        record.holds = ["a", "b"]
        await cp.save_record(record)
        await cp.accept_findings("D", ["a"])
        record = await cp.get_record("D")
        assert record.holds == ["b"] and record.cleared_holds == ["a"]

    async def test_accepted_codes_accumulate_without_duplicates(self):
        await cp.accept_findings("D", ["b", "a"])
        await cp.accept_findings("D", ["a", "c"])
        assert (await cp.get_record("D")).cleared_holds == ["a", "b", "c"]

    async def test_unverified_hs_is_recorded_free(self):
        await cp.accept_findings("D", ["hs_unverified"], by="ops", hs_code="1520")
        entry = await cp.get_hs_policy("1520")
        assert entry.status == "free" and entry.confirmed_by == "ops"

    async def test_recorded_free_defaults_to_operator(self):
        await cp.accept_findings("D", ["hs_unverified"], hs_code="1520")
        assert (await cp.get_hs_policy("1520")).confirmed_by == "operator"

    async def test_other_codes_do_not_touch_hs_policy(self):
        await cp.accept_findings("D", ["related_parties"], hs_code="1520")
        assert await cp.get_hs_policy("1520") is None

    async def test_unverified_without_hs_code_records_nothing(self):
        await cp.accept_findings("D", ["hs_unverified"])
        assert await cp.list_hs_policy() == []

    async def test_hold_flags_prefixed(self):
        record = await cp.get_record("D")
        record.holds = ["x", "y"]
        await cp.save_record(record)
        assert await cp.hold_flags("D") == ["compliance:x", "compliance:y"]

    @pytest.mark.parametrize("deal_id", [None, ""])
    async def test_hold_flags_without_a_deal(self, deal_id):
        assert await cp.hold_flags(deal_id) == []

    async def test_hold_flags_survive_a_storage_failure(self, monkeypatch):
        monkeypatch.setattr(cp, "get_record", AsyncMock(side_effect=RuntimeError("down")))
        assert await cp.hold_flags("D") == []


# ---------------------------------------------------------------------------
# Payer
# ---------------------------------------------------------------------------

class TestPayer:
    @pytest.mark.parametrize("payer", [{}, {"payer_name": ""}, {"payer_name": "   "}, {"payer_name": None}])
    async def test_unreported_payer_needs_review(self, payer):
        result = await cp.check_payer(_deal(), payer)
        assert _codes_of(result.findings) == ["payer_unknown"]

    async def test_buyer_paying_is_clear(self):
        result = await cp.check_payer(_deal(), {"payer_name": "GLACONCHEMIE GmbH", "payer_country": "DE"})
        assert result.status == "clear"
        assert (await cp.get_record("DEAL-E")).holds == []

    async def test_third_party_payer(self):
        result = await cp.check_payer(_deal(), {"payer_name": "Unrelated Holdings"})
        assert _codes_of(result.findings) == ["payer_not_buyer"]

    async def test_name_only_sharing_noise_words_is_third_party(self):
        result = await cp.check_payer(_deal(), {"payer_name": "GmbH Ltd Trading"})
        assert _codes_of(result.findings) == ["payer_not_buyer"]

    async def test_no_buyer_party_cannot_compare_names(self):
        result = await cp.check_payer(SimpleNamespace(deal_id="DEAL-E", parties=[]), {"payer_name": "Anyone"})
        assert result.status == "clear"

    async def test_payment_from_embargoed_country_blocks(self):
        result = await cp.check_payer(_deal(), {"payer_name": "Glaconchemie", "payer_country": "IR"})
        assert result.status == "block"
        assert _codes_of(result.findings) == ["payer_country_embargoed", "payer_country_mismatch"]

    async def test_payment_from_high_risk_country(self):
        result = await cp.check_payer(_deal(), {"payer_name": "Glaconchemie", "payer_country": "RU"})
        assert "payer_country_high_risk" in _codes_of(result.findings)

    async def test_country_mismatch(self):
        result = await cp.check_payer(_deal(), {"payer_name": "Glaconchemie", "payer_country": "FR"})
        assert _codes_of(result.findings) == ["payer_country_mismatch"]

    async def test_country_name_matches_code(self):
        result = await cp.check_payer(
            _deal(buyer=_party(country="IR"), hs_code="1520"), {"payer_name": "Glaconchemie", "payer_country": "Iran"})
        assert "payer_country_mismatch" not in _codes_of(result.findings)

    async def test_buyer_without_country_skips_the_comparison(self):
        result = await cp.check_payer(_deal(buyer=_party(country=None)), {"payer_name": "Glaconchemie", "payer_country": "FR"})
        assert result.status == "clear"

    async def test_accepted_review_stays_accepted(self):
        await cp.accept_findings("DEAL-E", ["payer_not_buyer"])
        result = await cp.check_payer(_deal(), {"payer_name": "Unrelated Holdings"})
        assert result.status == "clear" and result.findings[0].severity == "info"

    async def test_recheck_replaces_payer_holds_only(self):
        record = await cp.get_record("DEAL-E")
        record.holds = ["hs_unverified", "payer_not_buyer"]
        await cp.save_record(record)
        await cp.check_payer(_deal(), {"payer_name": "Glaconchemie"})
        assert (await cp.get_record("DEAL-E")).holds == ["hs_unverified"]


# ---------------------------------------------------------------------------
# Shipment and seal
# ---------------------------------------------------------------------------

class TestShipment:
    @pytest.fixture(autouse=True)
    def _docs(self, monkeypatch):
        self.pi = SimpleNamespace(quantity_mt=20.0)
        self.po = SimpleNamespace(quantity_mt=20.0)
        monkeypatch.setattr(cp, "_pi_po", AsyncMock(side_effect=lambda _d: (self.pi, self.po)))

    async def _check(self, **loading):
        await cp.record_loading("DEAL-E", _loading(**loading))
        return await cp.check_shipment(_deal())

    async def test_matching_loading_is_clear(self):
        result = await self._check()
        assert result.status == "clear" and result.findings == []

    async def test_nothing_recorded_blocks(self):
        result = await cp.check_shipment(_deal())
        assert _codes_of(result.findings) == ["loading_not_verified"] and result.status == "block"

    @pytest.mark.parametrize("missing", ["seal_numbers", "container_numbers"])
    async def test_container_and_seal_are_both_required(self, missing):
        result = await self._check(**{missing: []})
        assert _codes_of(result.findings) == ["loading_not_verified"]

    async def test_blank_seal_entries_do_not_count(self):
        result = await self._check(seal_numbers=["  ", ""])
        assert _codes_of(result.findings) == ["loading_not_verified"]

    async def test_loading_numbers_are_normalised(self):
        record = await cp.record_loading("D", _loading(container_numbers=[" mscu1 ", ""], seal_numbers=[" s1 "]))
        assert record.loading.container_numbers == ["MSCU1"] and record.loading.seal_numbers == ["S1"]

    async def test_other_heading_loaded_blocks(self):
        result = await self._check(hs_code="9301")
        assert _codes_of(result.findings) == ["shipment_hs_mismatch"] and result.status == "block"

    async def test_same_heading_other_subheading_passes(self):
        assert (await self._check(hs_code="152090")).status == "clear"

    async def test_no_hs_recorded_is_not_a_mismatch(self):
        assert (await self._check(hs_code=None)).status == "clear"

    async def test_quantity_not_recorded(self):
        assert _codes_of((await self._check(loaded_quantity_mt=None)).findings) == ["shipment_qty_unknown"]

    @pytest.mark.parametrize(("loaded", "flagged"), [(21.0, False), (19.0, False), (21.01, True), (18.99, True), (0.0, True)])
    async def test_quantity_tolerance_is_inclusive(self, loaded, flagged):
        result = await self._check(loaded_quantity_mt=loaded)
        assert _codes_of(result.findings) == (["shipment_qty_mismatch"] if flagged else [])

    async def test_po_quantity_is_preferred_over_pi(self):
        self.pi.quantity_mt = 20.5
        result = await self._check(loaded_quantity_mt=19.5)
        assert result.status == "clear"
        assert self.po.quantity_mt == 20.0

    async def test_falls_back_to_pi_quantity(self):
        self.po = None
        result = await self._check(loaded_quantity_mt=30.0)
        assert _codes_of(result.findings) == ["shipment_qty_mismatch"]

    async def test_no_documents_cannot_compare_quantity(self):
        self.pi = self.po = None
        assert (await self._check(loaded_quantity_mt=999.0)).status == "clear"

    async def test_pi_and_po_quantities_disagree(self):
        self.pi.quantity_mt = 30.0
        result = await self._check()
        assert _codes_of(result.findings) == ["shipment_pi_po_mismatch"]

    async def test_no_photos(self):
        assert _codes_of((await self._check(photos=[])).findings) == ["shipment_no_photos"]

    async def test_accepted_review_stays_accepted(self):
        await cp.accept_findings("DEAL-E", ["shipment_no_photos"])
        result = await self._check(photos=[])
        assert result.status == "clear" and result.findings[0].severity == "info"

    async def test_accepting_a_block_does_not_lift_it(self):
        await cp.accept_findings("DEAL-E", ["loading_not_verified"])
        assert (await cp.check_shipment(_deal())).status == "block"

    async def test_recheck_replaces_shipment_holds_only(self):
        await self._check(photos=[])
        record = await cp.get_record("DEAL-E")
        record.holds = sorted(record.holds + ["hs_unverified"])
        await cp.save_record(record)
        await self._check()
        assert (await cp.get_record("DEAL-E")).holds == ["hs_unverified"]

    async def test_tolerance_is_configurable(self, monkeypatch):
        monkeypatch.setenv("COMPLIANCE_QTY_TOLERANCE_PCT", "1")
        assert _codes_of((await self._check(loaded_quantity_mt=21.0)).findings) == ["shipment_qty_mismatch"]


class TestPiPoLookup:
    async def test_reads_both_documents(self, monkeypatch):
        repo = SimpleNamespace(get_pi_by_deal=AsyncMock(return_value="PI"), get_po_by_deal=AsyncMock(return_value="PO"))
        monkeypatch.setattr("services.dealmaker.repository.get_repository", lambda: repo)
        assert await cp._pi_po("D") == ("PI", "PO")

    async def test_repository_without_pi_lookup(self, monkeypatch):
        repo = SimpleNamespace(get_po_by_deal=AsyncMock(return_value="PO"))
        monkeypatch.setattr("services.dealmaker.repository.get_repository", lambda: repo)
        assert await cp._pi_po("D") == (None, "PO")

    async def test_lookup_failure(self, monkeypatch):
        repo = SimpleNamespace(get_po_by_deal=AsyncMock(side_effect=RuntimeError("down")))
        monkeypatch.setattr("services.dealmaker.repository.get_repository", lambda: repo)
        assert await cp._pi_po("D") == (None, None)


class TestSeal:
    @pytest.mark.parametrize("seals", [[], ["", "  "]])
    async def test_nothing_reported(self, seals):
        assert await cp.check_seal("D", seals) is None
        assert (await cp.get_record("D")).bl_seal_numbers == []

    async def test_no_loading_on_record_stores_the_seal(self):
        assert await cp.check_seal("D", ["s1"]) is None
        assert (await cp.get_record("D")).bl_seal_numbers == ["S1"]

    async def test_match_is_case_insensitive(self):
        await cp.record_loading("D", _loading(seal_numbers=["SEAL1"]))
        assert await cp.check_seal("D", [" seal1 "]) is None
        assert (await cp.get_record("D")).holds == []

    async def test_subset_of_applied_seals_is_fine(self):
        await cp.record_loading("D", _loading(seal_numbers=["S1", "S2"]))
        assert await cp.check_seal("D", ["S2"]) is None

    async def test_different_seal_blocks_and_holds(self):
        await cp.record_loading("D", _loading(seal_numbers=["S1"]))
        finding = await cp.check_seal("D", ["S9"])
        assert finding.code == "seal_mismatch" and finding.severity == "block"
        assert finding.data == {"bl": ["S9"], "loaded": ["S1"]}
        assert "seal_mismatch" in (await cp.get_record("D")).holds

    async def test_one_unknown_seal_among_known_blocks(self):
        await cp.record_loading("D", _loading(seal_numbers=["S1", "S2"]))
        assert (await cp.check_seal("D", ["S1", "S9"])).code == "seal_mismatch"

    async def test_reported_seals_accumulate(self):
        await cp.check_seal("D", ["B"])
        await cp.check_seal("D", ["A", "B"])
        assert (await cp.get_record("D")).bl_seal_numbers == ["A", "B"]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class TestStorage:
    def test_no_mongo_means_memory(self, monkeypatch):
        monkeypatch.setattr("shared.config.settings.mongo_uri", "", raising=False)
        assert cp._collection("x") is None

    async def test_record_read_from_mongo(self, monkeypatch):
        monkeypatch.setattr(cp, "_collection", lambda _n: _Col(doc={"deal_id": "D", "holds": ["x"]}))
        assert (await cp.get_record("D")).holds == ["x"]

    async def test_record_missing_in_mongo_uses_memory(self, monkeypatch):
        await cp.save_record(cp.ComplianceRecord(deal_id="D", holds=["mem"]))
        monkeypatch.setattr(cp, "_collection", lambda _n: _Col(doc=None))
        assert (await cp.get_record("D")).holds == ["mem"]

    async def test_record_read_failure_uses_memory(self, monkeypatch):
        await cp.save_record(cp.ComplianceRecord(deal_id="D", holds=["mem"]))
        monkeypatch.setattr(cp, "_collection", lambda _n: _Col(fail=True))
        assert (await cp.get_record("D")).holds == ["mem"]

    async def test_record_write_is_an_upsert(self, monkeypatch):
        col = _Col()
        monkeypatch.setattr(cp, "_collection", lambda _n: col)
        await cp.save_record(cp.ComplianceRecord(deal_id="D"))
        flt, doc, upsert = col.replaced[0]
        assert flt == {"deal_id": "D"} and doc["deal_id"] == "D" and upsert is True

    async def test_record_write_failure_keeps_memory_copy(self, monkeypatch):
        monkeypatch.setattr(cp, "_collection", lambda _n: _Col(fail=True))
        await cp.save_record(cp.ComplianceRecord(deal_id="D", holds=["x"]))
        monkeypatch.setattr(cp, "_collection", lambda _n: None)
        assert (await cp.get_record("D")).holds == ["x"]

    async def test_policy_read_from_mongo(self, monkeypatch):
        monkeypatch.setattr(cp, "_collection", lambda _n: _Col(doc={"hs_code": "1520", "status": "free"}))
        assert (await cp.get_hs_policy("1520")).status == "free"

    async def test_policy_read_failure_uses_memory(self, monkeypatch):
        await cp.set_hs_policy(cp.HsPolicyEntry(hs_code="1520", status="licensed"))
        monkeypatch.setattr(cp, "_collection", lambda _n: _Col(fail=True))
        assert (await cp.get_hs_policy("1520")).status == "licensed"

    async def test_policy_write_goes_to_mongo(self, monkeypatch):
        col = _Col()
        monkeypatch.setattr(cp, "_collection", lambda _n: col)
        await cp.set_hs_policy(cp.HsPolicyEntry(hs_code="1520", status="free"))
        assert col.replaced[0][0] == {"hs_code": "1520"}

    async def test_policy_write_failure_keeps_memory_copy(self, monkeypatch):
        monkeypatch.setattr(cp, "_collection", lambda _n: _Col(fail=True))
        await cp.set_hs_policy(cp.HsPolicyEntry(hs_code="1520", status="free"))
        monkeypatch.setattr(cp, "_collection", lambda _n: None)
        assert (await cp.get_hs_policy("1520")).status == "free"

    async def test_policy_list_from_mongo(self, monkeypatch):
        monkeypatch.setattr(cp, "_collection", lambda _n: _Col(doc={"hs_code": "1520", "status": "free"}))
        assert [e.hs_code for e in await cp.list_hs_policy()] == ["1520"]

    async def test_policy_list_failure_uses_memory(self, monkeypatch):
        await cp.set_hs_policy(cp.HsPolicyEntry(hs_code="1520", status="free"))
        monkeypatch.setattr(cp, "_collection", lambda _n: _Col(fail=True))
        assert [e.hs_code for e in await cp.list_hs_policy()] == ["1520"]
