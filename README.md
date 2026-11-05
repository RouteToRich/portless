# PortLess — autonomous merchant exporter

PortLess runs a zero-inventory export trade from India end to end with as few human
clicks as the money at risk allows: it finds who is buying, finds who can supply, talks
to both, writes the contract, holds the buyer's money in a neutral escrow, books the
inspection, the ship and the customs filing, releases money against independent proof,
pays the supplier and keeps the margin. Every step that moves money or commits PortLess
to a counterparty is a **gate** whose autonomy an operator chooses; everything else runs
on events.

```
 catalogue ─► scan (own Apify actors) ─► opportunity ─► deal ─► enrichment + screening
      │                                                            │
      ▼                                                            ▼
 buyer outreach funnel  ◄──────────── supplier RFQ ─► quotes ─► verification ─► auto-source
      │  (interested buyer)                                                      │
      ▼                                                                          ▼
 negotiation (LLM, HITL-gated e-mails) ─► agreement ─► Proforma Invoice ─► buyer accepts
      │
      ▼
 neutral escrow (Tazapay / Escrow.com) ─► funded ─► Purchase Order ─► T1 ─► QC (SGS…) ─► T2
      │                                                                            │
      ▼                                                                            ▼
 freight ─► customs (ICEGATE) ─► cleared ─► T3 ─► supplier payout (capped at funds released)
      │
      ▼
 DELIVERED ─► buyer acceptance window ─► accept / silence ─► T4 ─► settled ─► balance ─► PAID
```

Status: every module is implemented and tested; the pipeline runs end to end locally with
stub connectors and in-memory stores. Live operation needs credentials (escrow provider,
payout rail, inspectors, forwarder, e-mail, Apify) and the two external Apify actors
(`india-supplier-finder-ai`, `container-tracking-ai`) — see
[PRODUCTION_TRACKER.md](PRODUCTION_TRACKER.md).

---

## Repository

| Path | What |
|---|---|
| Repository root | FastAPI modular monolith: 15 service/agent modules, shared orchestration, connectors, tests. [BACKEND_README.md](BACKEND_README.md) is the module map. |
| [RouteToRich/portless-ui](https://github.com/RouteToRich/portless-ui) | **Separate repo** — React + Vite operator console (Opportunity Radar, Deal Board/Detail, Approvals Inbox, Money & Margin, Exceptions, Autonomy Settings, Catalogue, System Health). Check it out next to this repo as `../portless-ui`; it talks to this backend. |
| [`workflow.md`](workflow.md) | **The end-to-end workflow as built** — every stage, event, gate, API call and the money chain. Start here to understand the system. |
| [`CLAUDE.md`](CLAUDE.md) | Engineering reference: commands, architecture, every module and env var, cross-module flows. |
| [`PRODUCTION_TRACKER.md`](PRODUCTION_TRACKER.md) | What is done, what is open before real money, company setup, deployment, marketing. |
| [`needed/`](needed/) | Specifications and analysis: master spec, Apify actor contracts, automation gap analysis, and `portless_complete_automation_flow.excalidraw` (the flow diagram). |
| `workflow-implementation.md`, `technical_doc.md`, `layman_workflow.md` | Implementation/reference guide (third-party API registry, build order), zero-cost setup, plain-English guide. |

---

## Quick start

Backend (Python 3.11+):

```bash
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env            # optional — everything degrades gracefully without it
uvicorn app:app --reload --port 8081     # all services on one port; Swagger at /docs
```

UI (Node 18+) — separate repo, cloned next to this one:

```bash
cd ..
git clone https://github.com/RouteToRich/portless-ui.git
cd portless-ui
npm install
npm run dev                     # http://localhost:5173, talks to the backend on 8081
```

Tests:

```bash
python -m pytest tests --rootdir=.             # ~7 min
```

Without `MONGO_URI` every store is in-memory and lost on restart.

---

## How it runs unattended

**Events, not pollers.** Each service publishes a domain event after it commits state;
`shared/orchestration/continuations.py` subscribes and starts the next step. The bus is
NATS JetStream when a Synadia `*.creds` file is present (durable, replayed after a
restart), otherwise in-process.

**Gates.** A step that commits money or PortLess's word is a HITL approval with an
autonomy level: L0 waits for a human, L1 waits with a timeout and a default, L2/L3 run
immediately. The level, timeout, on-timeout default and an optional value threshold are
set per action in Autonomy Settings (`/hitl-approval/autonomy-policy`). Money gates are
capped by `AUTONOMY_MONEY_CEILING` (L1) and need two different approvers above
`FOUR_EYES_THRESHOLD_USD`.

| Gate | Default | What APPROVE does |
|---|---|---|
| `SEND_BUYER_OUTREACH`, `DISPATCH_RFQ`, `SEND_DEAL_NOTIFICATION` | L2 | sends the templated e-mail |
| `CONFIRM_SUPPLIER_SELECTION` | L0 (L2 when GST+IEC verified and the quote is in the price corridor) | records the supplier, opens the negotiation |
| `SEND_NEGOTIATION_EMAIL` | L0, 4 h → APPROVE at L1 | sends the agent's draft |
| `CONFIRM_NEGOTIATION_AGREEMENT`, `CONFIRM_PI_ACCEPTANCE`, `CONFIRM_COUNTERPARTY_RISK` | L0 | closes the negotiation / accepts the PI / issues the PI after a `review` screening |
| `ISSUE_PO`, `BOOK_FREIGHT`, `FILE_CUSTOMS` | L0 (deal profile may raise) | issues the PO, books freight, files the Shipping Bill |
| `RELEASE_ESCROW_TRANCHE`, `RELEASE_SUPPLIER_PAYOUT`, `CONFIRM_BUYER_ACCEPTANCE` | L0, money ceiling L1 | moves money — only against proof, in order, never above what was received |
| `RESOLVE_EXCEPTION` | L1 | runs the proposed playbook (re-inspection, cancel-unfunded) or records the operator's decision |

**Counterparties act by e-mail.** Buyers and suppliers never log in. Every outbound mail
carries a token (`[BUY:…]`, `[RFQ:…]`, `[NEG:…]`); the Email Processor maps the reply to
the right deal and stage: a buyer saying "interested", a supplier's quote, a counter-offer,
"we accept the PI", "goods received", or a complaint. Anything unclear becomes a
`PARSE_INBOUND_EMAIL` approval for a human.

**Nothing stalls silently.** The deal watcher sends reminders (buyer PI/escrow, supplier
PO/inspection, outreach follow-ups), polls carrier tracking, opens the acceptance window
after a fallback period, and raises `deal.exception` on expiry or stall. The Exception
Resolver answers the recurring failures with fixed playbooks before asking an LLM, and
escalates everything else to the operator with a plan.

**Money is never guessed.** The escrow is neutral and staged (30/40/20/10 on PO issued,
QC passed, cleared, accepted). Tranches release only against independent proof, in order,
never above the principal; the final tranche needs the buyer's acceptance or a lapsed
window that opens on **delivery**; supplier payouts never exceed the net funds already
released; the deal is `PAID` only when the supplier has the full PO value. Every deal has
a ledger, a double-entry journal (CSV), a timeline and an evidence pack with a SHA-256.

**The operator sees it all.** SSE/webhook/Telegram pushes for every waiting gate, a daily
digest (gates, deals per stage, stalls, exceptions, money, Apify spend), and
`GET /health/readiness` listing every stub or fallback that still stands between the
system and real money (`ENV_PROFILE=production` refuses to boot while one fails).

---

## Configuration

Copy `.env.example`; it documents every variable. The groups that matter most:

- **Persistence / bus:** `MONGO_URI`, `MONGO_DB_NAME`, `NATS_CREDS_FILE`.
- **Identification:** `APIFY_TOKEN` (own actors on the `rich_minds` account), `APIFY_MAX_RUNS_PER_DAY`, the product catalogue (`PUT /trade-min/catalogue`).
- **Money:** Tazapay / Escrow.com, Payoneer, webhook secrets, `AUTONOMY_MONEY_CEILING`, `FOUR_EYES_THRESHOLD_USD`.
- **Operations:** inspector API keys (SGS, QIMA, BV, Intertek, TÜV), Cogoport / Freightos, ICEGATE, `INSPECTION_WINDOW_TRIGGER`, `INSPECTION_WINDOW_FALLBACK_DAYS`.
- **Communication:** Gmail SMTP + IMAP, `PORTLESS_EMAIL_DOMAIN`, `HITL_TELEGRAM_*`, `DIGEST_*`.
- **Automation:** `AUTONOMY_<ACTION>` (+ `_TIMEOUT_SECONDS`, `_ON_TIMEOUT`, `_AUTO_APPROVE_BELOW_USD`), `OUTREACH_*`, `SOURCING_AUTO_SOURCE_VERIFIED`, `AUTO_DEAL_FROM_OPPORTUNITY_ENABLED`, `EXCEPTION_PLAYBOOKS_ENABLED`, `QC_REINSPECTION_MAX`, `ESCROW_FUNDING_GRACE_DAYS`.
- **Live market intelligence (optional):** `TAVILY_API_KEY`.

Secrets never go in the repo: `.env` and `*.creds` are gitignored and the Apify
token is read from `~/.apify/auth.json` in development.
