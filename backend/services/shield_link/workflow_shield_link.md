# Shield-Link — Workflow & Implementation Reference

> **Module:** `services/shield_link/` · **Port:** 8007 · **Prefix:** `/shield-link`
> **Role:** Orchestrates the **physical-world third parties** — quality-control
> inspection (QC), freight/logistics, and customs house agent (CHA) filing — and
> drives the deal through the shipping states.
> **Status:** ✅ Full · **Persistence:** MongoDB (`SHIELD_LINK_MONGO_URI`) + in-memory

---

## 1. Purpose

- Book QC (SGS/Intertek), freight (Cogoport/Freightos), and CHA (ICEGATE) via
  connectors; each booking creates a **Third-Party Order (TPO)**.
- Receive provider webhooks (HMAC-verified), update the TPO, and transition the deal.
- **Crash-safe:** a `PENDING` TPO is written before the connector call.

## 2. Where it sits (callers ↔ callees)

| Invoked by (upstream) | Why |
|---|---|
| `api_gateway` | QC / freight / CHA endpoints |
| `hitl_approval.callbacks` | On **APPROVE** of `BOOK_FREIGHT` → `book_freight()`; `FILE_CUSTOMS` → `file_cha()` |
| Providers (SGS / Cogoport / Freightos) | Inbound `POST /shield-link/{qc,freight}/webhook` |

| Invokes (downstream) | Why |
|---|---|
| `shared/connectors/shields` (SGS, Cogoport, Freightos, ICEGATE) | Book/poll orders; **stub when key absent** |
| `deals.service.update_status` | `→ SUPPLIER_DISPATCH_READY`, `QC_PASSED`/`QC_FAILED`, `IN_TRANSIT`, `CLEARED` |

## 3. Workflow

```
POST /shield-link/qc        (deal at PO_ISSUED)
  → SGS book → TPO(PENDING) → deal → SUPPLIER_DISPATCH_READY
POST /shield-link/qc/webhook (HMAC)  → deal → QC_PASSED | QC_FAILED

POST /shield-link/freight   (deal at QC_PASSED)
  → Cogoport/Freightos book → deal → IN_TRANSIT
POST /shield-link/freight/webhook (HMAC, ARRIVED) → deal → CLEARED

POST /shield-link/cha       → ICEGATE CHA filing (customs)
```

## 4. API endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/shield-link/health` | Health |
| `POST` | `/shield-link/qc` · `/qc/webhook` | Book QC · SGS callback |
| `POST` | `/shield-link/freight` · `/freight/webhook` | Book freight · milestone callback |
| `POST` | `/shield-link/cha` | CHA / customs filing |
| `GET` | `/shield-link/tpo/{tpo_id}` · `/deals/{deal_id}/orders` | TPO reads |

## 5. Completion status

| Feature | Status |
|---|---|
| QC + freight + CHA booking via connectors | ✅ |
| HMAC-verified provider webhooks | ✅ |
| Crash-safe PENDING TPO before connector call | ✅ |
| Deal transitions across shipping states | ✅ |
| Graceful stubs when keys absent | ✅ |
| `tests/test_shield_link.py` | ✅ |

## 6. Waiting for configuration

| Var | Unlocks |
|---|---|
| `SGS_API_KEY` (+ `SGS_WEBHOOK_SECRET`) | Live QC booking + verified webhook |
| `COGOPORT_API_KEY` / `FREIGHTOS_API_KEY` (+ `*_WEBHOOK_SECRET`) | Live freight + verified webhook |
| `ICEGATE_API_KEY` | Live CHA filing |
| `SHIELD_LINK_MONGO_URI` | Durable TPO storage |

If a `*_WEBHOOK_SECRET` is unset the connector **logs a warning and accepts all** — set them in production.

## 7. QC inspection providers & the CRF → payment-release link

The QC step is the **trust anchor** for money release: an independent inspector's
**Clean Report of Findings (CRF)** is what lets the escrow release the quality
tranche to PortLess (see
[`../escrow_guard/workflow_escrow_guard.md`](../escrow_guard/workflow_escrow_guard.md)
§5.4). So the inspector must be **independent, trusted by the buyer, and digitally
verifiable**.

### 7.1 Trusted, digital, widely-accepted inspection providers

| Provider | Recognition | Digital / API | Notes |
|---|---|---|---|
| **SGS** | ⭐ World's largest (Swiss); de-facto global standard; runs govt pre-shipment inspection (PSI) programs | Digital certificates + verification portal; enterprise API/EDI | **Already wired** (connector + `qc/webhook`) |
| **Bureau Veritas** | ⭐ 2nd largest; universally recognized | Digital reports + online verification | Strong PSI + traceability |
| **Intertek** | ⭐ 3rd major; widely accepted | Digital platform + integrations | "Total Quality Assurance" |
| **Cotecna** | Strong in PSI / govt import programs | Digital reports | Common in mandated-inspection corridors |
| **QIMA** | Digital-first inspection co., popular with importers | ⭐ Online platform + API-style integration, real-time digital reports | Most developer-friendly; candidate 2nd connector |

For **maximum buyer acceptance** use **SGS** or **Bureau Veritas** (their CRF is
the trade standard, rarely disputed). For the **most API-native** experience,
**QIMA**. SGS gives both reasonably well and is the current default.

### 7.2 How the QC certificate authorizes payment release

1. Inspection booked (`POST /shield-link/qc`) → agency inspects → issues a
   **digital CRF** (pass/fail).
2. CRF arrives via `POST /shield-link/qc/webhook`, **HMAC-verified**, hash stored
   on the TPO → deal `QC_PASSED`.
3. The **authenticated CRF is the release trigger** for the escrow's quality
   tranche — Escrow-Guard releases that tranche to PortLess on `qc.passed`.
4. `QC_FAILED` → no release; dispute / refund path.

So Shield-Link's QC output is not just a status flip — it is the **independently
attested proof** the escrow relies on. Keep the CRF **signature + hash** on the
TPO as the audit trail.

### 7.3 Practical notes & caveats

- **Name the inspector in the PI / sales contract** (e.g. "SGS pre-shipment
  inspection, CRF required") so buyer acceptance of that certificate is
  **pre-agreed** — no certificate is automatically binding otherwise.
- **Pre-shipment inspection ≠ arrival condition** — a CRF certifies quality at
  inspection time, not transit damage; the escrow keeps a buyer-acceptance /
  inspection-window backstop on the final tranche.
- **API access is usually enterprise/commercial** (account + agreement), not
  public self-serve. Verify current API availability per provider before building
  a new connector.

## 8. Nice-to-have / pending

- Live GPS/track polling loop for in-transit milestones.
- Multi-provider freight rate comparison + auto-select cheapest.
- Retry/dead-letter on connector failures (currently surfaces `ConnectorError`).
- **Second QC connector** (QIMA / Bureau Veritas) behind the existing connector
  interface, so buyers can pick the inspector they trust.
- **Digital CRF verification** beyond HMAC — validate the issuer's signature /
  verification-portal / QR, and keep a tamper-evident hash on the TPO.

## 9. Files

`schemas.py` (ThirdPartyOrder + requests) · `service.py` (book_qc / handle_qc /
book_freight / file_cha + webhooks + transitions) · `repository.py` · `router.py` ·
`client.py` (`ShieldLinkClient`) · `config.py` · `main.py`.
