"""The application, serving ``portless_automation``, for the console's Automation mode.

Listens on localhost only; the main backend signs the operator in and forwards
their requests here. It starts from the same blanked environment as a run, so
nothing done on these pages can reach a real person or a paid service.
"""
from __future__ import annotations

import os
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from services.automation.safety import (
    REPO_ROOT, VIEW_PORT, NetworkGuard, assert_isolated, assert_on_automation_database, without_startup_resume,
)


def main() -> None:
    assert_isolated()
    NetworkGuard().install()
    sys.path.insert(0, str(REPO_ROOT))
    work = Path(tempfile.gettempdir()) / "portless-automation"
    work.mkdir(exist_ok=True)
    os.environ.setdefault("DEALMAKER_STORAGE_DIR", str(work / "dealmaker"))
    os.environ.setdefault("DOCUGEN_STORAGE_DIR", str(work / "docugen"))
    os.chdir(work)

    import uvicorn

    import app as portless_app

    without_startup_resume()
    application = portless_app.app
    original = application.router.lifespan_context

    @asynccontextmanager
    async def checked(app: Any) -> Any:
        async with original(app):
            assert_on_automation_database()
            yield

    application.router.lifespan_context = checked
    uvicorn.run(application, host="127.0.0.1", port=VIEW_PORT, log_level="warning")


if __name__ == "__main__":
    main()
