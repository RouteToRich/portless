"""Counterparty e-mails say only what is true for that deal (review board B-20)."""
from __future__ import annotations

import pathlib
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_verified_only_when_gst_and_iec_passed():
    from shared.orchestration.matchmaking import _supplier_line
    assert "GST- and IEC-verified" in _supplier_line(SimpleNamespace(supplier_verified=True))
    unverified = _supplier_line(SimpleNamespace(supplier_verified=False))
    assert "verified Indian" not in unverified and "we verify before the order" in unverified


def test_the_inspector_is_the_one_we_can_book(monkeypatch):
    from shared.connectors.qc_providers import inspector_phrase
    monkeypatch.setenv("QC_PROVIDERS_ENABLED", "qima")
    assert inspector_phrase() == "an independent inspection agency (QIMA)"
    monkeypatch.setenv("QC_PROVIDERS_ENABLED", "qima,sgs")
    assert "you choose" in inspector_phrase() and "SGS" in inspector_phrase()


def test_no_template_promises_what_is_not_yet_true():
    """Phrases that were wrong for some deals must not come back in any e-mail source."""
    banned = ("reorder through us", "by the agency you choose", "inspection agency you choose\"")
    files = list((ROOT / "shared").rglob("*.py")) + list((ROOT / "services").rglob("*.py")) + \
        list((ROOT / "agents").rglob("*.py")) + list((ROOT / "services").rglob("*.j2"))
    hits = [f"{p.relative_to(ROOT)}: {b}" for p in files for b in banned if b in p.read_text(encoding="utf-8", errors="ignore")]
    assert hits == []


def test_supplier_reasons_mention_ex_works():
    from shared import company_profile as cp
    text = " ".join(cp.supplier_reasons())
    assert "ex-works" in text and "reorder" not in text
