# Opportunity Preparation Pipeline — Endpoints & Third-Party Connectors

Covers everything from `POST /trade-min/scan` (opportunity discovery) through
`POST /deals` (seeding a deal from an opportunity). Before an opportunity is
saved, its top buyers and suppliers are checked (`services/trade_min/validation.py`:
sanctions screening, e-mail deliverability, GSTN / DGFT IEC when the number is
known) and each carries a `validation` (`verified` / `unverified` / `failed`).
`deals/service.py` scores sellers with those results: a failed check fails the
Match Engine gatekeeper, a check not run yet is scored provisionally and recorded
as `verification="unverified"` (never as verified). The full GSTN / DGFT / MCA /
Udyam verification still runs at quote intake (`POST /sourcing/quotes`).

---

## 1. Internal endpoints (8)

| Method + Path | File:Line | Functional role | Call type |
|---|---|---|---|
| `POST /trade-min/scan` | `services/trade_min/router.py:17-24` | Runs the 5-phase discovery pipeline; returns enriched, ranked opportunities | HTTP (external trigger) |
| `GET /trade-min/opportunities` | `services/trade_min/router.py:27-42` | Lists active opportunities, sorted by urgency tier then arbitrage rank | HTTP |
| `GET /trade-min/opportunities/{id}` | `services/trade_min/router.py:45-54` | Fetches a single enriched opportunity | HTTP |
| `GET /trade-min/opportunities/{id}/match-seed` | `services/trade_min/router.py:57-82` | Pre-computes Match Engine input fields (buyer target price, supplier FOB price, supplier-to-port distance) so Deals doesn't re-call connectors | HTTP, also called in-process by Deals |
| `POST /match-engine/score` | `services/match_engine/router.py` | Scores a candidate seller against the opportunity (deterministic CMS formula) | In-process Python call from `deals/service.py` in the monolith; HTTP in standalone microservice mode |
| `POST /trade-min/opportunities/{id}/convert` | `services/trade_min/router.py:85-102` | Marks an opportunity `CONVERTED` after a deal is created, so it's not surfaced again | In-process call from `deals/service.py`, mirrors this HTTP route |
| `POST /deals` | `services/deals/router.py:71-84` | Accepts `opportunity_id`; orchestrates seed → score → seed parties → create deal at `MATCHING` | HTTP (entry point) |
| `GET /deals/{deal_id}/match-scores` | `services/deals/router.py:150-166` | Surfaces the persisted match/score results for a deal | HTTP |

In the combined monolith (`app.py`), Deals → Trade-Min and Deals → Match Engine
are direct Python function calls (`deals/service.py`), not network hops. In
standalone microservice mode each module's `client.py` makes the same calls
over HTTP.

---

## 2. Third-party connectors actively invoked (8)

Confirmed by grepping `_safe_quote(...)` call sites in
`services/trade_min/service.py:222,231,479-485,575,590,687-688` — these are
the only connector names the live pipeline calls today.

| Phase | Provider | Base URL | Env var | Status |
|---|---|---|---|---|
| 1 — Historical demand | UN Comtrade | `https://comtradeapi.un.org` | `COMTRADE_SUBSCRIPTION_KEY` | Free tier: 500 calls/day, up to 100K records/call (auto-approved signup) |
| 1 — Historical demand | US Census Int'l Trade | `https://api.census.gov/data/timeseries/intltrade/imports/hs` | `CENSUS_API_KEY` | Free, instant key, no payment ever |
| 2 — Live demand signals | Alibaba RFQ | `https://gw.api.alibaba.com/openapi/param2/1` | `ALIBABA_APP_KEY` / `ALIBABA_APP_SECRET` | Requires a paid Alibaba.com GGS seller membership; API access itself is free to registered developers on top of that |
| 2 — Live demand signals | GDELT Project | `https://api.gdeltproject.org/api/v2/doc/doc` | none | Free, fully open, no key |
| 2 — Live demand signals | EU TED | `https://ted.europa.eu/api/v3.0` | none | Free, fully open, no key |
| 3 — Tariff gate | EU Access2Markets | env-supplied URL template | `ACCESS2MARKETS_API_URL` / `ACCESS2MARKETS_API_KEY` | Free EU public data; connector no-ops to WITS fallback until a production URL is configured (early-access registration) |
| 3 — Tariff gate | World Bank WITS (TRAINS) | `https://wits.worldbank.org/API/V1/SDMX/V21/rest/data/DF_WITS_Tariff_TRAINS` | none | Free, fully open, no key — primary tariff source today |
| 4 — Seller discovery | IndiaMART | `https://seller.indiamart.com/lmsrest/api/v1` | `INDIAMART_API_KEY` / `INDIAMART_MOBILE` | API access bundled into a paid Gold/Platinum seller subscription (see pricing below); not purchasable standalone |
| 4 — Seller discovery | TradeIndia | `https://api.tradeindia.com/v1` | `TRADEINDIA_API_KEY` | API access bundled into a paid Gold/Platinum membership; not purchasable standalone |

**Already working for free in this codebase today, no key needed:** GDELT,
EU TED, WITS TRAINS (3 of 8).
**Free but require a one-time signup key:** UN Comtrade, US Census (2 of 8).
**Require an existing paid seller/developer account:** Alibaba RFQ,
IndiaMART, TradeIndia (3 of 8).

---

## 3. MacMap & Volza — registered, not invoked

Both are wired as connector classes and listed in
`shared/connectors/registry.py:39-86` under a block explicitly commented
**"LEGACY / PAID — retired from the default pipeline, kept for future use."**
`services/trade_min/service.py`'s live phase logic (`_P1_WEIGHTS` at line
108-111, and the `_safe_quote()` call sites) does **not** call `"volza"` or
`"macmap"` — only `comtrade` and `census` feed Phase 1, and only
`access2markets`/`wits_tariff` feed Phase 3. The module docstring at the top
of `service.py` (lines 3-6) still lists Volza and MacMap in the phase
descriptions; that comment is stale relative to the actual code and should be
read as historical, not current behavior.

They are excluded from the 8-connector active count above, but documented
here since they're fully implemented and can be re-enabled by adding their
API keys and restoring them to the Phase 1 / Phase 3 connector lists.

| Provider | Base URL | Env var | File:Line |
|---|---|---|---|
| MacMap (ITC Market Access Map) | `https://api.macmap.org/v2` | `MACMAP_API_KEY` | `shared/connectors/trade_intel.py:1143-1158` |
| Volza | `https://backend.volza.com` | `VOLZA_API_KEY` | `shared/connectors/trade_intel.py:238-256` |

---

## 4. Pricing summary — all 10 connectors

Figures marked "confirmed" come from pricing stated directly in this
codebase's connector docstrings (which document the provider's official
pricing page as of when they were written) or from a provider's own current
pricing page found via web search. Figures marked "not publicly listed" are
sales-gated (`contact sales` / `request a quote`) and cannot be stated as a
number without guessing.

| Provider | Free tier | Paid tier | Confidence |
|---|---|---|---|
| GDELT | Fully free, no key, ~1 req/sec soft limit | — | Confirmed (code) |
| EU TED | Fully free, no key | — | Confirmed (code) |
| World Bank WITS TRAINS | Fully free, no key | — | Confirmed (code) |
| EU Access2Markets | Free EU public data (once a production endpoint is registered) | — | Confirmed (code) |
| UN Comtrade | 500 calls/day, up to 100K records/call, auto-approved | Premium: unlimited calls, up to 250K records/call — price not publicly listed, sold via `subscriptions@un.org` | Free tier confirmed (code); paid price not publicly listed |
| US Census Int'l Trade | Free, instant key, unlimited practical use for this pipeline | — | Confirmed (code) |
| IndiaMART | None — API only ships bundled with a paid seller plan | Gold ≈ ₹2,70,000/year, Platinum ≈ ₹5,00,000/year (India seller-subscription pricing, varies by category/region) | Paid pricing found publicly, but is a general seller-membership rate card, not an API-specific price |
| TradeIndia | None — API only ships bundled with a paid seller plan | Plans from ≈ ₹7,499 per 6 months (Business Pro) up; higher tiers (VIP/Super Premium) not fully priced publicly | Partial — some tiers publicly priced, others sales-gated |
| Alibaba RFQ | Beta API free to registered developers | Requires an underlying paid Alibaba.com GGS/Gold Supplier membership (RFQ quota, e.g. 60 quotes/month, gated by that plan) — membership price not captured in search results | API access free; underlying membership price not publicly confirmed |
| Volza | None | $0.10 per search + from $0.02 per shipment record (per this codebase's docstring); a separate general-access plan starts at ~$1,500/year and per-record pricing elsewhere on Volza's site is quoted around $3.50/record — Volza's public pricing is inconsistent across their own pages | Confirmed a paid model exists; exact current rate varies by source, treat as approximate |
| MacMap | Free basic portal registration/browsing | Institutional License required for full API access — price not publicly listed, sold via `marketapi@intracen.org` | Free registration confirmed; paid API price not publicly listed |

**Bottom line:** of the 8 connectors the live pipeline actually calls today,
3 are entirely free with no key, 2 are free but need a signup key, and 3
require an existing paid seller/developer account whose cost is a general
membership fee rather than a standalone per-call API price. Neither MacMap
nor Volza publish a fixed public API price — both are quote-based.

---

## 5. Summary counts

- **Internal endpoints:** 8 (5 trade_min, 1 match_engine, 2 deals)
- **Third-party APIs actively called:** 8
- **Third-party connectors implemented but not currently invoked:** 2 (MacMap, Volza)
- **Free / no key required:** 3 of 8 active (GDELT, EU TED, WITS TRAINS)
- **Free with a signup key:** 2 of 8 active (Comtrade, Census)
- **Requires an existing paid account:** 3 of 8 active (Alibaba, IndiaMART, TradeIndia)

---

## 6. Which of the 8 internal endpoints are actually required to *build* the list

Only **1 of the 8** is load-bearing for producing the opportunities list itself:

- **`POST /trade-min/scan`** runs the 5-phase pipeline and is the only endpoint
  that *creates* `EnrichedOpportunity` records.

The rest are not alternate ways to build the list — each does a different job
on data `/scan` already produced, and none of them can substitute for it:

| Endpoint | Role relative to list-building |
|---|---|
| `GET /trade-min/opportunities` | Reads the list `/scan` already wrote |
| `GET /trade-min/opportunities/{id}` | Reads one opportunity `/scan` already wrote |
| `GET /trade-min/opportunities/{id}/match-seed` | Pre-computation convenience, run on demand per opportunity, not part of building the list |
| `POST /trade-min/opportunities/{id}/convert` | Lifecycle flag flipped *after* a deal already exists — downstream of list-building |
| `POST /match-engine/score` | Belongs to the next stage: scoring one opportunity into a deal |
| `POST /deals` | Belongs to the next stage: seeding a deal from one opportunity |
| `GET /deals/{deal_id}/match-scores` | Reads back a deal's score — downstream of both list-building and deal creation |

**Conclusion:** you cannot pick "one from each division" as an equivalent
3-endpoint substitute — the divisions aren't parallel alternatives, they're
sequential stages (discover → read/convert → score → deal). Preparing the
opportunities list needs exactly `POST /trade-min/scan`; the other 7 endpoints
serve later, per-opportunity steps and stay in the system regardless.

---

## 7. Do all 3 paid connectors need to stay, or can we drop to one?

The 3 paid connectors (Alibaba RFQ, IndiaMART, TradeIndia) sit in two
different phases and are **not** equally droppable:

**Phase 2 — live demand signals** (`_phase2_live_signals`, `service.py:462-553`)
calls Alibaba RFQ, GDELT, and EU TED concurrently. GDELT and EU TED are free.
`_safe_quote()` swallows a missing-key error gracefully, so if
`ALIBABA_APP_KEY` is absent the phase still runs — it just loses the "buyer
posted an RFQ this week" signal. **Alibaba RFQ can be dropped entirely
without breaking the pipeline.**

**Phase 4 — seller discovery** (`_phase4_seller_discovery`, `service.py:682-701`)
calls only IndiaMART and TradeIndia, concurrently, with **no free fallback**.
If both are absent, `candidates` comes back empty — the opportunity has no
seller/FOB-price data at all, which starves `get_match_seed`
(`trade_min/router.py:57-82`) and leaves Match Engine and `POST /deals` with
nothing to score.

| Paid API | Droppable? | Consequence |
|---|---|---|
| Alibaba RFQ | Yes | Pipeline runs fine; GDELT + EU TED still populate Phase 2 |
| IndiaMART | Only if TradeIndia stays | Fewer seller candidates, list not empty |
| TradeIndia | Only if IndiaMART stays | Fewer seller candidates, list not empty |
| Both IndiaMART + TradeIndia | **No** | Phase 4 returns zero sellers — breaks `match-seed`, scoring, and deal seeding |

**Minimum viable paid footprint: 1 of 3** — either IndiaMART or TradeIndia
alone (they are structurally interchangeable, just different Indian supplier
directories), with Alibaba dropped. You cannot go to 0 paid APIs today,
because Phase 4 has no free alternative currently wired in.

---

## 8. Free alternatives researched for seller discovery (Phase 4)

Researched whether IndiaMART/TradeIndia could be replaced entirely with a
free source, since Phase 4 is the one phase with no existing free fallback.

**Most viable candidate: `data.gov.in` (Open Government Data Platform India)
— Udyam MSME registration dataset.** Free, public, API-accessible with only
a free `data.gov.in` signup key (same model as India's own API Setu). Returns
registered MSME manufacturer records: company name, location/district, and
manufacturing-vs-services activity type.

**Why it can't fully replace IndiaMART/TradeIndia today:** it's a government
*registry*, not a commercial *seller directory*. It does not provide the
fields `_seller_from_raw()` (`service.py:704-725`) needs to build a usable
`SellerCandidate`:

| Field required by `SellerCandidate` | Udyam dataset has it? |
|---|---|
| Company name, location, state | Yes |
| `estimated_fob_price_usd` | No — no pricing data at all |
| HS-code-to-product mapping | No — only free-text "activity" category, unreliable to map to HS codes |
| `annual_capacity_mt` | No |
| `listing_url` / `contact_id` (for RFQ dispatch) | No |

Two other government sources were checked and ruled out:

- **GeM (Government e-Marketplace)** — 6M+ registered sellers, but no
  official free API; the only API access found is via commercial scraping
  vendors (e.g. RealDataAPI), which just swaps one paid vendor for another.
- **DGFT IEC exporter directory** — no public discovery API; access requires
  already being an onboarded IEC holder with a whitelisted IP. This is
  verification infrastructure (used later, at `sourcing/service.py` quote
  intake), not a seller-discovery source.

**Recommendation:** no free, like-for-like replacement exists for Phase 4's
commercial directory role. The practical option is to keep exactly one paid
directory (per §7) and use the free Udyam dataset as a secondary
**enrichment/verification** signal — confirming a seller found via the paid
directory is a genuine registered MSME manufacturer — rather than as the
primary discovery source. This mirrors the role Udyam already plays later in
the flow (regulatory verification at quote intake), just applied earlier as
an optional cross-check, not a replacement connector call in Phase 4.

---

## 9. IndiaMART / TradeIndia — is there a per-API-call cost?

No. Neither provider meters or charges per API invocation. Both bundle API
access into a flat seller subscription — you either hold a qualifying paid
seller account (API works, no per-call charge) or you don't (API key cannot
even be generated). There is no marginal cost per additional request beyond
the fixed daily call cap.

**IndiaMART** (`shared/connectors/trade_intel.py:454-456`):
- Docstring: *"Pricing: Included in seller subscription (Gold/Platinum
  plan). Rate limit: 1,000 calls/day on standard plan."*
- Confirmed via IndiaMART's own help docs: the "Generate Key" option to
  create an API key is restricted to **paying clients only** — there is no
  standalone API product to buy.
- Cheapest confirmed path to a paid seller account: ≈ ₹3,000/month or
  ≈ ₹28,000/year (Mini Dynamic Catalogue plan) — it is unconfirmed whether
  this entry tier specifically unlocks Lead Manager API access, or whether
  API access requires stepping up to Gold (≈ ₹2,70,000/year) or Platinum
  (≈ ₹5,00,000/year). IndiaMART does not publish a plan-to-API-access
  mapping publicly.
- Marginal cost per call: ₹0, up to the 1,000/day cap.

**TradeIndia** (`shared/connectors/trade_intel.py:627-628`):
- Docstring: *"Pricing: Included in paid membership (Gold/Platinum plan).
  Rate limit: 500 calls/day on standard plan."*
- No official TradeIndia developer/API pricing page could be found publicly
  — their public pages are seller-listing pages, not an API portal. Unlike
  IndiaMART, there is no confirmed entry-level rupee figure for the tier
  that unlocks API access; this would require contacting TradeIndia sales
  directly.
- Marginal cost per call: ₹0, up to the 500/day cap (once on a qualifying
  paid membership).

| | IndiaMART | TradeIndia |
|---|---|---|
| Per-call fee | None — flat, bundled | None — flat, bundled |
| Cheapest *confirmed* path to API access | ≈ ₹28,000/yr (tier unconfirmed) to ≈ ₹2,70,000/yr (Gold, confirmed) | Not publicly documented |
| Included daily call cap | 1,000 calls/day | 500 calls/day |
| Marginal cost beyond the cap | Not documented (would require a plan upgrade) | Not documented |

**Conclusion:** the real cost decision isn't "how many API calls will this
cost" — it's "which annual seller membership tier to buy." IndiaMART has at
least a partially documented price ladder; TradeIndia's is entirely
sales-gated.

---

## 10. Broader market scan — alternate buyer/supplier discovery sources

A wider candidate list (23 providers) was evaluated for Indian
supplier/buyer discovery, beyond the two (IndiaMART, TradeIndia) already
wired into Phase 4 and the ones (Alibaba RFQ, EU TED) already wired into
Phase 2. None of these are integrated into `trade_min/service.py` today —
this is reference material for a future connector decision, not a change to
the active pipeline.

Grouped first by **which side of the transaction the source informs** —
buyer discovery (Phase 2 role) vs. seller/supplier discovery (Phase 4
role) vs. sources that cover both. Within each group, sources are sorted
into **API available** (sub-sorted free before paid) and **No API**, since
that split determines whether a source can be wired into this pipeline's
connector pattern at all, before cost is even a question.

### A. Buyer-information sources (Phase 2 role — "who is buying")

**API available — free**

| Provider | Data type | API | Cost | Reliability notes |
|---|---|---|---|---|
| **EU TED** *(already wired, §2)* | Named EU institutional buyers — formal tenders, budgets, deadlines | Yes (free, no key) | Free | Best free, named-buyer source; EU-only |

**API available — paid**

| Provider | Data type | API | Approx. cost | Reliability notes |
|---|---|---|---|---|
| **Volza** *(registered, not invoked — §3)* | Buyer/importer identity from Bill-of-Lading manifests, 203 countries | Yes (paid, usage-based) | $0.10/search + $1.50–$3.50/record; general access from ~$1,500/yr | Named, verified buyer companies (not just country-level trends) — the strongest *buyer*-identity source on this list; pricing is inconsistent across Volza's own pages, treat figures as approximate |
| **Alibaba RFQ** *(already wired, §2)* | Active buyer RFQ feed — buyer country + product, not a real company name in this connector's current mapping | Yes (paid, bundled in seller membership) | Beta API free to registered developers; underlying GGS membership price not public | Strongest buying-*intent* signal (buyer is shopping right now), weakest buyer-*identity* signal on this list |

**No API**

| Provider | Data type | Notes |
|---|---|---|
| **Alibaba** (general marketplace, beyond the RFQ connector) | Foreign buyer discovery across all categories, not just active RFQs | Partner/affiliate only — no open self-serve API; only the RFQ feed above is actually reachable |
| `Trade Connect – Buyer Connect (Govt of India)` | Curated global buyer leads via DGFT | Free (govt portal), web tool only, no programmatic access |
| `Indian embassies' commercial wings & trade offices` | Buyer lists, market reports, introductions | Free (govt), web/email/PDF only |

### B. Seller/supplier-information sources (Phase 4 role — "who can supply")

**API available — free**

*(none — every seller/supplier data source found offers a free registration
tier at most; actual data access is paid in every case)*

**API available — paid**

| Provider | Data type | API | Approx. cost | Reliability notes |
|---|---|---|---|---|
| **IndiaMART** *(already wired, §1/§9)* | Indian supplier directory, leads | Yes (paid) | ₹28K–₹5L/yr, bundled, no per-call fee | Largest Indian B2B directory; already in production use in this codebase |
| **TradeIndia** *(already wired, §1/§9)* | Indian supplier directory, leads | Yes (paid) | Gold/Platinum, not publicly listed | Secondary Indian directory; already in production use |
| **Seair Exim Solutions** | India export shipment data — exporter names, HS code, price, port, from 190+ Indian ports | Yes (per product page), also sold via IndiaMART storefront | From ≈ ₹1,500/yr for base data services; API tier pricing not separately published | 15+ years in the India EXIM data business; data-service pricing found is for the web/report product, not confirmed as the API-tier price |
| **EximFetch** | India trade *reference* data — ports, HSN codes, customs locations, airline codes | Yes (REST API) | Paid, "applicable fees on pricing page" — exact figure not public | Reference/enrichment data, not a seller contact list — useful for normalizing HS codes/ports alongside a real supplier source, not for discovery on its own |
| **ExportersIndia** | Indian exporter directory + WhatsApp-integrated CRM (CRM@Weblink, Meta partner) | Indirect — CRM/messaging API, not a data-list API | Not public | The API surface is for lead *communication*, not for pulling supplier lists programmatically |

**No API**

| Provider | Data type | Notes |
|---|---|---|
| `TradeWheel`, `Made in India (madeinindia.world)`, `Indian Trade Mart`, `GlobalSources`, `EximNext` | Web-first Indian/global supplier directories | Free to browse; manual export or negotiated enterprise feed only |
| `Export Promotion Councils (EPCs)`, `MSME portals & industry associations` (CII, PHDCCI, FIEO, state MSME), `EPC/FIEO trade fairs & events` | Authoritative member/exporter directories | Free (govt/industry); web pages, PDFs, event-based — same institutional-trust tier as the free Udyam MSME dataset in §8, but less API-friendly (Udyam at least has a `data.gov.in` API) |

### C. Sources covering both buyer and seller data

**API available — free**

*(none)*

**API available — paid**

| Provider | Data type | API | Approx. cost | Reliability notes |
|---|---|---|---|---|
| **Trademo** *(registered, not invoked — legacy)* | Global Buyer/Supplier List + Profile API, searchable by HS code/country | Yes (paid, sold via Microsoft/Azure Marketplace) | Not publicly listed — quote-based via Datarade/Marketplace | Enterprise-grade, structured, sold as a proper SaaS API product (unusual for this space); no self-serve price transparency |
| **Kompass India** | Global B2B directory — firmographics, products, contacts, import/export activity filter | Yes (enterprise) | SaaS from ≈ €1,490/yr; on-premise €16,000–€30,000/yr | Long-established global directory (60M+ companies); pricing floor is publicly confirmed, unusual for this category |
| **Eximpedia** | Global import/export shipment data, 200+ countries — buyer name, supplier name, HS code, price, port | Yes (REST API) | Tiered plans (Starter → Enterprise), country-count-gated; exact rupee/dollar figures not public | Broad country coverage; API exists but pricing requires contacting sales |
| **EximJunction / Exim Trade Data** | Global trade data, 200+ countries — HS code, importer, exporter, port | Yes (API + bulk delivery) | Starter → Premium tiers, +18% GST, exact figures not public | Positions itself as enterprise-capable (bulk delivery option); same "contact for pricing" gap as Eximpedia |

**No API**

| Provider | Data type | Notes |
|---|---|---|
| `The Dollar Business`, `Happily Trade EXIM` | AI-based EXIM analysis / verified buyer+seller transaction histories | Free to browse / not public; web UI and exported reports only |

### Takeaways

- **For supplier discovery (Phase 4 role):** nothing in this list beats
  IndiaMART/TradeIndia (group B) on API self-serve access *and*
  India-specific supplier coverage. Kompass and Trademo (group C) are more
  enterprise-polished but neither publishes a usable low-cost self-serve
  price the way IndiaMART does.
- **For buyer discovery (Phase 2 role, per the earlier buyer-vs-supplier
  question):** **Volza** (group A) is the standout — it's the only source on
  this list that returns *named, verified buyer companies* with shipment
  history, via a usable pay-per-use API, rather than country-level trend
  data (Comtrade/Census) or a placeholder buyer label (this codebase's
  current Alibaba RFQ mapping — see §2/answer above). Volza is already a
  registered-but-unused connector in this codebase (§3); wiring it into
  Phase 2 would be the smallest-lift way to get real buyer identities
  without adding a brand-new provider.
- **No entirely free alternative exists** for either buyer or supplier
  discovery at this data quality — every API-backed option across groups
  A/B/C is paid; the no-API rows in each group are free-to-browse but
  conflict with this pipeline's connector pattern (`shared/connectors/` —
  typed `quote/book/status/webhook_verify` interface, not a scraper).
