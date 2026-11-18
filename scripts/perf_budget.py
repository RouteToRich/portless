"""Response-time budgets for the key endpoints (todo: performance regression, checked in the nightly run).

  python scripts/perf_budget.py --base https://backend.example --token $TOKEN [--runs 5]

Each endpoint is called ``--runs`` times (after one warm-up); the median must stay under its
budget in ``BUDGETS_MS`` (``PERF_BUDGET_SCALE`` multiplies them, e.g. 2 on a cold free tier).
Read-only GETs only — nothing is written. Exit 0 within budget, 1 over budget, 2 unreachable.
Prints one line per endpoint and a JSON summary on the last line.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from typing import Any

import httpx

BUDGETS_MS: dict[str, float] = {
    "/health": 300,
    "/deals?limit=100": 1500,
    "/deals/board/money": 2500,
    "/deals/prospects/desk": 4000,
    "/hitl-approval/approvals?status=PENDING": 1200,
    "/escrow-guard/ledgers": 2500,
    "/trade-min/buyers?limit=50": 3000,
    "/trade-min/opportunities?limit=50": 3000,
    "/health/watchers": 800,
}


async def measure(base: str, token: str | None, *, runs: int = 5, scale: float = 1.0,
                  transport: Any = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    rows, over, failed = [], [], []
    async with httpx.AsyncClient(base_url=base.rstrip("/"), headers=headers, timeout=30.0, transport=transport) as c:
        for path, budget in BUDGETS_MS.items():
            budget *= scale
            times, status = [], None
            try:
                await c.get(path)  # warm-up (caches, cold start)
                for _ in range(max(1, runs)):
                    t0 = time.perf_counter()
                    r = await c.get(path)
                    times.append((time.perf_counter() - t0) * 1000)
                    status = r.status_code
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{path}: {type(exc).__name__}")
                continue
            med = statistics.median(times)
            row = {"path": path, "median_ms": round(med, 1), "max_ms": round(max(times), 1), "budget_ms": budget,
                   "status": status, "ok": med <= budget and status == 200}
            rows.append(row)
            if status != 200:
                failed.append(f"{path}: HTTP {status}")
            elif med > budget:
                over.append(f"{path}: {med:.0f} ms > {budget:.0f} ms")
    return {"rows": rows, "over": over, "failed": failed}


async def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default=os.getenv("PORTLESS_BACKEND_URL", ""))
    p.add_argument("--token", default=os.getenv("PORTLESS_SMOKE_TOKEN"))
    p.add_argument("--runs", type=int, default=5)
    a = p.parse_args(argv)
    if not a.base:
        print("no --base / PORTLESS_BACKEND_URL")
        return 2
    scale = float(os.getenv("PERF_BUDGET_SCALE", "1") or 1)
    out = await measure(a.base, a.token, runs=a.runs, scale=scale)
    for r in out["rows"]:
        print(f"{'OK  ' if r['ok'] else 'SLOW'} {r['path']}: median {r['median_ms']} ms (budget {r['budget_ms']:.0f})")
    print(json.dumps({"over": out["over"], "failed": out["failed"]}))
    if out["failed"] and not out["rows"]:
        return 2
    return 1 if out["over"] or out["failed"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
