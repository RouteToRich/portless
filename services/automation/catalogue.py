"""Reads docs/testing/TEST_CASES.md into test cases and their iterations."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from .safety import REPO_ROOT

CATALOGUE_PATH = REPO_ROOT / "docs" / "testing" / "TEST_CASES.md"
_KEYWORDS = ("Given ", "When ", "Then ", "And ", "But ")


@dataclass
class Step:
    keyword: str
    text: str
    doc: str | None = None
    table: list[list[str]] | None = None

    def filled(self, row: dict[str, str]) -> "Step":
        fill = lambda s: _fill(s, row)  # noqa: E731
        return Step(self.keyword, fill(self.text), fill(self.doc) if self.doc is not None else None, self.table)


@dataclass
class Case:
    tc_id: str
    title: str
    feature: str
    phase: str
    layer: str
    priority: str
    kind: str
    tags: list[str]
    steps: list[Step]
    rows: list[dict[str, str]] = field(default_factory=list)

    def iterations(self) -> list[dict[str, str]]:
        return self.rows or [{}]

    def to_doc(self) -> dict[str, Any]:
        return {
            "_id": self.tc_id, "title": self.title, "feature": self.feature, "phase": self.phase, "layer": self.layer,
            "priority": self.priority, "kind": self.kind, "tags": self.tags,
            "iterations": len(self.iterations()), "steps": [f"{s.keyword} {s.text}" for s in self.steps],
        }


def _fill(text: str, row: dict[str, str]) -> str:
    for key, value in row.items():
        text = text.replace(f"<{key}>", value)
    return text


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _tag_value(tags: list[str], prefix: str, default: str) -> str:
    return next((t[len(prefix):] for t in tags if t.startswith(prefix)), default)


def _kind(tags: list[str]) -> str:
    return next((k for k in ("positive", "negative", "exception", "edge", "compliance") if f"@{k}" in tags), "other")


def _steps(lines: list[str], start: int) -> tuple[list[Step], int]:
    """Steps from ``start`` until the next tag, scenario or Examples line."""
    steps: list[Step] = []
    i = start
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("@") or s.startswith(("Scenario", "Examples:")):
            break
        keyword = next((k for k in _KEYWORDS if s.startswith(k)), None)
        if keyword:
            steps.append(Step(keyword.strip(), s[len(keyword):].strip()))
        elif s.startswith('"""'):
            i += 1
            body: list[str] = []
            while lines[i].strip() != '"""':
                body.append(lines[i].strip())
                i += 1
            steps[-1].doc = "\n".join(body)
        elif s.startswith("|") and steps:
            steps[-1].table = (steps[-1].table or []) + [_cells(s)]
        i += 1
    return steps, i


def _feature_cases(block: str, phase_heading: str) -> list[Case]:
    lines = block.splitlines()
    feature_tags: list[str] = []
    i = 0
    while not lines[i].startswith("Feature:"):
        feature_tags += lines[i].split()
        i += 1
    feature = lines[i][len("Feature:"):].strip()
    i += 1
    background: list[Step] = []
    cases: list[Case] = []
    tags: list[str] = []
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("Background:"):
            background, i = _steps(lines, i + 1)
            continue
        if s.startswith("@"):
            tags = s.split()
        elif s.startswith("Scenario"):
            title = s.split(":", 1)[1].strip()
            steps, i = _steps(lines, i + 1)
            rows: list[dict[str, str]] = []
            if i < len(lines) and lines[i].strip().startswith("Examples:"):
                i += 1
                header = None
                while i < len(lines) and lines[i].strip().startswith("|"):
                    cells = _cells(lines[i])
                    if header is None:
                        header = cells
                    else:
                        rows.append(dict(zip(header, cells)))
                    i += 1
            all_tags = feature_tags + tags
            tc_id = next((t[1:] for t in tags if t.startswith("@TC-")), f"TC-UNKNOWN-{len(cases)}")
            layer = next((x for x in ("ui", "e2e", "flow", "api") if f"@{x}" in all_tags), "api")
            cases.append(Case(
                tc_id=tc_id, title=title, feature=feature,
                phase=_tag_value(all_tags, "@phase:", phase_heading.lower()),
                layer=layer, priority=next((t[1:] for t in tags if re.fullmatch(r"@P\d", t)), "P2"),
                kind=_kind(tags), tags=all_tags, steps=background + steps, rows=rows,
            ))
            tags = []
            continue
        i += 1
    return cases


@lru_cache(maxsize=1)
def load() -> tuple[str, tuple[Case, ...]]:
    """(content sha, every case in the catalogue)."""
    text = CATALOGUE_PATH.read_text(encoding="utf-8")
    cases: list[Case] = []
    heading = ""
    for part in re.split(r"(```gherkin\n.*?```)", text, flags=re.S):
        if part.startswith("```gherkin"):
            cases += _feature_cases(part[len("```gherkin\n"):-3], heading)
        else:
            found = re.findall(r"^#{2,3} (.+)$", part, re.M)
            if found:
                heading = found[-1]
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12], tuple(cases)
