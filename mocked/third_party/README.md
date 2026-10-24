# Third-Party API — Mock Responses

Realistic **raw upstream responses** for every external API in the PortLess
registry — the shapes the `shared/connectors/*` adapters receive. Use these to
test connector parsing/normalisation, seed a mock upstream (MSW / json-server /
WireMock), and build UI without live keys.

> **Convention:** keys prefixed with `_` (e.g. `_provider`, `_endpoint`, `_docs`,
> `_note`, `_app_status`) are **documentation metadata, not part of the real API
> response** — strip them when feeding a mock server. For the array-style APIs
> (US Census) the real payload is under `_response`.
>
> Shapes were cross-checked against public API docs where available; providers
> with gated/undocumented schemas are marked **representative** — verify field
> names against your issued key before relying on them in a real integration.

---

## Registry

### Trade intelligence & demand (Phase 1–2)

| File | Provider | Cost | In-app status |
|---|---|---|---|
| `un_comtrade.json` | UN Comtrade — trade stats | Free (open) | Real HTTP |
| `us_census_trade.json` | US Census — imports by HS | Free (key) | Real HTTP |
| `gdelt_news.json` | GDELT — disruption news | Free (open) | Real HTTP |
| `wits_tariff.json` | World Bank WITS — TRAINS tariffs | Free (open) | Real HTTP |
| `macmap_tariff.json` | ITC MacMap — applied tariffs | Key (free tier) | Representative |
| `alibaba_rfq.json` | Alibaba — RFQ | Key | Representative |
| `indiamart_leads.json` | IndiaMART — LMS leads | Key | Real field shape |
| `volza_import_records.json` | Volza — shipment records | Paid | Representative |
| `eu_ted_tenders.json` | EU TED — public tenders | Free (open) | Representative |

### Regulatory & verification

| File | Provider | Cost | In-app status |
|---|---|---|---|
| `vies_vat.json` | EU VIES — VAT validation | Free (open) | Real HTTP |
| `apisetu_gstn.json` | GSTN — GST taxpayer (India) | Key (free) | Real field shape |
| `apisetu_dgft_iec.json` | DGFT — Import-Export Code | Key (free) | Representative |
| `apisetu_mca.json` | MCA — company master data | Key (free) | Representative |
| `apisetu_udyam.json` | Udyam — MSME registration | Key (free) | Representative |
| `allianz_trade_credit.json` | Allianz Trade — buyer credit | Paid | Representative |

### Execution (QC · freight · customs · payments)

| File | Provider | Cost | In-app status |
|---|---|---|---|
| `sgs_inspection.json` | SGS / Intertek — QC | Per-inspection | Representative (stub w/o key) |
| `freightos_rate.json` | Freightos — freight estimator | Free tier / paid | Representative (stub w/o key) |
| `cogoport_rate.json` | Cogoport — freight booking | Per-booking | Representative (stub w/o key) |
| `icegate_shipping_bill.json` | ICEGATE — customs / LEO | Govt (key) | Representative (stub w/o key) |
| `stripe_payment_intent.json` | Stripe — escrow hold | Per-txn (free test) | Real object shape (stub w/o key) |
| `payoneer_payout.json` | Payoneer — supplier payout | Per-payout | Representative (stub w/o key) |

---

## How these map into the pipeline

```
un_comtrade / us_census / gdelt / alibaba / indiamart / ted / volza
        └─► trade_min (opportunity discovery + urgency)
wits_tariff / macmap_tariff
        └─► trade_min / match_engine (tariff gate)
vies / gstn / dgft / mca / udyam / allianz
        └─► sourcing verification + match_engine gatekeeper (fail ⇒ CMS 0)
sgs_inspection            └─► shield_link QC       → QC_PASSED / QC_FAILED
freightos / cogoport      └─► shield_link freight  → IN_TRANSIT → CLEARED
icegate_shipping_bill     └─► docugen customs      → customs.filed
stripe_payment_intent     └─► escrow_guard hold    → ESCROW_FUNDED
payoneer_payout           └─► escrow_guard payout  → PAID
```

Every connector **degrades to a graceful stub** when its key is absent, so the
whole pipeline runs on these mocks with **zero credentials**. See
`technical_doc.md` (§8 Third-party APIs — free vs paid) for the $0 path per
provider, and `workflow.md` (Third-Party API Registry) for connection status.
