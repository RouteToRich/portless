# DocuGen — Workflow & Implementation Reference

> **Module:** `services/docugen/` · **Port:** 8008 · **Prefix:** `/docugen`
> **Role:** Generates the **export documents** (CI, PL, CoO, SDF, Shipping Bill)
> and files them with customs (ICEGATE stub), producing the Shipping Bill + LEO.
> **Status:** ✅ Full · **Persistence:** MongoDB (`DOCUGEN_MONGO_URI`) + in-memory

---

## 1. Purpose

- Render each document type from deal data as HTML (Jinja2 templates), persist,
  and expose them per deal.
- File the Shipping Bill via ICEGATE (stubbed) and emit `customs.filed` with the
  Shipping Bill number + Let-Export-Order (LEO).

## 2. Where it sits (callers ↔ callees)

| Invoked by (upstream) | Why |
|---|---|
| **api_gateway** | Proxies external / UI calls — `DocuGenClient` registered as `"docugen"` in `api_gateway/clients.py` |
| **exception_resolver** (AUTO_FIX) | `DOCUMENT_ERROR` → `docugen_service.generate_document()` regenerates the failed document (in-process; `agent.py::_auto_fix_document_error`) |
| **Operator / orchestrator** | Drives document generation + customs filing during the fulfilment phase (parallel to freight) |

| Invokes (downstream) | Why |
|---|---|
| **`deals.service.get_deal`** | Read deal data to populate documents — its only real dependency |
| **`get_publisher().publish`** | Emits **`customs.filed`** on Shipping-Bill generation and on `file_customs` |
| ~~`IcegateConnector`~~ | ⚠️ **Not wired.** `file_customs()` returns an **inline stub** Shipping Bill + LEO — it does **not** call `IcegateConnector`. Real ICEGATE integration is pending ([§7](#7-nice-to-have--pending)). |

> DocuGen **does not transition the deal** — it only *reads* the deal and *emits*
> `customs.filed`. The `CLEARED` transition is driven by **Shield-Link's freight
> webhook**, not DocuGen.

### 2.1 When & from where each API is called

| API | When (trigger) | From where (caller) |
|---|---|---|
| `POST /docugen/documents` | Fulfilment phase — generate CI / PL / CoO / SDF / Shipping Bill (parallel to freight) | **Operator / UI** → api_gateway; **exception_resolver** AUTO_FIX (`DOCUMENT_ERROR`), in-process |
| `POST /docugen/customs/file` | Goods ready to export — file the Shipping Bill | **Operator / UI** → api_gateway *(target: orchestrator on `IN_TRANSIT` / `CLEARED`)* |
| `GET /docugen/documents/{id}` · `/deals/{id}/documents` | On demand — fetch / audit a deal's documents | **UI / Operator / other services** → api_gateway |
| `GET /docugen/health` | Liveness | api_gateway / health checks |

## 3. Workflow

```
POST /docugen/documents {deal_id, doc_type}
  → get_deal(deal_id)             ← Deals
  → render template (CI | PL | CoO | SDF | SHIPPING_BILL) → persist

POST /docugen/customs/file {deal_id}
  → ICEGATE filing (stub) → Shipping Bill + LEO → emit customs.filed
```

Runs **in parallel** with freight (Shield-Link) during the CLEARED phase.

## 4. API endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/docugen/health` | Health |
| `POST` | `/docugen/documents` | Generate a document |
| `GET` | `/docugen/documents/{doc_id}` | Fetch one |
| `GET` | `/docugen/deals/{deal_id}/documents` | All documents for a deal |
| `POST` | `/docugen/customs/file` | File Shipping Bill → `customs.filed` |

## 5. Completion status

| Feature | Status |
|---|---|
| CI / PL / CoO / SDF / Shipping-Bill templates | ✅ |
| Customs filing + `customs.filed` event | ✅ (ICEGATE **stub**) |
| Document persistence + per-deal listing | ✅ |
| `tests/test_docugen.py` | ✅ |

## 6. Waiting for configuration

- `DOCUGEN_MONGO_URI` — durable document storage; unset → in-memory.
- `ICEGATE_API_KEY` — real customs filing; unset → realistic stub (SB + LEO).

## 7. Nice-to-have / pending

- Real ICEGATE integration (replace stub) + digital-signature of the SB.
- PDF output + object-store persistence (currently HTML).
- Dual-doc cross-check (invoice ↔ packing list ↔ BL) before customs filing.

## 8. Files

`schemas.py` (DocumentResponse, CustomsFilingResponse) · `service.py`
(generate_document + file_customs) · templates (Jinja2) · `repository.py` ·
`router.py` · `client.py` (`DocuGenClient`) · `config.py` · `main.py`.
