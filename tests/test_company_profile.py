"""Company profile: one professional, truthful voice in every counterparty e-mail."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from shared import company_profile as cp

_ENV = ("PORTLESS_SENDER_NAME", "PORTLESS_SENDER_TITLE", "PORTLESS_DESK_NAME", "PORTLESS_WEBSITE",
        "PORTLESS_CONTACT_EMAIL", "PORTLESS_PHONE", "PORTLESS_ADDRESS", "PORTLESS_IEC", "PORTLESS_GSTIN",
        "PORTLESS_LEGAL_NAME", "PORTLESS_BRAND_NAME")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def test_signature_without_settings_is_the_desk_and_no_blank_contact_lines():
    sig = cp.signature()
    assert sig.startswith("\n\nKind regards,\n\nTrade Desk | ")
    assert "Merchant exporter, India" in sig
    assert "IEC" not in sig and "GSTIN" not in sig and "None" not in sig and "|  |" not in sig


def test_signature_prints_only_what_is_configured(monkeypatch):
    monkeypatch.setenv("PORTLESS_SENDER_NAME", "Priya Raman")
    monkeypatch.setenv("PORTLESS_SENDER_TITLE", "Head of Trade")
    monkeypatch.setenv("PORTLESS_IEC", "0123456789")
    monkeypatch.setenv("PORTLESS_WEBSITE", "www.example.in")
    sig = cp.signature()
    assert "Priya Raman\nHead of Trade\n" in sig
    assert "IEC 0123456789" in sig and "GSTIN" not in sig
    assert "www.example.in" in sig and "Trade Desk" not in sig


def test_with_signature_replaces_a_model_written_sign_off():
    out = cp.with_signature("Dear team,\n\nThank you.\n\nBest regards,\nPortLess Trade Team")
    assert out.count("regards") == 1 and "PortLess Trade Team" not in out
    assert out.startswith("Dear team,\n\nThank you.\n\nKind regards,")


def test_with_signature_keeps_body_text_that_merely_mentions_regards():
    body = "Dear team,\n\nWith regards to the price, we can hold USD 980 per MT."
    assert cp.with_signature(body).startswith(body)


def test_profile_for_the_llm_has_no_invented_track_record():
    for audience in ("buyer", "supplier"):
        facts = cp.for_llm(audience)
        text = " ".join([facts["who_we_are"], *facts["why_work_with_us"], *facts["how_a_deal_works"]]).lower()
        for claim in ("years", "clients", "award", "largest", "leading", "no. 1"):
            assert claim not in text, (audience, claim)
        assert facts["registration_and_contact"] == []


def test_buyer_steps_follow_the_real_escrow_schedule():
    steps = " ".join(cp.buyer_steps())
    assert "30%" in steps and "10% when you accept the goods" in steps


def test_buyer_introduction_introduces_portless_and_ends_with_the_signature():
    from shared.orchestration.outreach import draft
    deal = SimpleNamespace(deal_id="DEAL-1", hs_code="10063020", quantity_mt=20.0)
    cand = SimpleNamespace(candidate_id="c1", legal_name="Gulf Staples LLC", pitch_angle="", quantity_mt=None)
    _subject, body = draft(deal, cand)
    assert body.startswith("Dear Gulf Staples LLC team,")
    assert cp.one_line() in body and "How an order works:" in body
    assert "Kind regards," in body and "not interested" in body


def test_rfq_introduces_portless_to_the_supplier():
    from services.sourcing.service import rfq_email_body
    body = rfq_email_body("Cauvery Mills", "We are sourcing 20 MT.")
    assert body.startswith("Dear Cauvery Mills team,") and "About us:" in body
    assert "Why manufacturers work with us:" in body and body.rstrip().endswith("Merchant exporter, India")
