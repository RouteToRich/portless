# Mocked API Responses

Complete set of realistic JSON responses for every PortLess service. Use these to:
- Build UI screens without requiring the backend to run
- Understand the exact shape of every API response
- Seed frontend mock servers (MSW, json-server, Storybook)
- Validate UI rendering across all deal states and error scenarios

---

## Folder Structure

```
mocked/
├── health/                    API Gateway health checks (System Health screen)
│   ├── gateway_ok.json        GET /health — gateway online
│   ├── downstream_all_ok.json GET /health/downstream — all 13 services "ok"
│   └── downstream_degraded.json  GET /health/downstream — 2 services unreachable
│
├── rag/                       RAG knowledge base (Knowledge Base screen)
│   ├── stats.json             GET /rag/stats — indexed chunk count
│   ├── ingest_accepted.json   POST /rag/upload — 202 job accepted
│   ├── job_completed.json     GET /rag/ingest/{job_id} — completed job
│   └── query.json             POST /rag/query — test-chat answer + retrieved chunks
│
├── deals/                     Deal service responses
│   ├── deal_list.json         GET /deals — 4 deals in different states
│   ├── deal_detail_negotiating.json
│   ├── deal_detail_escrow_funded.json
│   ├── deal_detail_in_transit.json  (includes vessel tracking)
│   ├── deal_detail_paid.json        (includes all documents + customs)
│   └── deal_detail_dispute.json     (QC failure → DISPUTE)
│
├── trade_min/
│   └── scan_result.json       GET /trade-min/opportunities — 3 opportunities
│
├── match_engine/
│   └── score_result.json      GET /match-engine/score/{deal_id} — CMS breakdown
│
├── sourcing/
│   └── quote_submit_response.json   POST /sourcing/quotes — verified + transitioned
│
├── negotiation_agent/
│   ├── negotiation_agreed.json      2-round negotiation → AGREED
│   └── negotiation_failed.json      3-round negotiation → FAILED
│
├── email_gateway/                Outbound email transport (IMAP fetch + SMTP send)
│   ├── send_request.json         POST /email-gateway/send — SendEmailRequest body
│   ├── send_result_sent.json     SendEmailResult — delivered (Gmail configured)
│   └── send_result_logged.json   SendEmailResult — log-only (dev/CI, no creds)
│
├── email_processor/              Inbound email processing (match → price → advance)
│   ├── inbound_api_request.json  POST /email-processor/inbound — InboundApiRequest (w/ attachment)
│   ├── inbound_processed.json    ProcessingResult — matched live negotiation → run_round() → PROCESSED
│   ├── inbound_needs_operator.json  ProcessingResult — mapped but no price → PARSE_INBOUND_EMAIL HITL
│   ├── inbound_unmatched.json    ProcessingResult — no negotiation match → UNMATCHED (audit)
│   ├── inbound_record.json       GET /email-processor/inbound/{id} — full InboundEmailRecord
│   ├── inbound_list.json         GET /email-processor/inbound — mixed statuses
│   └── sendgrid_webhook_request.json  Provider webhook form-field reference
│
├── dealmaker/
│   ├── pi_issued.json               POST /dealmaker/pi — PI with line items
│   ├── po_issued.json               POST /dealmaker/po — PO with margin
│   └── margin_breach_error.json     422 error when margin < 8%
│
├── docugen/
│   ├── documents_list.json          GET /docugen/documents?deal_id=... — all 5 doc types
│   ├── customs_filed.json           POST /docugen/customs/file — ICEGATE LEO issued
│   └── document_render_failed.json  Document generation failure response
│
├── shield_link/
│   ├── qc_booking.json              POST /shield-link/qc — SGS booking confirmed
│   ├── qc_webhook_passed.json       POST /shield-link/webhooks/qc — PASSED
│   ├── qc_webhook_failed.json       POST /shield-link/webhooks/qc — FAILED
│   ├── freight_booking.json         POST /shield-link/freight — Cogoport confirmed
│   └── freight_milestone_cleared.json  POST /shield-link/webhooks/freight — ARRIVED
│
├── escrow_guard/
│   ├── hold_created.json            POST /escrow-guard/holds — Stripe PaymentIntent
│   ├── hold_funded_webhook.json     Stripe webhook → ESCROW_FUNDED
│   ├── payout_completed.json        Payoneer payout → PAID
│   ├── payout_failed_webhook.json   Payoneer failure → exception raised
│   └── deal_escrow_summary.json     GET /escrow-guard/deals/{id}/summary
│
├── hitl_approval/
│   ├── approval_pending_l0.json     L0 — manual approval required, no timeout
│   ├── approval_pending_l1.json     L1 — auto-approves on timeout
│   ├── approval_auto_approved_l2.json  L2 — auto-approved, operator notified
│   ├── approval_rejected.json       Operator REJECT → exception raised
│   ├── approval_list.json           GET /hitl-approval/approvals — 3 items
│   └── sse_events.json              SSE stream examples + resolve request shapes
│
├── exception_resolver/
│   ├── exception_qc_failed.json        QC_FAILED — HIGH severity, ESCALATED
│   ├── exception_payment_failed.json   PAYMENT_FAILED — CRITICAL, ESCALATED
│   ├── exception_freight_delay.json    FREIGHT_DELAY — MEDIUM, MONITOR → RESOLVED
│   ├── exception_customs_rejected.json CUSTOMS_REJECTED — HIGH, ESCALATED
│   ├── exception_document_error.json   DOCUMENT_ERROR — LOW, AUTO_FIX → RESOLVED
│   ├── exception_margin_breach.json    MARGIN_BREACH — HIGH, ESCALATED
│   ├── exception_connector_error.json  CONNECTOR_ERROR — LOW, MONITOR → RESOLVED
│   ├── exception_negotiation_failed.json NEGOTIATION_FAILED — HIGH, CANCEL→ESCALATED
│   └── exception_list.json            GET /exception-resolver/exceptions — 4 items
│
├── flows/                      End-to-end sequence maps
│   ├── happy_path_full_flow.json    All 17 steps DRAFT → PAID
│   ├── qc_failure_exception_flow.json  QC_FAILED → DISPUTE path
│   └── payment_failure_flow.json   Payout failure → retry path
│
└── third_party/                Raw upstream responses for every external API
    │                           (what shared/connectors/* receive) — see its README
    ├── un_comtrade.json · us_census_trade.json · gdelt_news.json
    ├── wits_tariff.json · macmap_tariff.json · alibaba_rfq.json
    ├── indiamart_leads.json · volza_import_records.json · eu_ted_tenders.json
    ├── vies_vat.json · apisetu_{gstn,dgft_iec,mca,udyam}.json · allianz_trade_credit.json
    └── sgs_inspection.json · freightos_rate.json · cogoport_rate.json
        · icegate_shipping_bill.json · stripe_payment_intent.json · payoneer_payout.json
```

> **`third_party/`** holds realistic *upstream* API responses (real shapes,
> cross-checked against public docs where available) so you can test connector
> parsing and run the pipeline without live keys. See `third_party/README.md`.

---

## Deal Status Reference

| Status | Screen | Key API |
|---|---|---|
| `DRAFT` | Deal created, awaiting matching | `POST /deals` |
| `MATCHING` | Match Engine scoring | `POST /match-engine/score` |
| `NEGOTIATING` | Negotiation rounds in progress | `POST /sourcing/quotes` |
| `NEGOTIATION_AGREED` | Price locked, issue PI | `POST /negotiation-agent/.../round` |
| `PI_SENT` | PI sent to buyer | `POST /dealmaker/pi` |
| `PI_ACCEPTED` | Buyer confirmed PI | `POST /dealmaker/pi/{id}/accept` |
| `ESCROW_PENDING` | Awaiting buyer payment | `POST /escrow-guard/holds` |
| `ESCROW_FUNDED` | Buyer paid, ready for PO | Stripe webhook |
| `PO_ISSUED` | PO sent to supplier | `POST /dealmaker/po` |
| `SUPPLIER_DISPATCH_READY` | QC booked, supplier packing | `POST /shield-link/qc` |
| `QC_PASSED` | SGS inspection passed | SGS webhook |
| `QC_FAILED` | SGS inspection failed | SGS webhook → exception |
| `IN_TRANSIT` | Vessel departed | `POST /shield-link/freight` |
| `CLEARED` | Arrived + customs cleared | Freight webhook |
| `PAID` | Supplier paid | Payoneer webhook |
| `DISPUTE` | QC failure dispute open | Exception resolver → HITL |
| `CANCELLED` | Deal cancelled | HITL REJECT on CANCEL |
| `REFUNDED` | Escrow refunded to buyer | Manual process |

---

## HITL Approval Actions

| Action | Triggered by | Autonomy |
|---|---|---|
| `ISSUE_PO` | DealMaker before PO issuance | L0 (always ask) |
| `BOOK_FREIGHT` | Shield-Link before freight booking | L1 (auto-approve on timeout) |
| `RELEASE_SUPPLIER_PAYOUT` | Escrow-Guard before payout | L2 (auto-approve, notify) |
| `FILE_CUSTOMS` | DocuGen/Shield-Link for CHA filing | L1 |
| `RESOLVE_EXCEPTION` | Exception Resolver on escalation | L1 |
| `SEND_NEGOTIATION_EMAIL` | Negotiation Agent before each outbound draft (sent via Email Gateway) | L0 |
| `PARSE_INBOUND_EMAIL` | Email Processor when a mapped reply has no extractable price | L0 |

---

## Exception Categories

| Category | Triggered by | Typical Decision |
|---|---|---|
| `QC_FAILED` | shield_link | ESCALATE |
| `FREIGHT_DELAY` | shield_link | MONITOR |
| `PAYMENT_FAILED` | escrow_guard | ESCALATE |
| `NEGOTIATION_FAILED` | negotiation_agent | CANCEL → ESCALATE |
| `CUSTOMS_REJECTED` | shield_link/docugen | ESCALATE |
| `DOCUMENT_ERROR` | docugen | AUTO_FIX |
| `CONNECTOR_ERROR` | any service | MONITOR |
| `MARGIN_BREACH` | dealmaker | ESCALATE |
| `APPROVAL_REJECTED` | hitl_approval | ESCALATE |
| `CALLBACK_FAILED` | hitl_approval | ESCALATE |

---

## SSE Integration (HITL stream)

```javascript
// Connect to SSE stream
const events = new EventSource('/hitl-approval/stream');

events.onmessage = (e) => {
  const data = JSON.parse(e.data);
  if (data.event_type === 'approval_created') {
    showApprovalBadge(data);
    // Navigate to deep_link for operator to review
  }
  if (data.event_type === 'approval_resolved') {
    updateApprovalStatus(data.approval_id, data.status);
  }
};
```

See `hitl_approval/sse_events.json` for exact event shapes and resolve request bodies.
