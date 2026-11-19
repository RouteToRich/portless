"""One automation run, in its own process. Started by ``service.start_run``.

The order matters: the environment is checked and the network guard installed
before any PortLess module is imported.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from services.automation.safety import REPO_ROOT, NetworkGuard, assert_isolated, assert_on_automation_database, without_startup_resume

_DRAFTING_CALLERS = ("buyer", "supplier", "negotiation", "orchestrator")
_NOT_DRAFTING = ("intent", "classif", "extract", "resolver", "exception", "market")


def _select(cases: Sequence[Any], args: argparse.Namespace) -> list[Any]:
    chosen = []
    for case in cases:
        if args.phase and case.phase != args.phase:
            continue
        if args.priority and case.priority != args.priority:
            continue
        if args.only and not any(case.tc_id.startswith(p) for p in args.only.split(",")):
            continue
        if args.tag and f"@{args.tag.lstrip('@')}" not in case.tags:
            continue
        chosen.append(case)
    return chosen


def _hook_mail_and_llm(mail: list[dict[str, str]], llm: list[str], guard_state: dict[str, Any]) -> None:
    """No e-mail leaves this process; only e-mail drafting reaches the LLM gateway."""
    from services.email_gateway import sender
    from services.email_gateway.schemas import SendEmailResult, SendOutcome
    from shared.llm_gateway import LLMClient
    from shared.llm_gateway.client import LLMConfigurationError
    from shared.orchestration import kill_switch

    async def composed_only(req: Any) -> Any:
        await kill_switch.assert_running(f"E-mail to {req.to_address}")
        if not str(req.to_address).endswith(".invalid"):
            guard_state["real_recipients"].append(str(req.to_address))
            raise RuntimeError(f"AUTOMATION: refused to address a real mailbox: {req.to_address}")
        mail.append({"to": req.to_address, "subject": req.subject, "body": req.body[:4000]})
        return SendEmailResult(outcome=SendOutcome.SENT, message_id=f"<{req.email_id or 'qa'}@portless-qa.invalid>", detail="automation: composed, never delivered")

    sender.send_email = composed_only  # type: ignore[assignment]

    real_chat = LLMClient.chat

    async def drafting_only(self: Any, messages: Any, caller: str = "unknown", **kwargs: Any) -> Any:
        name = caller.lower()
        if not guard_state["all_llm"] and (not name.startswith(_DRAFTING_CALLERS) or any(w in name for w in _NOT_DRAFTING)):
            raise LLMConfigurationError(f"automation: {caller} runs on its deterministic fallback")
        llm.append(caller)
        return await real_chat(self, messages, caller=caller, **kwargs)

    LLMClient.chat = drafting_only  # type: ignore[method-assign]


async def _live_directory(client: Any, scope: str) -> dict[str, Any]:
    """Real buyers and suppliers, by a real Apify scan. Afterwards the token is
    removed, so no scenario can start a paid run; a full run reuses a directory
    that already exists."""
    from services.automation import engine, seed
    from shared import catalogue as product_catalogue

    before = await seed.load_directory()
    scanned = False
    if os.environ.get("APIFY_TOKEN") and (scope == "live" or not before["buyer"] or not before["supplier"]):
        await product_catalogue.set_catalogue([{
            "product_id": "automation-rice", "hs_code": seed.HS_CODE, "name": "milled rice", "products": ["rice", "milled rice"],
            "markets": [seed.MARKET], "default_quantity_mt": 20.0,
        }], updated_by="automation")
        response = await client.post("/trade-min/scan", headers=engine.PERSONAS["staff"], json={
            "hs_codes": [seed.HS_CODE], "target_markets": [seed.MARKET], "force_refresh": True, "worldwide_search": True,
        })
        print("live scan answered", response.status_code, response.text[:200], flush=True)
        scanned = response.status_code == 200
    os.environ["APIFY_TOKEN"] = ""
    print("directory neutralised", await seed.neutralise_directory(), flush=True)
    found = await seed.load_directory()
    print("live directory", found, flush=True)
    return {"scanned": scanned, **found}


async def _run(args: argparse.Namespace, guard: NetworkGuard) -> int:
    import httpx

    import app as portless_app
    from services.automation import catalogue, engine, steps  # noqa: F401 — registers the steps
    from services.automation.store import RUNS, Sink, database, now

    db = database(os.environ["MONGO_URI"]) if os.environ.get("MONGO_URI") else None
    sink = Sink(db, args.run_id)
    mail: list[dict[str, str]] = []
    llm: list[str] = []
    state: dict[str, Any] = {"real_recipients": [], "all_llm": False}
    _hook_mail_and_llm(mail, llm, state)
    sha, every_case = catalogue.load()
    cases = _select(every_case, args) if args.scope not in ("journeys", "live") else []
    await sink.catalogue([c.to_doc() for c in every_case])
    planned = sum(len(c.iterations()) for c in cases)
    if db is not None:
        await db[RUNS].update_one({"_id": args.run_id}, {"$set": {"status": "RUNNING", "catalogue_sha": sha, "planned": planned, "heartbeat_at": now()}}, upsert=True)

    without_startup_resume()
    application = portless_app.app
    status = "FINISHED"
    async with application.router.lifespan_context(application):
        if db is not None:
            assert_on_automation_database()
        background = set(asyncio.all_tasks())
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://automation", timeout=1800) as client:
            scan = await _live_directory(client, args.scope)
            if db is not None and scan:
                await db[RUNS].update_one({"_id": args.run_id}, {"$set": {"live_scan": scan}})
            # The catalogue (docs/testing/TEST_CASES.md) was written before the
            # supplier had to confirm the order and acknowledge the PO; like the
            # pytest suite it runs without those two steps. The LLM journeys run
            # the real flow with both on.
            commitment_env = {k: os.environ.get(k) for k in ("SUPPLIER_CONFIRMATION_REQUIRED", "PO_ACK_REQUIRED",
                                                             "QC_READINESS_DATE_REQUIRED", "NEGOTIATION_PACKING_REQUIRED",
                                                             "QC_FACILITY_ADDRESS_REQUIRED",
                                                             "SUPPLIER_BANK_VERIFICATION_REQUIRED",
                                                             "REGISTRATIONS_REQUIRED", "COUNTRY_PROFILE_REQUIRED",
                                                             "QUESTION_AGENT_ENABLED", "FX_PEGS_ENABLED",
                                                             "CONTAINER_SURVEY_REQUIRED")}
            if cases:
                os.environ["SUPPLIER_CONFIRMATION_REQUIRED"] = "false"
                os.environ["PO_ACK_REQUIRED"] = "false"
                os.environ["QC_READINESS_DATE_REQUIRED"] = "false"
                os.environ["QC_FACILITY_ADDRESS_REQUIRED"] = "false"
                os.environ["NEGOTIATION_PACKING_REQUIRED"] = "false"
                os.environ["SUPPLIER_BANK_VERIFICATION_REQUIRED"] = "false"
                os.environ["REGISTRATIONS_REQUIRED"] = "false"
                os.environ["COUNTRY_PROFILE_REQUIRED"] = "false"
                os.environ["QUESTION_AGENT_ENABLED"] = "false"
                os.environ["FX_PEGS_ENABLED"] = "false"
                os.environ["CONTAINER_SURVEY_REQUIRED"] = "false"
            linked: list[tuple[Any, str]] = []  # (case, journey id): a Gherkin case played by an LLM journey
            from services.automation import browser as browser_runner
            ready, why_not = browser_runner.available()
            session: Any = None

            def needs_browser(c: Any) -> bool:
                return c.layer == "ui" or any(s.text.startswith("the browser is signed in") for s in c.steps)

            for case in cases:
                journey_id = next((tag.split(":", 1)[1] for tag in case.tags if tag.startswith("@journey:")), None)
                if journey_id:
                    linked.append((case, journey_id))
                    continue
                prepare = None
                if needs_browser(case):
                    if not ready:
                        for i, row in enumerate(case.iterations()):
                            await sink.result({"tc_id": case.tc_id, "iteration": i, "row": row, "status": "needs_browser",
                                               "error": f"Runs in a browser; no browser runner here: {why_not}.",
                                               "steps": [], "emails": [], "phase": case.phase, "layer": case.layer, "duration_ms": 0})
                        continue
                    if session is None:
                        session = browser_runner.BrowserSession()
                        await session.start()
                    prepare = session.prepare(client)
                for i, row in enumerate(case.iterations()):
                    result = await engine.run_iteration(case, row, client, background, mail, llm, prepare=prepare)
                    result.update(iteration=i, phase=case.phase, layer=case.layer)
                    await sink.result(result)
                    print(f"{case.tc_id}[{i}] {result['status']}: {(result['error'] or '')[:300]}", flush=True)
            if session is not None:
                await session.stop()
            for key, value in commitment_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            wanted = ",".join(filter(None, [args.only, *(jid for _, jid in linked)])) if args.only else args.only
            if args.scope in ("all", "journeys"):
                from services.automation import journeys
                state["all_llm"] = True
                await journeys.run_all(sink, mail, guard, background, only=wanted,
                                       replay=await _replay_from(sink, getattr(args, "replay_run", "")))
                state["all_llm"] = False
            elif linked:
                from services.automation import journeys
                state["all_llm"] = True
                await journeys.run_all(sink, mail, guard, background, only=",".join(jid for _, jid in linked),
                                       replay=await _replay_from(sink, getattr(args, "replay_run", "")))
                state["all_llm"] = False
            for case, journey_id in linked:
                played = next((r for r in reversed(sink.results) if r["tc_id"] == journey_id), None)
                result = dict(played) if played else {
                    "status": "not_automated", "error": f"Played by journey {journey_id}, which did not run.",
                    "steps": [], "emails": [], "duration_ms": 0}
                result.update(tc_id=case.tc_id, iteration=0, row={}, phase=case.phase, layer=case.layer, journey=journey_id)
                await sink.result(result)
                print(f"{case.tc_id}[0] {result['status']} (journey {journey_id}): {(result.get('error') or '')[:300]}", flush=True)

    counts: dict[str, int] = {}
    for r in sink.results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = {"blocked_connections": sorted(set(guard.blocked))[:20], "real_recipients": state["real_recipients"]}
    await sink.progress(status=status, finished_at=now(), safety=summary)
    print("RESULT", json.dumps({"totals": counts, **summary}), flush=True)
    if args.report:
        Path(args.report).write_text(json.dumps(sink.results, default=str, indent=1), encoding="utf-8")
    return 0


async def _replay_from(sink: Any, run_id: str) -> dict[str, list[dict[str, Any]]]:
    """Recorded counterparty replies of an earlier run, per journey (empty without a run id)."""
    if not run_id or sink.db is None:
        return {}
    from .store import RESULTS
    out: dict[str, list[dict[str, Any]]] = {}
    async for doc in sink.db[RESULTS].find({"run_id": run_id, "phase": "llm-journeys"}):
        replies = [r for r in (doc.get("counterparty_replies") or []) if r.get("reply")]
        if replies and doc.get("tc_id") not in out:
            out[doc["tc_id"]] = replies
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scope", default="all", choices=["all", "catalogue", "journeys", "live"])
    parser.add_argument("--phase", default="")
    parser.add_argument("--priority", default="")
    parser.add_argument("--only", default="")
    parser.add_argument("--tag", default="", help="only cases with this tag, e.g. smoke (the after-deploy set)")
    parser.add_argument("--report", default="")
    parser.add_argument("--replay-run", default="", help="play the journeys again with the counterparty replies "
                        "recorded by this earlier run (deterministic re-run of a failing journey)")
    args = parser.parse_args()

    assert_isolated()
    guard = NetworkGuard()
    guard.install()
    sys.path.insert(0, str(REPO_ROOT))
    work = Path(tempfile.gettempdir()) / "portless-automation"
    work.mkdir(exist_ok=True)
    # The automation database never holds a DGFT denied list: without this every test deal would
    # stop at a compliance review for the missing list (the catalogue and journeys were written
    # before the list check).
    os.environ.setdefault("COMPLIANCE_DGFT_LIST", "allow")
    os.environ.setdefault("EXPORT_CERTS_REQUIRED", "false")  # likewise: no certificate is ever recorded there
    os.environ.setdefault("FX_REFERENCE_AUTO_REFRESH", "false")  # the network guard allows no rate feed
    # The daily data jobs read public sites (UN / OFAC lists, DGFT / CBIC notices, FDA data); the network
    # guard refuses them, so a run would only log blocked connections. Their own scenarios turn them on.
    for name in ("COUNTRY_FEEDS_ENABLED", "REGULATORY_WATCH_ENABLED", "DESTINATION_RULES_ENABLED", "REFUSALS_ENABLED",
                 "REGISTRATION_CHECKS_ENABLED", "DEMAND_LEADS_ENABLED", "INDIA_EXPORTS_ENABLED", "DIRECTORY_SOURCES_ENABLED",
                 "FREIGHT_PROCUREMENT_ENABLED", "DATA_JOBS_ENABLED", "MARKET_CHECK_ENABLED"):
        os.environ.setdefault(name, "false")
    os.environ.setdefault("DEALMAKER_STORAGE_DIR", str(work / "dealmaker"))
    os.environ.setdefault("DOCUGEN_STORAGE_DIR", str(work / "docugen"))
    os.chdir(work)  # a relative env_file=".env" finds nothing here
    logging.basicConfig(level=getattr(logging, os.getenv("AUTOMATION_LOG_LEVEL", "WARNING").upper(), logging.WARNING), filename=str(work / f"{args.run_id}.log"), filemode="w",
                        format="%(levelname)s %(name)s: %(message)s", encoding="utf-8")
    return asyncio.run(_run(args, guard))


if __name__ == "__main__":
    sys.exit(main())
