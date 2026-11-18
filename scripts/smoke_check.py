"""Post-deploy smoke test: run it against a freshly deployed backend and block the rollout on
a non-zero exit.

    python scripts/smoke_check.py --base https://api.example.com --token "$PORTLESS_SMOKE_TOKEN"
    python scripts/smoke_check.py --base http://localhost:8081 --token … --allow-warn --json

Checks, in order (each with a timeout):
1. ``GET /health`` answers ``ok`` (no sign-in);
2. every module's ``GET /<module>/health`` answers 200;
3. ``GET /health/readiness`` (signed in): no ``fail``; ``warn`` only with ``--allow-warn``;
4. one signed-in read per area (deals, approvals, exceptions, escrow ledgers, directory,
   opportunities) answers 200 — the database is reachable and the routes are mounted.

The token is a staff bearer token (auth-service). Exit 0 = all passed, 1 = something failed,
2 = the backend could not be reached at all.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import httpx

MODULES = ["deals", "trade-min", "match-engine", "sourcing", "negotiation-agent", "dealmaker", "shield-link",
           "docugen", "escrow-guard", "hitl-approval", "exception-resolver", "email-gateway", "email-processor"]
READS = [
    ("deals", "/deals?limit=1"),
    ("approvals", "/hitl-approval/approvals?status=PENDING"),
    ("exceptions", "/exception-resolver/exceptions?limit=1"),
    ("escrow ledgers", "/escrow-guard/ledgers"),
    ("directory", "/trade-min/directory/buyers/page?limit=1"),
    ("opportunities", "/trade-min/opportunities?limit=1"),
]


async def run(base: str, token: str | None, *, allow_warn: bool = False, timeout: float = 20.0,
              client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    base = base.rstrip("/")
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    results: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        results.append({"check": name, "ok": ok, "detail": detail})

    try:
        try:
            r = await client.get(f"{base}/health")
        except httpx.HTTPError as exc:
            return {"ok": False, "unreachable": True, "results": [{"check": "health", "ok": False, "detail": str(exc)}]}
        add("health", r.status_code == 200 and r.json().get("status") == "ok", f"HTTP {r.status_code}")

        async def module(m: str) -> None:
            try:
                resp = await client.get(f"{base}/{m}/health")
                add(f"{m} health", resp.status_code == 200, f"HTTP {resp.status_code}")
            except httpx.HTTPError as exc:
                add(f"{m} health", False, str(exc))
        await asyncio.gather(*(module(m) for m in MODULES))

        try:
            resp = await client.get(f"{base}/health/readiness", headers=auth)
            if resp.status_code != 200:
                add("readiness", False, f"HTTP {resp.status_code}")
            else:
                rep = resp.json()
                fails = [c["id"] for c in rep.get("checks", []) if c.get("status") == "fail"]
                warns = [c["id"] for c in rep.get("checks", []) if c.get("status") == "warn"]
                ok = not fails and (allow_warn or not warns)
                add("readiness", ok, f"fail: {', '.join(fails) or 'none'} · warn: {', '.join(warns) or 'none'}")
        except (httpx.HTTPError, ValueError) as exc:
            add("readiness", False, str(exc))

        for name, path in READS:
            try:
                resp = await client.get(f"{base}{path}", headers=auth)
                add(f"read {name}", resp.status_code == 200, f"HTTP {resp.status_code}")
            except httpx.HTTPError as exc:
                add(f"read {name}", False, str(exc))
    finally:
        if own:
            await client.aclose()
    return {"ok": all(r["ok"] for r in results), "unreachable": False, "results": results}


def _execute(base: str, token: str | None, allow_warn: bool) -> dict[str, Any]:
    return asyncio.run(run(base, token, allow_warn=allow_warn))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="backend base URL")
    ap.add_argument("--token", default=None, help="staff bearer token (or PORTLESS_SMOKE_TOKEN)")
    ap.add_argument("--allow-warn", action="store_true", help="readiness warnings do not fail the check")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)
    import os
    token = args.token or os.getenv("PORTLESS_SMOKE_TOKEN")
    report = _execute(args.base, token, args.allow_warn)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for r in report["results"]:
            print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['check']:<28} {r['detail']}")
        print("SMOKE OK" if report["ok"] else "SMOKE FAILED")
    if report.get("unreachable"):
        return 2
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
