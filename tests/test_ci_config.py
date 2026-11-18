"""CI and pre-commit configuration stay in place and pass locally."""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_backend_workflow_runs_the_gates():
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for step in ("pytest -q", "gen_unit_gherkin.py --check", "ruff check . --select E9,F63,F7,F82"):
        assert step in text


def test_ruff_errors_only_is_clean():
    """Syntax errors and undefined names (one was a missing logger in deals/router.py)."""
    ruff = shutil.which("ruff")
    cmd = [ruff] if ruff else [sys.executable, "-m", "ruff"]
    try:
        out = subprocess.run([*cmd, "check", ".", "--select", "E9,F63,F7,F82", "--quiet"], cwd=ROOT,
                             capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        pytest.skip("ruff not installed")
    if "No module named ruff" in out.stderr:
        pytest.skip("ruff not installed")
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.parametrize("path,refused", [
    (".env", True), ("services/x/.env", True), ("NGS-portless.creds", True),
    ("data/llm_gateway_tracking.txt", True), (".env.example", False), ("services/deals/service.py", False),
])
def test_precommit_refuses_secrets_and_the_tracking_file(path, refused):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import precommit_forbidden
    finally:
        sys.path.pop(0)
    assert precommit_forbidden.forbidden(path) is refused
    assert precommit_forbidden.main([path]) == (1 if refused else 0)


def test_security_job_scans_code_dependencies_and_secrets():
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for step in ("bandit -r services shared agents", "pip-audit", "gitleaks"):
        assert step in text


def test_post_deploy_smoke_and_nightly_workflows():
    smoke = (ROOT / ".github/workflows/post-deploy-smoke.yml").read_text(encoding="utf-8")
    assert "scripts/smoke_check.py" in smoke and "PORTLESS_SMOKE_TOKEN" in smoke
    nightly = (ROOT / ".github/workflows/nightly-automation.yml").read_text(encoding="utf-8")
    assert "scripts/nightly_automation.py" in nightly and "schedule" in nightly
