"""Suite markers are applied by file (tests/markers.py)."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests import markers as m

TESTS = Path(__file__).resolve().parent


def test_every_listed_file_exists():
    listed = m.SMOKE | m.MONEY | m.INTEGRATION | m.SLOW_FILES
    missing = sorted(n for n in listed if not (TESTS / n).is_file())
    assert not missing, f"tests/markers.py lists files that do not exist: {missing}"


@pytest.mark.parametrize(("name", "expected"), [
    ("test_escrow_money_chain.py", {"regression", "unit", "money"}),
    ("test_health.py", {"regression", "integration", "smoke"}),
    ("test_trade_min.py", {"regression", "integration", "slow"}),
    ("test_brand_new_feature.py", {"regression", "unit"}),
])
def test_markers_by_file(name, expected):
    assert set(m.markers_for(TESTS / name)) == expected


def test_this_test_carries_its_markers(request):
    names = {mark.name for mark in request.node.iter_markers()}
    assert {"regression", "unit"} <= names and "integration" not in names


def test_markers_are_registered(pytestconfig):
    registered = " ".join(pytestconfig.getini("markers"))
    for name in m.MARKERS:
        assert f"{name}:" in registered
