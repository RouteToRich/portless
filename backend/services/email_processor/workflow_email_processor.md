# Email Processor — Workflow & Implementation Reference

> **Module:** `services/email_processor/`
> **Role:** Inbound email **processing & persistence** — match a fetched email to
> a live negotiation, extract the counter-price, persist an audit record, and
> trigger the negotiation orchestration.
> **Port:** 8013 · **Persistence:** MongoDB Motor (in-memory fallback)

---

## 1. Single responsibility

The Email Processor receives already-fetched emails and does the "further
processing." It does **not** connect to IMAP/SMTP — that transport is the Email
Gateway's job (`services/email_gateway/`). Emails reach the Processor via:

- the Gateway's IMAP poller (in-process call or `POST /email-processor/inbound`),
- provider webhooks (`POST /email-processor/inbound/sendgrid` · `/mailgun`),
- manual submission (`POST /email-processor/inbound/manual`).

All entry points funnel into the same `service.process_inbound()`.

---

## 2. Processing pipeline (`service.process_inbound`)

```
Inbound email arrives (Gateway forward / webhook / manual)
   │
   ▼
1. Deduplicate by message_id (idempotent re-delivery)
   ▼
2. Persist RECEIVED record (crash-safe; attachments stored)
   ▼
3. Map to a LIVE negotiation (mapper.map_email)
      In-Reply-To → References → Subject [NEG:uuid] → override
      _negotiation_is_active() skips terminal sessions (AGREED/FAILED/ESCALATED)
      no match → status=UNMATCHED, return
   ▼
4. status=MAPPED; cross-post to negotiation_agent email_history
   ▼
5. Extract counter-price (5 regex patterns; bounds-checked)
      no price → status=NEEDS_OPERATOR + HITL approval, return
   ▼
6. run_round(neg_id, RoundRequest(price, party, channel="email",
             inbound_message, attachment_refs))   ← orchestration agent starts
      success → status=PROCESSED
      failure → status=NEEDS_OPERATOR + HITL approval
```

**Ordering guarantee:** the RECEIVED record is persisted (step 2) *before*
`run_round()` triggers the LangGraph orchestrator (step 6). A crash between the
two leaves an auditable trace.

---

## 3. API

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/email-processor/health` | Health check |
| `POST` | `/email-processor/inbound` | Receiving API — structured fetched email (`InboundApiRequest`) |
| `POST` | `/email-processor/inbound/sendgrid` | SendGrid Inbound Parse webhook |
| `POST` | `/email-processor/inbound/mailgun` | Mailgun Routes webhook |
| `POST` | `/email-processor/inbound/manual` | Operator manual submission |
| `GET` | `/email-processor/inbound` | List inbound records (filter: status, negotiation_id, limit) |
| `GET` | `/email-processor/inbound/{inbound_id}` | Fetch one inbound record |

`EmailProcessorClient` (`client.py`) wraps the receiving API — used by the Email
Gateway's poller when `FORWARD_VIA_HTTP=true`, and by any external fetcher.

---

## 4. Matching & price extraction

- **Threading (highest confidence):** parse `<EMAIL-{id}.{negotiation_id}@portless.io>`
  from `In-Reply-To`, then the `References` chain. The Email Gateway embeds this
  Message-ID on every outbound email.
- **Subject token:** `[NEG:{negotiation_id}]` — survives clients that strip
  threading headers.
- **Override:** operator-supplied `negotiation_id` (manual submission / forward).
- Only **live** negotiations match; terminal sessions are rejected.
- **Price:** 5 ordered regex patterns (`$X/MT`, `X USD`, contextual, `X per MT`,
  `[PRICE:X]`), validated against `PRICE_EXTRACTION_MIN`/`MAX`.

---

## 5. Status lifecycle

`RECEIVED → MAPPED → PROCESSED` (happy path), or terminal `UNMATCHED`, or
`NEEDS_OPERATOR` (price missing / `run_round` failed → HITL approval created).

---

## 6. Configuration

| Variable | Default | Effect |
|----------|---------|--------|
| `EMAIL_PROCESSOR_MONGO_URI` | `""` | MongoDB; empty = in-memory fallback |
| `EMAIL_PROCESSOR_MONGO_DB_NAME` | `email_processor` | Database name |
| `EMAIL_PROCESSOR_INBOUND_COLLECTION` | `inbound_emails` | Collection name |
| `EMAIL_PROCESSOR_URL` | `http://localhost:8013` | Base URL for `EmailProcessorClient` (shared config) |
| `PORTLESS_TRADE_ADDRESS` | `portless-trade@portless.io` | Receiving address |
| `PRICE_EXTRACTION_MIN` / `MAX` | `0.01` / `10000000` | Valid extracted-price bounds |
| `SENDGRID_WEBHOOK_PUBLIC_KEY` | `""` | SendGrid ECDSA key; empty = skip verification |
| `MAILGUN_SIGNING_KEY` | `""` | Mailgun HMAC key; empty = skip verification |

---

## 7. Files

| File | Responsibility |
|------|----------------|
| `config.py` | Mongo, identity, webhook keys, price bounds |
| `schemas.py` | `InboundRawEmail`, `EmailAttachment`, `InboundEmailRecord`, `InboundApiRequest`, `ProcessingResult`, enums |
| `service.py` | `process_inbound()` pipeline + price extraction + HITL escalation |
| `mapper.py` | Map email → live negotiation (headers / subject / override) |
| `repository.py` | `InMemory` + `Mongo` `EmailProcessorRepository` |
| `router.py` | Receiving API + webhooks + manual + query endpoints |
| `main.py` | Lifespan: init/close repository |
| `client.py` | `EmailProcessorClient.submit_inbound()` |

---

## 8. Where the Email Processor is used across the app

| Caller / consumer | How it interacts | Direction |
|-------------------|------------------|-----------|
| **Email Gateway** (IMAP poller) | Forwards each fetched `InboundRawEmail` → `process_inbound()` (in-process) or `POST /email-processor/inbound` | Into the Processor |
| **SendGrid / Mailgun** | Provider push → `POST /email-processor/inbound/sendgrid` · `/mailgun` (HMAC/ECDSA verified) | Into the Processor |
| **Operator / dashboard** | Manual replay → `POST /email-processor/inbound/manual`; queue views → `GET /email-processor/inbound[/{id}]` | Both |
| **Negotiation Agent** | Processor calls `run_round()` to advance the negotiation, and `record_inbound_email()` to append to the email audit thread | Out of the Processor |
| **HITL Approval** | Processor calls `create_approval()` (`PARSE_INBOUND_EMAIL`) when price extraction or `run_round()` fails | Out of the Processor |
| **Negotiation email_history** | Mapper reads outbound records (`get_email_repository`) to decide buyer vs supplier | Read |

---

## 9. Calling functions & APIs

**Entry point (how emails arrive → in-process):**
```python
from services.email_processor.service import process_inbound
from services.email_processor.schemas import InboundRawEmail, ProcessingResult

result: ProcessingResult = await process_inbound(
    raw,                             # InboundRawEmail (sender, subject, bodies, attachments)
    negotiation_id_override=None,    # skip auto-mapping when known
    party_override="supplier",       # IMAP folder hint ("buyer"|"supplier")
    counter_price_override=None,     # skip auto price-extraction when known
)
# result.status ∈ {PROCESSED, UNMATCHED, NEEDS_OPERATOR};  result.round_no set on PROCESSED
```

**Over HTTP (the Gateway in FORWARD_VIA_HTTP mode, or any external fetcher):**
```python
from services.email_processor.client import EmailProcessorClient
result = await EmailProcessorClient().submit_inbound(raw, party_override="buyer")  # POST /email-processor/inbound
```

| Function / method | Signature | Notes |
|-------------------|-----------|-------|
| `service.process_inbound` | `async (InboundRawEmail, negotiation_id_override?, party_override?, counter_price_override?) -> ProcessingResult` | The full pipeline (dedupe→persist→map→price→advance) |
| `service._extract_price` | `(body: str) -> float | None` | 5 ordered regex patterns, bounds-checked |
| `service._create_price_hitl` | `async (InboundEmailRecord) -> str` | Creates `PARSE_INBOUND_EMAIL` HITL; returns approval_id |
| `service.get_inbound` / `list_inbound` | `async (...) -> InboundEmailRecord[...]` | Query helpers behind the GET endpoints |
| `mapper.map_email` | `async (from_address, subject, in_reply_to, references, negotiation_id_override?, party_override?) -> MappingResult` | Header → References → subject token → override |
| `mapper._negotiation_is_active` | `async (negotiation_id) -> bool` | **Live-session gate** — rejects AGREED/FAILED/ESCALATED |
| `EmailProcessorClient.submit_inbound` | `async (InboundRawEmail, *, overrides) -> ProcessingResult` | Wraps `POST /email-processor/inbound` |
| `EmailProcessorClient.get_inbound` / `list_inbound` / `health` | `async (...)` | Wrap the GET endpoints |

**Cross-service calls made by the Processor** (in-process imports in the monolith;
swap for the respective clients in microservice mode):
```python
from agents.negotiation_agent.service    import run_round, record_inbound_email
from agents.negotiation_agent.repository  import get_repository as neg_repo        # deal_id lookup
from agents.negotiation_agent.email_history import get_email_repository            # mapper party lookup
from services.hitl_approval.service       import create_approval                   # + CreateApprovalRequest
```
All cross-service calls are wrapped in try/except and are **non-fatal**: a
failure in `record_inbound_email` or `run_round` is logged and downgrades the
inbound record to `NEEDS_OPERATOR` rather than crashing the pipeline.

**HTTP API + webhook helpers (`router.py`):**

| Endpoint | Handler / helper |
|----------|------------------|
| `POST /email-processor/inbound` | `inbound_api(InboundApiRequest)` |
| `POST /email-processor/inbound/sendgrid` | `inbound_sendgrid` → `_verify_sendgrid_signature` (placeholder), `_extract_form_attachments`, `_sendgrid_form_to_raw` |
| `POST /email-processor/inbound/mailgun` | `inbound_mailgun` → `_verify_mailgun_signature` (HMAC-SHA256), `_mailgun_form_to_raw` |
| `POST /email-processor/inbound/manual` | `inbound_manual(ManualInboundRequest)` |
| `GET /email-processor/inbound[/{id}]` | `list_inbound` / `get_inbound` |

---

## 10. Nice-to-have features

| Feature | Priority | Notes |
|---------|----------|-------|
| SendGrid ECDSA signature verification | High | `_verify_sendgrid_signature` is a placeholder; install `ecdsa` and verify `X-Twilio-Email-Event-Webhook-Signature` |
| `PATCH /inbound/{id}/reject` | High | Let operators mark spam/irrelevant; keeps the HITL queue clean |
| `PATCH /inbound/{id}/reassign` | High | Re-map an `UNMATCHED` email to a negotiation without full re-submission |
| `POST /inbound/{id}/retry` | High | Re-run the pipeline after an operator resolves a `NEEDS_OPERATOR` item |
| Strip quoted reply text before price extraction | High | Avoids matching a previously-quoted price in the `> ...` history |
| HTML-body price extraction fallback | Medium | Only `body_text` is scanned today; some senders omit plain text |
| Multi-currency extraction (₹ / € / £ → USD) | Medium | Add currency patterns + conversion |
| PDF / Excel attachment **content** parsing | Medium | Attachments are captured + forwarded as `attachment_refs`; parsing their contents for a price is pending (pypdf / openpyxl) |
| `AMBIGUOUS` mapping resolution endpoint | Medium | Enum + schema exist; present candidates to an operator when a sender matches several live negotiations |
| Spam filtering | Medium | Use SendGrid spam score / Mailgun flag → auto-`REJECTED` above a threshold |
| Bulk re-process for `UNMATCHED` backlog | Low | `POST /inbound/retry-unmatched` |
| Metrics / observability | Low | Processing latency, price-hit rate, UNMATCHED rate per source |

---

## 11. Pending items

| Item | Status | Detail |
|------|--------|--------|
| SendGrid ECDSA verification | ⚠️ Placeholder | `_verify_sendgrid_signature` logs and returns without verifying; Mailgun HMAC **is** implemented |
| `AMBIGUOUS` status handling | ⚠️ Partial | Enum + `MappingConfidence.AMBIGUOUS` exist; mapper skips sender-address fallback (false-positive risk) and there is no resolution endpoint |
| Operator action endpoints (reject / reassign / retry) | ❌ Not started | Only the ingestion + query endpoints exist |
| Attachment content → price | ❌ Not started | Filenames are forwarded as `attachment_refs`; contents are not parsed |
| Deal-status transition on AGREED/FAILED | ⚠️ Out of scope here | The negotiation session advances via `run_round()`, but moving the **Deal** status is owned by the negotiation agent / event bus, not the Processor |
