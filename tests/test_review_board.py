"""The review-board skill: briefs exist and accepted findings land in `todo` once
(.claude/skills/portless-review-board)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "portless-review-board"
_spec = importlib.util.spec_from_file_location("append_findings", SKILL / "scripts" / "append_findings.py")
af = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(af)

TODO = """# PortLess

## P0 — must be done
- [ ] **(Eng)** **Escrow watcher alert** — something.

---

## P1 — needed before more deals
- [x] **(Eng)** **Funnel analytics** — done.

---

## P2 — improvements
- [ ] **(Eng)** Something small.
"""


def test_the_skill_has_its_members_and_judge():
    assert (SKILL / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: portless-review-board")
    members = {p.stem for p in (SKILL / "members").glob("*.md")}
    assert members == {"trader", "cfo", "compliance", "operations", "counterparty", "ceo", "engineer", "operator"}
    for m in members:
        text = (SKILL / "members" / f"{m}.md").read_text(encoding="utf-8")
        assert "**Checklist**" in text and "## Learned" in text and f'"member": "{m}"' in text
    assert "judged.json" in (SKILL / "judge.md").read_text(encoding="utf-8")


def _judged():
    return {"date": "2026-11-19", "findings": [
        {"id": "B-01", "status": "accepted", "title": "Supplier payout fee not reconciled", "priority": "P0", "owner": "Eng",
         "fix": "Compare the Payoneer fee with the assumed %", "why_money": "a hidden fee eats the margin",
         "evidence": "services/escrow_guard/service.py:700"},
        {"id": "B-02", "status": "accepted", "title": "Funnel analytics", "priority": "P1"},          # already done in todo
        {"id": "B-03", "status": "rejected", "title": "Nice colours", "reason": "no money"},
        {"id": "B-04", "status": "accepted", "title": "Buyer reply rate per corridor on the radar", "priority": "P2",
         "owner": "Biz"},
        {"id": "B-05", "status": "accepted", "title": "Escrow watcher alert", "priority": "P1"},      # open in todo
    ]}


def test_accepted_findings_go_to_their_section_once():
    new, added, skipped = af.append(TODO, _judged())
    assert added == ["B-01", "B-04"] and skipped == ["B-02", "B-05"]
    p0 = new.index("## P0"); p1 = new.index("## P1"); p2 = new.index("## P2")
    b01 = new.index("**Supplier payout fee not reconciled**")
    b04 = new.index("**Buyer reply rate per corridor on the radar**")
    assert p0 < b01 < p1 and p2 < b04
    assert "- [ ] **(Eng) P0** **Supplier payout fee not reconciled** — Compare the Payoneer fee with the assumed %. " \
           "Why: a hidden fee eats the margin. Evidence: services/escrow_guard/service.py:700. (review-board B-01 2026-11-19)" in new
    assert "### 22.2 Review-board findings (2026-11-19) — P0" in new
    assert new.index("### 22.2 Review-board findings (2026-11-19) — P0") < new.index("---")  # stays inside P0
    again, added2, skipped2 = af.append(new, _judged())
    assert added2 == [] and again == new


def test_the_cli_writes_and_recounts(tmp_path):
    todo = tmp_path / "todo"
    todo.write_text(TODO, encoding="utf-8")
    judged = tmp_path / "judged.json"
    judged.write_text(json.dumps(_judged()), encoding="utf-8")
    assert af.main([str(judged), "--todo", str(todo)]) == 0
    text = todo.read_text(encoding="utf-8")
    assert "(review-board B-01 2026-11-19)" in text  # metrics recount needs the real list's layout
