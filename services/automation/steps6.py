"""Steps for the inspection lifecycle: QIMA by API, SGS / BV by portal (sandbox),
the supplier's goods-ready date and the deal's inspection view.

The worker runs with ``QC_SANDBOX_PROVIDERS=qima,sgs,bv``: bookings reach
``services.qc_sandbox`` in process, so "QIMA validates / reports" and "the desk
confirms / reports" drive that sandbox, and everything after it — polling,
webhooks, documents, the deal — is the real application.
"""
from __future__ import annotations

import hashlib
from typing import Any

import httpx

from . import seed as fx
from .engine import Ctx, step
from .steps import _deal_id, _reply


def _sandbox() -> httpx.AsyncClient:
    from services.qc_sandbox.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qc-sandbox.local")


async def _qc_orders(ctx: Ctx) -> list[Any]:
    from services.shield_link.repository import get_repository
    orders = [o for o in await get_repository().list_by_deal(_deal_id(ctx)) if o.order_type == "QC"]
    return sorted(orders, key=lambda o: o.created_at)


async def _qc(ctx: Ctx) -> Any:
    orders = [o for o in await _qc_orders(ctx) if o.status != "CANCELLED"]
    assert orders, "no QC order for the deal"
    return orders[-1]


def _ready_date(ctx: Ctx, working_days: int = 8) -> str:
    from shared.orchestration import qc_schedule
    when = qc_schedule.add_business_days(qc_schedule.today(), working_days).isoformat()
    ctx.ids["READY_DATE"] = when
    return when


async def _book(ctx: Ctx, provider: str, ready: str | None) -> None:
    body: dict[str, Any] = {"deal_id": _deal_id(ctx), "provider": provider, "quantity_mt": ctx.ids.get("qty") or 20}
    if ready:
        body["goods_ready_date"] = ready
    await ctx.call("POST", "/shield-link/qc", json_body=body)
    assert ctx.response is not None and ctx.response.status_code == 201, ctx.response.text if ctx.response else "no response"
    data = ctx.response.json()
    ctx.ids["QC"], ctx.ids["QC_REF"] = data["tpo_id"], data["ext_ref"]


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------

@step(r'a deal "([^"]+)" whose "([^"]+)" inspection is booked')
async def deal_with_booking(ctx: Ctx, name: str, provider: str) -> None:
    ctx.ids.update(await fx.seed_deal("PO_ISSUED", qc_provider=provider))
    await _book(ctx, provider, _ready_date(ctx, 6))
    _ready_date(ctx, 10)  # a later date for a reschedule


@step(r'the QC provider "([^"]+)" is enabled')
async def provider_enabled(ctx: Ctx, provider: str) -> None:
    """Only QIMA is booked by default (QC_PROVIDERS_ENABLED); a scenario that
    exercises a dormant provider enables it for itself."""
    import os
    current = os.environ.get("QC_PROVIDERS_ENABLED", "qima")
    if provider not in current.split(","):
        ctx.set_env("QC_PROVIDERS_ENABLED", f"{current},{provider}")


@step(r"the supplier must acknowledge the PO and give a goods-ready date")
async def readiness_required(ctx: Ctx) -> None:
    ctx.set_env("PO_ACK_REQUIRED", "true")
    ctx.set_env("QC_READINESS_DATE_REQUIRED", "true")
    _ready_date(ctx, 8)


# ---------------------------------------------------------------------------
# When — booking, provider side, polling
# ---------------------------------------------------------------------------

@step(r'the inspection is booked with "([^"]+)" for goods ready in (\d+) working days')
async def book_with_ready(ctx: Ctx, provider: str, days: str) -> None:
    await _book(ctx, provider, _ready_date(ctx, int(days)))


@step(r"QIMA validates the order for the inspection date")
async def qima_validates(ctx: Ctx) -> None:
    order = await _qc(ctx)
    async with _sandbox() as client:
        resp = await client.post(f"/_sandbox/qima/orders/{order.ext_ref}/validate", json={"inspection_date": order.inspection_date})
    assert resp.status_code == 200, resp.text


@step(r'QIMA reports "(PASS|FAIL|PENDING)" for the order')
async def qima_reports(ctx: Ctx, result: str) -> None:
    order = await _qc(ctx)
    async with _sandbox() as client:
        resp = await client.post(f"/_sandbox/qima/orders/{order.ext_ref}/inspect", json={"result": result, "remark": "automation"})
    assert resp.status_code == 200, resp.text


@step(r"the QC status is polled")
async def poll(ctx: Ctx) -> None:
    await ctx.call("POST", f"/shield-link/deals/{_deal_id(ctx)}/qc/poll")
    assert ctx.response is not None and ctx.response.status_code == 200, ctx.response.text if ctx.response else ""


async def _desk(ctx: Ctx, provider: str, action: str, body: dict[str, Any], *, wrong: bool = False) -> None:
    async with _sandbox() as client:
        resp = await client.post(f"/_sandbox/{provider}/bookings/{ctx.ids['QC_REF']}/{action}", json=body)
    assert resp.status_code == 200, resp.text
    webhook = resp.json()["webhook"]
    headers = dict(webhook["headers"])
    if wrong:
        headers = {k: ("0" * 64 if k.startswith("x-") else v) for k, v in headers.items()}
    await ctx.call("POST", webhook["path"], content=webhook["body"].encode(), persona="anonymous", headers=headers)


@step(r'the "([^"]+)" desk confirms the booking by webhook( with a wrong signature)?')
async def desk_confirms(ctx: Ctx, provider: str, wrong: str | None) -> None:
    await _desk(ctx, provider, "confirm", {}, wrong=bool(wrong))


@step(r'the "([^"]+)" desk reports "(PASS|FAIL)" with container "([^"]+)" and seal "([^"]+)" by webhook')
async def desk_reports(ctx: Ctx, provider: str, result: str, container: str, seal: str) -> None:
    await _desk(ctx, provider, "report", {
        "result": result, "container_numbers": [container], "seal_numbers": [seal],
        "loaded_quantity_mt": ctx.ids.get("qty") or 20, "loaded_hs_code": fx.HS_CODE,
        "photos": ["https://photos.invalid/seal.jpg"],
    })
    assert ctx.response is not None and ctx.response.status_code < 300, ctx.response.text


@step(r'the supplier replies "([^"]+)" from the known address')
async def supplier_replies(ctx: Ctx, text: str) -> None:
    await _reply(ctx, ctx.ids["supplier_mail"], f"Re: Purchase Order [NEG:{ctx.ids['N']}]", ctx.fill(text))


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------

@step(r'the QC order has provider status "([^"]+)"')
async def provider_status(ctx: Ctx, expected: str) -> None:
    order = await _qc(ctx)
    assert order.provider_status == expected, f"provider status {order.provider_status}"


@step(r"the QC order is booked for the goods-ready date")
async def booked_for_ready(ctx: Ctx) -> None:
    from shared.orchestration import qc_schedule
    order = await _qc(ctx)
    want = qc_schedule.inspection_date_for(ctx.ids["READY_DATE"]).isoformat()
    assert order.inspection_date == want, f"booked {order.inspection_date}, supplier ready {ctx.ids['READY_DATE']}"


@step(r"QIMA received the booking with the deal's PO number and the supplier's name")
async def qima_received(ctx: Ctx) -> None:
    from services.qc_sandbox import qima
    order = await _qc(ctx)
    booking = qima.STATE.orders[order.ext_ref]["booking"]
    assert booking["referenceNumber"] == ctx.ids.get("PO"), f"referenceNumber {booking['referenceNumber']}"
    assert booking["supplierName"] == ctx.ids.get("supplier_company"), f"supplierName {booking['supplierName']}"
    assert order.provider_order_number == qima.STATE.orders[order.ext_ref]["orderGeneralInfo"]["orderNumber"]


@step(r"the QC order keeps (\d+) report PDFs? whose hash is the CRF hash")
async def kept_reports(ctx: Ctx, n: str) -> None:
    from services.shield_link.qc import load_report
    order = await _qc(ctx)
    assert len(order.qc_documents) == int(n), f"{len(order.qc_documents)} reports kept"
    data, _ = await load_report(order.qc_documents[0]["storage_uri"])
    assert data.startswith(b"%PDF-"), "the kept report is not a PDF"
    assert order.crf_hash == hashlib.sha256(data).hexdigest(), "CRF hash is not the report's SHA-256"


@step(r'QIMA cancelled the first order with reason "([^"]+)"')
async def qima_cancelled(ctx: Ctx, reason: str) -> None:
    from services.qc_sandbox import qima
    first = (await _qc_orders(ctx))[0]
    sandbox_order = qima.STATE.orders[first.ext_ref]
    assert sandbox_order["orderGeneralInfo"]["status"] == "90" and sandbox_order.get("cancel_reason") == reason
    assert first.status == "CANCELLED"


@step(r"no QC order exists for the deal")
async def no_qc(ctx: Ctx) -> None:
    orders = await _qc_orders(ctx)
    assert not orders, f"{len(orders)} QC orders"


@step(r"the response is a PDF")
async def response_pdf(ctx: Ctx) -> None:
    assert ctx.response is not None and ctx.response.content.startswith(b"%PDF-"), "not a PDF"
    assert ctx.response.headers.get("content-type", "").startswith("application/pdf")


# ---------------------------------------------------------------------------
# PO price from the negotiation (TC-PO-013)
# ---------------------------------------------------------------------------

@step(r'a deal "([^"]+)" at status "([^"]+)" from a negotiation with supplier quote ([\d.]+), '
      r"our counter ([\d.]+) and supplier's last reply (none|[\d.]+)")
async def deal_from_negotiation(ctx: Ctx, alias: str, status: str, quote: str, counter: str, reply: str) -> None:
    """The supplier quoted, PortLess countered lower, and the supplier either
    stayed silent or answered with its own price. The PO may use only a price
    the supplier itself stated — never our counter."""
    from agents.negotiation_agent.repository import get_repository as neg_repo
    ctx.ids.update(await fx.seed_deal(status, supplier_price=float(quote)))
    ctx.ids[alias] = ctx.ids["D"]
    repo = neg_repo()
    session = await repo.get_session_by_deal(ctx.ids["D"])
    assert session is not None, "the fixture has no negotiation session"
    await repo.save_session(session.model_copy(update={
        "supplier_quote_price": float(quote),
        "current_ask": float(counter),
        "last_offer_to_supplier": float(counter),
        "supplier_last_quote": None if reply == "none" else float(reply),
    }))


# ---------------------------------------------------------------------------
# Re-inspection limit reached (TC-QC-019)
# ---------------------------------------------------------------------------

@step(r'a deal "([^"]+)" whose inspections have already failed the allowed number of times')
async def deal_out_of_reinspections(ctx: Ctx, alias: str) -> None:
    """The deal is back at SUPPLIER_DISPATCH_READY on its re-inspection, and
    ``QC_REINSPECTION_MAX`` earlier inspections already failed — the next FAIL
    is one beyond the limit."""
    from datetime import datetime, timedelta, timezone

    from services.shield_link.repository import get_repository
    from services.shield_link.schemas import ThirdPartyOrderRecord
    from shared.orchestration.playbooks import reinspection_max
    ctx.ids.update(await fx.seed_deal("SUPPLIER_DISPATCH_READY", released=["T1"]))
    ctx.ids[alias] = ctx.ids["D"]
    earlier = datetime.now(timezone.utc) - timedelta(days=10)
    for n in range(reinspection_max()):
        await get_repository().save(ThirdPartyOrderRecord(
            tpo_id=f"TPO-QC-{ctx.ids['uid']}-F{n + 1}", deal_id=ctx.ids["D"], order_type="QC",
            provider="sgs", status="FAILED", ext_ref=f"QC-{ctx.ids['uid']}-F{n + 1}", qc_result="FAIL",
            provider_status="REPORT_ISSUED", created_at=earlier + timedelta(days=n), updated_at=earlier + timedelta(days=n),
        ))


@step(r'the buyer\'s packing is "([^"]+)"')
async def buyer_packing(ctx: Ctx, text: str) -> None:
    """The buyer stated its packing during the negotiation (fixture fact)."""
    from agents.negotiation_agent.repository import get_repository as neg_repo
    from shared.orchestration import packing
    repo = neg_repo()
    session = await repo.get_session_by_deal(ctx.ids["D"])
    assert session is not None, "the fixture has no negotiation session"
    spec = packing.parse_packing(text)
    assert spec is not None, f"not a packing statement: {text}"
    await repo.save_session(session.model_copy(update={"packing": spec.model_dump(mode="json")}))
