"""QIMA Public API sandbox.

Mirrors the contract published on https://developers.qima.com (guides dated
2025-11 to 2026-10): the same paths, the four request headers, the
``{"message", "content"}`` envelope and the documented field names, status codes
and error answers. Routes:

  POST   /auth/v2/token                                Get token (MD5 password)
  PUT    /auth/v2/token                                Refresh token
  DELETE /auth/v2/token                                Revoke token
  POST   /v1.0/inspection                              Create booking (200 / 202 / 400 / 409)
  POST   /v1.0/async-inspection                        Create booking asynchronously
  GET    /booking-result/{requestId}                   PENDING → SUCCEED / FAILED
  GET    /user/{userId}/inspection-orders              Search orders
  GET    /user/{userId}/inspection-order/{orderId}     Order detail
  DELETE /user/{userId}/inspection/{orderId}           Cancel (reason required)
  GET    /user/{userId}/reports                        Search reports
  GET    /user/{userId}/report/{productId}/pdf-info    PDF file metadata
  GET    /user/{userId}/interactive-report/{productId} Interactive report
  GET    /user/{userId}/file/{fileId}                  File download (binary)

QIMA documents only order status ``20`` = ``WAITING_FOR_VALIDATION`` and lists
``60`` as the completed order; the sandbox's ``30`` = ``VALIDATED`` (the order is
confirmed and an inspector scheduled) and ``90`` = ``CANCELLED`` are its own
stand-ins. Report statuses (30 / 40 / 50 …) and ``overrallResult`` (sic) are as
documented. QIMA documents no webhook: a client polls.

Sandbox control (not part of QIMA's API) plays the inspection itself:

  POST /_sandbox/qima/orders/{orderId}/validate   {"inspection_date"?: "YYYY-MM-DD"}
  POST /_sandbox/qima/orders/{orderId}/inspect    {"result": "PASS"|"FAIL"|"PENDING", ...}
  POST /_sandbox/qima/expire-tokens               every issued token answers 401 from now on
  GET  /_sandbox/qima/orders                      every order with the booking body it came from
  POST /_sandbox/qima/reset
"""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .pdf import report_pdf

SANDBOX_ACCESS_TOKEN = "qima-sandbox-access-token"
SANDBOX_ACCOUNT = "portless-sandbox"
SANDBOX_PASSWORD = "portless-sandbox-password"
SANDBOX_USER_ID = "5B7F0C2E9A4D4F1B8E6A3C2D1F0E9A8B"
SANDBOX_COMPANY_ID = "7C1E2D3F4A5B4C6D8E9F0A1B2C3D4E5F"
REFERER = "https://preprodmy.qima.com/"

STATUS_TEXT = {"20": "WAITING_FOR_VALIDATION", "30": "VALIDATED", "60": "COMPLETED", "90": "CANCELLED"}
SERVICE_TYPES = {"1": "PSI", "2": "DUPRO", "3": "IPC", "4": "CLC", "5": "PEO"}
# 1-4 are QIMA's documented booking ids; "5" = PEO is the simulator's stand-in
# (QIMA lists "peo" only as a search alias — the real id is per account).
_ALIAS_TO_TYPE = {"psi": "1", "dupro": "2", "ipc": "3", "clc": "4"}
_DEFAULT_SEARCH = "psi,ipc,dupro,clc,pm,peo,sr,fpsi,sc"


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()  # noqa: S324 — QIMA's documented password hash


def _hex() -> str:
    return uuid4().hex.upper()


def _fmt(d: date) -> str:
    return d.strftime("%d-%b-%Y")


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    for pattern in ("%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(str(value).strip().title(), pattern).date()
        except ValueError:
            continue
    return None


class _State:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.tokens: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.requests: dict[str, dict[str, Any]] = {}
        self.reports: dict[str, dict[str, Any]] = {}     # productId → report item
        self.files: dict[str, dict[str, Any]] = {}       # fileId → meta + bytes
        self.seq = 368590
        self.report_seq = 25004670


STATE = _State()
router = APIRouter()


def _envelope(content: Any, message: str | None = None, status: int = 200) -> JSONResponse:
    return JSONResponse({"message": message, "content": content}, status_code=status)


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"message": message, "content": None}, status_code=status)


def _check_session(request: Request, user_id: str | None = None) -> JSONResponse | None:
    """401 / 403 as the guides describe: the access token identifies the client
    application, the bearer token + Ai-User-Id the signed-in user."""
    if request.headers.get("ai-api-access-token") != SANDBOX_ACCESS_TOKEN:
        return _error(403, "Ai-Api-Access-Token is missing or invalid")
    if request.headers.get("referer") != REFERER:
        return _error(403, "Referer is missing or invalid")
    bearer = request.headers.get("authorization", "")
    token = bearer.removeprefix("Bearer ").strip()
    session = STATE.tokens.get(token)
    if session is None or session.get("expired") or int(session["validBefore"]) < time.time():
        return _error(401, "Token or user authentication failed")
    header_user = request.headers.get("ai-user-id")
    if header_user != session["userId"] or (user_id is not None and user_id != session["userId"]):
        return _error(401, "Token or user authentication failed")
    return None


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _issue(user_id: str) -> dict[str, Any]:
    token = {
        "id": _hex(),
        "userId": user_id,
        "userType": "client",
        "token": "eyJhbGciOiJkaXIiLCJlbmMiOiJBMTI4Q0JDLUhTMjU2In0.." + secrets.token_urlsafe(24),
        "validBefore": str(int(time.time()) + 3600),
        "refreshKey": uuid4().hex,
        "loginType": "Login",
    }
    STATE.tokens[token["token"]] = dict(token)
    return token


@router.post("/auth/v2/token")
async def get_token(request: Request) -> JSONResponse:
    if request.headers.get("ai-api-access-token") != SANDBOX_ACCESS_TOKEN or request.headers.get("referer") != REFERER:
        return _error(403, "Ai-Api-Access-Token is missing, invalid, or not valid for the user type")
    body = await request.json()
    if body.get("userType") not in ("client", "employee"):
        return _error(403, "userType must be client or employee")
    if body.get("account") != SANDBOX_ACCOUNT or body.get("password") != md5_hex(SANDBOX_PASSWORD):
        return _error(401, "The username and password don't match")
    token = _issue(SANDBOX_USER_ID)
    return _envelope({
        "token": token,
        "user": {
            "id": SANDBOX_USER_ID, "login": SANDBOX_ACCOUNT, "firstName": "PortLess", "lastName": "Sandbox",
            "email": "qc@portless-sandbox.invalid", "status": "ACTIVE", "businessUnit": "CIS",
            "company": {"id": SANDBOX_COMPANY_ID, "name": "PortLess Sandbox", "country": "India", "companyType": "client"},
        },
    })


@router.put("/auth/v2/token")
async def refresh_token(request: Request) -> JSONResponse:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    session = STATE.tokens.get(token)
    if request.headers.get("ai-api-access-token") != SANDBOX_ACCESS_TOKEN:
        return _error(403, "Ai-Api-Access-Token is missing or invalid")
    if session is None or request.headers.get("ai-api-refresh-key") != session["refreshKey"]:
        return _error(401, "Credentials, token, or refresh key are invalid or don't match")
    STATE.tokens.pop(token, None)
    return _envelope({"token": _issue(session["userId"]), "refreshKey": None})


@router.delete("/auth/v2/token")
async def revoke_token(request: Request) -> JSONResponse:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    session = STATE.tokens.get(token)
    if session is None or request.headers.get("ai-api-refresh-key") != session["refreshKey"]:
        return _error(401, "Credentials, token, or refresh key are invalid or don't match")
    STATE.tokens.pop(token, None)
    return _envelope(True)


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------

def _validate_booking(body: dict[str, Any], user_id: str) -> str | None:
    if body.get("userId") != user_id:
        return "userId must match the Ai-User-Id header"
    if _parse_date(body.get("serviceDate")) is None:
        return "serviceDate must be dd-MMM-yyyy, for example 18-DEC-2030"
    if body.get("shipDate") and _parse_date(body.get("shipDate")) is None:
        return "shipDate must be dd-MMM-yyyy"
    if str(body.get("serviceType") or "") not in SERVICE_TYPES:
        return f"serviceType {body.get('serviceType')!r} is not a valid inspection service type"
    if not body.get("supplierCode"):
        missing = [f for f in ("supplierName", "supplierContactName", "supplierCountryID", "supplierAddress") if not body.get(f)]
        if missing:
            return f"supplier info is required when supplierCode is omitted: {', '.join(missing)}"
    products = body.get("products") or []
    if not products:
        return "At least one product is required"
    for p in products:
        missing = [f for f in ("name", "categoryId", "familyId", "typeId") if not p.get(f)]
        if missing:
            return f"product {p.get('name')!r} is missing {', '.join(missing)}"
    return None


def _create_order(body: dict[str, Any], user_id: str) -> dict[str, Any]:
    STATE.seq += 1
    order_id = _hex()
    service_date = _parse_date(body["serviceDate"])
    ship_date = _parse_date(body.get("shipDate")) or service_date
    service_type = str(body["serviceType"])
    products = []
    for p in body["products"]:
        refs = p.get("refs") or []
        qty = sum(int(r.get("qty") or 0) for r in refs) or int(p.get("prodQuantity") or 0)
        po = p.get("poNumber") or (refs[0].get("poNumber") if refs else None)
        checklist = p.get("checkList") or {}
        products.append({
            "productBean": {
                "orderId": order_id, "productId": _hex(), "prodName": p["name"],
                "prodCategoryClient": p["categoryId"], "prodFamilyClient": p["familyId"], "prodType": p["typeId"],
                "prodReference": refs[0].get("skuCode") if refs else p.get("prodReference"),
                "prodQuantity": qty, "prodQuantityUnit": "Pcs", "poNumber": po,
                "bookingtype": body.get("bookingType") or "General",
            },
            "productChecklistBean": {
                "prodSampleLevel": checklist.get("sampleLevel") or "II",
                "aqlCritical": checklist.get("aqlCritical", "0"), "aqlMajor": checklist.get("aqlMajor", "2.5"),
                "aqlMinor": checklist.get("aqlMinor", "4.0"),
            },
            "completed": False,
        })
    factory = None
    if body.get("factoryName"):
        factory = {
            "factoryId": _hex(), "factoryName": body["factoryName"], "factoryMGRName": body.get("factoryContactName"),
            "factoryMGRNumber": body.get("factoryContactPhone"), "factoryMGREmail": [body.get("factoryContactEmail")],
            "factoryAddress": body.get("factoryAddress"), "factoryCountry": body.get("factoryCountryID"),
        }
    order = {
        "booking": body,
        "orderGeneralInfo": {
            "orderId": order_id, "orderPlacer": SANDBOX_ACCOUNT, "status": "20", "statusText": STATUS_TEXT["20"],
            "serviceType": service_type, "serviceTypeText": SERVICE_TYPES[service_type],
            "bookingDate": _fmt(datetime.now(timezone.utc).date()), "orderNumber": f"Q26{STATE.seq:08d}-PP",
            "companyId": SANDBOX_COMPANY_ID, "userId": user_id,
            "allowChangeInspectionDate": bool(body.get("allowChangeInspectionDate")),
            "clientRefNb": body.get("referenceNumber"), "expectedInspectionDate": _fmt(service_date),
            "actualInspectionDate": _fmt(service_date), "expectedShipDate": _fmt(ship_date),
            "customField1": body.get("customField1"), "customField2": body.get("customField2"),
            "customField3": body.get("customField3"), "confirmed": False,
        },
        "orderExtra": {"orderId": order_id, "cancelable": True, "editable": True, "isReInspection": bool(body.get("previousOrderNumber")),
                       "nbOfInspectors": 1, "quickBooking": False, "multiSupplierOrder": False},
        "orderFactory": factory,
        "orderSupplier": {
            "orderId": order_id, "supplierName": body.get("supplierName"), "supplierMGRName": body.get("supplierContactName"),
            "supplierMGRNumber": body.get("supplierContactPhone"), "supplierMGREmail": [body.get("supplierContactEmail")],
            "supplierAddress": body.get("supplierAddress"), "supplierCity": body.get("supplierCityID"),
            "supplierCountry": body.get("supplierCountryID"), "supplierId": _hex(),
            "supplierProductLines": body.get("supplierProductLines") or [],
        },
        "products": products,
        "orderSpecialRequests": [],
    }
    STATE.orders[order_id] = order
    return order


def _order_content(order: dict[str, Any], *, priced: bool) -> dict[str, Any]:
    content = {
        "draft": {"draftId": None if priced else _hex(), "isAnOrderNotADraft": True, "draftSteps": None},
        "orderGeneralInfo": order["orderGeneralInfo"],
        "orderExtra": order["orderExtra"],
        "orderFactory": order["orderFactory"],
        "orderSupplier": order["orderSupplier"],
        "products": order["products"],
    }
    if priced:
        qty = sum(p["productBean"]["prodQuantity"] for p in order["products"])
        content["orderPrice"] = {
            "orderId": order["orderGeneralInfo"]["orderId"], "totalManday": 1.0, "totalCharge": 332.79,
            "inspectionChargeType": "MAN_DAY", "inspUnitPrice": 309.0, "inspectionCharge": 309.0,
            "totalQuantity": float(qty), "sampleLevel": "II", "sampleSizeTotal": 200.0, "taxAmount": 23.79,
        }
        content["orderSpecialRequests"] = order["orderSpecialRequests"]
    return content


def _duplicate(body: dict[str, Any]) -> bool:
    ref = body.get("referenceNumber")
    when = _parse_date(body.get("serviceDate"))
    return bool(ref) and any(
        o["booking"].get("referenceNumber") == ref and _parse_date(o["booking"].get("serviceDate")) == when
        and o["orderGeneralInfo"]["status"] != "90"
        for o in STATE.orders.values()
    )


@router.post("/v1.0/inspection")
async def create_inspection(request: Request) -> JSONResponse:
    if (denied := _check_session(request)) is not None:
        return denied
    body = await request.json()
    user_id = request.headers["ai-user-id"]
    if (problem := _validate_booking(body, user_id)) is not None:
        return _error(400, problem)
    if _duplicate(body):
        return _error(409, "Duplicate booking request found")
    order = _create_order(body, user_id)
    if order["orderFactory"] is None:
        return _envelope(_order_content(order, priced=False), "Factory not provided ,factory confirm skipped", status=202)
    return _envelope(_order_content(order, priced=False))


@router.post("/v1.0/async-inspection")
async def create_inspection_async(request: Request) -> JSONResponse:
    if (denied := _check_session(request)) is not None:
        return denied
    body = await request.json()
    request_id = _hex()
    STATE.requests[request_id] = {"body": body, "user_id": request.headers["ai-user-id"], "polls": 0}
    return _envelope(request_id, "successfully receive your request, you can call /booking-result API to check booking result")


@router.get("/booking-result/{request_id}")
async def booking_result(request_id: str, request: Request) -> JSONResponse:
    if (denied := _check_session(request)) is not None:
        return denied
    pending = STATE.requests.get(request_id)
    if pending is None:
        return _error(500, f"booking request {request_id} not found")
    pending["polls"] += 1
    base = {"requestId": request_id, "clientRefNumber": pending["body"].get("referenceNumber"),
            "companyId": SANDBOX_COMPANY_ID, "userId": pending["user_id"]}
    if pending["polls"] == 1:
        return _envelope({**base, "orderNumber": None, "orderId": None, "status": "PENDING", "comments": "processing"})
    if "order_id" not in pending:
        problem = _validate_booking(pending["body"], pending["user_id"]) or ("Duplicate booking request found" if _duplicate(pending["body"]) else None)
        if problem:
            return _envelope({**base, "orderNumber": None, "orderId": None, "status": "FAILED", "comments": problem})
        pending["order_id"] = _create_order(pending["body"], pending["user_id"])["orderGeneralInfo"]["orderId"]
    gi = STATE.orders[pending["order_id"]]["orderGeneralInfo"]
    return _envelope({**base, "orderNumber": gi["orderNumber"], "orderId": gi["orderId"], "status": "SUCCEED",
                      "comments": "Async place order successfully"})


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def _order_or_500(order_id: str) -> dict[str, Any] | None:
    return STATE.orders.get(order_id)


@router.get("/user/{user_id}/inspection-orders")
async def search_orders(user_id: str, request: Request) -> JSONResponse:
    if (denied := _check_session(request, user_id)) is not None:
        return denied
    q = request.query_params
    types = {_ALIAS_TO_TYPE.get(a.strip()) for a in (q.get("service-type") or _DEFAULT_SEARCH).split(",")}
    statuses = {s.strip() for s in (q.get("status") or "").split(",") if s.strip()}
    keyword = (q.get("keyword") or "").lower()
    items = []
    for order in STATE.orders.values():
        gi = order["orderGeneralInfo"]
        if gi["serviceType"] not in types or (statuses and gi["status"] not in statuses):
            continue
        pos = ",".join(filter(None, (p["productBean"]["poNumber"] for p in order["products"])))
        haystack = " ".join(filter(None, (gi["orderNumber"], pos, order["orderSupplier"]["supplierName"]))).lower()
        if keyword and keyword not in haystack:
            continue
        items.append({
            "orderId": gi["orderId"], "serviceType": gi["serviceType"], "serviceTypeText": gi["serviceTypeText"],
            "status": gi["status"], "statusText": gi["statusText"].replace("_", " ").title(),
            "supplierName": order["orderSupplier"]["supplierName"], "supplierIdCode": order["booking"].get("supplierCode"),
            "refNumber": gi["orderNumber"], "clientReference": gi["clientRefNb"],
            "productNames": ",".join(p["productBean"]["prodName"] for p in order["products"]), "poNumbers": pos,
            "serviceDate": gi["actualInspectionDate"], "bookingDate": gi["bookingDate"],
            "confirmBySupplier": "YES" if gi["confirmed"] else "NO", "isSupplierConfirmed": gi["confirmed"],
            "isQuickBooked": False, "businessType": None,
        })
    size = int(q.get("pageSize") or 20)
    page = max(1, int(q.get("pageNumber") or 1))
    chunk = items[(page - 1) * size: page * size]
    return _envelope({"pageNo": page, "totalPageNum": max(1, -(-len(items) // size)), "totalSize": len(items),
                      "pageItems": chunk, "countByFields": None})


@router.get("/user/{user_id}/inspection-order/{order_id}")
async def order_detail(user_id: str, order_id: str, request: Request) -> JSONResponse:
    if (denied := _check_session(request, user_id)) is not None:
        return denied
    order = _order_or_500(order_id)
    if order is None:
        return _error(500, "PSI_0001:The order is not existing!")
    return _envelope(_order_content(order, priced=True))


@router.delete("/user/{user_id}/inspection/{order_id}")
async def cancel_order(user_id: str, order_id: str, request: Request) -> JSONResponse:
    if (denied := _check_session(request, user_id)) is not None:
        return denied
    order = _order_or_500(order_id)
    if not request.query_params.get("reason"):
        return _error(400, "reason is required")
    if order is None or not order["orderExtra"]["cancelable"]:
        return _error(500, "The order can no longer be cancelled")
    order["orderGeneralInfo"].update(status="90", statusText=STATUS_TEXT["90"])
    order["orderExtra"].update(cancelable=False, editable=False)
    order["cancel_reason"] = request.query_params.get("reason")
    return _envelope(True)


# ---------------------------------------------------------------------------
# Reports + files
# ---------------------------------------------------------------------------

def _millis(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


@router.get("/user/{user_id}/reports")
async def search_reports(user_id: str, request: Request) -> JSONResponse:
    if (denied := _check_session(request, user_id)) is not None:
        return denied
    q = request.query_params
    keyword = (q.get("keyword") or "").lower()
    statuses = {int(s) for s in (q.get("status") or "").split(",") if s.strip().isdigit()}
    items = [r for r in STATE.reports.values()
             if (not keyword or keyword in f"{r['orderNumber']} {r['poNumber'] or ''}".lower())
             and (not statuses or r["status"] in statuses)]
    size = int(q.get("page-size") or 50)
    page = max(1, int(q.get("page") or 1))
    public = [{k: v for k, v in r.items() if not k.startswith("_")} for r in items[(page - 1) * size: page * size]]
    return _envelope({"pageNo": page, "totalPageNum": max(1, -(-len(items) // size)), "totalSize": len(items),
                      "pageItems": public, "countByFields": None})


@router.get("/user/{user_id}/report/{product_id}/pdf-info")
async def pdf_info(user_id: str, product_id: str, request: Request) -> JSONResponse:
    if (denied := _check_session(request, user_id)) is not None:
        return denied
    report = STATE.reports.get(product_id)
    if report is None:
        return _error(500, "invalid productId or no report available yet")
    meta = {k: v for k, v in STATE.files[report["_file_id"]].items() if k != "bytes"}
    return _envelope([meta])


@router.get("/user/{user_id}/interactive-report/{product_id}")
async def interactive_report(user_id: str, product_id: str, request: Request) -> JSONResponse:
    if (denied := _check_session(request, user_id)) is not None:
        return denied
    report = STATE.reports.get(product_id)
    if report is None or request.query_params.get("orderId") != report["orderId"]:
        return _error(500, "report not found for this orderId / productId")
    verdict = {"PASSED": "Pass", "FAILED": "Fail"}.get(report["overrallResult"], "Pending")
    return _envelope({
        "reportId": report["_report_id"], "reportNumber": report["orderNumber"].replace("Q", "R-Cloud-PP-", 1),
        "orderId": report["orderId"],
        "downloadUrlOfPhotos": f"https://ppapi.qima.com/file/download/secure-file-id/{_hex()}",
        "downloadUrlOfReport": f"https://ppapi.qima.com/file/download/secure-file-id/{report['_file_id']}",
        "overallResult": {"isMissed": False, "overallResult": verdict},
        "assessmentResults": [
            {"color": "#00A876" if report["workmanshipResult"] == "WITHIN AQL" else "#E5353D", "label": "Workmanship",
             "result": "Pass" if report["workmanshipResult"] == "WITHIN AQL" else "Fail", "secondaryLabel": None, "status": None},
        ],
        "reportDecision": {"decidedBy": None, "decision": "Pending", "decisionComments": None, "decisionTime": "",
                           "productReference": []},
        "keyIndicator": {"isVisible": True, "aqlLevel": "II", "inspectionResultSummary": {
            "checkedCritical": str(report["criticalChecked"]), "foundCritical": str(report["criticalFound"]),
            "maxCritical": str(report["criticalMax"]), "testResults": report["testResult"],
            "workmanShipResult": report["workmanshipResult"]}},
        "inspectionDetails": {"serviceDescription": "Pre-shipment Inspection", "serviceType": report["serviceTypeText"]},
        "productDetails": {"poNumber": report["poNumber"], "productName": report["productName"],
                           "prodQuantityUnit": report["prodQuantityUnit"]},
        "interactiveReportVersion": "v1",
    }, "OK")


@router.get("/user/{user_id}/file/{file_id}")
async def download_file(user_id: str, file_id: str, request: Request) -> Response:
    if (denied := _check_session(request, user_id)) is not None:
        return denied
    meta = STATE.files.get(file_id)
    if meta is None:
        return _error(500, "File download failed: file not found")
    return Response(meta["bytes"], media_type="application/octet-stream",
                    headers={"content-disposition": f'attachment; filename="{meta["fileName"]}"'})


# ---------------------------------------------------------------------------
# Sandbox control — plays QIMA's operations team and the inspector
# ---------------------------------------------------------------------------

@router.post("/_sandbox/qima/orders/{order_id}/validate")
async def sandbox_validate(order_id: str, request: Request) -> JSONResponse:
    order = _order_or_500(order_id)
    if order is None:
        return _error(404, "no such order")
    body = await _json_or_empty(request)
    gi = order["orderGeneralInfo"]
    if body.get("inspection_date"):
        gi["actualInspectionDate"] = _fmt(date.fromisoformat(body["inspection_date"]))
    gi.update(status="30", statusText=STATUS_TEXT["30"], confirmed=True)
    return _envelope(_order_content(order, priced=True), "validated")


@router.post("/_sandbox/qima/orders/{order_id}/inspect")
async def sandbox_inspect(order_id: str, request: Request) -> JSONResponse:
    """The inspection happened: order → 60 COMPLETED, one report (status 30,
    diffused to client) and one final-report PDF per product."""
    order = _order_or_500(order_id)
    if order is None:
        return _error(404, "no such order")
    body = await _json_or_empty(request)
    verdict = str(body.get("result") or "PASS").upper()
    overall = {"PASS": "PASSED", "FAIL": "FAILED"}.get(verdict, "PENDING")
    major_found = int(body.get("major_found", 0 if verdict == "PASS" else 6))
    minor_found = int(body.get("minor_found", 1 if verdict == "PASS" else 9))
    critical_found = int(body.get("critical_found", 0))
    gi = order["orderGeneralInfo"]
    gi.update(status="60", statusText=STATUS_TEXT["60"], confirmed=True)
    order["orderExtra"].update(cancelable=False, editable=False)
    when = _parse_date(gi["actualInspectionDate"]) or datetime.now(timezone.utc).date()
    created = []
    for product in order["products"]:
        bean = product["productBean"]
        product["completed"] = True
        STATE.report_seq += 1
        file_id = _hex()
        file_name = f"R-Cloud-PP-{STATE.report_seq}_{bean['prodName']}.pdf"
        pdf = report_pdf([
            "QIMA SANDBOX - Inspection report (not a real report)",
            f"Order {gi['orderNumber']}  Service {gi['serviceTypeText']}",
            f"Product {bean['prodName']}  PO {bean['poNumber']}  Qty {bean['prodQuantity']}",
            f"Inspection date {_fmt(when)}",
            f"Overall result {overall}",
            f"Defects found: critical {critical_found}, major {major_found}, minor {minor_found}",
            str(body.get("remark") or ""),
        ])
        STATE.files[file_id] = {
            "id": file_id, "srcId": bean["productId"], "fileType": "PROD_FINAL_REPORT", "fileName": file_name,
            "comments": "", "objectKey": f"{bean['productId']}/PROD_FINAL_REPORT/{file_id}/{file_name}",
            "fileSize": len(pdf), "version": 0, "s3Url": None, "createDate": _fmt(when), "updateDate": _fmt(when),
            "bytes": pdf,
        }
        report = {
            "orderId": gi["orderId"], "productId": bean["productId"], "orderNumber": gi["orderNumber"],
            "orderNumberProductSeq": gi["orderNumber"], "clientRefNb": gi["clientRefNb"], "poNumber": bean["poNumber"],
            "inspectionDate": _millis(when), "status": 30, "companyId": SANDBOX_COMPANY_ID, "companyName": "PortLess Sandbox",
            "productName": bean["prodName"], "supplierName": order["orderSupplier"]["supplierName"],
            "serviceType": gi["serviceType"], "serviceTypeText": gi["serviceTypeText"], "prodReference": bean["prodReference"],
            "bookingType": bean["bookingtype"], "prodQuantity": bean["prodQuantity"], "prodQuantityUnit": "Pcs",
            "specResult": "OK" if verdict == "PASS" else "NOT OK", "testResult": "Passed" if verdict == "PASS" else "Failed",
            "workmanshipResult": "WITHIN AQL" if verdict == "PASS" else "BEYOND AQL", "measurementResult": "N/A",
            "overrallResult": overall,
            "criticalChecked": 125, "majorChecked": 125, "minorChecked": 125,
            "criticalFound": critical_found, "majorFound": major_found, "minorFound": minor_found,
            "criticalMax": 0, "majorMax": 7, "minorMax": 10,
            "isReInspection": "Yes" if order["orderExtra"]["isReInspection"] else "No",
            "reportRemark": body.get("remark") or "-", "read": False, "inspectionDateMMMFormat": _fmt(when),
            "sampleSize": 125, "realMD": 1, "totalCharge": "332.79",
            "sendReportTime": int(time.time() * 1000),
            "refResults": [{"productId": bean["productId"], "prodRefPoNb": bean["poNumber"],
                            "aqlResult": "WITHIN AQL" if verdict == "PASS" else "BEYOND AQL", "status": 30}],
            "_file_id": file_id, "_report_id": _hex(),
        }
        STATE.reports[bean["productId"]] = report
        created.append({"productId": bean["productId"], "fileId": file_id, "overrallResult": overall})
    return _envelope({"orderId": order_id, "reports": created}, "inspected")


@router.post("/_sandbox/qima/expire-tokens")
async def sandbox_expire_tokens() -> JSONResponse:
    for session in STATE.tokens.values():
        session["expired"] = True
    return _envelope(len(STATE.tokens))


@router.get("/_sandbox/qima/orders")
async def sandbox_orders() -> JSONResponse:
    return _envelope([{"orderId": oid, "booking": o["booking"], "orderGeneralInfo": o["orderGeneralInfo"],
                       "cancel_reason": o.get("cancel_reason")} for oid, o in STATE.orders.items()])


@router.post("/_sandbox/qima/reset")
async def sandbox_reset() -> JSONResponse:
    STATE.reset()
    return _envelope(True)


async def _json_or_empty(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    import json
    return json.loads(raw)
