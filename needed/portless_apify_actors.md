# PortLess — Apify Actors Needed for Trade Automation

Date: 2026-10-22 · Updated: 2026-10-23 (§8 — what is built and wired) · Source: `needed/portless_automation_gap_analysis.md` §5.E (moved here)
Purpose: input for the actor generator — for each data need, check whether an existing
Apify Store actor is good enough to use as-is, or whether PortLess needs its own wrapper
Actor on top of it.

---

## 1. Context

PortLess is a merchant-export platform (India → overseas buyers). Its backend runs a
deal from opportunity scan → RFQ → negotiation → PI → escrow → PO → QC → freight →
customs → payout. Paid trade-intelligence APIs (Volza, ImportGenius, Trademo, IndiaMART
and Alibaba partner APIs) are the most expensive part of the opportunity scan; Apify
pay-per-result actors cover most of the same data at a few dollars per 1,000 results.

Every actor below was confirmed listed on the Apify Store on 2026-10-22 via the public
Store API (`https://api.apify.com/v2/store?search=…`). **Users** = Store users in the
last 30 days — a rough reliability signal; actors with 1–2 users are fragile. Prices are
from Store snippets and were **not** verified per result — confirm on each actor's page.

---

## 2. Core data sources (replace paid connectors)

| ID | Replaces connector | Data PortLess needs | Candidate actors (users, 30 d) | Indicative price |
|---|---|---|---|---|
| C-1 | `VolzaConnector`, `ImportGeniusConnector` | Named US importers (buyers), their overseas suppliers, bills of lading, HS codes, shipment history, **past unit price** | Primary: `zen-studio/importyeti-scraper` (97), `memo23/importyeti-scraper` (25), `jungle_synthesizer/importyeti-bill-of-lading-scraper` (24), `jungle_synthesizer/importyeti-hs-code-scraper` (16, search by HS code). Fallback: `scrapesage/importyeti-scraper` (21), `logiover/importyeti-scraper` (9), `muhammadafzal/importyeti-scraper` (2), `crawloop/importyeti-scraper` (1) | from ~$4 / 1,000 |
| C-2 | `TrademoConnector` | Company import/export shipment counts and USD values, top HS codes, monthly trends, named buyers | `jungle_synthesizer/trademo-global-trade-company-shipment-profile-scraper` (1) | check Store page |
| C-3 | `AlibabaRfqConnector` | Live buying requests: product, quantity, buyer country and name, posting date, quotes left | `blueskyscraper/alibaba-rfq-scraper` (18), `apt_marble/alibaba-rfq-scraper` (8) | check Store page |
| C-4 | `IndiamartConnector` | Indian suppliers: company, price, MOQ, GST number, turnover, location | `thirdwatch/indiamart-supplier-scraper` (67), `haketa/indiamart-scraper` (28), `sourabhbgp/indiamart-scraper` (19); fallback `tortuga/indiamart-scraper` (1), `goat255/indiamart-scraper` (1) | from ~$2 / 1,000 |
| C-5 | `TradeIndiaConnector` (connector exists, no data source behind it) | Indian suppliers with price, email, phone; TradeIndia buy leads (buyer intent) | Suppliers: `haketa/tradeindia-scraper` (12), `scrapesage/tradeindia-scraper` (11). Buy leads: `scrapers_lat/tradeindia-buyers-scraper` (4) | check Store page |
| C-6 | `ComtradeConnector` | Bilateral trade value by HS code (country-level demand) | `thirdwatch/trade-data-scraper` (3), `logiover/comtrade-trade-data-scraper` (3) | from ~$0.90 / 1,000 (Comtrade's own free tier may suffice) |

---

## 3. Actors needed to close automation gaps

| ID | Gap it closes | Candidate actors (users, 30 d) | Plugs into |
|---|---|---|---|
| E-1 | **Indian supplier export history.** A supplier with no MCA/Udyam history scores 0 on reliability → CMS < 75 → DISCARD → deal stalls at `MATCHING`. Indian customs records give real export history. | `parseforge/zauba-scraper` (6), `trev0n/zauba-scraper` (1), `getascraper/zauba-trade-data-scraper` (1) | Match Engine reliability score, sourcing |
| E-2 | **Buyer contacts.** Trade actors return company names, rarely an email. | `vdrmota/contact-info-scraper` (3,042), `harvestapi/linkedin-company` (6,885), `harvestapi/linkedin-company-employees` (5,586 — procurement / purchasing manager), `clearpath/email-finder-api` (420) | Buyer outreach in the negotiation flow, RFQ dispatch |
| E-3 | **Email checks before sending.** Bounces damage the sending domain. | `bounceverify/bounceverify-email-verifier` (1,701), `michael.g/email-verifier-validator` (505) | Before `email_gateway` sends to a new address |
| E-4 | **Buyers outside the US.** ImportYeti covers US sea imports only. | `memo23/europages-scraper` (133 — returns EU VAT IDs for the existing `ViesConnector`), `santamaria-automations/kompass-scraper` (18), `zen-studio/kompass-company-scraper` (10), `compass/crawler-google-places` (44k — Gulf and other distributors) | Trade-Min buyer discovery |
| E-5 | **Finding buyer websites** to feed E-2. | `apify/google-search-scraper` (24k) | Enrichment chain |
| E-6 | **Consumer demand in destination markets.** | `apify/google-trends-scraper` (2,590), `junglee/amazon-bestsellers` (498) | `demand_signals.py`, opportunity score |
| E-7 | **Shipment tracking without Cogoport / Freightos keys.** No freight webhook → deal never reaches `CLEARED`. | `muhammetakkurtt/maersk-tracking-scraper` (16), `muhammetakkurtt/cma-cgm-cargo-tracking-scraper` (18), `muhammetakkurtt/cosco-cargo-tracking-scraper` (11), `muhammetakkurtt/oocl-cargo-tracking-scraper` (14), `brii3343/msc-container-tracking-scraper` (1), `solidcode/myshiptracking-scraper` (4, vessel position) | Deal watcher polls `IN_TRANSIT` deals and emits the ARRIVED milestone |
| E-8 | **Website content for the RAG store** — buyer/supplier sites, destination-market import rules. | `apify/website-content-crawler` (13.6k, free) | `shared/rag/` ingestion |
| E-9 | **Sanctions screening** of the counterparty before the PI goes out — no such check exists today. | `ryanclinton/opensanctions-search` (3), `weirworks/sanctions-change-feed` (2). Low usage: the OpenSanctions API may be better. | Sourcing verification, before `PI_SENT` |
| E-10 | **Foreign buyer company verification.** | `ryanclinton/opencorporates-search` (17), `foxlabs/lei-company-ownership` (2). Low usage: the GLEIF API may be better. | Alongside `ViesConnector` / `AllianzTradeConnector` |
| E-11 | *(Optional)* Supplier sourcing outside India. | `haketa/made-in-china-scraper` (18), `devcake/globalsources-supplier-scraper` (15) | Only if PortLess sources outside India |

No usable actor was found for **ocean freight rates** (the DAT actors cover US
trucking); PortLess keeps Freightos for rates.

---

## 4. Limits

- **Coverage:** ImportYeti is US imports by sea only — no EU, Gulf or other corridors.
  Trademo's scraper returns profile summaries, not full manifests.
- **Contacts:** trade actors return company names and shipment history, rarely a buyer
  email — hence the E-5 → E-2 → E-3 chain.
- **Terms of service:** scraping IndiaMART, TradeIndia, Alibaba, Trademo, LinkedIn,
  Europages and Kompass likely breaches their terms — acceptable for research, risky as
  the commercial data backbone. Public customs records (ImportYeti, Zauba) and the
  carriers' own tracking pages are the safest of the set.
- **Reliability:** community actors break when the source site changes. Prefer actors
  with recent updates and good run stats; keep a second actor as fallback.

---

## 5. Recommended use (priority order)

1. Alibaba RFQ actor (C-3) → live buyer intent (the Level 3 signal the scanner lacks).
2. ImportYeti actor (C-1) → qualify US-corridor buyers and capture real past unit
   prices, replacing today's 1.25× FOB buyer-price heuristic (gap A-2).
3. IndiaMART / TradeIndia actors (C-4, C-5) → supplier shortlists, verified through the
   existing GSTN check, then Zauba export history (E-1) for the reliability score.
4. Contact chain (E-5 → E-2 → E-3) before any outbound buyer email.
5. Carrier tracking actors (E-7) as the milestone source until freight API keys exist.

---

## 6. How PortLess will connect them

1. **One connector, not one per actor.** A single `ApifyActorConnector` in
   `shared/connectors/apify.py` with the standard
   `quote / book / status / webhook_verify` interface, running actors through Apify's
   `run-sync-get-dataset-items` endpoint with an `APIFY_TOKEN`; empty result when the
   token is absent (graceful stub), registered in `registry.py`.
2. **Actor IDs in config, primary + fallback.** e.g.
   `APIFY_ACTOR_IMPORTYETI=zen-studio/importyeti-scraper,scrapesage/importyeti-scraper` —
   when a community actor breaks, the fallback runs and the fix is a config change.
3. **Map output onto existing shapes** (e.g. `BuyerSignal.unit_price_usd`, supplier
   candidates), so a paid API can replace any source later without touching the pipeline.
4. **Enrichment order:** buyer company (C-1, E-4) → website (E-5) → contacts (E-2) →
   email check (E-3) → outreach. Suppliers: IndiaMART / TradeIndia → Zauba (E-1) →
   GSTN / DGFT / MCA / Udyam checks.

---

## 7. Questions for the actor generator

For each ID in §2 and §3:

1. **Fit:** does the best candidate actor return every field in "Data PortLess needs"
   (especially unit price on C-1, GST number on C-4/C-5, VAT ID on E-4, ETA and
   milestone on E-7)?
2. **Health:** recent build, success rate of recent runs, maintainer still active?
3. **Wrapper needed?** Build a PortLess-owned wrapper Actor when any of these hold:
   - no single actor returns all required fields (wrapper merges two or more);
   - only low-usage / fragile actors exist (wrapper adds fallback across actors);
   - outputs differ across candidates (wrapper normalises to one schema);
   - a chain is always run together (E-5 → E-2 → E-3, or E-7 across carriers by
     container prefix) and is cheaper or simpler as one Actor.
   Otherwise use the Store actor directly through `ApifyActorConnector`.
4. **Output:** for each ID, the verdict (use as-is / wrap / build new), the chosen
   primary and fallback actor, and the output schema PortLess should map.


---

## 8. Implementation (2026-10-23) — the actors PortLess generated and how the backend uses them

The actor generator (`/actor-pipeline`, `C:/workArea/pandian/apify-actors`, Apify
account **`rich_minds`**) produced PortLess-owned actors for the needs in §2–§3.
The backend talks to all of them through **one connector**,
`shared/connectors/apify.py` (base `ApifyActorConnector`: start run →
wait → read dataset; one thin subclass per actor that maps the dataset rows
onto the pipeline's own shapes). Registry group 7 in
`shared/connectors/registry.py`.

### 8.1 Verdict per need

| ID | Verdict | PortLess actor (`rich_minds~…`) | Connector name | Plugs into |
|---|---|---|---|---|
| C-1 | **built** (wrapper over ImportYeti Store actors with fallback chain, normalised schema, AI fit) | `us-importer-finder-ai` | `apify_us_importers` | Trade-Min Phase 2 (US market): `BuyerSignal` with website / phone / contact route / fit score / pitch angle / shipments; the Comtrade benchmark is passed as `unitValueUsdPerKg` so `estValueUsd12m` is filled |
| C-3 | **built** (wrapper over Alibaba RFQ Store actors, AI fit + quote draft) | `alibaba-rfq-leads-ai` | `apify_alibaba_rfq` | Trade-Min Phase 2 (every market): `BuyerSignal(signal_type="rfq")` with buyer name, country, quantity, stated target price (USD/MT when the buyer quoted USD per kg/ton) |
| C-6 | **built** (own source: UN Comtrade API, Store scraper fallback) | `export-market-finder-ai` | `apify_export_markets` | Trade-Min Phase 1: demand signal + **`price_benchmark_usd_per_mt`** (what the market pays per MT) → `MatchEngineSeed.buyer_target_price_usd` when no named buyer carries a price — replaces the 1.25× FOB heuristic (gap A-2) |
| C-4 / C-5 / E-1 | **slot wired, actor to generate** | `india-supplier-finder-ai` | `apify_india_suppliers` | Trade-Min Phase 4: `SellerCandidate` with price (normalised to USD/MT), MOQ, GSTIN/IEC, e-mail/phone/website → RFQ by e-mail, GSTN verification |
| E-2 / E-5 | **built** (own source: Google SERP proxy + the company's pages; Store fallbacks) | `company-contact-finder-ai` | `apify_contact_finder` | Deal enrichment (`shared/orchestration/enrichment.py`): `Party.email / website / phone / address / vat_number / contact_confidence` |
| E-3 | **built** (own verifier; BounceVerify deep check optional) | `email-list-qualifier-ai` | `apify_email_qualifier` | Deal enrichment: `Party.email_status` (ok / risky / invalid) for addresses the contact finder could not verify |
| E-9 / E-10 | **built** (own source: OFAC/UN/EU/UK list files, GLEIF, VIES) | `counterparty-check-ai` | `apify_counterparty_check` | Deal enrichment: `Party.sanctions_status` (clear / review / block), `lei`, `vat_valid`; gates the Proforma Invoice (`CONFIRM_COUNTERPARTY_RISK` on review, withheld on block) |
| E-7 | not built | — | — | Carrier tracking actors remain the plan until freight API keys exist |
| E-4 / E-6 / E-8 / E-11 | not built | — | — | Optional; the US importer + Alibaba RFQ actors cover the first corridors |

### 8.2 Where each actor runs in the trade loop

```
POST /trade-min/scan
  Phase 1  comtrade · census · export-market-finder-ai  → demand_index, price_benchmark_usd_per_mt
  Phase 2  alibaba-rfq-leads-ai (all markets) · us-importer-finder-ai (US) · alibaba API · EU TED · GDELT
  Phase 4  india-supplier-finder-ai · IndiaMART · TradeIndia                → seller_candidates (USD/MT)
GET  /trade-min/sources            → which of the above are live / stub / mock (Radar header)
deal.created (MATCHING)
  enrichment: company-contact-finder-ai → email-list-qualifier-ai → counterparty-check-ai
              → Party.email / website / phone / email_status / sanctions_status
              block ⇒ deal.exception + RFQ gate forced L0
  DISPATCH_RFQ → suppliers with an e-mail get the RFQ by e-mail ([RFQ:<id>] token)
negotiation.agreed
  counterparty-check-ai re-run when the buyer's verdict is older than 30 days
  clear → PI · review → CONFIRM_COUNTERPARTY_RISK gate · block → PI withheld
POST /deals/{id}/enrich            → operator re-runs the chain; PATCH /deals/{id}/parties/{role} corrects a contact
```

### 8.3 Configuration and cost controls

`APIFY_TOKEN` (or the Apify CLI login in `~/.apify/auth.json`, unless
`APIFY_TOKEN_AUTODISCOVER=false`), `APIFY_ACTOR_OWNER=rich_minds`,
`APIFY_ACTOR_<KEY>` per-actor override, `APIFY_ENABLE_AI` (AI tier on by
default), `APIFY_MAX_ITEMS=25` (each actor's free units), `APIFY_RUN_TIMEOUT_SECONDS=240`,
`APIFY_MAX_DISCOVERY_CHARGE_USD=0.5`, `APIFY_WEBHOOK_SECRET`. Every call passes
`dedupeAcrossRuns=false` (PortLess keeps its own memory; the actors' cross-run
dedupe would hide a buyer from the next scan). Trade-Min caches actor results
(`services/trade_min/config.py`: export markets / suppliers 7 d, US importers
3 d, Alibaba RFQ 12 h). Without a token every connector returns an empty,
well-formed result and the readiness check (`GET /health/readiness`) flags it.
**The unit-test suite pins stub mode** (`tests/conftest.py`) so no paid run
ever starts from CI.

Account note: the `rich_minds` plan currently reports *"plan cannot run Store
Actors"* for the wrapper actors' live discovery (`us-importer-finder-ai`,
`alibaba-rfq-leads-ai`, `1688-supplier-finder-ai`); their `list` / `dataset`
modes and the own-source actors (`export-market-finder-ai`,
`company-contact-finder-ai`, `counterparty-check-ai`, `email-list-qualifier-ai`)
run. Upgrade the plan (or run the Store actors from an allowed account and feed
their datasets) before relying on live US-importer / Alibaba discovery.

### 8.4 `india-supplier-finder-ai` — the actor still to generate

Seller-side twin of the buyer actors. Brief for `/actor-researcher` →
`/actor-pipeline` (origin: this document, §2 C-4/C-5 + §3 E-1):

* **Sources:** IndiaMART / TradeIndia Store scrapers (`thirdwatch/indiamart-supplier-scraper`,
  `haketa/indiamart-scraper`, `haketa/tradeindia-scraper`) with fallback chain;
  optional Zauba export-history actor for the reliability signal; GSTIN format
  check + state → port distance.
* **Input:** `hsCodes[]`, `products[]`, `originCountry="IN"`, `states[]` (optional),
  `goalDescription`, `minScore`, `maxQualified`, `enableAi`, the common
  `dedupeAcrossRuns` / `sourceMode` / webhook keys.
* **Dataset row** (the connector `ApifyIndiaSupplierConnector._seller` already
  maps these, with aliases): `itemId`, `name` (company), `city`, `state`,
  `productName`, `minPrice`, `maxPrice`, `priceUnit` (`USD/kg` | `USD/MT` | `INR/kg`),
  `moq`, `annualCapacityMt`, `url`, `contactId`, `bestEmail`, `phones[]`,
  `website`, `gstin`, `iec`, `udyam`, `exportShipments12m` (Zauba), `score`,
  `fitReason`, `generatedText` (bilingual RFQ draft), `dedupeKey`, `scrapedAt`.
* **Unit:** qualified supplier; junk (no price, no contact, GSTIN invalid) never charged.

Until it exists Phase 4 keeps the IndiaMART / TradeIndia API connectors (stub
without keys) and `TRADE_MIN_MOCK_MODE` serves canned actor rows.
