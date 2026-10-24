# Email Gateway — Workflow & Implementation Reference

> **Module:** `services/email_gateway/`
> **Role:** Email **transport** — a single home for IMAP + SMTP. Monitors/fetches
> inbound mail (automatic) and sends outbound mail (API).
> **Port:** 8012 · **State:** none (stateless transport)

---

## 1. Single responsibility

The Email Gateway owns all **credentialed email I/O** and nothing else:

- **Inbound (IMAP):** an automatic poller fetches new mail every 15 minutes and
  **forwards** each message to the Email Processor. It does not match, persist,
  or interpret emails.
- **Outbound (SMTP):** a `POST /email-gateway/send` API sends one email. Called
  by the negotiation flow after HITL approval.

Because both halves use the **same Gmail account**, the gateway is the single
place IMAP and SMTP credentials live (`IMAP_*` + `GMAIL_*`/`SMTP_*`).

Inbound *processing* (match to a live negotiation, extract price, persist,
advance) lives in the separate **Email Processor** (`services/email_processor/`).

---

## 2. Inbound flow (automatic)

```
Every IMAP_POLL_INTERVAL_SECONDS (default 900 = 15 min):
  ImapPoller (buyer folder)     ImapPoller (supplier folder)
        │                              │
        ▼                              ▼
  connect IMAP → SEARCH UNSEEN → FETCH RFC822 → mark \Seen
        │
        ▼
  parse MIME → InboundRawEmail (sender, subject, bodies, attachments)
        │
        ▼
  forward callback:
    • in-process  → email_processor.service.process_inbound   (default)
    • HTTP        → EmailProcessorClient.submit_inbound         (FORWARD_VIA_HTTP=true)
```

- Two pollers run as background asyncio tasks (buyer + supplier folders). When
  both folders are `INBOX` (default) the Processor's `message_id` dedupe keeps
  each email single-processed.
- Each message is marked `\Seen` immediately so a mid-batch crash does not
  re-fetch it.
- Attachments are captured (`filename`, `content_type`, `size`, base64) and
  travel with the forwarded `InboundRawEmail`.
- Polling is disabled (logged, not an error) when `IMAP_USER` / `IMAP_PASSWORD`
  are unset — the SMTP send API still works.
- Consecutive fetch errors back off exponentially (`min(60·n, 600)` seconds).

## 3. Outbound flow (API)

```
negotiation_agent.send_approved_email()   (after HITL approve)
        │
        ▼
sender.send_email(SendEmailRequest)   ==  POST /email-gateway/send
        │
        ├── Gmail configured  → aiosmtplib STARTTLS send → SendOutcome.SENT
        └── not configured    → log only                 → SendOutcome.LOGGED
        (configured send failure → raises EmailSendError → HTTP 502)
```

The gateway embeds a structured `Message-ID` `<{email_id}.{negotiation_id}@portless.io>`
and appends a `[NEG:{negotiation_id}]` subject token on every outbound email, so
the Email Processor can thread the eventual reply back to its negotiation.

---

## 4. API

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/email-gateway/health` | Health check |
| `POST` | `/email-gateway/send` | Send one email (`SendEmailRequest` → `SendEmailResult`) |

`EmailGatewayClient` (`client.py`) wraps `/send` for the negotiation agent in
microservice mode; in the monolith the flow imports `sender.send_email` directly.

---

## 5. Configuration

| Variable | Default | Effect |
|----------|---------|--------|
| `IMAP_HOST` | `imap.gmail.com` | IMAP server host |
| `IMAP_PORT` | `993` | IMAP SSL port |
| `IMAP_USER` | `""` | Gmail address; empty = polling disabled |
| `IMAP_PASSWORD` | `""` | Gmail App Password (not account password) |
| `IMAP_USE_SSL` | `true` | SSL/TLS for IMAP |
| `IMAP_POLL_INTERVAL_SECONDS` | `900` | Poll cadence (**15 min**) |
| `IMAP_BUYER_FOLDER` | `INBOX` | Gmail label for buyer replies |
| `IMAP_SUPPLIER_FOLDER` | `INBOX` | Gmail label for supplier replies |
| `GMAIL_SENDER_ADDRESS` | `""` | SMTP sender; empty = log-only send |
| `GMAIL_APP_PASSWORD` | `""` | SMTP App Password (same account as IMAP) |
| `SMTP_FROM_NAME` | `PortLess Trade Agent` | From display name |
| `SMTP_HOST` / `SMTP_PORT` | `smtp.gmail.com` / `587` | SMTP server (STARTTLS) |
| `FORWARD_VIA_HTTP` | `false` | `false` = forward inbound in-process; `true` = POST to Processor API |
| `EMAIL_GATEWAY_URL` | `http://localhost:8012` | Base URL for `EmailGatewayClient` (shared config) |

---

## 6. Files

| File | Responsibility |
|------|----------------|
| `config.py` | Single IMAP + SMTP settings |
| `imap_poller.py` | `ImapPoller` (fetch + MIME/attachment parse) + buyer/supplier factories |
| `sender.py` | `send_email()` SMTP transport + `EmailSendError` |
| `schemas.py` | `SendEmailRequest`, `SendEmailResult`, `SendOutcome` |
| `router.py` | `/email-gateway/health`, `/email-gateway/send` |
| `main.py` | Lifespan: pick forward callback, start/stop pollers |
| `client.py` | `EmailGatewayClient.send_email()` |

---

## 7. Where the Email Gateway is used across the app

| Caller / consumer | How it interacts | Direction |
|-------------------|------------------|-----------|
| **Negotiation Agent** (`send_approved_email`) | Imports `services.email_gateway.sender.send_email` (monolith) or uses `EmailGatewayClient` (microservice) to deliver an HITL-approved draft | Outbound (calls the Gateway) |
| **Email Processor** (`process_inbound`) | Receives each fetched `InboundRawEmail` the Gateway forwards — in-process call or `POST /email-processor/inbound` | Inbound (the Gateway calls it) |
| **HITL callback** (`hitl_approval/callbacks.py`) | On `SEND_NEGOTIATION_EMAIL` approve → `negotiation_agent.send_approved_email()` → Gateway `send_email()` | Outbound (indirect) |
| **Gmail mailbox** | The IMAP pollers connect out to `imap.gmail.com:993` and pull UNSEEN mail; the sender connects to `smtp.gmail.com:587` | Both |
| **External IMAP fetcher** (optional) | Can replace the built-in poller entirely and `POST /email-processor/inbound` directly — the Gateway is then only the SMTP half | Inbound |

The Gateway has **no inbound dependency on domain logic** — it never imports
negotiation/deal code. Its only outbound dependency is the Processor's
`InboundRawEmail`/`InboundApiRequest` schema (the forwarding contract).

---

## 8. Calling functions & APIs — outbound (SMTP)

**In-process (monolith):**
```python
from services.email_gateway.sender import send_email, EmailSendError
from services.email_gateway.schemas import SendEmailRequest, SendEmailResult, SendOutcome

result: SendEmailResult = await send_email(SendEmailRequest(
    to_address="supplier@example.com",
    subject="Re: PortLess Quote — Round 2",
    body="We can meet you at $455/MT.",
    negotiation_id="3f8a1b2c-...",   # → subject [NEG:...] + Message-ID threading
    round_no=2,
    email_id="EMAIL-A1B2C3",         # → unique Message-ID for reply mapping
    cc=[],                            # optional
))
# result.outcome ∈ {SENT, LOGGED};  result.message_id = "<EMAIL-A1B2C3.3f8a...@portless.io>"
# raises EmailSendError when Gmail IS configured but the SMTP send fails
```

**Over HTTP (microservice):**
```python
from services.email_gateway.client import EmailGatewayClient
result = await EmailGatewayClient().send_email(SendEmailRequest(...))   # POST /email-gateway/send
```

| Function / method | Signature | Notes |
|-------------------|-----------|-------|
| `sender.send_email` | `async (SendEmailRequest) -> SendEmailResult` | Log-only when unconfigured; raises `EmailSendError` on configured failure |
| `sender._build_message_id` | `(email_id, negotiation_id) -> str` | `""` unless both present |
| `sender._build_mime` | `(req, message_id) -> MIMEMultipart` | Adds `[NEG:]` subject token, `Message-ID`, `Cc` |
| `EmailGatewayClient.send_email` | `async (SendEmailRequest) -> SendEmailResult` | Wraps `POST /email-gateway/send` |
| `EmailGatewayClient.health` | `async () -> dict` | Wraps `GET /email-gateway/health` |

**HTTP API:** `POST /email-gateway/send` → `200` `SendEmailResult` (SENT / LOGGED),
or `502` when a configured SMTP send fails.

---

## 9. Fetching methods — inbound (IMAP)

**Startup wiring (`main.py`):**
```python
callback = _forward_callback()                 # process_inbound (in-proc) | client.submit_inbound (http)
buyer    = create_buyer_poller(callback)        # folder = IMAP_BUYER_FOLDER, party_hint="buyer"
supplier = create_supplier_poller(callback)     # folder = IMAP_SUPPLIER_FOLDER, party_hint="supplier"
await buyer.start(); await supplier.start()      # background asyncio tasks
```

| Function / method | Signature | Role |
|-------------------|-----------|------|
| `create_buyer_poller` / `create_supplier_poller` | `(callback) -> ImapPoller` | One poller per party folder |
| `ImapPoller.start` / `.stop` | `async () -> None` | Launch / cancel the background task |
| `ImapPoller._poll_loop` | `async () -> None` | Loop: fetch → callback → sleep(interval); exponential back-off on error |
| `ImapPoller._fetch_unseen` | `async () -> AsyncIterator[InboundRawEmail]` | The IMAP conversation (below) |
| `_mime_to_raw_email` | `(email.Message) -> InboundRawEmail` | Normalises a MIME message |
| `_extract_attachments` | `(email.Message) -> list[EmailAttachment]` | Walks parts; base64-encodes attachment bytes |
| `_extract_bodies` | `(email.Message) -> (plain, html)` | First text/plain + text/html parts |

**IMAP conversation per poll cycle** (`aioimaplib`, async SSL):
```
IMAP4_SSL(host, port) → wait_hello_from_server() → login(user, password)
  → select(folder) → search("UNSEEN")
  → for uid: fetch(uid, "(RFC822)") → parse MIME → store(uid,"+FLAGS","(\Seen)") → yield
  → logout()
```
Each yielded `InboundRawEmail` is handed to the forward callback with
`party_override=<folder hint>`. The Processor dedupes by `Message-ID`, so an
email seen by both pollers (same folder) is still processed once.

---

## 10. Nice-to-have features

| Feature | Priority | Notes |
|---------|----------|-------|
| IMAP IDLE (push) instead of fixed-interval poll | Medium | Near-real-time inbound without 15-min latency; falls back to poll on servers without IDLE |
| Outbound attachments | Medium | `SendEmailRequest` carries text + `cc` only today; add `attachments` + MIME parts |
| Send retry + dead-letter queue | Medium | Retry transient SMTP failures with back-off; persist permanently-failed sends for operator review |
| Bounce / delivery-failure ingestion | Medium | Consume SendGrid/Mailgun event webhooks → mark the outbound `EmailHistoryRecord` FAILED |
| Non-Gmail providers (O365, AWS SES, Postmark) | Low | Config already abstracts host/port; add provider-specific auth |
| Persistent IMAP connection / connection pool | Low | Avoid reconnect cost each cycle (mostly relevant at short intervals) |
| Outbound rate limiting | Low | Token-bucket to stay under Gmail per-day / per-second send caps |
| Per-message send metrics | Low | Latency, success/failure rate, queue depth |

---

## 11. Pending items

| Item | Status | Detail |
|------|--------|--------|
| Outbound attachments | ❌ Not started | Send is text + `cc` only; no attachment MIME parts yet |
| Bounce/delivery webhooks | ❌ Not started | No feedback loop from provider events into email history |
| IMAP IDLE | ❌ Not started | Inbound latency is bounded by `IMAP_POLL_INTERVAL_SECONDS` (default 900s) |
| `aiosmtplib` dependency | ✅ Declared | In `requirements.txt`; only imported when Gmail is configured (log-only mode needs nothing) |
| Local `.env` hygiene | ⚠️ Watch | A stale `IMAP_POLL_INTERVAL_SECONDS` in a developer `.env` overrides the 900s default |
