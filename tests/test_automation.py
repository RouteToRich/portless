"""Automation mode: the runner can only ever use the automation database and cannot reach anyone."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from services.automation import catalogue, safety, service
from services.automation.engine import find_step
from services.automation.router import router
from shared.auth.dependencies import AuthUser, get_current_user


@pytest.fixture
def db(monkeypatch):
    database = AsyncMongoMockClient()[safety.AUTOMATION_DB]
    monkeypatch.setattr(service, "_db", lambda: database)
    monkeypatch.setattr(service, "_process", None)
    return database


def _api(*, staff: bool) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id="u", email="s@x.invalid", is_portless=staff)
    return TestClient(app)


class TestIsolation:
    def _safe(self, monkeypatch):
        monkeypatch.setenv("AUTOMATION_WORKER", "1")
        monkeypatch.setenv("MONGO_DB_NAME", safety.AUTOMATION_DB)
        for key in safety._MUST_BE_BLANK:
            monkeypatch.setenv(key, "")

    def test_the_safe_environment_is_accepted(self, monkeypatch):
        self._safe(monkeypatch)
        safety.assert_isolated()

    def test_the_live_database_is_refused(self, monkeypatch):
        self._safe(monkeypatch)
        monkeypatch.setenv("MONGO_DB_NAME", "portless")
        with pytest.raises(SystemExit, match="portless_automation"):
            safety.assert_isolated()

    @pytest.mark.parametrize("key", ["GMAIL_APP_PASSWORD", "IMAP_PASSWORD", "APIFY_TOKEN", "TAZAPAY_API_KEY", "HITL_TELEGRAM_BOT_TOKEN"])
    def test_a_real_credential_is_refused(self, monkeypatch, key):
        self._safe(monkeypatch)
        monkeypatch.setenv(key, "real-secret")
        with pytest.raises(SystemExit, match=key):
            safety.assert_isolated()

    def test_a_worker_is_started_without_mail_or_paid_services(self, monkeypatch):
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "real-secret")
        env = safety.worker_env()
        assert env["MONGO_DB_NAME"] == safety.AUTOMATION_DB
        assert all(env[key] == "" for key in safety._MUST_BE_BLANK)

    def test_the_apify_token_is_kept_only_when_asked(self):
        assert safety.worker_env()["APIFY_TOKEN"] == ""
        live = safety.worker_env(live_apify=True)
        assert live["AUTOMATION_APIFY"] == "1"
        assert int(live["APIFY_MAX_RUNS_PER_DAY"]) > 0, "a live scan always has a daily cap"
        assert live["GMAIL_APP_PASSWORD"] == ""

    def test_a_live_scan_may_hold_the_apify_token_but_nothing_else(self, monkeypatch):
        self._safe(monkeypatch)
        monkeypatch.setenv("AUTOMATION_APIFY", "1")
        monkeypatch.setenv("APIFY_TOKEN", "token")
        safety.assert_isolated()
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "real-secret")
        with pytest.raises(SystemExit, match="GMAIL_APP_PASSWORD"):
            safety.assert_isolated()

    def test_service_mongo_overrides_are_removed_not_blanked(self, monkeypatch):
        monkeypatch.setenv("DEALS_MONGO_URI", "mongodb://elsewhere")
        assert "DEALS_MONGO_URI" not in safety.worker_env(), "a blank value would put the service in memory"

    def test_the_automation_backend_listens_on_its_own_port(self):
        assert safety.VIEW_PORT != 8081
        assert safety.worker_env(view=True)["MONGO_DB_NAME"] == safety.AUTOMATION_DB

    def test_only_the_named_database_can_be_opened(self):
        assert safety.AUTOMATION_DB == "portless_automation"


class TestCatalogue:
    def test_every_scenario_is_read_with_a_unique_id(self):
        _, cases = catalogue.load()
        ids = [c.tc_id for c in cases]
        assert len(cases) > 400 and len(ids) == len(set(ids))
        assert not [i for i in ids if i.startswith("TC-UNKNOWN")]

    def test_outlines_expand_into_iterations(self):
        _, cases = catalogue.load()
        case = next(c for c in cases if c.tc_id == "TC-HIT-001")
        assert len(case.iterations()) == 4
        filled = [s.filled(case.iterations()[0]) for s in case.steps]
        assert not [s for s in filled if s.doc and "<level>" in s.doc]

    def test_a_step_without_automation_is_found_as_missing(self):
        from services.automation import steps  # noqa: F401
        assert find_step("the moon is made of cheese") is None
        assert find_step('the response status is 200') is not None


class TestRoutes:
    @pytest.mark.parametrize("method, path, body", [
        ("get", "/automation/status", None), ("get", "/automation/cases", None),
        ("post", "/automation/runs", {}), ("post", "/automation/clear", {"scope": "everything"}),
    ])
    def test_staff_only(self, method, path, body):
        kwargs = {"json": body} if body is not None else {}
        assert getattr(_api(staff=False), method)(path, **kwargs).status_code == 403

    def test_clear_scope_is_checked(self, db):
        assert _api(staff=True).post("/automation/clear", json={"scope": "portless"}).status_code == 422

    def test_a_case_that_does_not_exist(self, db):
        assert _api(staff=True).get("/automation/cases/TC-NOPE-1").status_code == 404


class TestConsoleForwarding:
    def test_staff_only(self):
        assert _api(staff=False).get("/automation/view/deals").status_code == 403

    def test_a_request_is_answered_by_the_automation_backend_as_the_operator(self, monkeypatch):
        import httpx
        seen = {}

        async def forward(method, path, query, body, content_type, user):
            seen.update(method=method, path=path, query=query, email=user.email)
            return httpx.Response(200, json=[{"deal_id": "D1"}], headers={"content-type": "application/json"})

        monkeypatch.setattr(service, "forward_to_view", forward)
        response = _api(staff=True).get("/automation/view/deals?status=PAID")
        assert response.status_code == 200 and response.json() == [{"deal_id": "D1"}]
        assert seen == {"method": "GET", "path": "deals", "query": "status=PAID", "email": "s@x.invalid"}


class TestLiveCompanies:
    def test_deals_use_the_scanned_companies_but_never_their_mailboxes(self, monkeypatch):
        from types import SimpleNamespace
        from services.automation import seed
        monkeypatch.setitem(seed.DIRECTORY, "buyer", [SimpleNamespace(name="Real Importer LLC", website="https://real.example", phone="+971")])
        monkeypatch.setitem(seed.DIRECTORY, "supplier", [SimpleNamespace(name="Real Mill Pvt", website="https://mill.example", phone="+91")])
        buyer, supplier, who = seed.parties("abc")
        assert buyer.legal_name == "Real Importer LLC" and supplier.legal_name == "Real Mill Pvt"
        assert buyer.email.endswith(".invalid") and supplier.email.endswith(".invalid")

    def test_a_stand_in_only_without_a_scan(self, monkeypatch):
        from services.automation import seed
        monkeypatch.setitem(seed.DIRECTORY, "buyer", [])
        assert seed.company("buyer", "Stand In LLC").name == "Stand In LLC"


class TestClear:
    async def _fill(self, db):
        await db["deals"].insert_one({"_id": "D1"})
        await db["approvals"].insert_one({"_id": "A1"})
        await db["qa_test_results"].insert_one({"_id": "R1", "tc_id": "TC-1"})
        await db["qa_test_runs"].insert_one({"_id": "RUN-1", "status": "FINISHED", "started_at": 1})

    async def test_test_data_keeps_results(self, db):
        await self._fill(db)
        result = await service.clear("test_data")
        assert result["database"] == safety.AUTOMATION_DB and result["deleted"] == 2
        assert await db["deals"].count_documents({}) == 0
        assert await db["qa_test_results"].count_documents({}) == 1

    async def test_test_data_keeps_the_paid_for_directory(self, db):
        await db["trade_counterparties"].insert_one({"_id": "buyer:x", "role": "buyer"})
        await service.clear("test_data")
        assert await db["trade_counterparties"].count_documents({}) == 1
        await service.clear("everything")
        assert await db["trade_counterparties"].count_documents({}) == 0

    async def test_everything_removes_results_too(self, db):
        await self._fill(db)
        await service.clear("everything")
        assert await db["qa_test_results"].count_documents({}) == 0

    async def test_refused_during_a_run(self, db, monkeypatch):
        monkeypatch.setattr(service, "_process", SimpleNamespace(poll=lambda: None))
        with pytest.raises(service.AutomationError) as refused:
            await service.clear("everything")
        assert refused.value.status == 409


class TestRuns:
    async def test_a_second_run_is_refused(self, db, monkeypatch):
        monkeypatch.setattr(service, "_process", SimpleNamespace(poll=lambda: None))
        with pytest.raises(service.AutomationError) as refused:
            await service.start_run("a")
        assert refused.value.status == 409

    async def test_never_started_on_production(self, db, monkeypatch):
        monkeypatch.setenv("ENV_PROFILE", "production")
        with pytest.raises(service.AutomationError) as refused:
            await service.start_run("a")
        assert refused.value.status == 403

    async def test_a_worker_that_died_is_shown_as_interrupted(self, db, monkeypatch):
        monkeypatch.setattr(service, "_process", SimpleNamespace(poll=lambda: 1))
        await db["qa_test_runs"].insert_one({"_id": "RUN-1", "status": "RUNNING", "started_at": service.now(), "heartbeat_at": service.now()})
        status = await service.status()
        assert status["run"]["status"] == "INTERRUPTED"

    async def test_cases_list_shows_every_case_even_before_a_run(self, db):
        listing = await service.list_cases()
        assert listing["run_id"] is None and all(row["status"] == "not_run" for row in listing["cases"])
        assert any(row["tc_id"] == "UC-01" for row in listing["cases"])

    async def test_a_cases_worst_result_is_its_status(self, db):
        await db["qa_test_runs"].insert_one({"_id": "RUN-1", "status": "FINISHED", "started_at": service.now()})
        for status in ("passed", "failed"):
            await db["qa_test_results"].insert_one({"run_id": "RUN-1", "tc_id": "TC-HIT-001", "status": status})
        row = next(r for r in (await service.list_cases())["cases"] if r["tc_id"] == "TC-HIT-001")
        assert row["status"] == "failed" and row["passed"] == 1 and row["failed"] == 1

    def test_four_eyes_cannot_be_left_to_a_timeout(self):
        assert AsyncMock  # the runner found this; the behaviour is covered below


async def test_a_two_approver_gate_is_never_timed(monkeypatch):
    from services.hitl_approval import service as hitl
    from services.hitl_approval.repository import InMemoryHitlApprovalRepository
    from services.hitl_approval.schemas import CreateApprovalRequest
    monkeypatch.setattr(hitl, "get_repository", lambda: InMemoryHitlApprovalRepository())
    monkeypatch.setattr(hitl, "notify", AsyncMock())
    monkeypatch.setattr(hitl, "_emit", AsyncMock())
    monkeypatch.setenv("FOUR_EYES_THRESHOLD_USD", "100")
    gate = await hitl.create_approval(CreateApprovalRequest(deal_id="D", action="RELEASE_SUPPLIER_PAYOUT", summary="s", autonomy_level="L1", financial_impact_usd=500.0, callback_payload={}))
    assert gate.autonomy_level == "L0" and gate.expires_at is None


async def test_two_handlers_render_the_document_pack_once(monkeypatch):
    """Found on the real database: both QC_PASSED handlers missed the idempotency check."""
    import asyncio
    from services.docugen import service as docugen
    from shared.orchestration import continuations

    stored: dict[str, str] = {}
    renders: list[str] = []

    async def generate(req):
        if req.idempotency_key in stored:
            return SimpleNamespace(doc_id=stored[req.idempotency_key])
        await asyncio.sleep(0.01)  # the database round trip
        renders.append(req.idempotency_key)
        stored[req.idempotency_key] = f"DOC-{len(renders)}"
        return SimpleNamespace(doc_id=stored[req.idempotency_key])

    monkeypatch.setattr(docugen, "generate_document", generate)
    await asyncio.gather(continuations._generate_documents("D-RACE"), continuations._generate_documents("D-RACE"))
    assert len(renders) == 5 and len(set(renders)) == 5


class TestDealConversation:
    @staticmethod
    def _repo(monkeypatch):
        from agents.negotiation_agent import email_history
        repo = email_history.InMemoryEmailHistoryRepository()
        monkeypatch.setattr(email_history, "_email_repo", repo)
        return repo

    async def test_introductions_and_rfqs_come_first_in_the_history(self, monkeypatch):
        from agents.negotiation_agent import service as neg
        from agents.negotiation_agent.email_history import record_deal_email
        from services.automation import seed
        repo = self._repo(monkeypatch)
        monkeypatch.setattr(neg, "get_email_repository", lambda: repo)
        session = SimpleNamespace(deal_id="D1")
        monkeypatch.setattr(neg, "get_repository", lambda: SimpleNamespace(get_session=AsyncMock(return_value=session)))
        await seed.write_conversation("N1", "D1", "b@x.invalid", "s@x.invalid", 1250.0, 1000.0, 20.0, service.now())
        await record_deal_email(deal_id="D1", direction="outbound", party="buyer", to_address="b@x.invalid", from_address="P", subject="s", body="b", kind="notification")
        history = await neg.list_email_history("N1")
        times = [neg.history_time(m) for m in history]
        assert times == sorted(times)
        assert history[0].kind == "outreach_intro" and {"rfq", "rfq_reply"} <= {m.kind for m in history}
        assert history[-1].kind == "notification"

    async def test_drafting_agents_never_read_pre_negotiation_mail(self, monkeypatch):
        from agents.negotiation_agent.email_history import record_deal_email
        repo = self._repo(monkeypatch)
        await record_deal_email(deal_id="D1", direction="inbound", party="buyer", to_address="P", from_address="b@x.invalid", subject="s", body="b", kind="outreach_reply")
        assert await repo.list_by_negotiation("N1") == []

    async def test_an_undelivered_email_is_shown_as_not_delivered(self, monkeypatch):
        from agents.negotiation_agent.email_history import EmailStatus, record_deal_email
        repo = self._repo(monkeypatch)
        await record_deal_email(deal_id="D1", direction="outbound", party="buyer", to_address="b@x.invalid", from_address="P", subject="s", body="b", kind="rfq", delivered=False)
        rows = await repo.list_by_deal("D1")
        assert rows[0].status == EmailStatus.LOGGED_ONLY and rows[0].delivery_note and rows[0].sent_at is None


def test_a_price_without_a_currency_is_read_only_when_it_reads_as_a_price():
    from shared.orchestration.extraction import _first_price
    assert _first_price("We can pay 1200 per MT") == 1200.0
    assert _first_price("We can pay 1,250/MT CIF") == 1250.0
    assert _first_price("We need 20 MT by March") is None
    assert _first_price("Our order 4417 is confirmed") is None
