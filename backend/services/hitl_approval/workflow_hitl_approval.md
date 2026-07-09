# HITL Approval — Workflow & Implementation Reference

> **Module:** `services/hitl_approval/` · **Port:** 8010 · **Prefix:** `/hitl-approval`
> **Role:** The **human-in-the-loop gate** for the whole platform — pauses risky
> actions for operator sign-off, streams pending approvals to the UI, and fires
> the approved action via a **saga callback**.
> **Status:** ✅ Full · **Persistence:** MongoDB (`HITL_APPROVAL_MONGO_URI`) + in-memory

---

## 1. Purpose

- Gate an action behind an approval whose behaviour depends on the **autonomy level**.
- Notify the UI (SSE stream + signed webhook) and let an operator approve/reject.
- On **APPROVE**, dispatch the stored `callback_payload` to the target service
  (saga continuation). On **REJECT/failure**, emit `deal.exception`.
- Auto-resolve expired L1 approvals via a background timeout watcher.

**Autonomy levels:** `L0` always ask · `L1` ask, auto-approve on timeout · `L2`
auto-approve + notify · `L3` fully autonomous + log.

## 2. Where it sits (callers ↔ callees)

| Invoked by (upstream — creates approvals) | Action |
|---|---|
| `negotiation_agent` | `SEND_NEGOTIATION_EMAIL` (every outbound draft, L0) |
| `dealmaker` (via caller/gateway) | `ISSUE_PO` |
| `shield_link` (via caller/gateway) | `BOOK_FREIGHT`, `FILE_CUSTOMS` |
| `escrow_guard` (via caller/gateway) | `RELEASE_SUPPLIER_PAYOUT` |
| `email_processor` | `PARSE_INBOUND_EMAIL` (price extraction failed) |
| `exception_resolver` | `RESOLVE_EXCEPTION` (ESCALATE/CANCEL) |
| `api_gateway` / UI | List, resolve, SSE stream |

| Invokes on APPROVE (`callbacks.py` saga dispatch) | Target |
|---|---|
| `ISSUE_PO` | `dealmaker.service.issue_po()` |
| `BOOK_FREIGHT` | `shield_link.service.book_freight()` |
| `FILE_CUSTOMS` | `shield_link.service.file_cha()` |
| `RELEASE_SUPPLIER_PAYOUT` | `escrow_guard.service.request_payout()` |
| `SEND_NEGOTIATION_EMAIL` | `negotiation_agent.service.send_approved_email()` / `reject_email()` |
| `RESOLVE_EXCEPTION` | logs + emits `deal.exception` |

On any callback failure → `deal.exception` (`action_required: manual_retry_or_intervention`).

## 3. Workflow

```
POST /hitl-approval/approvals {action, autonomy_level, callback_payload, ...}
  L0 → PENDING (no timeout)          L2 → AUTO_APPROVED now + notify
  L1 → PENDING (expires_at set)      L3 → AUTO_APPROVED + warn
  → notify(): SSE fan-out + signed webhook (HITL_NOTIFY_WEBHOOK_URL)

POST /hitl-approval/approvals/{id}/resolve {APPROVE|REJECT, modified_body?}
  APPROVE → execute_callback(action, payload) → target service
  REJECT  → emit deal.exception
  (already-resolved → 409)

Background: run_timeout_watcher() polls; expired L1 → auto-resolve(default_on_timeout)
GET /hitl-approval/stream  → text/event-stream (last 50 events replayed on reconnect)
```

## 4. API endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/hitl-approval/health` | Health |
| `POST` | `/hitl-approval/approvals` | Create gate (autonomy-aware) |
| `GET` | `/hitl-approval/approvals[/{id}]` | List / fetch |
| `POST` | `/hitl-approval/approvals/{id}/resolve` | Operator decision → saga callback |
| `GET` | `/hitl-approval/stream` | SSE stream for the UI |

## 5. Completion status

| Feature | Status |
|---|---|
| L0–L3 autonomy gates | ✅ |
| Saga callbacks (all 6 actions) | ✅ |
| SSE stream + replay buffer | ✅ |
| Signed webhook notify | ✅ |
| Timeout watcher (auto-resolve L1) | ✅ |
| Idempotency + already-resolved 409 | ✅ |
| `tests/test_hitl_approval.py` | ✅ |

## 6. Waiting for configuration

| Var | Effect |
|---|---|
| `HITL_APPROVAL_MONGO_URI` | Durable approval storage; unset → in-memory |
| `HITL_NOTIFY_WEBHOOK_URL` (+ `HITL_NOTIFY_WEBHOOK_SECRET`) | Push signed notifications to the UI backend (optional) |

## 7. Nice-to-have / pending

- Role/permission model for who may approve which action class.
- Per-action-class default autonomy policy (config-driven).
- Escalation chains + reminders on approaching L1 timeout.

## 8. Files

`schemas.py` · `service.py` (create/resolve/auto-resolve/timeout watcher) ·
`callbacks.py` (saga dispatch) · `notification.py` (SSE + webhook) ·
`repository.py` · `router.py` · `client.py` (`HitlApprovalClient`) · `config.py` ·
`main.py`.
