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
- **Multi-provider by design:** buyers must be able to pick the QC agency and
  freight/visibility provider *they* trust — see §7 and §8 for the supported
  provider ecosystem and each provider's API surface.

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
| `GET` | `/shield-link/qc/providers` | Buyer-facing QC provider catalog (top-5: acceptance, evidence, India, fee) |
| `POST` | `/shield-link/qc/quote` | Quote QC cost for a provider (feeds negotiation `cost_envelope.qc_cost`) |
| `POST` | `/shield-link/qc` · `/qc/webhook` | Book QC (provider: request → buyer-opted → default) · provider callback |
| `POST` | `/shield-link/freight/quote` | Quote lane freight cost (feeds negotiation `logistics_cost` — CIF pricing) |
| `POST` | `/shield-link/freight` · `/freight/webhook` | Book freight · milestone callback |
| `POST` | `/shield-link/cha` | CHA / customs filing |
| `GET` | `/shield-link/tpo/{tpo_id}` · `/deals/{deal_id}/orders` | TPO reads |

## 5. Completion status

| Feature | Status |
|---|---|
| QC + freight + CHA booking via connectors | ✅ |
| **Top-5 buyer-selectable QC connectors** (SGS, QIMA, BV, Intertek, TÜV SÜD) in `shared/connectors/qc_providers.py`, separated from freight (`shields.py`) | ✅ |
| QC provider catalog + cost-quote endpoints | ✅ |
| Provider resolution at booking: explicit → buyer-opted (negotiation session) → default | ✅ |
| Per-provider HMAC webhook signatures (`x-sgs/-qima/-bv/-intertek/-tuvsud-signature`) | ✅ |
| Crash-safe PENDING TPO before connector call | ✅ |
| Deal transitions across shipping states | ✅ |
| Graceful stubs when keys absent (per-provider `*_API_KEY` placeholders in `.env.example`) | ✅ |
| `tests/test_shield_link.py` · `tests/test_qc_providers.py` | ✅ |

## 6. Waiting for configuration

| Var | Unlocks |
|---|---|
| `SGS_API_KEY` / `QIMA_API_KEY` / `BUREAU_VERITAS_API_KEY` / `INTERTEK_API_KEY` / `TUV_SUD_API_KEY` (each + `*_BASE_URL`, `*_WEBHOOK_SECRET`) | Live QC booking + verified webhook per provider |
| `COGOPORT_API_KEY` / `FREIGHTOS_API_KEY` (+ `*_WEBHOOK_SECRET`) | Live freight + verified webhook |
| `ICEGATE_API_KEY` | Live CHA filing |
| `SHIELD_LINK_MONGO_URI` | Durable TPO storage |

If a `*_WEBHOOK_SECRET` is unset the connector **logs a warning and accepts all** — set them in production.

## 7. QC inspection provider ecosystem

The QC step is the **trust anchor** for money release: an independent inspector's
**Clean Report of Findings (CRF)** is what lets the escrow release the quality
tranche to PortLess (see
[`../escrow_guard/workflow_escrow_guard.md`](../escrow_guard/workflow_escrow_guard.md)
§5.4). The inspector must therefore be **independent, trusted by the buyer, and
digitally verifiable** — and because different buyers trust different agencies,
Shield-Link is designed to offer the buyer a **choice of provider** at booking
time rather than hard-wiring one.

Selection criteria applied below: (a) certificate is **globally accepted** by
buyers/banks/customs, (b) **API feasibility** — a real integration path exists,
(c) **visual evidence** — photos and/or video in the inspection deliverable,
(d) **available in India** — our primary export origin; **all providers below
are confirmed to operate in India** (see the India column in §7.2).

### 7.1 Provider-by-provider detail

#### SGS (Switzerland) — ✅ already wired (default)

- **Acceptance:** world's largest TIC (testing/inspection/certification) firm;
  runs government pre-shipment-inspection (PSI) programs; its CRF is the de-facto
  trade standard and is rarely disputed by buyers or banks.
- **Digital / API surface:**
  - **QiiQ** remote-inspection platform — live audio/video streaming plus photo
    and document capture during the inspection; the full recording is retained
    for assessment and audit.
  - **KAES** (Key Account Execution System) — end-to-end digital management of
    the inspection lifecycle across all SGS offices; data exposed for analytics.
  - Digital certificates with an online verification portal.
  - Machine integration is **enterprise API/EDI under a key-account agreement**
    — there is no public self-serve developer portal. Our connector
    (`shared/connectors/shields.py`) models it as `quote/book/status/webhook_verify`
    with the `x-sgs-signature` HMAC webhook.
- **Photos/videos:** ✅ photo-backed inspection reports; ✅ **video** via QiiQ
  remote inspections. Also offers **Container Loading Check (CLC)** — photos of
  stuffing, packaging, markings and container seals (see §9).
- **India:** ✅ SGS India — one of the largest TIC networks in the country
  (offices + accredited labs across all major industrial hubs and ports).

#### Bureau Veritas (France)

- **Acceptance:** #2 globally; universally recognized; strong in government PSI
  and import-conformity (VOC) programs — best-in-class for buyer acceptance
  alongside SGS.
- **Digital / API surface:**
  - **BVPort** — secure portal for reports and project dashboards.
  - **OneSource** — online supply-chain quality-management platform (supplier
    and systems compliance monitoring); the practical integration channel.
  - **CPS Supplier Portal** — toolkit for consumer-products supply chains.
  - No public developer portal; integration is **enterprise/commercial** via
    OneSource data feeds — model behind the same connector interface.
- **Photos/videos:** ✅ photo-documented inspection reports; remote/video
  inspection offered on selected industrial services.
- **India:** ✅ Bureau Veritas India — established local subsidiary with a
  nationwide inspection network and labs.

#### Intertek (UK)

- **Acceptance:** #3 major TIC brand ("Total Quality Assurance"); widely
  accepted by Western buyers.
- **Digital / API surface:**
  - **i2Q** — one-stop digital platform for inspection management (booking,
    scheduling, results).
  - **InterLink 2.0** — client platform that explicitly advertises
    **ready-to-integrate APIs** for data exchange — the most concrete named API
    channel among the "big three".
  - **eReporting** — customizable digital reports that embed photographs,
    attachments and signatures; inspectors capture photos offline and sync in
    the background.
- **Photos/videos:** ✅ photos, attachments and e-signatures embedded in
  reports; remote video inspection available for technical services.
- **India:** ✅ Intertek India — full inspection + accredited-lab coverage
  across the major export hubs.

#### QIMA (Hong Kong, ex-AsiaInspection) — ⭐ most API-native; candidate 2nd connector

- **Acceptance:** digital-first inspection company, very popular with importers;
  accreditation is solid (ISO 17020) though the brand is younger than the big
  three — ideal as the *developer-friendly* option next to SGS/BV.
- **Digital / API surface — the only one with a true public developer portal:**
  - Portal: `https://developers.qima.com` (spec) / `https://api.qima.com`
    (Developer Portal); REST + JSON; code samples in curl/JS/Node/Java.
  - **Modules and representative operations:**

    | Module | Operations |
    |---|---|
    | **Inspection** | create inspection order (PSI, DUPRO, PEO/pre-customs types), search orders, get order detail, cancel order |
    | **Audit** | create audit order (`createOrderWithoutDraft`), manage audit programs |
    | **Lab Testing** | create LT booking order, create inquiry order |
    | **Report** | search reports, get report of a specific inspection |
    | **File** | upload files; **download inspection/audit PDF report** (`getFile`) |
    | **Supplier** | supplier CRUD, get supplier detail (`getSupplierDetailInfoById`) |
    | **Parameter** | reference data — geography, product categories, AQL sample levels |

  - This maps 1:1 onto our connector interface: `book` → create order, `status`
    → get order, plus report download for CRF archival on the TPO.
- **Photos/videos:** ✅ photo-rich digital reports (same-/next-day, downloadable
  as PDF via API); live-video remote inspection offered as a product option.
- **India:** ✅ 10,000+ inspections already performed in India; inspectors in
  the major Indian production hubs (onsite within 48h) plus ISO 17025 labs in
  India (textiles, toys; CPSC-approved for the US market).

#### Cotecna (Switzerland)

- **Acceptance:** strong in government-mandated PSI / import-verification
  corridors (Africa, Middle East, parts of Asia) — the right choice when the
  *destination country* mandates a specific PSI agency.
- **Digital / API surface:** digital reports and certificates; integration is
  enterprise-arranged (no public portal).
- **Photos/videos:** ✅ photo-supported findings in PSI reports.
- **India:** ✅ Cotecna Inspection India Pvt Ltd — local entity handling PSI
  and commodity inspection.

#### TÜV SÜD / TÜV Rheinland (Germany)

- **Acceptance:** premium recognition with EU buyers (especially DACH);
  certificates carry strong weight for regulated/technical goods.
- **Digital / API surface:** customer portals and digital certificates;
  enterprise integration only.
- **Photos/videos:** ✅ photo documentation in inspection reports; remote video
  inspection offered for industrial equipment.
- **India:** ✅ TÜV SÜD South Asia and TÜV Rheinland India — both run
  substantial local subsidiaries with labs and inspection teams.

#### HQTS / V-Trust (China-focused, budget tier)

- **Acceptance:** regional; photo-heavy reports and low cost, but weaker
  standing with conservative Western buyers/banks — offer only as an
  explicitly-flagged budget option, never as CRF anchor for escrow release
  unless the buyer pre-agrees.
- **Digital / API surface:** online booking portals; limited/none public API.
- **Photos/videos:** ✅ very photo-dense reports (often 50–100+ photos).
- **India:** ✅ HQTS — Delhi regional office since 2006, own inspectors,
  nationwide coverage incl. PSI and container-loading supervision. ✅ V-Trust —
  full-time inspectors in 17 Indian cities (Mumbai, Delhi, Chennai, Bangalore,
  Tiruppur, Surat, Ahmedabad, Kolkata, …) offering PSI, DUPRO and container
  loading supervision.

### 7.2 Summary matrix

| Provider | Buyer acceptance | Public API | Integration channel | Photos | Video | Available in India |
|---|---|---|---|---|---|---|
| **SGS** (wired) | ⭐⭐⭐ global standard | ✖ (enterprise API/EDI) | key-account agreement + HMAC webhook | ✅ | ✅ QiiQ | ✅ SGS India — offices + labs in all major hubs/ports |
| **Bureau Veritas** | ⭐⭐⭐ global standard | ✖ (enterprise) | OneSource / BVPort feeds | ✅ | partial | ✅ BV India — nationwide network + labs |
| **Intertek** | ⭐⭐⭐ | ✖ public, ✅ named APIs (InterLink 2.0) | InterLink 2.0 API + i2Q | ✅ | partial | ✅ Intertek India — inspection + accredited labs |
| **QIMA** | ⭐⭐ importer favourite | ✅ **developers.qima.com** | REST/JSON, self-serve | ✅ | ✅ option | ✅ 10k+ inspections done; inspectors in all hubs, ISO 17025 labs |
| **Cotecna** | ⭐⭐ (mandated corridors) | ✖ | enterprise | ✅ | ✖ | ✅ Cotecna Inspection India Pvt Ltd |
| **TÜV SÜD/Rheinland** | ⭐⭐⭐ (EU) | ✖ | enterprise | ✅ | partial | ✅ TÜV SÜD South Asia + TÜV Rheinland India |
| **HQTS / V-Trust** | ⭐ regional | ✖ | portal only | ✅✅ | ✖ | ✅ HQTS Delhi (since 2006); V-Trust inspectors in 17 cities |

**India note:** every QC provider above is confirmed operational in India — no
provider needs to be excluded for our India-origin export corridor.

**Recommendation:** keep **SGS** as default (max acceptance, already wired);
build **QIMA** as the second connector (only true public API — fastest to
integrate and gives buyers a photo-rich, cheaper option); add **Bureau
Veritas / Intertek** as enterprise integrations when deal volume justifies the
commercial agreements. Surface the choice as a `provider` field on
`POST /shield-link/qc` (see §11).

### 7.3 How the QC certificate authorizes payment release

1. Inspection booked (`POST /shield-link/qc`) → agency inspects → issues a
   **digital CRF** (pass/fail).
2. CRF arrives via `POST /shield-link/qc/webhook`, **HMAC-verified**, hash stored
   on the TPO → deal `QC_PASSED`.
3. The **authenticated CRF is the release trigger** for the escrow's quality
   tranche — Escrow-Guard releases that tranche to PortLess on `qc.passed`.
4. `QC_FAILED` → no release; dispute / refund path.

So Shield-Link's QC output is not just a status flip — it is the **independently
attested proof** the escrow relies on. Keep the CRF **signature + hash** on the
TPO as the audit trail, and (once the QIMA connector lands) archive the report
PDF + photos fetched via the provider's report API.

### 7.4 Practical notes & caveats

- **Name the inspector in the PI / sales contract** (e.g. "SGS pre-shipment
  inspection, CRF required") so buyer acceptance of that certificate is
  **pre-agreed** — no certificate is automatically binding otherwise. With
  multi-provider support, the *buyer-selected* provider is what gets written
  into the PI.
- **Pre-shipment inspection ≠ arrival condition** — a CRF certifies quality at
  inspection time, not transit damage; the escrow keeps a buyer-acceptance /
  inspection-window backstop on the final tranche.
- **API access is usually enterprise/commercial** (account + agreement), not
  public self-serve — QIMA is the exception. Verify current API availability
  per provider before building a new connector.
- If the **destination country mandates a PSI agency** (govt programs), that
  mandate overrides buyer preference — keep a per-corridor provider rule table.

## 8. Freight & mid-shipment visibility provider ecosystem

Two distinct capabilities, often conflated:

1. **Booking/execution** — get quotes, book the shipment, manage documents
   (what Cogoport/Freightos do; already wired).
2. **Visibility/tracking** — normalized milestone events while the container is
   in transit (what powers the buyer's "where is my cargo" view and our
   `IN_TRANSIT → CLEARED` transition).

### 8.1 Booking providers (wired)

#### Cogoport (India)

- Digital forwarder/marketplace: instant ocean/air rates, booking, document
  management, tracking; strong on India-origin lanes (our primary corridor).
- **API:** partner/enterprise API (no public developer portal) — our connector
  models `quote/book/status` with the `x-cogoport-signature` HMAC webhook.
- **Photos/videos:** ✖ — milestone/status data and documents, no media.
- **India:** ✅ Cogoport is an **Indian company** (Mumbai HQ) — best-in-class
  for India-origin lanes.

#### Freightos / WebCargo

- Global freight marketplace + rate platform; WebCargo (air eBooking) is now
  unified under the Freightos brand.
- **API:** public developer portal — `https://developers.freightos.com` —
  covering **quotes, bookings, tracking/shipment updates, documentation and
  messaging** (JSON/XML). The most self-serve of the booking options.
- **Photos/videos:** ✖ — events and documents only.
- **India:** ✅ marketplace covers India-origin ocean and air lanes (forwarders
  quoting ex-India), though it has no India-first focus like Cogoport.

### 8.2 Visibility / container-tracking providers (proposed additions)

These take a container number / MBL / booking ref and push back normalized
milestone events (gate-in, loaded, departed, transshipment, discharged,
gate-out, empty return) + predictive ETAs — exactly the shape of our
`freight/webhook`.

| Provider | Model | API surface | Available in India | Notes |
|---|---|---|---|---|
| **Vizion** | API-only (the product *is* the API) | REST: subscribe a container/BL → normalized events **pushed via webhook**; per-container pricing | ✅ carrier/port-based coverage — tracks all Indian gateway ports (Nhava Sheva/JNPT, Mundra, Chennai, …); no local office needed | Best architectural fit for Shield-Link — same webhook pattern we already use |
| **Terminal49** | API-first tracking | REST + webhooks for container/BL milestones | ✅ same carrier-based model — Indian ports covered via the shipping lines | Vizion alternative, similar per-container pricing |
| **project44** | Enterprise multimodal visibility | APIs for ocean/road/air tracking; 1,000+ carriers, 600+ terminals; predictive ETAs, exception alerts; **POD documents on road legs** | ✅ global network covers India lanes and terminals | Six-figure enterprise commitment — later stage |
| **FourKites** | Enterprise visibility | 800+ carriers incl. Maersk/MSC; predictive ETA, risk alerts, document/POD retrieval | ✅ strong India presence — major engineering office in Chennai; India lanes covered | Enterprise pricing — later stage |
| **SeaRates (DP World)** | Logistics APIs marketplace | REST: rates, container tracking, distances/routes | ✅ DP World itself operates Indian terminals (Nhava Sheva, Mundra, Chennai, Cochin) | Affordable; also gives a rate-comparison leg |
| **Maersk (carrier-direct)** | Carrier developer portal | `api.maersk.com` — Track & Trace conforming to the **DCSA T&T standard**, plus booking/eB/L APIs | ✅ Maersk is a top carrier on India export trades | DCSA schema means one integration pattern reuses across Hapag-Lloyd, CMA CGM, etc. |
| **ShipsGo / GoComet** | Budget trackers | REST APIs, per-shipment pricing | ✅ GoComet is an **Indian company** (Mumbai); ShipsGo tracks Indian ports globally | Cost-effective fallback tier |

**India note:** ocean-visibility coverage is **carrier/port-based, not
country-based** — a provider tracks Indian shipments if it integrates the
carriers calling at Indian ports, which all of the above do. All rows are
confirmed usable for India-origin exports; GoComet and Cogoport are themselves
Indian companies, and FourKites and DP World (SeaRates) have significant local
operations.

**Recommendation:** add **Vizion** (or Terminal49) as the first visibility
connector — webhook-push of normalized events drops straight into
`POST /shield-link/freight/webhook`; adopt the **DCSA event vocabulary** for
our internal milestone enum so carrier-direct APIs (Maersk et al.) plug in
without remapping later.

### 8.3 Do shipping providers give images/videos?

Short answer: **ocean tracking APIs do not carry images** — they return
event/milestone JSON, vessel position and ETA data. Visual evidence during the
shipping phase comes from three other places:

| Stage | Visual evidence | Who provides it |
|---|---|---|
| Container stuffing (origin) | **Container Loading Check (CLC)** — photos of goods, packaging, marks, container condition and **seal number** | QC agencies (SGS, QIMA, BV, Intertek) as an add-on inspection type |
| In transit | ✖ no imagery — milestone events, GPS/AIS position, predictive ETA only | Vizion / project44 / FourKites / carrier APIs |
| Arrival / delivery (road leg) | **POD (proof-of-delivery) documents and photos**, damage exceptions | project44 / FourKites document APIs; last-mile carriers |
| Damage/dispute | Survey report with photos | Marine surveyor (bookable via the same QC agencies) |

**Implication for Shield-Link:** to give buyers photo evidence across the whole
journey, book a **CLC at stuffing** (reuses the QC connector — just another
inspection type) and store its photos on the TPO; rely on milestone events in
transit; capture POD documents at destination. This is cheap to add because it
is the same `book_qc` path with `inspection_type: CONTAINER_LOADING_CHECK`.

## 9. Evidence matrix — what the buyer sees per deal stage

| Deal state | Evidence available | Source |
|---|---|---|
| `SUPPLIER_DISPATCH_READY` | Inspection booked confirmation | QC provider `book` response |
| `QC_PASSED` / `QC_FAILED` | **CRF + photo report (+ video where QiiQ/remote used)**, HMAC-verified, hash on TPO | SGS today; QIMA report-PDF API next |
| `IN_TRANSIT` | CLC photos (if booked) + live milestones/ETA | QC provider CLC + Cogoport/Freightos (+ Vizion later) |
| `CLEARED` | Arrival milestone, customs docs | freight webhook + DocuGen/ICEGATE |
| `PAID` | POD document/photo (road leg) | visibility provider document API (future) |

## 10. Approximate provider costs (indicative, 2026)

What each provider costs to **use the service** and to **use the API**. All
figures are indicative — public price lists and industry comparisons where
available, quote-based bands otherwise. Enterprise contracts are always
negotiated; confirm at the commercial stage before budgeting a corridor.

### 10.1 QC inspection providers

One pre-shipment inspection ≈ **1 man-day** for a typical single-container /
AQL-sampled lot (remote factories may add travel charges). Published rates are
China-market; India rates are broadly comparable.

| Provider | Inspection service (per man-day) | API / integration cost | Pricing model |
|---|---|---|---|
| **SGS** | ~$300–500+ (quote-based; govt/commodity PSI programs sometimes ~0.25–1% of shipment value instead) | No separate fee — API/EDI bundled into the key-account agreement | Quote per engagement; premium tier |
| **QIMA** | From ~$309 all-inclusive (Zone A); $300–500 band by product; specialized (chem/pharma) up to ~$700 | **Free** — public REST API included with a client account | Transparent published rates; self-serve booking |
| **Bureau Veritas** | ~$300–500 (quote-based) | Bundled into the OneSource/enterprise contract | Quote per engagement |
| **Intertek** | ~$300–500 (quote-based) | InterLink 2.0 API included with a commercial agreement | Quote per engagement |
| **TÜV SÜD** | ~$400–600 (premium, quote-based) | Bundled into enterprise/portal agreement | Quote per engagement |
| **Cotecna** | Quote-based; mandated-PSI corridors typically ~0.25–1% of FOB value | Enterprise agreement | % of value or per man-day |
| **HQTS** | ~$250–290 | Portal only (no API) | Published budget rates |
| **V-Trust** | ~$249 | Portal only (no API) | Published budget rates |

**Per-unit impact:** a $320 man-day on a 25 MT lot ≈ **$12.8/MT** — this is
exactly what `POST /shield-link/qc/quote` returns as
`qc_cost_per_unit_usd` and what the Negotiation Agent bakes into
`cost_envelope.qc_cost` (the connector stub fees — SGS 320 / QIMA 289 /
BV 340 / Intertek 330 / TÜV SÜD 380 — sit deliberately inside these bands).

### 10.2 Freight booking + customs

| Provider | Service cost | API cost |
|---|---|---|
| **Cogoport** | Free to register/quote/book — margin is embedded in the freight rate | Partner API by agreement (no public price) |
| **Freightos** | Shipper marketplace free to quote/book (rate includes margin); forwarder SaaS tiers ~$100–500+/mo | API access tied to the platform subscription |
| **ICEGATE / CHA** | Statutory filing charges nominal; CHA service fee ≈ ₹2,000–6,000 (~$25–75) per Shipping Bill | Government EDI — no commercial API fee (via registered CHA) |

### 10.3 Visibility / container tracking

| Provider | Approximate cost | Model |
|---|---|---|
| **Vizion** | ~$2–5 per tracked container (volume-tiered; no free tier) | Pay-per-container API |
| **Terminal49** | Free tier for basic tracking; paid API/webhook plans from ~$99/mo | Freemium → subscription |
| **project44** | Six-figure annual contract (~$100k+/yr) | Enterprise |
| **FourKites** | Six-figure annual contract | Enterprise |
| **SeaRates (DP World)** | API plans roughly $49–499/mo by tier | Subscription |
| **Maersk (carrier-direct)** | **Free** for Maersk customers (developer portal) | Carrier account |
| **ShipsGo** | Prepaid credits, typically <$1 per container | Pay-per-container |
| **GoComet** | Mid-market SaaS, quote-based (order of $10–50k/yr) | Subscription |

**Cost strategy implied:** start with the free/cheap tier (Maersk direct +
Terminal49/ShipsGo, or Vizion pay-per-container as volume grows); the
enterprise platforms (project44/FourKites) only pay off at high shipment
volume. On the QC side, QIMA gives the lowest all-in cost *with* an API;
SGS/BV command a premium that buys maximum certificate acceptance.

## 11. Multi-provider integration — status

QC-side items are now **implemented** (2026-07-16); visibility items remain
planned:

1. ✅ **Top-5 QC connectors** — `shared/connectors/qc_providers.py`: SGS,
   QIMA, Bureau Veritas, Intertek, TÜV SÜD behind the uniform
   `quote/book/status/webhook_verify` interface; per-provider `*_API_KEY` /
   `*_BASE_URL` / `*_WEBHOOK_SECRET` placeholders (graceful stub when absent);
   QC is now **separated from freight** (`shields.py` keeps
   Cogoport/Freightos/ICEGATE only).
2. ✅ **`QC_PROVIDER_CATALOG`** — buyer-facing metadata (acceptance tier,
   photo/video evidence, India availability, indicative fee) + free-text alias
   resolution (`"Bureau Veritas"` → `bv`).
3. ✅ **Catalog + quote endpoints** — `GET /shield-link/qc/providers`,
   `POST /shield-link/qc/quote` (used by the Negotiation Agent to price QC
   into the CIF floor).
4. ✅ **Provider resolution at booking** — explicit request provider →
   buyer-opted provider from the deal's negotiation session → **cheapest
   catalog provider** (`SHIELD_LINK_QC_FALLBACK_STRATEGY=cheapest`, default;
   set `default` to book `default_qc_provider` instead); unknown names are
   rejected (422). If the buyer named several acceptable agencies during
   negotiation, the cheapest among them was already stored as the opted one.
5. ✅ **Per-provider webhook verification** — the TPO's booked provider picks
   the HMAC scheme on `/qc/webhook`.
6. ✅ **Negotiation integration** — first buyer email lists the supported
   inspectors and asks the buyer to opt; the opted provider is captured
   (LLM intent classifier + deterministic name scan), stored on the session,
   and its quoted cost re-prices `cost_envelope.qc_cost` (see
   `../../agents/negotiation_agent/workflow_negotiation_agent.md`).
7. ✅ **Freight quote in pricing** — `POST /shield-link/freight/quote`
   (read-only, per-container rate → per-unit cost at 25 MT/40HC) is called at
   negotiation start so ocean freight sits inside the CIF margin floor
   (`NEGOTIATION_FREIGHT_COST_ENABLED`, default true). Freight *booking*
   remains gated on `QC_PASSED` — quotes parallel, commitments sequential.
8. ⏳ **Vizion (or Terminal49) visibility connector** feeding normalized
   DCSA-style events into `freight/webhook`.
9. ⏳ **CLC inspection type** on the QC path for stuffing-time photo evidence.
10. ⏳ Real provider API mappings once commercial agreements land (QIMA public
    API first — order create/search/cancel + report `getFile` for CRF archive).

## 12. Nice-to-have / pending

- Live GPS/track polling loop for in-transit milestones (superseded by the
  Vizion webhook plan in §11 if adopted).
- Multi-provider freight rate comparison + auto-select cheapest (SeaRates /
  Freightos quotes make this feasible).
- Retry/dead-letter on connector failures (currently surfaces `ConnectorError`).
- **Digital CRF verification** beyond HMAC — validate the issuer's signature /
  verification-portal / QR, and keep a tamper-evident hash on the TPO.
- Per-corridor mandated-PSI-agency rule table (§7.4).

## 13. Files

`schemas.py` (ThirdPartyOrder + requests) · `service.py` (book_qc / handle_qc /
book_freight / file_cha + webhooks + transitions) · `repository.py` · `router.py` ·
`client.py` (`ShieldLinkClient`) · `config.py` · `main.py`.
