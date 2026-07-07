# Email Monitor — Workflow & Implementation Reference

> **Module:** `services/email_monitor/`  
> **Role:** Cross-cutting email I/O hub — used by the entire PortLess platform wherever email must be sent or received.  
> **Persistence:** MongoDB Motor (in-memory fallback for dev/CI)

---

## Table of Contents

1. [Purpose & Scope](#1-purpose--scope)
2. [Where It Is Used Across the App](#2-where-it-is-used-across-the-app)
3. [Inbound Pipeline — Receive & Route](#3-inbound-pipeline--receive--route)
4. [Inbound Sources](#4-inbound-sources)
5. [Email Mapping Strategy](#5-email-mapping-strategy)
6. [Price Extraction](#6-price-extraction)
7. [IMAP Inbox Polling](#7-imap-inbox-polling)
8. [HITL Escalation for Unresolvable Emails](#8-hitl-escalation-for-unresolvable-emails)
9. [Webhook Signature Verification](#9-webhook-signature-verification)
10. [Persistence & Data Model](#10-persistence--data-model)
11. [API Endpoints](#11-api-endpoints)
12. [Configuration Reference](#12-configuration-reference)
13. [Status Reference](#13-status-reference)
14. [App Integration (Monolith & Microservice)](#14-app-integration-monolith--microservice)
15. [Implementation Status](#15-implementation-status)

---

## 1. Purpose & Scope

The Email Monitor is the **single gateway for all email I/O** in PortLess.  
It handles:

- **Receiving** inbound emails from buyers and suppliers (via IMAP polling or provider webhooks).
- **Routing** those emails to the correct negotiation session.
- **Parsing** counter-offer prices from email body text.
- **Triggering** the next negotiation round automatically.
- **Auditing** every inbound email with full lifecycle status tracking.
- **Escalating** unresolvable emails to a human operator via HITL.

Outbound sending (SMTP) lives in `agents/negotiation_agent/email_dispatcher.py`.  
The Email Monitor only covers the **inbound side** but bridges back into the negotiation pipeline for every parsed reply.

---

## 2. Where It Is Used Across the App

| Consumer | How It Calls Email Monitor | What It Expects Back |
|----------|-----------------------------|----------------------|
| **Negotiation Agent** | `service.record_inbound_email()` — called by email_monitor after mapping, so the negotiation agent's own email_history audit table also gets the inbound record | RECEIVED record in the negotiation's email thread |
| **HITL Approval service** | Email monitor calls `hitl_approval.service.create_approval(action="PARSE_INBOUND_EMAIL")` when price extraction fails | HITL approval created; operator resolves and re-submits price |
| **Operator dashboard (UI)** | `GET /email-monitor/inbound` + `GET /email-monitor/inbound/{id}` | List of inbound emails for the operator queue |
| **Any unmonitored channel** (WhatsApp, phone, portal) | `POST /email-monitor/inbound/manual` — operator pastes the text, optionally supplies `negotiation_id` + `counter_price` | Runs through same 6-step pipeline; negotiation advanced |
| **SendGrid cloud email** | Webhook `POST /email-monitor/inbound/sendgrid` | Email processed; negotiation round advanced |
| **Mailgun cloud email** | Webhook `POST /email-monitor/inbound/mailgun` | Same |
| **Gmail IMAP (autonomous)** | `ImapPoller` polls inbox → calls `service.process_inbound()` directly | Fully autonomous email-driven negotiation |

---

## 3. Inbound Pipeline — Receive & Route

Every inbound email (regardless of source) passes through the same 6-step pipeline in `service.process_inbound()`:

```
Email arrives (webhook POST, IMAP poll, or manual submit)
       │
       ▼
Step 1 — Deduplicate
  └── Check message_id in repository
  └── If already stored → return existing result (idempotent)
       │
       ▼
Step 2 — Persist RECEIVED record (crash-safe audit trace)
  └── status = RECEIVED, source = imap|sendgrid|mailgun|manual
       │
       ▼
Step 3 — Map to NegotiationSession (mapper.map_email)
  └── Priority 1: In-Reply-To header → parse <EMAIL-id.neg-id@portless.io>
  └── Priority 2: References chain  → same pattern across all Message-IDs
  └── Priority 3: Subject [NEG:uuid] → token appended by EmailDispatcher
  └── Priority 4: Manual override   → operator-supplied negotiation_id
  └── No match → status = UNMATCHED, log warning, return
       │
       ▼
Step 4 — Enrich & cross-post
  └── Fetch deal_id from negotiation session
  └── status = MAPPED
  └── Call negotiation_agent.service.record_inbound_email()
       │
       ▼
Step 5 — Extract counter-price
  └── 5 regex patterns (ordered: $X/MT, X USD, "price is X", X per kg, [PRICE:X])
  └── Price outside [min, max] bounds discarded
  └── No price found → status = NEEDS_OPERATOR → create HITL approval, return
       │
       ▼
Step 6 — Advance negotiation
  └── run_round(negotiation_id, RoundRequest(counter_price, party, channel="email"))
  └── Success → status = PROCESSED, processed_at stamped
  └── run_round() fails → status = NEEDS_OPERATOR → create HITL approval, return
```

---

## 4. Inbound Sources

| Source | How Configured | Endpoint / Trigger |
|--------|---------------|--------------------|
| **Gmail IMAP** | `IMAP_MONITOR_USER` + `IMAP_MONITOR_PASSWORD` in `.env` | `ImapPoller` background task; polls every `IMAP_MONITOR_POLL_INTERVAL_SECONDS` (default 600s) |
| **SendGrid Inbound Parse** | MX record → SendGrid, webhook URL in dashboard | `POST /email-monitor/inbound/sendgrid` |
| **Mailgun Routes** | Mailgun domain + Route forward rule | `POST /email-monitor/inbound/mailgun` |
| **Manual submission** | Operator uses the PortLess dashboard or API directly | `POST /email-monitor/inbound/manual` |

All four sources funnel into the **same `process_inbound()` function** — the processing logic is identical regardless of origin.

---

## 5. Email Mapping Strategy

Implemented in `mapper.py`. Three strategies applied in strict priority order:

### Strategy 1: Email threading headers (highest confidence)

The `EmailDispatcher` (negotiation agent outbound) embeds a structured `Message-ID` on every outbound email:

```
Message-ID: <EMAIL-{email_id}.{negotiation_id}@portless.io>
```

When the buyer/supplier replies, their email client populates `In-Reply-To` and `References` with this value. The mapper extracts the UUID and looks up the active session.

### Strategy 2: Subject token (medium confidence)

Every outbound subject line includes `[NEG:{negotiation_id}]`:

```
Subject: PortLess Trade Update — Round 2 [NEG:3f8a1b2c-...]
```

This token survives email clients that strip `In-Reply-To`/`References` headers.

### Strategy 3: Manual override (operator-supplied)

Operator passes `negotiation_id` explicitly in the manual submission request. Skips the above entirely.

### Party determination

After mapping, `_get_party()` looks up the outbound `EmailHistoryRecord` that was sent *to* the sender's address — the `party` field on that record (`buyer`/`supplier`) tells us who replied. Falls back to IMAP folder hint if email history is empty.

---

## 6. Price Extraction

Five regex patterns, applied in order (most specific first):

```python
r"\$\s*([\d,]+(?:\.\d{1,2})?)\s*(?:/\s*(?:MT|kg|unit|pc|pcs|piece|ton|lb))?"   # $450/MT
r"([\d,]+(?:\.\d{1,2})?)\s*(?:USD|usd)\b"                                        # 450 USD
r"(?:price|offer|quote|bid|ask|propose|counter)[^\d$]{0,20}([\d,]+...)"           # "offer 450"
r"([\d,]+(?:\.\d{1,2})?)\s*(?:per\s+(?:MT|kg|unit|piece|pc|pcs|ton))"            # 450 per MT
r"\[PRICE:([\d.]+)\s*(?:USD)?\]"                                                   # [PRICE:450 USD]
```

Extracted values are validated against `price_extraction_min` / `price_extraction_max` (configurable). The `[PRICE:X]` marker can be injected by the operator in the manual submission body as an unambiguous override.

---

## 7. IMAP Inbox Polling

Implemented in `imap_poller.py`. Two independent `ImapPoller` instances run as background asyncio tasks — one for the buyer folder, one for the supplier folder.

### Lifecycle

```
on_startup() → create_buyer_poller(callback) + create_supplier_poller(callback)
             → poller.start() → creates asyncio.Task(_poll_loop)
                                    └── _fetch_unseen() → yield InboundRawEmail
                                    └── callback(raw_email, party_override=hint)

on_shutdown() → poller.stop() → cancel task, wait for cleanup
```

### Error handling

Consecutive fetch errors trigger exponential back-off up to 10 minutes:

```python
backoff = min(60 * consecutive_errors, 600)
```

### Gmail label separation

For production, each party (buyer/supplier) gets a dedicated Gmail label:

| Label | `IMAP_MONITOR_BUYER_FOLDER` | `IMAP_MONITOR_SUPPLIER_FOLDER` |
|-------|------------------------------|-------------------------------|
| `PortLess-Buyers` | Gmail filter: subject contains `[NEG:]` from known buyer domains |  |
| `PortLess-Suppliers` | | Gmail filter: `[NEG:]` from known supplier domains |

When both folders are `INBOX` (default), `message_id` deduplication prevents double-processing.

### ⚠️ Current status

`_fetch_unseen()` is a stub. The aioimaplib connection code is written out in a comment block inside the method but is not active code. Fill in the `── IMPLEMENT using aioimaplib ──` section to enable real polling.

---

## 8. HITL Escalation for Unresolvable Emails

When the pipeline cannot automatically resolve an inbound email, it creates a `PARSE_INBOUND_EMAIL` HITL approval:

| Trigger | HITL created? | Operator action |
|---------|--------------|----------------|
| Price not found in body | ✅ Yes (L0) | Operator reads email, enters price manually |
| `run_round()` throws exception | ✅ Yes (L0) | Operator resolves the negotiation state |
| Email unmatched (no negotiation found) | ❌ No — logged as UNMATCHED | Operator re-submits via `POST /email-monitor/inbound/manual` with `negotiation_id` |

The HITL `callback_payload` carries `inbound_id`, `negotiation_id`, `party`, and `body_preview` so the operator can re-call the manual endpoint with the correct data.

---

## 9. Webhook Signature Verification

| Provider | Algorithm | Status |
|----------|-----------|--------|
| **Mailgun** | HMAC-SHA256 over `timestamp + token` | ✅ Fully implemented |
| **SendGrid** | ECDSA (NIST P-256) over `timestamp + body` | ⚠️ Placeholder — code is commented-in but not active |

To enable SendGrid verification: install `ecdsa` (`pip install ecdsa`) and uncomment the body in `_verify_sendgrid_signature()`.

---

## 10. Persistence & Data Model

### Collection: `inbound_emails`

```json
{
  "inbound_id":         "IN-A1B2C3D4E5",
  "from_address":       "supplier@bangkokrice.th",
  "to_address":         "portless-trade@portless.io",
  "subject":            "Re: PortLess Trade Quote — Round 1 [NEG:uuid]",
  "body_text":          "We can offer $460/MT Ex-Works...",
  "message_id":         "<xyz@bangkokrice.th>",
  "in_reply_to":        "<EMAIL-A1B2....neg-uuid@portless.io>",
  "references":         ["<EMAIL-A1B2....neg-uuid@portless.io>"],
  "source":             "sendgrid_webhook",
  "status":             "processed",
  "negotiation_id":     "neg-3f8a1b2c...",
  "deal_id":            "DEAL-2025-001",
  "party":              "supplier",
  "mapping_confidence": "header",
  "extracted_price":    460.0,
  "hitl_approval_id":   null,
  "processed_at":       "2025-03-15T14:05:00Z",
  "received_at":        "2025-03-15T14:04:58Z",
  "created_at":         "2025-03-15T14:04:59Z",
  "updated_at":         "2025-03-15T14:05:01Z"
}
```

### MongoDB indexes

| Field(s) | Type | Purpose |
|----------|------|---------|
| `inbound_id` | Unique | Primary key |
| `negotiation_id` | Single | Filter by negotiation |
| `message_id` | Single | Deduplication lookup |
| `status` | Single | Queue filtering (NEEDS_OPERATOR, UNMATCHED) |
| `received_at DESC` | Single | Chronological listing |

---

## 11. API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/email-monitor/health` | Health check |
| `POST` | `/email-monitor/inbound/sendgrid` | SendGrid Inbound Parse webhook |
| `POST` | `/email-monitor/inbound/mailgun` | Mailgun Routes webhook |
| `POST` | `/email-monitor/inbound/manual` | Operator manual email submission |
| `GET` | `/email-monitor/inbound` | List inbound emails (filter: status, negotiation_id, limit) |
| `GET` | `/email-monitor/inbound/{inbound_id}` | Get single inbound email record |

### Missing operator action endpoints (not yet implemented)

| Method | Path | Needed for |
|--------|------|-----------|
| `PATCH` | `/email-monitor/inbound/{id}/reject` | Mark email as spam/irrelevant |
| `PATCH` | `/email-monitor/inbound/{id}/reassign` | Re-map UNMATCHED email to a negotiation |
| `POST` | `/email-monitor/inbound/{id}/retry` | Re-run pipeline on UNMATCHED/NEEDS_OPERATOR |

---

## 12. Configuration Reference

| Variable | Default | Effect |
|----------|---------|--------|
| `EMAIL_MONITOR_MONGO_URI` | `""` | MongoDB; empty = in-memory fallback |
| `PORTLESS_TRADE_ADDRESS` | `portless-trade@portless.io` | Receiving address used in `to_address` |
| `IMAP_MONITOR_HOST` | `imap.gmail.com` | IMAP server host |
| `IMAP_MONITOR_PORT` | `993` | IMAP SSL port |
| `IMAP_MONITOR_USER` | `""` | Gmail address; empty = polling disabled |
| `IMAP_MONITOR_PASSWORD` | `""` | Gmail App Password (not account password) |
| `IMAP_MONITOR_USE_SSL` | `true` | SSL/TLS for IMAP connection |
| `IMAP_MONITOR_POLL_INTERVAL_SECONDS` | `600` | Polling interval (10 min default) |
| `IMAP_MONITOR_BUYER_FOLDER` | `INBOX` | Gmail label for buyer replies |
| `IMAP_MONITOR_SUPPLIER_FOLDER` | `INBOX` | Gmail label for supplier replies |
| `SENDGRID_WEBHOOK_PUBLIC_KEY` | `""` | SendGrid ECDSA public key; empty = skip verification |
| `MAILGUN_SIGNING_KEY` | `""` | Mailgun HMAC key; empty = skip verification |
| `PRICE_EXTRACTION_MIN` | `0.01` | Minimum valid extracted price |
| `PRICE_EXTRACTION_MAX` | `10_000_000.0` | Maximum valid extracted price |

---

## 13. Status Reference

### `InboundEmailRecord.status`

| Status | Meaning | Terminal? |
|--------|---------|-----------|
| `RECEIVED` | Parsed and stored, mapping in progress | No |
| `MAPPED` | Negotiation session identified | No |
| `PROCESSED` | `run_round()` called; negotiation advanced | **Yes** |
| `NEEDS_OPERATOR` | Price not found OR `run_round()` failed; HITL created | No (awaiting HITL resolution) |
| `AMBIGUOUS` | Sender matched multiple active negotiations | No (needs operator) |
| `UNMATCHED` | No negotiation found | **Yes** (operator must re-submit manually) |
| `REJECTED` | Marked as spam/irrelevant by operator | **Yes** |

### `MappingConfidence`

| Value | How matched |
|-------|------------|
| `HEADER` | In-Reply-To or References header matched `<EMAIL-id.neg-id@portless.io>` |
| `SUBJECT_TOKEN` | `[NEG:uuid]` token in subject line |
| `MANUAL_OVERRIDE` | Operator supplied `negotiation_id` in the request |
| `AMBIGUOUS` | Sender address matched multiple sessions (not yet implemented) |
| `UNMATCHED` | No strategy succeeded |

---

## 14. App Integration (Monolith & Microservice)

### Monolith (`app.py`)

```python
# Router mounted:
from services.email_monitor.router import router as email_monitor_router
app.include_router(email_monitor_router)

# Startup / shutdown wired:
from services.email_monitor.main import on_startup as em_startup, on_shutdown as em_shutdown
await em_startup()   # initialises repository + starts IMAP pollers
await em_shutdown()  # stops pollers, closes repository
```

### Cross-service calls (monolith mode — in-process)

```python
# Email monitor → negotiation agent
from agents.negotiation_agent.service import run_round, record_inbound_email
from agents.negotiation_agent.repository import get_repository as neg_repo

# Email monitor → HITL approval
from services.hitl_approval.service import create_approval
```

In microservice mode, replace these with HTTP calls via the respective service clients (`NegotiationAgentClient`, `HitlApprovalClient`).

---

## 15. Implementation Status

> Last reviewed: 2026-07-07.  
> Legend: ✅ Fully implemented · ⚠️ Partial / stub · ❌ Not started · 💡 Nice-to-have

### Inbound processing pipeline (`service.py`)

| Feature | Status | Notes |
|---------|--------|-------|
| Crash-safe RECEIVED record on arrival | ✅ | Written before any processing |
| Message-ID deduplication (idempotent re-delivery) | ✅ | `find_by_message_id()` — safe for webhook retries |
| Map email to negotiation session | ✅ | 3-tier strategy via `mapper.map_email()` |
| Fetch `deal_id` from negotiation session | ✅ | Non-fatal; debug-logged on failure |
| Cross-post to negotiation agent email_history | ✅ | `record_inbound_email()` called after mapping |
| Price extraction (5 regex patterns) | ✅ | `$X/MT`, `X USD`, contextual, `per MT`, `[PRICE:X]` |
| Price bounds validation (min/max) | ✅ | Configurable via env |
| Advance negotiation via `run_round()` | ✅ | Full `RoundRequest` with `channel="email"` |
| `PROCESSED` status + `processed_at` timestamp | ✅ | |
| `NEEDS_OPERATOR` status + HITL creation on price fail | ✅ | `PARSE_INBOUND_EMAIL` HITL approval (L0) |
| `NEEDS_OPERATOR` status + HITL creation on `run_round()` fail | ✅ | Same HITL type |
| `UNMATCHED` status + warning log | ✅ | No HITL — operator re-submits manually |
| Full status lifecycle persisted to repository | ✅ | Every state transition saved |

### Email mapping (`mapper.py`)

| Feature | Status | Notes |
|---------|--------|-------|
| In-Reply-To header parsing | ✅ | Pattern: `<EMAIL-id.neg-id@portless.io>` |
| References chain scanning | ✅ | All Message-IDs in chain scanned |
| `[NEG:uuid]` subject token | ✅ | Fallback when headers stripped by email client |
| Manual override (operator-supplied `negotiation_id`) | ✅ | Highest priority; skips automatic matching |
| Party determination via outbound email history | ✅ | Looks up `to_address` in `EmailHistoryRecord` |
| Party fallback from IMAP folder hint | ✅ | `party_hint` set per poller (buyer/supplier) |
| Active session validation before mapping | ✅ | Rejects mapping to terminal sessions |
| `AMBIGUOUS` status (sender in multiple active negotiations) | ⚠️ | Enum + schema exist; mapper intentionally skips sender-address lookup to avoid false positives. No resolution endpoint. |

### Inbound sources

| Source | Status | Notes |
|--------|--------|-------|
| Manual submission via REST API | ✅ | `POST /email-monitor/inbound/manual` with optional `negotiation_id` + `counter_price` override |
| SendGrid Inbound Parse webhook | ✅ | Form parsing, MIME normalisation, raw headers parsed |
| Mailgun Routes webhook | ✅ | Form parsing, JSON headers parsed |
| Gmail IMAP polling — infrastructure | ✅ | `ImapPoller` class, lifecycle, back-off, per-party pollers, Gmail label config |
| Gmail IMAP polling — actual connection | ⚠️ | `_fetch_unseen()` is a stub; aioimaplib code written in comment block, marked `── IMPLEMENT ──` |

### Webhook signature verification

| Provider | Status | Notes |
|----------|--------|-------|
| Mailgun HMAC-SHA256 | ✅ | `HMAC(signing_key, timestamp+token)` — fully implemented |
| SendGrid ECDSA (NIST P-256) | ⚠️ | Code written in comment block inside `_verify_sendgrid_signature()`; function currently just logs debug and returns. Install `ecdsa` and uncomment to activate. |

### Persistence (`repository.py`)

| Feature | Status | Notes |
|---------|--------|-------|
| `InMemoryEmailMonitorRepository` (dev/CI) | ✅ | Full CRUD + `find_by_message_id` |
| `MongoEmailMonitorRepository` (production) | ✅ | Motor async, upsert on save |
| Indexes: `inbound_id` (unique), `negotiation_id`, `message_id`, `status`, `received_at DESC` | ✅ | Created on startup |
| `init_repository()` / `close_repository()` | ✅ | Falls back to in-memory on MongoDB failure |
| Repository guard (`get_repository()` raises if not init) | ✅ | |

### API endpoints (`router.py`)

| Endpoint | Status | Notes |
|----------|--------|-------|
| `GET /email-monitor/health` | ✅ | |
| `POST /email-monitor/inbound/sendgrid` | ✅ | |
| `POST /email-monitor/inbound/mailgun` | ✅ | |
| `POST /email-monitor/inbound/manual` | ✅ | |
| `GET /email-monitor/inbound` | ✅ | Filters: `status`, `negotiation_id`, `limit` |
| `GET /email-monitor/inbound/{inbound_id}` | ✅ | 404 on missing |
| `PATCH /email-monitor/inbound/{id}/reject` | ❌ | No endpoint to mark as spam/irrelevant |
| `PATCH /email-monitor/inbound/{id}/reassign` | ❌ | No endpoint to re-map UNMATCHED email |
| `POST /email-monitor/inbound/{id}/retry` | ❌ | No endpoint to re-run pipeline on UNMATCHED/NEEDS_OPERATOR |

### App integration

| Feature | Status | Notes |
|---------|--------|-------|
| Router mounted in monolith `app.py` | ✅ | Included in router list |
| `on_startup` / `on_shutdown` wired in `app.py` lifespan | ✅ | Repository init + IMAP poller lifecycle |
| Standalone `main.py` for microservice mode | ✅ | `create_app()` with own lifespan |
| `process_inbound()` callable from other services | ✅ | Direct import (monolith) or HTTP (microservice) |
| Cross-service calls use try/except (non-fatal) | ✅ | `record_inbound_email`, `run_round` failures don't crash the pipeline |

---

### Nice-to-have (not started)

| Feature | Priority | Notes |
|---------|----------|-------|
| Complete IMAP `_fetch_unseen()` with aioimaplib | **Critical** | Fill in the `── IMPLEMENT ──` block; uncomment the provided aioimaplib code |
| Activate SendGrid ECDSA signature verification | **High** | `pip install ecdsa`; uncomment code in `_verify_sendgrid_signature()` |
| `PATCH /inbound/{id}/reject` endpoint | **High** | Let operators mark spam; prevents HITL queue pollution |
| `PATCH /inbound/{id}/reassign` endpoint | **High** | Re-map UNMATCHED emails without full re-submission |
| `POST /inbound/{id}/retry` endpoint | **High** | Re-run pipeline after operator resolves NEEDS_OPERATOR via HITL |
| Strip quoted reply content before price extraction | High | Regex to strip `> ...` lines; avoids false matches on previously quoted prices |
| HTML body price extraction (`body_html` fallback) | Medium | Currently only `body_text` is searched; some senders omit plain text |
| Multi-currency price extraction (₹ INR, € EUR, £ GBP) | Medium | Add currency-specific patterns with conversion to USD |
| `AMBIGUOUS` mapping resolution endpoint | Medium | When sender address matches multiple negotiations; present list to operator |
| PDF/Excel attachment price extraction | Medium | Parse quoted-price attachments (pypdf / openpyxl) |
| Spam/junk filtering | Medium | Check SendGrid spam score or Mailgun spam flag; auto-reject if above threshold |
| Bulk re-process endpoint for UNMATCHED backlog | Low | `POST /email-monitor/inbound/retry-unmatched` |
| Seller/buyer address book for improved matching | Low | Maintain `from_address → negotiation_id` lookup as a confidence hint |
| Outbound bounce / delivery-failure handling | Low | Feed SendGrid/Mailgun event webhooks back in; mark outbound email as FAILED |
| Additional provider support (AWS SES, Postmark) | Low | New webhook endpoints following the same normalisation pattern |
| Rate limiting on webhook endpoints | Low | Prevent webhook flooding via a token-bucket middleware |
| UI page — Email Monitor queue | Low | Frontend page showing NEEDS_OPERATOR and UNMATCHED emails with action buttons |
| Metrics / observability | Low | Track processing latency, price-extraction hit rate, UNMATCHED rate per source |
