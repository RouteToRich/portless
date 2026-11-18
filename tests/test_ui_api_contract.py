"""Every backend path the console calls exists (todo: UI ↔ backend API contract).

Reads ``../portless-ui/src/api/*.ts`` for ``api.get|post|put|patch|delete("…")`` calls,
turns each ``${…}`` into a path parameter and checks a backend route with that method and
shape exists in the monolith. A renamed or removed route then fails here instead of on a page.
Skipped when the console repo is not next to this one (the backend CI checks out only itself).
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
UI_API = ROOT.parent / "portless-ui" / "src" / "api"
_CALL = re.compile(r"\bapi\s*\.\s*(get|post|put|patch|delete)\s*(?:<[^()]*?>)?\s*\(\s*([`'\"])(.+?)\2", re.S)
# Paths built from a variable (not a literal) cannot be checked here.
_DYNAMIC_ONLY = re.compile(r"^\$\{[^}]+\}$")


def _ui_calls() -> set[tuple[str, str]]:
    calls: set[tuple[str, str]] = set()
    for f in UI_API.glob("*.ts"):
        text = f.read_text(encoding="utf-8")
        for m in _CALL.finditer(text):
            method, raw = m.group(1).upper(), m.group(3)
            path = raw.split("?")[0]
            if _DYNAMIC_ONLY.match(path) or not path.startswith(("/", "$")):
                continue
            path = re.sub(r"\$\{[^}]+\}", "{p}", path)
            if path.startswith("{p}"):
                continue  # base chosen at run time (automation view prefix etc.)
            calls.add((method, path.rstrip("/") or "/"))
    return calls


def _backend_routes() -> list[tuple[set[str], re.Pattern[str]]]:
    from app import app
    out = []
    for r in app.routes:
        methods = getattr(r, "methods", None)
        path = getattr(r, "path", None)
        if not methods or not path:
            continue
        pattern = re.sub(r"\{[^}:]+:path\}", ".+", path)
        pattern = re.sub(r"\{[^}]+\}", "[^/]+", pattern)
        out.append((set(methods), re.compile("^" + pattern.rstrip("/") + "/?$")))
    return out


@pytest.mark.skipif(not UI_API.is_dir(), reason="portless-ui is not checked out next to the backend")
def test_every_console_call_has_a_backend_route():
    routes = _backend_routes()
    calls = _ui_calls()
    assert len(calls) > 50  # the parser still finds the console's calls
    missing = []
    for method, path in sorted(calls):
        probe = path.replace("{p}", "X1")
        if not any(method in m and rx.match(probe) for m, rx in routes):
            missing.append(f"{method} {path}")
    assert missing == [], "console calls with no backend route:\n" + "\n".join(missing)
