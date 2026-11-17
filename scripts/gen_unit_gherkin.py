"""Generate Gherkin for every unit test, grouped by the module it tests.

    python scripts/gen_unit_gherkin.py            # writes docs/testing/unit/*.feature + README.md
    python scripts/gen_unit_gherkin.py --check    # exit 1 when a collected test is missing

Each scenario is read from the test's own code (AST), so it stays true to what
the test does:

* Feature    — one per module under test (services/<x>, agents/<x>, shared/...);
               the module is the one the test file uses most.
* Rule       — the test file (its docstring), then the test class.
* Scenario   — one per test function: the docstring's first line, else the name.
               ``@pytest.mark.parametrize`` → Scenario Outline + Examples.
* Given      — fixtures and arrange statements before the action.
* When       — the last action before the first assertion (or the body of
               ``pytest.raises``).
* Then / And — the assertions, as written.

Every scenario carries the pytest node id, so it can be run directly:
``pytest "tests/test_x.py::TestY::test_z"``.
"""
from __future__ import annotations

import argparse
import ast
import collections
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
OUT = ROOT / "docs" / "testing" / "unit"
MAX_CODE = 160
MAX_GIVEN = 4
MAX_THEN = 6

# Fixture names that say nothing in a scenario (plumbing).
_PLUMBING_FIXTURES = {"self", "monkeypatch", "tmp_path", "caplog", "capsys", "request", "event_loop", "mocker"}


# ── helpers ──────────────────────────────────────────────────────────────────

def _code(node: ast.AST, limit: int = MAX_CODE) -> str:
    text = " ".join(ast.unparse(node).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _human(name: str) -> str:
    words = re.sub(r"^test_?", "", name).replace("__", "_").split("_")
    text = " ".join(w for w in words if w)
    return (text[:1].upper() + text[1:]) if text else name


def _doc_line(node: ast.AST) -> str | None:
    doc = ast.get_docstring(node)  # type: ignore[arg-type]
    if not doc:
        return None
    first = doc.strip().splitlines()[0].strip().rstrip(".")
    return first or None


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


# ── module of a test file ────────────────────────────────────────────────────

_APP_ROOTS = ("services", "agents", "shared")


def _module_of(import_path: str) -> str | None:
    """services.trade_min.service → services/trade_min; shared.orchestration.compliance →
    shared/orchestration/compliance; shared.connectors.apify → shared/connectors/apify."""
    parts = import_path.split(".")
    if not parts or parts[0] not in _APP_ROOTS and parts[0] != "app":
        return None
    if parts[0] == "app":
        return "app"
    if parts[0] in ("services", "agents"):
        return "/".join(parts[:2]) if len(parts) >= 2 else None
    # shared: keep the sub-package and the module (shared/connectors/apify)
    if len(parts) >= 3 and parts[1] in ("orchestration", "connectors", "documents", "domain", "bus", "auth",
                                         "llm_gateway", "clients"):
        if parts[1] in ("orchestration", "connectors"):
            return "/".join(parts[:3])
        return "/".join(parts[:2])
    return "/".join(parts[:2]) if len(parts) >= 2 else "shared"


def alias_modules(tree: ast.Module) -> dict[str, str]:
    """Imported name → module under test, for every import in the file (lazy ones too)."""
    alias_mod: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                mod = _module_of(f"{node.module}.{a.name}") or _module_of(node.module)
                if mod:
                    alias_mod[a.asname or a.name] = mod
        elif isinstance(node, ast.Import):
            for a in node.names:
                mod = _module_of(a.name)
                if mod:
                    alias_mod[(a.asname or a.name).split(".")[0]] = mod
    return alias_mod


def module_counts(node: ast.AST, alias_mod: dict[str, str]) -> collections.Counter[str]:
    """How often each module is used inside ``node`` (names, imports, patch targets)."""
    counts: collections.Counter[str] = collections.Counter()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id in alias_mod:
            counts[alias_mod[n.id]] += 1
        elif isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names:
                if mod := (_module_of(f"{n.module}.{a.name}") or _module_of(n.module)):
                    counts[mod] += 2
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            m = re.match(r"^/?((?:services|agents|shared)(?:[./]\w+)+)", n.value)
            if m and (mod := _module_of(m.group(1).replace("/", "."))):
                counts[mod] += 1
            elif (r := re.match(r"^/([a-z][a-z-]+)/", n.value)) and (svc := _ROUTE_PREFIX.get(r.group(1))):
                counts[svc] += 1  # an HTTP route of that service
    return counts


# URL prefix of each service's router → its module (route tests name only the path).
_ROUTE_PREFIX = {
    "deals": "services/deals", "dealmaker": "services/dealmaker", "docugen": "services/docugen",
    "escrow-guard": "services/escrow_guard", "hitl-approval": "services/hitl_approval",
    "shield-link": "services/shield_link", "sourcing": "services/sourcing", "trade-min": "services/trade_min",
    "negotiation-agent": "agents/negotiation_agent", "exception-resolver": "services/exception_resolver",
    "email-gateway": "services/email_gateway", "email-processor": "services/email_processor",
    "match-engine": "services/match_engine", "automation": "services/automation", "identity": "services/auth",
}


_SUPPORT_MODULES = frozenset({"shared/domain", "shared/config", "shared/integration_mode"})


def primary_module(tree: ast.Module) -> str:
    counts = module_counts(tree, alias_modules(tree))
    if any(m not in _SUPPORT_MODULES for m in counts):
        for m in _SUPPORT_MODULES:
            counts.pop(m, None)
    return counts.most_common(1)[0][0] if counts else "misc"


def test_module(fn: ast.AST, cls: ast.AST | None, alias_mod: dict[str, str], file_module: str) -> str:
    """The module one test exercises: the one its own body uses most, then its class, then the file's."""
    for scope in (fn, cls):
        if scope is None:
            continue
        counts = module_counts(scope, alias_mod)
        # Enums, events and settings are used by every test: they decide only when
        # nothing else is used.
        if any(m not in _SUPPORT_MODULES for m in counts):
            for m in _SUPPORT_MODULES:
                counts.pop(m, None)
        if counts:
            top = counts.most_common()
            best = top[0][1]
            tied = [m for m, c in top if c == best]
            return file_module if file_module in tied else tied[0]
    return file_module


# ── one test → one scenario ──────────────────────────────────────────────────

@dataclass
class Scenario:
    name: str
    node_id: str
    tags: list[str]
    given: list[str] = field(default_factory=list)
    when: list[str] = field(default_factory=list)
    then: list[str] = field(default_factory=list)
    examples: list[tuple[list[str], list[list[str]], list[str] | None]] = field(default_factory=list)


def _parametrize(decorators: list[ast.expr]) -> list[tuple[list[str], list[list[str]], list[str] | None]]:
    tables = []
    for d in decorators:
        if not (isinstance(d, ast.Call) and _code(d.func, 999).endswith("parametrize")) or len(d.args) < 2:
            continue
        names_node, values_node = d.args[0], d.args[1]
        if isinstance(names_node, ast.Constant) and isinstance(names_node.value, str):
            names = [n.strip() for n in names_node.value.split(",") if n.strip()]
        elif isinstance(names_node, (ast.Tuple, ast.List)):
            names = [n.value for n in names_node.elts if isinstance(n, ast.Constant)]
        else:
            names = [_code(names_node)]
        rows: list[list[str]] = []
        if isinstance(values_node, (ast.List, ast.Tuple, ast.Set)):
            for elt in values_node.elts:
                if isinstance(elt, ast.Call) and _code(elt.func, 99).endswith("param"):
                    elt = elt.args[0] if len(elt.args) == 1 else ast.Tuple(elts=list(elt.args), ctx=ast.Load())
                if len(names) > 1 and isinstance(elt, (ast.Tuple, ast.List)):
                    rows.append([_code(v, 80) for v in elt.elts])
                else:
                    rows.append([_code(elt, 120)])
        else:
            rows.append([f"<values from {_code(values_node, 80)}>"] + [""] * (len(names) - 1))
        ids = None
        for kw in d.keywords:
            if kw.arg == "ids" and isinstance(kw.value, (ast.List, ast.Tuple)):
                ids = [str(getattr(e, "value", _code(e, 40))) for e in kw.value.elts]
        tables.append((names, rows, ids))
    return tables


def _is_assert_stmt(stmt: ast.stmt) -> bool:
    if isinstance(stmt, ast.Assert):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, (ast.Call, ast.Await)):
        call = stmt.value.value if isinstance(stmt.value, ast.Await) else stmt.value
        if isinstance(call, ast.Call):
            name = _code(call.func, 99)
            return bool(re.search(r"(^|\.)assert_\w+$|(^|\.)(called_once_with|assert_called)", name))
    return False


def _raises(stmt: ast.stmt) -> tuple[str, list[ast.stmt]] | None:
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        for item in stmt.items:
            ctx = item.context_expr
            if isinstance(ctx, ast.Call) and _code(ctx.func, 99).endswith("raises"):
                exc = _code(ctx.args[0], 80) if ctx.args else "an error"
                match = next((kw for kw in ctx.keywords if kw.arg == "match"), None)
                return exc + (f" matching {_code(match.value, 60)}" if match else ""), stmt.body
    return None


def _assert_text(stmt: ast.stmt) -> str:
    if isinstance(stmt, ast.Assert):
        return _code(stmt.test)
    return _code(stmt)


def _expected_value(stmt: ast.stmt) -> bool:
    """``expected = …`` / ``landed = lc.landed(...)`` computed only to compare against."""
    if isinstance(stmt, ast.Assign):
        names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
        return any(n.startswith(("expected", "want", "exp_")) for n in names)
    return False


def _is_action(stmt: ast.stmt) -> bool:
    if any(isinstance(n, ast.Await) for n in ast.walk(stmt)):
        return True
    return isinstance(getattr(stmt, "value", None), ast.Call) and not _expected_value(stmt)


def _action_index(stmts: list[ast.stmt]) -> int:
    """The statement that exercises the code: the last awaited call, else the last
    call that is not just building an expected value, else the last statement."""
    for i in range(len(stmts) - 1, -1, -1):
        if any(isinstance(n, ast.Await) for n in ast.walk(stmts[i])):
            return i
    for i in range(len(stmts) - 1, -1, -1):
        s = stmts[i]
        value = getattr(s, "value", None)
        if isinstance(value, ast.Call) and not _expected_value(s):
            return i
    return len(stmts) - 1


def _steps(fn: ast.FunctionDef | ast.AsyncFunctionDef, param_names: set[str]) -> tuple[list[str], list[str], list[str]]:
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]  # docstring
    given: list[str] = []
    fixtures = [a.arg for a in fn.args.args if a.arg not in _PLUMBING_FIXTURES and a.arg not in param_names]
    for f in fixtures:
        given.append(f"the {f.replace('_', ' ')} fixture")

    when: list[str] = []
    then: list[str] = []
    arrange: list[ast.stmt] = []

    def visit(stmts: list[ast.stmt]) -> None:
        nonlocal arrange
        for stmt in stmts:
            r = _raises(stmt)
            if r:
                exc, inner = r
                acts = [s for s in inner if not _is_assert_stmt(s)]
                when.append(_code(acts[-1]) if acts else "the guarded call runs")
                then.append(f"it raises {exc}")
                then.extend(_assert_text(s) for s in inner if _is_assert_stmt(s))
                continue
            if _is_assert_stmt(stmt):
                if arrange:
                    # the action under test; the other lines are setup or expected values.
                    # Between assertions only a real call is a further action.
                    if when and not any(_is_action(s) for s in arrange):
                        given.extend(_code(s) for s in arrange)
                    else:
                        i = _action_index(arrange)
                        when.append(_code(arrange[i]))
                        given.extend(_code(s) for j, s in enumerate(arrange) if j != i)
                arrange = []
                then.append(_assert_text(stmt))
                continue
            if isinstance(stmt, (ast.With, ast.AsyncWith)) and not _raises(stmt):
                given.append(f"within {', '.join(_code(i.context_expr, 90) for i in stmt.items)}")
                visit(stmt.body)
                continue
            if isinstance(stmt, (ast.For, ast.AsyncFor)) and any(_is_assert_stmt(s) for s in ast.walk(stmt) if isinstance(s, ast.stmt)):
                when.append(f"for {_code(stmt.target, 40)} in {_code(stmt.iter, 80)}")
                for s in stmt.body:
                    if _is_assert_stmt(s):
                        then.append(_assert_text(s))
                continue
            arrange.append(stmt)
        if arrange:
            i = _action_index(arrange)
            when.append(_code(arrange[i]))
            given.extend(_code(s) for j, s in enumerate(arrange) if j != i)
            arrange = []

    visit(body)
    if len(given) > MAX_GIVEN + len(fixtures):
        extra = len(given) - (MAX_GIVEN + len(fixtures))
        given = given[: MAX_GIVEN + len(fixtures)] + [f"… {extra} more setup step(s)"]
    if len(then) > MAX_THEN:
        extra = len(then) - MAX_THEN
        then = then[:MAX_THEN] + [f"… {extra} more assertion(s)"]
    if not when:
        when.append("the test body runs")
    if not then:
        then.append("no exception is raised")
    return given, when[:3], then


def scenarios_of(path: Path, tree: ast.Module) -> list[tuple[str, str, str | None, Scenario]]:
    """(module, rule, rule description, scenario) per test function."""
    out: list[tuple[str, str, str | None, Scenario]] = []
    rel = path.relative_to(ROOT).as_posix()
    alias_mod = alias_modules(tree)
    file_mod = primary_module(tree)

    def handle(fn: ast.FunctionDef | ast.AsyncFunctionDef, cls: ast.ClassDef | None) -> None:
        tables = _parametrize(list(fn.decorator_list) + (list(cls.decorator_list) if cls else []))
        pnames = {n for names, _, _ in tables for n in names}
        given, when, then = _steps(fn, pnames)
        tags = ["@unit", f"@{path.stem.removeprefix('test_')}"]
        if isinstance(fn, ast.AsyncFunctionDef):
            tags.append("@async")
        for d in fn.decorator_list:
            text = _code(d, 99)
            if text.startswith("pytest.mark.") and "parametrize" not in text:
                tags.append("@" + text.removeprefix("pytest.mark.").split("(")[0])
        node = f"{rel}::{cls.name}::{fn.name}" if cls else f"{rel}::{fn.name}"
        sc = Scenario(name=_doc_line(fn) or _human(fn.name), node_id=node, tags=tags,
                      given=given, when=when, then=then, examples=tables)
        rule = cls.name if cls else "Module-level tests"
        out.append((test_module(fn, cls, alias_mod, file_mod), rule, _doc_line(cls) if cls else None, sc))

    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name.startswith("test"):
            handle(stmt, None)
        elif isinstance(stmt, ast.ClassDef) and stmt.name.startswith("Test"):
            for s in stmt.body:
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name.startswith("test"):
                    handle(s, stmt)
    return out


# ── rendering ────────────────────────────────────────────────────────────────

def _render_scenario(sc: Scenario, indent: str) -> list[str]:
    lines = [f"{indent}{' '.join(sc.tags)}"]
    kw = "Scenario Outline" if sc.examples else "Scenario"
    lines.append(f"{indent}{kw}: {sc.name}")
    lines.append(f"{indent}  # {sc.node_id}")
    first = True
    for keyword, steps in (("Given", sc.given), ("When", sc.when), ("Then", sc.then)):
        for i, step in enumerate(steps):
            lines.append(f"{indent}  {keyword if i == 0 else 'And'} {step}")
        first = False
    for names, rows, ids in sc.examples:
        lines.append("")
        lines.append(f"{indent}  Examples:")
        header = (["id"] if ids else []) + names
        lines.append(f"{indent}    | " + " | ".join(_cell(h) for h in header) + " |")
        for i, row in enumerate(rows):
            cells = row + [""] * (len(names) - len(row))
            if ids:
                cells = [ids[i] if i < len(ids) else ""] + cells
            lines.append(f"{indent}    | " + " | ".join(_cell(c) for c in cells) + " |")
    lines.append("")
    return lines


def _expanded(sc: Scenario) -> int:
    n = 1
    for _, rows, _ in sc.examples:
        n *= max(1, len(rows))
    return n


def build() -> dict[str, dict]:
    modules: dict[str, dict] = {}
    for path in sorted(TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        per_module: dict[str, list[tuple[str, str | None, Scenario]]] = collections.OrderedDict()
        for mod, rule, rdoc, sc in scenarios_of(path, tree):
            per_module.setdefault(mod, []).append((rule, rdoc, sc))
        for mod, items in per_module.items():
            entry = modules.setdefault(mod, {"files": [], "scenarios": 0, "cases": 0})
            entry["files"].append((path, ast.get_docstring(tree), items))
            entry["scenarios"] += len(items)
            entry["cases"] += sum(_expanded(sc) for _, _, sc in items)
    return modules


def write(modules: dict[str, dict]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.feature"):
        old.unlink()
    index = ["# Unit test cases in Gherkin",
             "",
             "Generated from the test code by `python scripts/gen_unit_gherkin.py` — do not edit by hand;",
             "regenerate after changing tests. Each scenario carries its pytest node id",
             "(`pytest \"<node id>\"` runs it). Given = fixtures and setup, When = the action under test,",
             "Then = the test's own assertions. Parametrized tests are Scenario Outlines; the *cases*",
             "column counts each Examples row (fixture parameters such as memory / Mongo backends",
             "multiply the collected total further).",
             "",
             "Business-level scenarios (what the platform does, end to end) are in",
             "[../TEST_CASES.md](../TEST_CASES.md); these are the unit-level cases per module.",
             "",
             "| Module | Feature file | Test files | Scenarios | Cases |",
             "|---|---|---|---:|---:|"]
    tot_s = tot_c = 0
    for mod in sorted(modules):
        entry = modules[mod]
        fname = mod.replace("/", "__") + ".feature"
        lines = [f"# Generated by scripts/gen_unit_gherkin.py from the unit tests of {mod}. Do not edit by hand.",
                 f"@module:{mod}",
                 f"Feature: {mod} — unit tests",
                 f"  {entry['scenarios']} scenarios ({entry['cases']} cases) from "
                 f"{len(entry['files'])} test file(s).",
                 ""]
        for path, doc, items in entry["files"]:
            rel = path.relative_to(ROOT).as_posix()
            by_rule: dict[str, list[tuple[str | None, Scenario]]] = collections.OrderedDict()
            for rule, rdoc, sc in items:
                by_rule.setdefault(rule, []).append((rdoc, sc))
            file_title = (doc.strip().splitlines()[0].strip() if doc else rel)
            for rule, scs in by_rule.items():
                rdoc = scs[0][0]
                title = f"{rel} — {rule}" if rule != "Module-level tests" else f"{rel}"
                lines.append(f"  Rule: {title}")
                lines.append(f"    {rdoc or file_title}")
                lines.append("")
                for _, sc in scs:
                    lines.extend(_render_scenario(sc, "    "))
        (OUT / fname).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        files = ", ".join(f"`{p.name}`" for p, _, _ in entry["files"])
        index.append(f"| `{mod}` | [{fname}]({fname}) | {files} | {entry['scenarios']} | {entry['cases']} |")
        tot_s += entry["scenarios"]
        tot_c += entry["cases"]
    index.append(f"| **Total** | | | **{tot_s}** | **{tot_c}** |")
    (OUT / "README.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    print(f"{len(modules)} modules, {tot_s} scenarios, {tot_c} cases → {OUT.relative_to(ROOT)}")


def check(modules: dict[str, dict]) -> int:
    """Every collected test function has a scenario."""
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
                         cwd=ROOT, capture_output=True, text=True).stdout
    collected = {re.sub(r"\[.*\]$", "", line.strip()) for line in out.splitlines() if "::" in line}
    documented = {sc.node_id for e in modules.values() for _, _, items in e["files"] for _, _, sc in items}
    missing = sorted(collected - documented)
    print(f"collected functions: {len(collected)}, documented: {len(documented)}, missing: {len(missing)}")
    for m in missing[:20]:
        print("  missing:", m)
    return 1 if missing else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    modules = build()
    if args.check:
        return check(modules)
    write(modules)
    return check(modules)


if __name__ == "__main__":
    sys.exit(main())
