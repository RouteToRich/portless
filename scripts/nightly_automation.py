"""Nightly automation run: every catalogue case and LLM journey on the automation database,
then a summary to Telegram (todo: nightly catalogue run + LLM journeys, results posted).

    python -X utf8 scripts/nightly_automation.py [--scope all|catalogue|journeys] [--tag smoke]

Starts the run exactly as the console does (``services.automation.service.start_run``: its own
worker process, ``portless_automation`` only, no real mail / payment / Apify credential), waits
for it, posts the totals, the failing cases and the safety block (refused real recipients,
blocked connections), and exits 0 when nothing failed and no real recipient was refused, 1
otherwise, 2 when the run could not start. Needs MONGO_URI (+ LLM gateway login for the
journeys); Telegram only when HITL_TELEGRAM_BOT_TOKEN / HITL_TELEGRAM_CHAT_ID are set.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def summary(run: dict[str, Any], failed: list[dict[str, Any]]) -> str:
    """Pure: the Telegram text for a finished run."""
    totals = run.get("totals") or {}
    safety = run.get("safety") or {}
    refused = safety.get("real_recipients") or []
    head = "✅" if not failed and not refused else "❌"
    lines = [f"{head} PortLess nightly automation {run.get('run_id', '?')} — {run.get('status', '?')}",
             "passed {p} · failed {f} · not automated {n} · browser {b}".format(
                 p=totals.get("passed", 0), f=totals.get("failed", 0), n=totals.get("not_automated", 0),
                 b=totals.get("needs_browser", 0))]
    for row in failed[:15]:
        lines.append(f"  • {row.get('tc_id')}: {str(row.get('error') or '')[:140]}")
    if len(failed) > 15:
        lines.append(f"  … and {len(failed) - 15} more")
    if refused:
        lines.append(f"SAFETY: {len(refused)} real recipient(s) refused — a test path reached real mailboxes")
    if safety.get("blocked_connections"):
        lines.append("blocked connections: " + ", ".join(safety["blocked_connections"][:6]))
    return "\n".join(lines)


def exit_code(run: dict[str, Any], failed: list[dict[str, Any]]) -> int:
    refused = (run.get("safety") or {}).get("real_recipients") or []
    return 0 if not failed and not refused and run.get("status") == "FINISHED" else 1


async def main(scope: str, tag: str) -> int:
    import shared.config  # noqa: F401  (loads .env)
    from services.automation import service
    try:
        started = await service.start_run("nightly", scope=scope, tag=tag)
    except Exception as exc:  # noqa: BLE001
        print(f"nightly automation could not start: {exc}")
        return 2
    run_id = started["run_id"]
    while service._alive():
        await asyncio.sleep(30)
    db = service._db()
    run = await db[service.RUNS].find_one({"_id": run_id}) or {}
    run["run_id"] = run_id
    failed = [r async for r in db[service.RESULTS].find({"run_id": run_id, "status": "failed"})]
    text = summary(run, failed)
    print(text)
    from shared import telegram
    if telegram.configured():
        await telegram.send_message(text)
    return exit_code(run, failed)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default="all", choices=["all", "catalogue", "journeys"])
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.scope, args.tag)))
