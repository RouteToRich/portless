# Pre-shipment inspection: QIMA, SGS, Bureau Veritas

How PortLess books the independent quality inspection, when it happens, what the supplier is asked, how the status and the report come back, and how to test it without a provider account.

Code: `shared/connectors/qc_providers.py` (providers), `services/shield_link/` (booking, status, reports), `shared/orchestration/qc_schedule.py` (dates), `shared/orchestration/supplier_commitment.py` (goods-ready date), `services/qc_sandbox/` (simulator). Tests: `tests/test_qc_inspection.py`, catalogue cases `TC-QC-023` to `TC-QC-038`.

## 1. Which provider has an API (checked 2026-11)

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

## 4. Documents

- **QIMA:** every final report PDF (`PROD_FINAL_REPORT`) is downloaded by API when the result arrives.
- **SGS / BV:** every file listed in the webhook's `documents[]` (or the staff relay) is downloaded.
- Stored like the other trade documents: Google Drive `<GOOGLE_DRIVE_ROOT_FOLDER>/<deal_id>/QC-<tpo>-<name>.pdf`, with local disk (`SHIELD_LINK_STORAGE_DIR`) as the logged fallback. Each record has its SHA-256, size and source.
- Served at `GET /shield-link/qc/{tpo_id}/documents/{index}`. The PortLess Inspection Order (DocuGen) is still issued at `qc.booked`.

## 5. Testing without a provider account: the sandbox

`services/qc_sandbox` simulates both channels:

- **QIMA:** the documented paths, headers, envelope, field names and errors (200 / 202 / 400 / 401 / 403 / 409 / 500), async booking, report search, pdf-info and a real PDF file. QIMA documents only order status `20` and the completed `60`; the sandbox's `30` VALIDATED and `90` CANCELLED are its own stand-ins (the connector reads unknown codes by their label).
- **SGS / BV:** the booking desk receives PortLess's booking request, confirms with an order number, and issues a report with a PDF, answering with the signed webhook.
- Control endpoints play the provider: `/_sandbox/qima/orders/{id}/validate`, `/inspect` (`{"result": "PASS"|"FAIL"|"PENDING"}`), `/_sandbox/{sgs|bv}/bookings/{ref}/confirm`, `/report`.

| How | Setting |
|---|---|
| In process (tests, automation) | `QC_SANDBOX_PROVIDERS=qima,sgs,bv` (or `all`). Connectors reach the sandbox through ASGI, with no network. |
| Standalone | `make qc-sandbox` (port 8014) + `QC_SANDBOX_URL=http://localhost:8014`. A `callback_url` on confirm or report delivers the webhook to a running PortLess. |

The automation worker always runs with the sandbox. Readiness check `qc_sandbox` **fails in production** while it is set; it is never a silent fallback.

## 6. Go-live checklist

1. QIMA: get the `Ai-Api-Access-Token`, a client account and the production host. Set `QIMA_*`, and read the product ids for your HS codes from `GET /parameter/v2/product-tree`. Confirm the AQL value format your account expects (the guide's own example uses `"0"`, `"1"`, `"3"`).
2. SGS / BV: get the booking desk addresses and set `SGS_BOOKING_EMAIL` / `BUREAU_VERITAS_BOOKING_EMAIL`. Agree with each desk to send reports to the PortLess mailbox. Set `SGS_WEBHOOK_SECRET` / `BUREAU_VERITAS_WEBHOOK_SECRET` if a relay posts the webhook.
3. Leave `QC_SANDBOX_PROVIDERS` unset. Readiness `qc_provider` is `ok` once one of the above is configured.
4. Open: a separate QIMA container-loading (CLC) order is not booked yet. The loading supervision is requested in `customField3`; the seal record comes from the inspector's report through `POST /shield-link/deals/{id}/loading`.
