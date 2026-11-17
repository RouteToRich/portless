"""Source hygiene: no control characters in source files.

A backspace character (\\x08) inside a regex once made a committed assertion in
tests/test_trade_documents.py impossible to fail — it searched for "\\x08None\\x08"
instead of a word boundary (found 2026-11-17). Shell heredocs turn "\\b" into such a
character without anyone seeing it. Until CI exists this test runs with every pytest
run: tab, newline and carriage return are the only control characters allowed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("services", "shared", "agents", "tests", "scripts")
TOP_LEVEL = ("app.py", "CLAUDE.md", "todo")
SUFFIXES = {".py", ".j2", ".html", ".json", ".toml", ".md", ".yml", ".yaml", ".cfg", ".ini"}
ALLOWED = {"\t", "\n", "\r"}


def _source_files() -> list[Path]:
    files = [ROOT / name for name in TOP_LEVEL if (ROOT / name).is_file()]
    for d in SOURCE_DIRS:
        for p in (ROOT / d).rglob("*"):
            if p.is_file() and p.suffix in SUFFIXES and "__pycache__" not in p.parts:
                files.append(p)
    return sorted(files)


def control_characters(text: str) -> list[tuple[int, int, str]]:
    """(line, column, hex) of every control character other than tab / newline / CR, and DEL."""
    found: list[tuple[int, int, str]] = []
    for line_no, line in enumerate(text.splitlines(keepends=False), start=1):
        for col, ch in enumerate(line, start=1):
            if (ord(ch) < 32 and ch not in ALLOWED) or ord(ch) == 127:
                found.append((line_no, col, hex(ord(ch))))
    return found


def test_the_scan_covers_the_sources():
    files = _source_files()
    assert len(files) > 100
    assert ROOT / "tests" / "test_trade_documents.py" in files


def test_no_control_characters_in_source_files():
    offenders = []
    for path in _source_files():
        hits = control_characters(path.read_text(encoding="utf-8", errors="replace"))
        if hits:
            line, col, code = hits[0]
            offenders.append(f"{path.relative_to(ROOT)}:{line}:{col} {code} ({len(hits)} in file)")
    assert not offenders, "control characters in source files:\n" + "\n".join(offenders)


@pytest.mark.parametrize(("text", "expected"), [
    ('re.search(r"\\bNone\\b", html)', []),                      # a real word boundary: fine
    ('re.search(r"' + chr(8) + "None" + chr(8) + '", html)', [(1, 13, "0x8"), (1, 18, "0x8")]),
    ("a\tb\r\nc", []),
    ("x" + chr(127), [(1, 2, "0x7f")]),
    ("ok\n" + chr(27) + "[0m", [(2, 1, "0x1b")]),
])
def test_detection(text, expected):
    assert control_characters(text) == expected
