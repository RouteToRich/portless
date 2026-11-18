"""pre-commit hook: refuse files that must never be committed.

.env / *.creds hold secrets (also git-ignored, but `git add -f` gets past that), and
data/llm_gateway_tracking.txt changes on every local run.
"""
from __future__ import annotations

import sys
from pathlib import PurePosixPath

FORBIDDEN_NAMES = {".env"}
FORBIDDEN_SUFFIXES = {".creds"}
FORBIDDEN_PATHS = {"data/llm_gateway_tracking.txt"}


def forbidden(path: str) -> bool:
    p = PurePosixPath(path.replace("\\", "/"))
    return p.name in FORBIDDEN_NAMES or p.suffix in FORBIDDEN_SUFFIXES or str(p) in FORBIDDEN_PATHS


def main(paths: list[str]) -> int:
    bad = [p for p in paths if forbidden(p)]
    for p in bad:
        print(f"refused: {p} must never be committed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
