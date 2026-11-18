"""Count the to-do list and write the counts at the top of `todo`.

    python -X utf8 scripts/todo_metrics.py          # rewrite the block
    python -X utf8 scripts/todo_metrics.py --check  # exit 1 when the block is out of date

An item is a line starting with "- [ ]" (open) or "- [x]" (done), at any indent. Items are
counted per "## " section, and the open ones per owner tag: (Eng), (Apify), (Ops), (Legal), (Biz), (QA).
The block sits between the two marker comments and is replaced on every run.
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

TODO = Path(__file__).resolve().parents[1] / "todo"
START = "<!-- todo-metrics:start -->"
END = "<!-- todo-metrics:end -->"
OPEN = re.compile(r"^\s*- \[ \]")
DONE = re.compile(r"^\s*- \[[xX]\]")
OWNERS = ("Eng", "Apify", "Ops", "Legal", "Biz", "QA")
SHORT = {  # section heading prefix -> label in the table
    "Where things stand": "Where things stand",
    "Go-live blockers": "Go-live blockers",
    "Owner's parallel track": "Owner's track (accounts, keys)",
    "P0": "P0 — before the first real trade",
    "P1": "P1 — before more than a few deals a month",
    "P2": "P2 — improvements",
    "Apify actors": "Apify actors (outside this repo)",
    "Decisions needed": "Decisions needed",
    "Done in the latest rounds": "Done in the latest rounds",
    "Appendix": "Appendix",
}


def _label(heading: str) -> str:
    for prefix, label in SHORT.items():
        if heading.startswith(prefix):
            return label
    return heading


def count(text: str) -> tuple[list[tuple[str, int, int]], dict[str, int]]:
    body = re.sub(re.escape(START) + r".*?" + re.escape(END), "", text, flags=re.S)
    sections: list[tuple[str, int, int]] = []
    owners = {o: 0 for o in OWNERS}
    current = None
    for line in body.splitlines():
        if line.startswith("## "):
            current = [_label(line[3:].strip()), 0, 0]
            sections.append(current)  # type: ignore[arg-type]
            continue
        if current is None:
            continue
        if OPEN.match(line):
            current[1] += 1
            for o in OWNERS:
                if f"({o})" in line:
                    owners[o] += 1
        elif DONE.match(line):
            current[2] += 1
    return [tuple(s) for s in sections if s[1] or s[2]], owners  # type: ignore[misc]


def block(text: str, today: str) -> str:
    sections, owners = count(text)
    total_open = sum(s[1] for s in sections)
    total_done = sum(s[2] for s in sections)
    pct = round(100 * total_done / (total_open + total_done)) if total_open + total_done else 0
    rows = "\n".join(f"| {name} | {o} | {d} |" for name, o, d in sections)
    owner_line = " · ".join(f"{o} {n}" for o, n in owners.items())
    return (
        f"{START}\n"
        f"## To-do at a glance ({today})\n\n"
        f"**{total_open} open · {total_done} done** ({pct} % done).\n\n"
        "| Section | Open | Done |\n|---|---:|---:|\n"
        f"{rows}\n"
        f"| **Total** | **{total_open}** | **{total_done}** |\n\n"
        f"Open items by owner: {owner_line} (untagged items are not counted; an item can name more than one owner).\n"
        "Recount: `python -X utf8 scripts/todo_metrics.py` (the test suite fails when this block is out of date).\n"
        f"{END}"
    )


def _today_in(text: str) -> str:
    m = re.search(re.escape(START) + r"\n## To-do at a glance \((\d{4}-\d{2}-\d{2})\)", text)
    return m.group(1) if m else date.today().isoformat()


def render(text: str, today: str) -> str:
    new = block(text, today)
    if START in text:
        return re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _: new, text, count=1, flags=re.S)
    anchor = "\n## Where things stand today"
    if anchor not in text:
        raise SystemExit("anchor '## Where things stand today' not found in todo")
    return text.replace(anchor, "\n" + new + "\n\n---\n" + anchor, 1)


def up_to_date(text: str) -> bool:
    """True when the block's counts match the list (the date is not compared)."""
    return START in text and render(text, _today_in(text)) == text


def main(argv: list[str]) -> int:
    text = TODO.read_text(encoding="utf-8")
    if "--check" in argv:
        ok = up_to_date(text)
        print("todo metrics up to date" if ok else "todo metrics out of date: run scripts/todo_metrics.py")
        return 0 if ok else 1
    TODO.write_text(render(text, date.today().isoformat()), encoding="utf-8")
    sections, _ = count(text)
    print(f"open {sum(s[1] for s in sections)}, done {sum(s[2] for s in sections)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
