# DealMaker — Workflow & Implementation Reference

> **Module:** `services/dealmaker/` · **Port:** 8006 · **Prefix:** `/dealmaker`
> **Role:** Issues the **Proforma Invoice (PI)** and **Purchase Order (PO)** —
> the commercial documents that move a deal from agreed price into execution.
> **Status:** ✅ Full · **Persistence:** MongoDB (`DEALMAKER_MONGO_URI`) + in-memory

---

## 1. Purpose

- Render the PI (to the buyer) and PO (to the supplier) as Jinja2 HTML, hash them
  (SHA-256), persist, and drive the corresponding deal-status transitions.
- Enforce the **margin guard** (net margin **≥ 8 %**) before issuing a PO.
- Be **idempotent** — re-issuing with the same key returns the existing document.
- Capture the buyer's **QC-team** and **escrow-provider** choices on the PI
  (target — see [§3.1](#31-buyer-selections-on-the-pi-target)).

## 2. Where it sits (callers ↔ callees)

| Invoked by (upstream) | Why |
|---|---|
| `api_gateway` | `POST /dealmaker/pi`, `/pi/{id}/accept`, `/po` |
| `hitl_approval.callbacks` | On **APPROVE** of an `ISSUE_PO` gate → `issue_po()` |

| Invokes (downstream) | Why |
|---|---|
| `deals.service.get_deal` | Read deal financials/state |
| `deals.service.update_status` | `→ PI_SENT`, `PI_ACCEPTED`, `PO_ISSUED` (guarded by `can_transition`) |

## 3. Workflow

```
POST /dealmaker/pi        (deal at NEGOTIATION_AGREED)
  → render PI (Jinja2) + SHA-256 → persist → deal → PI_SENT

POST /dealmaker/pi/{id}/accept
  → deal → PI_ACCEPTED   (unlocks escrow hold)

POST /dealmaker/po        (deal at ESCROW_FUNDED)
  → margin guard: net margin ≥ 8 %?   (else 422 / blocked)
  → render PO + SHA-256 → persist → deal → PO_ISSUED
```

The PO step is typically **HITL-gated**: a caller creates an `ISSUE_PO` approval
with a `callback_payload`; on approve, `hitl_approval/callbacks.py` invokes
`issue_po()` here.

### 3.1 Buyer selections on the PI (target)

The PI is where the buyer agrees to terms, so it is where **two buyer choices**
are captured. Both flow downstream into execution — no re-prompting later.

**(a) QC inspection team** — the buyer picks the inspector they trust from the
PortLess-supported list. PortLess books that agency in Shield-Link, and its
signed **Clean Report of Findings (CRF)** becomes the release trigger for the
escrow quality tranche.

| Supported QC team | Notes |
|---|---|
| **SGS** *(default)* | World's largest; already wired |
| **Bureau Veritas** | Universally recognized |
| **Intertek** | Widely accepted |
| **Cotecna** | PSI / government import programs |
| **QIMA** | Digital-first / API-native |

→ Comparison: [Shield-Link §7.1](../shield_link/workflow_shield_link.md#71-trusted-digital-widely-accepted-inspection-providers).
**Name the chosen inspector on the PI** so buyer acceptance of its CRF is
pre-agreed.

**(b) Escrow / payment-release provider** — the buyer picks the neutral custodian
that will hold their funds and release them in stages as the trade proceeds.

| Supported escrow | Notes |
|---|---|
| **Tazapay** | Cross-border B2B; local pay-in + INR payout |
| **Escrow.com** | Established licensed escrow; wire-centric |

→ Full design (staged release, validation, fees):
[Escrow-Guard target spec](../escrow_guard/workflow_escrow_guard.md).
The choice is stored on the deal and drives which provider Escrow-Guard opens the
escrow with at `PI_ACCEPTED → ESCROW_PENDING`; PortLess surfaces the payment
methods that the chosen provider supports.

### 3.2 Proposed PI fields (target)

Add to `IssuePiRequest`:

| Field | Values | Purpose |
|---|---|---|
| `qc_provider` | `sgs` \| `bureau_veritas` \| `intertek` \| `cotecna` \| `qima` (default `sgs`) | Which inspector Shield-Link books |
| `escrow_provider` | `tazapay` \| `escrow_com` | Which custodian Escrow-Guard opens |
| `payment_method` | one method the chosen escrow provider supports | Buyer pay-in method |
| `tranche_schedule` *(optional)* | override of the default staged-release split | Per-deal release %s |

These persist on the deal so Shield-Link books the right inspector and
Escrow-Guard opens the right escrow. The PI document should **print both choices**
(inspector + escrow provider) so the buyer's acceptance is explicit and on the
record.

## 4. API endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/dealmaker/health` | Health |
| `POST` | `/dealmaker/pi` | Issue PI → `PI_SENT` |
| `POST` | `/dealmaker/pi/{pi_id}/accept` | Buyer accepts → `PI_ACCEPTED` |
| `GET` | `/dealmaker/pi/{pi_id}` | Fetch PI |
| `POST` | `/dealmaker/po` | Issue PO (margin guard) → `PO_ISSUED` |
| `GET` | `/dealmaker/po/{po_id}` | Fetch PO |
| `GET` | `/dealmaker/deals/{deal_id}/documents` | All PI/PO for a deal |

## 5. Completion status

| Feature | Status |
|---|---|
| PI / PO issuance (Jinja2 HTML + SHA-256) | ✅ |
| Margin guard (≥ 8 %) on PO | ✅ |
| Idempotency on issuance | ✅ |
| Deal transitions (`PI_SENT` / `PI_ACCEPTED` / `PO_ISSUED`) | ✅ |
| `tests/test_dealmaker.py` | ✅ |

## 6. Waiting for configuration

- `DEALMAKER_MONGO_URI` — durable document storage; unset → in-memory.
- No external API keys (rendering + hashing are local).

## 7. Nice-to-have / pending

- PDF rendering (currently HTML) + object-store persistence.
- E-signature / buyer-accept link flow.
- Configurable margin floor per operator policy (shared with Match Engine).

## 8. Files

`schemas.py` (PiRequest/Response, IssuePoRequest, PoResponse) · `service.py`
(issue_pi / accept_pi / issue_po + margin guard + transitions) · templates
(Jinja2) · `repository.py` · `router.py` · `client.py` (`DealMakerClient`) ·
`config.py` · `main.py`.
