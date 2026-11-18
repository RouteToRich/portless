"""The counts at the top of `todo` match the list (scripts/todo_metrics.py)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("todo_metrics", ROOT / "scripts" / "todo_metrics.py")
tm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tm)  # type: ignore[union-attr]

SAMPLE = """# T

intro

---

## Where things stand today
- note

## P0 — must be done
- [ ] **(Eng)** a
  - [ ] **(Ops) (Legal)** b
- [x] **(Eng)** c

## P1 — later
- [X] d
"""


def test_the_block_at_the_top_of_todo_is_up_to_date():
    text = (ROOT / "todo").read_text(encoding="utf-8")
    assert tm.START in text, "run: python -X utf8 scripts/todo_metrics.py"
    assert text.index(tm.START) < text.index("## Where things stand today")
    assert tm.up_to_date(text), "todo counts are stale: run python -X utf8 scripts/todo_metrics.py"


def test_counts_per_section_and_owner():
    sections, owners = tm.count(SAMPLE)
    assert sections == [("P0 — before the first real trade", 2, 1), ("P1 — before more than a few deals a month", 0, 1)]
    assert owners == {"Eng": 1, "Apify": 0, "Ops": 1, "Legal": 1, "Biz": 0, "QA": 0}


def test_render_inserts_once_then_replaces():
    once = tm.render(SAMPLE, "2026-11-18")
    assert once.count(tm.START) == 1 and "**2 open · 2 done** (50 % done)" in once
    assert tm.up_to_date(once)
    changed = once.replace("- [ ] **(Eng)** a", "- [x] **(Eng)** a")
    assert not tm.up_to_date(changed)
    again = tm.render(changed, "2026-11-19")
    assert again.count(tm.START) == 1 and "**1 open · 3 done**" in again
    assert "2026-11-19" in again and "2026-11-18" not in again
