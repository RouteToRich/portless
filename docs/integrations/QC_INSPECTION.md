# Pre-shipment inspection: QIMA, SGS, Bureau Veritas

How PortLess books the independent quality inspection, when it happens, what the supplier is asked, how the status and the report come back, and how to test it without a provider account.

Code: `shared/connectors/qc_providers.py` (providers), `services/shield_link/` (booking, status, reports), `shared/orchestration/qc_schedule.py` (dates), `shared/orchestration/supplier_commitment.py` (goods-ready date), `services/qc_sandbox/` (simulator). Tests: `tests/test_qc_inspection.py`, catalogue cases `TC-QC-023` to `TC-QC-038`.

## 1. Which provider has an API (checked 2026-11)

**Only QIMA is booked today** (`QC_PROVIDERS_ENABLED=qima`, the default). Buyers are offered QIMA only, a buyer who names another inspector is booked with QIMA, and an explicit request for another provider answers 422. The SGS and BV portal channel below is built and tested but dormant; add a provider to `QC_PROVIDERS_ENABLED` to switch it on.

| Provider | Product | Public API | How PortLess connects |
|---|---|---|---|
| **QIMA** | QIMA Public API, [developers.qima.com](https://developers.qima.com) | **Yes.** Token auth, booking, order search and detail, cancel, report search, PDF info, file download. No webhook. | `api` channel. PortLess books by API, polls the status, and downloads the report. |
| **SGS** | SGS SMART (customer portal) | **No public API.** Booking, status and reports live in the portal. Integration is only available under a key-account agreement. | `portal` channel. A booking request goes to the SGS booking desk (`SGS_BOOKING_EMAIL`) or staff enter it on SGS SMART. The confirmation and the report come back on the signed webhook or from staff. |
| **Bureau Veritas** | BV OneSource Connect (`app.onesourceconnect.com`, which replaced InSpec-BV); OneSource Suite | **No public API.** "OneSource Integration" (PLM / third-party feeds) is sold per contract. | `portal` channel, the same as SGS (`BUREAU_VERITAS_BOOKING_EMAIL`). |

If SGS or BV later sign an integration with us, only the connector changes. Shield-Link already records every status the same way, whatever channel it came from.

### QIMA API in use

All calls go to the host QIMA gives you. The documented pre-production host is `https://ppapi.qima.com`. Every call sends four headers: `Ai-Api-Access-Token` (issued by QIMA), `Ai-User-Id` and `Authorization: Bearer <token>` (both from sign-in), and `Referer`. Every response uses the envelope `{"message", "content"}`.

| Step | Call | PortLess use |
|---|---|---|
| Sign in | `POST /auth/v2/token` `{account, password: MD5, userType: "client"}` → `content.token.{token, userId, validBefore, refreshKey}` | Session cached per account. It is renewed 60 s before `validBefore` and once after a 401. |
| Book | `POST /v1.0/inspection` (InspectionOrder). Answers 200 (created), 202 (`"Factory not provided ,factory confirm skipped"`), 400 (validation), 409 (duplicate). | `serviceDate` = inspection day (`dd-MMM-yyyy`), `serviceType` `1` = PSI, PO number as `referenceNumber`, supplier facts from the deal, product ids from `QIMA_PRODUCT_*`, AQL (sampling level II, critical 0 / major 2.5 / minor 4.0), `expectedFinishedPct` 100, `expectedPackedPct` 80, deal and order ids in `customField1/2`. On a 409, PortLess looks the order up and keeps it. |
| Status | `GET /user/{userId}/inspection-order/{orderId}` | `status` / `statusText`: `20` WAITING_FOR_VALIDATION → REQUESTED; validated or confirmed → CONFIRMED; `60` completed → INSPECTED; cancelled → CANCELLED. |
| Result | `GET /user/{userId}/reports?keyword=<orderNumber>` | Report `status` ≥ 30 (diffused). `overrallResult` (sic): any `FAILED` → FAIL, all `PASSED` → PASS, anything else → PENDING (the client decides, so it goes to a person). Defect counts are kept. |
| Report PDF | `GET /user/{userId}/report/{productId}/pdf-info?isSingleFile=true` → `GET /user/{userId}/file/{fileId}` | The PDF is stored with the deal's documents (Drive `<root>/<deal_id>/`) with its SHA-256. That hash is the CRF hash the QC escrow tranche cites. |
| Cancel | `DELETE /user/{userId}/inspection/{orderId}?reason=` | Used by reschedule (`orderExtra.cancelable` must be true). |

QIMA has no quote endpoint. The price appears on the order (`orderPrice`) once QIMA prices it, so the negotiation still uses the catalogue fee.

## 2. When the inspection happens

A pre-shipment inspection (PSI, a final random inspection under ISO 2859-1) samples the **finished, packed lot**. It is therefore booked for:

- the day the supplier says the goods are **100 % produced and at least 80 % packed** (`QC_MIN_PACKED_PCT`), and
- no sooner than **2 working days** after the booking (`QC_BOOKING_LEAD_BUSINESS_DAYS`), moved to the next working day if it falls on a weekend.

The same inspector supervises the **container stuffing and seals the container** (`LOADING_SUPERVISION`, when `COMPLIANCE_LOADING_CHECK_REQUIRED` is on), so the supplier plans loading right after the inspection. Freight and the QC-stage escrow tranche wait for the container and seal record.

| Moment | What happens | Who |
|---|---|---|
| Escrow funded → PO issued | PO e-mail asks the supplier to (1) acknowledge the PO and (2) give the date the goods will be 100 % produced and ≥ 80 % packed. | PortLess, automatic |
| Supplier replies | The acknowledgement and the date are read from the reply (`2026-12-15`, `15 December 2026`, `Dec 15`, `15/12/2026`, `in 10 days` …). If there is no date, a "When will the goods be ready for inspection?" e-mail goes out, and the next reply that names a date books the inspection. A date heard by phone is recorded with `POST /shield-link/deals/{id}/qc-readiness`. | Supplier, or staff |
| Booking | Inspection booked for the ready date (or the first day the agency can attend). Deal → `SUPPLIER_DISPATCH_READY`. The supplier gets the agency, its order number, the date, and what to prepare. | PortLess |
| Before the inspection | The supplier must say at least **one working day before** if the goods will not be ready (agencies charge for a wasted visit). Staff move it with `POST /shield-link/qc/{tpo_id}/reschedule`, which cancels at QIMA by API and books the new date. | Supplier → staff |
| Inspection day | Inspector samples the lot, then supervises stuffing and seals the container. | Agency |
| Report (QIMA usually the same or next day) | PASS → `QC_PASSED` (export documents, T2 tranche gate, freight gate once the loading record matches). FAIL → `QC_FAILED` → `DISPUTE` → re-inspection playbook. PENDING → an operator decides. | PortLess |
| No report by inspection day + 1 day + `QC_REPORT_GRACE_HOURS` (48) | The supplier gets reminders, then `deal.exception`. | Deal watcher |

**Packing.** The buyer's packing (bag type and weight, liner, marking or private label, pallets, fumigation) is asked before the agreement and sent with the booking: QIMA gets it in `unitPacking`, `outerPacking` and `sampleReferenceClientComments` (the checks: weight per unit, marking against the artwork, liner, pallets, fumigation certificate), and the SGS / BV booking request has a `packing` section. See `shared/orchestration/packing.py`.

What the supplier must have ready on the day (sent in the booking notice): the full quantity produced, at least 80 % packed and labelled, the rest open for sampling, the PO, packing list and product specification at hand, and someone to open bags or cartons and move goods.

## 3. Status in PortLess

Every QC order (`third_party_orders`, `order_type=QC`) has a provider status, whichever way the news came:

`REQUESTED → CONFIRMED → INSPECTED → REPORT_ISSUED (PASS | FAIL | PENDING)`, or `CANCELLED`

| Source | How it arrives |
|---|---|
| QIMA | Polled. The deal watcher asks every `QC_POLL_MINUTES` (60) while the deal is at `SUPPLIER_DISPATCH_READY`; `POST /shield-link/deals/{id}/qc/poll` polls now. |
| SGS / BV desk or relay | `POST /shield-link/qc/webhook`, signed with HMAC-SHA256 (`x-sgs-signature` / `x-bv-signature`). The body is `{ext_ref, status?, result?, provider_order_number?, inspection_date?, documents: [{name, url}], defects, container_numbers, seal_numbers, loaded_quantity_mt, …}`. |
| Staff reading the portal or an e-mail | `POST /shield-link/qc/{ref}/status` (staff only), same body without `ext_ref`. |

A result is applied **once**. A repeated poll or a replayed webhook is ignored. Every change is kept in `status_history` with its source and is published as `qc.status_updated`. The result is published as `qc.passed` / `qc.failed`.

`GET /shield-link/deals/{id}/qc` returns the order, provider status and order number, the inspection date, the supplier's ready date, the result, defects, the kept reports, the history and the next step.

### The deal's QC status

On top of the order's provider status, each deal carries one **QC status** (`DealRecord.qc_status`, `shared/orchestration/qc_status.py`), which the Deal Board's QC column shows:

| QC status | Meaning |
|---|---|
| `AWAITING_PO_ACK` | The PO was sent and the supplier has not acknowledged it |
| `AWAITING_READY_DATE` | The supplier acknowledged and was asked when the goods are ready |
| `NOT_BOOKED` | Nothing holds the booking back, but no inspection is booked |
| `BOOKING_FAILED` | The provider refused the booking, or the request never reached it |
| `REQUESTED` | Booked; the provider has not confirmed yet |
| `SCHEDULED` | The provider confirmed the date and inspector |
| `INSPECTED` | The inspection happened; the report is not out yet |
| `OVERDUE` | Inspection day + 1 day + `QC_REPORT_GRACE_HOURS` passed without a report |
| `RESULT_PENDING` | The report leaves the decision to the client, so a person decides |
| `REINSPECTION` | A re-inspection is booked after a failed one |
| `PASSED` / `FAILED` | Report PASS / FAIL (FAIL moves the deal QC_FAILED → DISPUTE) |

It is derived only from the deal status, the supplier's PO acknowledgement and goods-ready date, and the QC orders, and is rewritten on every change to any of them.

## 4. Documents

- **QIMA:** every final report PDF (`PROD_FINAL_REPORT`) is downloaded by API when the result arrives.
- **SGS / BV:** every file listed in the webhook's `documents[]` (or the staff relay) is downloaded.
- Stored like the other trade documents: Google Drive `<GOOGLE_DRIVE_ROOT_FOLDER>/<deal_id>/QC-<tpo>-<name>.pdf`, with local disk (`SHIELD_LINK_STORAGE_DIR`) as the logged fallback. Each record has its SHA-256, size and source.
- Served at `GET /shield-link/qc/{tpo_id}/documents/{index}`. The PortLess Inspection Order (DocuGen) is still issued at `qc.booked`.

## 5. Live and automation: one implementation

The real workflow and the automated one run the same code. Only `INTEGRATION_MODE` (`shared/integration_mode.py`) differs, and it covers QC, freight and customs, and escrow and payouts alike:

| `INTEGRATION_MODE` | Who sets it | What the connectors do |
|---|---|---|
| `live` (default, in `.env`) | Every deployment and the dev server | Always call the real provider with the configured keys. A missing key or a `your_..._here` placeholder is sent as-is; the provider refuses it and the step fails visibly (502 / `deal.exception`), never a made-up success. A portal provider without a real booking-desk address refuses to book, and a booking e-mail that was not delivered fails the booking. |
| `sandbox` | Only the automation worker and view server (`worker_env`; `assert_isolated` refuses anything else) and pytest | Stub answers. QIMA / SGS / BV in `QC_SANDBOX_PROVIDERS` go to the simulator below. |

`.env` and `.env.example` hold a placeholder for every QC, freight and escrow key. Replacing a placeholder with the real key is all it takes to switch that provider on. Readiness counts a placeholder as not configured, and its `integration_mode` check fails in production in sandbox.

### The simulator (automation and tests only)

`services/qc_sandbox` simulates both channels:

- **QIMA:** the documented paths, headers, envelope, field names and errors (200 / 202 / 400 / 401 / 403 / 409 / 500), async booking, report search, pdf-info and a real PDF file. QIMA documents only order status `20` and the completed `60`; the sandbox's `30` VALIDATED and `90` CANCELLED are its own stand-ins (the connector reads unknown codes by their label).
- **SGS / BV:** the booking desk receives PortLess's booking request, confirms with an order number, and issues a report with a PDF, answering with the signed webhook.
- Control endpoints play the provider: `/_sandbox/qima/orders/{id}/validate`, `/inspect` (`{"result": "PASS"|"FAIL"|"PENDING"}`), `/_sandbox/{sgs|bv}/bookings/{ref}/confirm`, `/report`.

| How | Setting |
|---|---|
| In process (tests, automation) | `INTEGRATION_MODE=sandbox` + `QC_SANDBOX_PROVIDERS=qima,sgs,bv` (or `all`). Connectors reach the sandbox through ASGI, with no network. |
| Standalone | `make qc-sandbox` (port 8014) + `INTEGRATION_MODE=sandbox` + `QC_SANDBOX_URL=http://localhost:8014`. A `callback_url` on confirm or report delivers the webhook to a running PortLess. |

`QC_SANDBOX_PROVIDERS` is ignored in live mode, so the simulator is never a silent fallback.

## 6. Go-live checklist

1. QIMA: get the `Ai-Api-Access-Token`, a client account and the production host. Set `QIMA_*`, and read the product ids for your HS codes from `GET /parameter/v2/product-tree`. Confirm the AQL value format your account expects (the guide's own example uses `"0"`, `"1"`, `"3"`).
2. SGS / BV: get the booking desk addresses and set `SGS_BOOKING_EMAIL` / `BUREAU_VERITAS_BOOKING_EMAIL`. Agree with each desk to send reports to the PortLess mailbox. Set `SGS_WEBHOOK_SECRET` / `BUREAU_VERITAS_WEBHOOK_SECRET` if a relay posts the webhook.
3. Replace the `your_..._here` placeholders in `.env` with the real values and keep `INTEGRATION_MODE=live`. Readiness `qc_provider` is `ok` once one of the above is configured. Freight (`COGOPORT_*`, `FREIGHTOS_*`, `ICEGATE_API_KEY`) and escrow (`TAZAPAY_*`, `ESCROW_COM_*`, `STRIPE_*`, `PAYONEER_*`) work the same way.
4. Open: a separate QIMA container-loading (CLC) order is not booked yet. The loading supervision is requested in `customField3`; the seal record comes from the inspector's report through `POST /shield-link/deals/{id}/loading`.

## Inspection phases — DUPRO / PSI / PEO (`shared/orchestration/inspection_plan.py`)

The buyer chooses which inspections the order gets: named in a reply ("we also
need a DUPRO", "add a pre-customs clearance inspection" — negation-aware), or set
by staff (`PUT /deals/{id}/inspection-plan` `{types, reason}`, Deal page >
Logistics > Inspection plan). Default `QC_DEFAULT_INSPECTION_TYPES=PSI`; the PSI
is always part of the plan.

| Phase | When | QIMA `serviceType` | Effect |
|---|---|---|---|
| DUPRO — During Production | `QC_DUPRO_DAYS_BEFORE_READY` (10) days before the goods-ready date, at `QC_DUPRO_PRODUCTION_PCT` (30 %) produced; skipped when production ends too soon | `QIMA_SERVICE_TYPE_DUPRO` (2, documented) | booked with the PSI; FAIL → rework notice to the supplier + exception; the deal does not move |
| PSI — Pre-Shipment | goods-ready date, 100 % produced, ≥ 80 % packed, supervised loading + sealing | `QIMA_SERVICE_TYPE` (1) | the release check: PASS → QC_PASSED, FAIL → QC_FAILED |
| PEO — Pre-Customs Clearance | after the freight booking, before the export filing | `QIMA_SERVICE_TYPE_PEO` — **no documented id; ask QIMA** (the simulator uses 5) | the FILE_CUSTOMS gate waits for its PASS; FAIL → customs held + exception |

DUPRO / PEO results emit `qc.phase_result` (never QC_PASSED / QC_FAILED). The
deal's `inspections` field lists every phase with its status for the console;
`qc_status` keeps following the PSI. Every booking carries the trade brief
(deal, PO, product, quantity, phase) in QIMA's client comments / the portal
request's `notes`.
