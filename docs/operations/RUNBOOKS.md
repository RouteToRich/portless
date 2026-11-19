# PortLess runbooks

For the on-call engineer and the trade desk during an incident. Each runbook says how you notice
the problem, what to check, what to do, and when it is over. Paths are on the backend
(`https://<backend>/…`, staff token). Every staff action below is recorded in the audit log
(`GET /hitl-approval/audit`).

**First step for any incident that may move money wrongly: pause.**
`POST /hitl-approval/kill-switch` with `{"paused": true, "reason": "<what you saw>"}` (console top
bar → Kill Switch). While paused nothing releases, pays out, books or e-mails; webhooks and
replies are still recorded, so nothing is lost. Resume with `{"paused": false, "reason": …}` only
once the runbook says the incident is over.

---

## 1. A provider is down (Tazapay, Escrow.com, QIMA, Maersk, Payoneer, Apify)

**You notice:** Telegram failure alerts, `deal.exception` with `CONNECTOR_ERROR`, gates whose
callback failed (`action_required: manual_retry_or_intervention`), `GET /health/failures`.

1. Confirm it is the provider: its status page; `GET /health/readiness` (is our key still valid?).
2. Do **not** retry money steps by hand while the provider is unclear — a release may have
   reached the provider and only the answer was lost. Pause if a money step is affected.
3. Escrow / payout: when the provider is back, read what it holds before anything else:
   `POST /escrow-guard/escrows/{id}/poll` per affected escrow, then
   `GET /escrow-guard/reconciliation/providers`. Only then retry a failed gate
   (the release routes need the original `approval_id`; they refuse a second release of a
   tranche already claimed).
4. QC (QIMA): bookings wait; `POST /shield-link/deals/{id}/qc/poll` once it answers.
5. Carrier (Maersk): milestones can be recorded by staff meanwhile
   (`POST /shield-link/freight/{ref}/milestone`, with the evidence).
6. Apify: scans and checks return empty results; nothing to do except watch the daily budget
   (`GET /health/costs` → `apify_budget`). The cache keeps answering repeated calls.

**Over when:** reconciliation shows no difference and the failed gates were retried or rejected.

## 2. An escrow is stuck

**You notice:** a deal sitting in `ESCROW_PENDING` / a tranche `SUBMITTED` for longer than expected;
the digest's "money in escrow" line; `RECONCILIATION:*` exceptions.

1. `GET /escrow-guard/deals/{deal_id}/ledger` — funded, shortfall, tranches and their status.
2. `POST /escrow-guard/escrows/{id}/poll` — re-read the provider.
3. Funding short (`PARTIALLY_FUNDED`): the buyer was e-mailed automatically; after
   `ESCROW_FUNDING_GRACE_DAYS` the `cancel_unfunded` playbook asks you (RESOLVE_EXCEPTION gate).
4. Tranche `SUBMITTED` and the provider shows it paid out: relay it with
   `POST /escrow-guard/escrows/{id}/tranches/{tid}/provider-confirmed` (provider ref + reason).
5. Provider refused the release document: the deal is in dispute — follow the dispute playbook;
   never re-submit without the corrected proof.

**Over when:** the ledger reconciles (`reconciled: true`) and the deal moved on.

## 3. A webhook never arrived

**You notice:** the provider shows an event (funding, payout, QC result, milestone) that the deal
does not; `GET /health/failures` may show refused signatures.

1. Refused signature? Check the `*_WEBHOOK_SECRET` matches the provider console. A secret
   rotation not done on both sides is the usual cause.
2. Every provider is also polled — wait one poll (`ESCROW_POLL_MINUTES`, `QC_POLL_MINUTES`,
   `FREIGHT_POLL_MINUTES`) or force it: escrow `…/poll`, QC `…/qc/poll`, freight
   `POST /shield-link/deals/{id}/track`.
3. Polling unavailable (portal QC providers SGS / BV): record the result as staff with evidence
   (`POST /shield-link/qc/{ref}/status`).

**Over when:** the deal shows the event and moved to its next step.

## 4. A wrong payout (amount or recipient)

**Treat as P0.** Pause first.

1. `GET /escrow-guard/deals/{deal_id}/ledger` and `/journal` — what was booked.
2. `GET /hitl-approval/audit?subject=<deal_id>` — who approved, which amount, from where.
3. Contact the payout provider (Payoneer) at once to stop / recall; record the provider case
   number on the deal (staff note).
4. Never "correct" with a second payout. Book the recovery when the provider confirms it.
5. Write the incident up (what, how much, why the gate let it through) and add a to-do item.

**Over when:** the money is recovered or written off with the founder's sign-off, and the cause
has a fix in the to-do list.

## 5. The Kill Switch itself

- **State unreadable** (console: "State unreadable"): PortLess treats it as paused. Check Mongo
  (`GET /health/readiness`), then `GET /hitl-approval/kill-switch?refresh=true`. A resume while
  the store is unreadable answers 503 on purpose.
- **Resuming:** check the pending money gates first (`GET /hitl-approval/approvals?status=PENDING`)
  — gates created while paused were created L0 with flag `platform_paused` and need a person.

## 6. The outbound mailbox is blocked

**You notice:** outreach / RFQ sends `send failed` (`OUTREACH:send_failed`, `RFQ:send_failed`),
SMTP errors in the log, bounces, a provider warning about spam.

1. `GET /health/readiness` → `smtp`. Log in to the mailbox provider: blocked, rate-limited,
   password changed?
2. While blocked, sends retry on `SEND_RETRY_MINUTES` (5, 30, 120) and then stop — nothing is
   lost; once fixed, `POST /deals/{id}/retry-sends` per prospect.
3. If blocked for spam: stop outreach (`OUTREACH_ENABLED=false`), review the last introductions
   (`GET /deals/{id}/notifications`), and check the unsubscribe link works before resuming.

**Over when:** a test e-mail is delivered and the retried sends show `sent`.

## 7. The LLM gateway is down

**You notice:** `LLMExhaustedException` in the log, `GET /health/failures` LLM failure rate,
negotiation drafts marked degraded / template.

1. Nothing breaks: negotiation drafts fall back to templates and stay L0 (a person reads every
   one), reply reading uses the keyword fallback, the exception resolver escalates.
2. Check the gateway (`LLM_GATEWAY_URL`) and the PortLess service account (expired password?).
3. Tell the desk that drafts need more editing until it is back.

**Over when:** `GET /health/failures` shows LLM calls succeeding again.

## 8. A background watcher stopped

**You notice:** Telegram "watcher stale", `GET /health/watchers`.

1. Is another instance holding the lease (`watcher_locks`)? A stale lease expires on its own.
2. Restart the backend instance; heartbeats resume within one interval.
3. After a long stop, run the sweeps that matter for money first: escrow poll, then the deal
   watcher (reminders and stall exceptions catch up by themselves).
