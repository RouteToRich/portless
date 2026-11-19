"""Settings parity between environments (todo 6.13: dev, staging and production use the same
settings schema).

    python scripts/env_parity.py dev=.env staging=staging.env production=prod.env
    python scripts/env_parity.py production=-          # read KEY=VALUE lines from stdin

The schema is ``.env.example``. For every environment file the report lists
- **missing**: schema keys the environment does not set (it runs on the default — fine for an
  optional key, but every environment should make the same choice);
- **unknown**: keys not in the schema (a typo, or a setting nobody documented);
- **placeholders**: values still ``your_..._here``;
- **sandbox in production**: ``INTEGRATION_MODE=sandbox`` or a ``*_SANDBOX_PROVIDERS`` /
  ``*_SANDBOX_URL`` key set in an environment named ``production``;
- **differs**: keys set in some environments and not in others (the parity gap itself).

Values are never printed — only key names. Exit 0 when production has no placeholder, no sandbox
setting and no key the other environments set that it lacks; else 1.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*)$")
ROOT = Path(__file__).resolve().parent.parent


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def schema_keys(example: str) -> set[str]:
    """Keys of .env.example, including commented-out optional ones (``# KEY=``)."""
    keys = set(parse(example))
    for line in example.splitlines():
        m = re.match(r"^\s*#\s*([A-Z][A-Z0-9_]{2,})\s*=", line)
        if m:
            keys.add(m.group(1))
    return keys


def is_placeholder(value: str) -> bool:
    v = value.strip().lower()
    return v.startswith("your_") or v.endswith("_here") or v in ("changeme", "todo", "xxx")


def report(schema: set[str], envs: dict[str, dict[str, str]]) -> dict[str, dict[str, list[str]]]:
    all_set = set().union(*(set(e) for e in envs.values())) if envs else set()
    out: dict[str, dict[str, list[str]]] = {}
    for name, env in envs.items():
        keys = set(env)
        sandbox = []
        if name.lower().startswith("prod"):
            sandbox = sorted(k for k, v in env.items()
                             if (k == "INTEGRATION_MODE" and v.lower() == "sandbox")
                             or (re.search(r"_SANDBOX_(PROVIDERS|URL)$", k) and v))
        out[name] = {
            "missing": sorted(schema - keys),
            "unknown": sorted(keys - schema),
            "placeholders": sorted(k for k, v in env.items() if v and is_placeholder(v)),
            "sandbox_in_production": sandbox,
            "differs": sorted(all_set - keys),
        }
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    schema = schema_keys((ROOT / ".env.example").read_text(encoding="utf-8"))
    envs: dict[str, dict[str, str]] = {}
    for arg in argv:
        name, _, path = arg.partition("=")
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        envs[name or path] = parse(text)
    rep = report(schema, envs)
    failed = False
    for name, r in rep.items():
        print(f"== {name}")
        for kind in ("placeholders", "sandbox_in_production", "differs", "unknown"):
            if r[kind]:
                print(f"  {kind} ({len(r[kind])}): {', '.join(r[kind][:40])}")
        print(f"  missing from schema defaults: {len(r['missing'])}")
        if name.lower().startswith("prod") and (r["placeholders"] or r["sandbox_in_production"] or r["differs"]):
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
