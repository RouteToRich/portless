"""Append the review board's accepted findings to `todo`.

    python -X utf8 .claude/skills/portless-review-board/scripts/append_findings.py docs/reviews/<date>-board/judged.json [--dry-run]

Each accepted finding becomes one open item in its priority section (P0 / P1 / P2), under a
heading ``### 22.2 Review-board findings (<date>) — <section>``:

    - [ ] **(Eng) P1** **<title>** — <fix> Why: <why_money>. Evidence: <evidence>. (review-board B-07)

A finding whose title (normalised) already matches an open or done item — or an earlier
review-board id — is skipped. The to-do metrics block is recounted afterwards.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
TODO = ROOT / "todo"
SECTIONS = {"P0": "## P0", "P1": "## P1", "P2": "## P2"}
OWNERS = {"Eng", "Ops", "Legal", "Biz", "QA"}


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


def _known(todo: str) -> tuple[set[str], set[str]]:
    titles, ids = set(), set()
    for line in todo.splitlines():
        if not re.match(r"^\s*- \[[ xX]\]", line):
            continue
        for seg in re.findall(r"\*\*([^*]+)\*\*", line):
            if len(seg) >= 8 and not seg.startswith("("):
                titles.add(_norm(seg))
        for bid in re.findall(r"review-board (B-\d+)", line):
            ids.add(bid)
    return titles, ids


def _item(f: dict, date: str) -> str:
    owner = f.get("owner") if f.get("owner") in OWNERS else "Eng"
    pr = f.get("priority") if f.get("priority") in SECTIONS else "P1"
    parts = [f"- [ ] **({owner}) {pr}** **{f['title'].strip().rstrip('.')}**"]
    if f.get("fix"):
        parts.append(f"— {f['fix'].strip().rstrip('.')}.")
    if f.get("why_money"):
        parts.append(f"Why: {f['why_money'].strip().rstrip('.')}.")
    if f.get("evidence"):
        parts.append(f"Evidence: {f['evidence'].strip().rstrip('.')}.")
    parts.append(f"(review-board {f.get('id', '?')} {date})")
    return " ".join(parts)


def append(todo: str, judged: dict) -> tuple[str, list[str], list[str]]:
    """(new todo text, added ids, skipped ids)."""
    date = judged.get("date", "")
    titles, ids = _known(todo)
    by_section: dict[str, list[str]] = {}
    added, skipped = [], []
    for f in judged.get("findings", []):
        if f.get("status") != "accepted" or not f.get("title"):
            continue
        if f.get("id") in ids or _norm(f["title"]) in titles:
            skipped.append(f.get("id", "?"))
            continue
        titles.add(_norm(f["title"]))
        pr = f.get("priority") if f.get("priority") in SECTIONS else "P1"
        by_section.setdefault(pr, []).append(_item(f, date))
        added.append(f.get("id", "?"))
    for pr, items in by_section.items():
        head = SECTIONS[pr]
        start = next((m.start() for m in re.finditer(rf"^{re.escape(head)}\b", todo, re.M)), None)
        if start is None:
            todo = todo.rstrip("\n") + f"\n\n{head}\n"
            start = todo.rfind(head)
        nxt = re.search(r"^## ", todo[start + 3:], re.M)
        end = start + 3 + nxt.start() if nxt else len(todo)
        block = f"### 22.2 Review-board findings ({date}) — {pr}\n" + "\n".join(items) + "\n\n"
        body = todo[start:end].rstrip("\n")
        if body.endswith("---"):
            body = body[:-3].rstrip("\n")
            todo = todo[:start] + body + "\n\n" + block + "---\n\n" + todo[end:]
        else:
            todo = todo[:start] + body + "\n\n" + block + todo[end:]
    return todo, added, skipped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("judged")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--todo", default=str(TODO))
    args = ap.parse_args(argv)
    judged = json.loads(Path(args.judged).read_text(encoding="utf-8"))
    path = Path(args.todo)
    new, added, skipped = append(path.read_text(encoding="utf-8"), judged)
    if not args.dry_run:
        path.write_text(new, encoding="utf-8")
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import todo_metrics  # type: ignore[import]
            path.write_text(todo_metrics.render(path.read_text(encoding="utf-8"), todo_metrics.date.today().isoformat()),
                            encoding="utf-8")
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - the findings are written either way
            print(f"metrics not recounted: {exc} — run scripts/todo_metrics.py", file=sys.stderr)
    print(f"added {len(added)}: {', '.join(added) or '-'} · skipped (already listed) {len(skipped)}: {', '.join(skipped) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
