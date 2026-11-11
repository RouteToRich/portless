---
title: PortLess test case catalogue
scope: Matching → negotiation → PI → escrow → PO → QC → freight → customs → delivery → payout → cash reconciliation
layers: [api, flow, ui, e2e]
format: Gherkin in fenced blocks, one Feature per block
compatible_with: [Zephyr Scale BDD script, Xray Cucumber, pytest-bdd, Playwright-BDD, Cucumber]
status: Not yet automated. Expected results come from reading the code on 2026-11-09; the self-contained API cases (authentication, validation, unknown records) were run once against the application.
---

# PortLess test case catalogue

Positive, negative and exception scenarios for every endpoint and workflow phase, from the first prospect to the final cash reconciliation, across the backend and the operator console.

## 1. How this file is organised

| Section | Use it for |
|---|---|
| [2. Format](#2-format-and-zephyr-mapping) | How a scenario maps to a Zephyr Scale test case |
| [3. Automation environment](#3-automation-environment) | Settings, database, collections, safety rules |
| [4. Step vocabulary](#4-step-vocabulary) | The fixed steps to implement once |
| [5. Fixtures](#5-fixtures) | Standard deals and their exact money figures |
| [6. Backend and flow scenarios](#6-backend-and-flow-scenarios) | Phases 0 to 13 |
| [7. UI scenarios](#7-ui-scenarios) | Every console page and action |
| [8. End-to-end journeys](#8-end-to-end-journeys) | Complete deals, UI to backend |
| [9. Open defects and decisions](#9-open-defects-and-decisions) | Behaviour that needs a fix or a product decision |
| [10. Extracting the scenarios](#10-extracting-the-scenarios) | Turning this file into runnable `.feature` files |

**Workflow phases**

```text
DRAFT → MATCHING → NEGOTIATING → NEGOTIATION_AGREED → PI_SENT → PI_ACCEPTED → ESCROW_PENDING → ESCROW_FUNDED
      → PO_ISSUED → SUPPLIER_DISPATCH_READY → QC_PASSED → IN_TRANSIT → CLEARED → PAID
Branches: NEGOTIATION_FAILED → MATCHING | CANCELLED
          PI_SENT | PI_ACCEPTED | ESCROW_PENDING → CANCELLED
          SUPPLIER_DISPATCH_READY → QC_FAILED → DISPUTE → SUPPLIER_DISPATCH_READY | CANCELLED | REFUNDED
Terminal: PAID, CANCELLED, REFUNDED
```

## 2. Format and Zephyr mapping

Each fenced `gherkin` block is one Feature. Each `Scenario` or `Scenario Outline` is one test case; an Outline's `Examples` rows are its data-driven iterations.

| Zephyr Scale field | Where it lives |
|---|---|
| Key | `@TC-<AREA>-<NNN>` tag. After import, add the Jira key as `@TestCaseKey=<KEY>` if your reporter needs it. |
| Name | Scenario title |
| Objective | Scenario title plus the Feature description |
| Precondition | `Given` steps and the Feature's `Background` |
| Test script | The Gherkin text (BDD script type) |
| Test data | `Examples` tables and doc strings |
| Priority | `@P0` money or legal safety · `@P1` core flow · `@P2` secondary |
| Labels | The remaining tags |
| Folder | The `@phase:*` tag |

| Tag group | Values |
|---|---|
| Result type | `@positive` `@negative` `@exception` `@edge` `@security` |
| Layer | `@api` `@flow` `@ui` `@e2e` |
| Domain | `@money` `@compliance` `@email` |
| Needs | `@needs:time` (backdates records) · `@needs:route-mock` (browser-level API mock) · `@needs:fake-connector` (a connector must fail) |
| State | `@known-defect` (the expected result is the correct behaviour; it fails today) · `@decision-needed` (documents today's behaviour until a product decision is made) |

Tag suggested automation runs: `@P0 and not @known-defect` for every commit, `@api or @flow` nightly, `@ui or @e2e` before a release.

## 3. Automation environment

### 3.1 Backend settings

Run the monolith (`uvicorn app:app --port 8099`) with:

```dotenv
ENV_PROFILE=dev
MONGO_URI=<cluster or local Mongo used only for testing>
MONGO_DB_NAME=portless_automation
EVENT_BUS_BACKEND=inprocess
AUTH_TRUST_GATEWAY_HEADERS=true
AUTH_PORTLESS_EMAILS=qa-staff@portless-qa.invalid,qa-staff2@portless-qa.invalid
PORTLESS_ACCOUNT_ID=qa-portless-account

# Mail transport stays log-only: leave these blank
GMAIL_SENDER_ADDRESS=
GMAIL_APP_PASSWORD=
IMAP_USER=
IMAP_PASSWORD=

# No paid or external calls: leave these blank
APIFY_TOKEN=
APIFY_TOKEN_AUTODISCOVER=false
LLM_GATEWAY_URL=
KNOWLEDGE_ENABLED=false
HITL_NOTIFY_WEBHOOK_URL=
HITL_TELEGRAM_BOT_TOKEN=
GOOGLE_DRIVE_REFRESH_TOKEN=
# ...and every provider API key (TAZAPAY_*, ESCROW_COM_*, STRIPE_*, PAYONEER_*, SGS_*, QIMA_*, COGOPORT_*, FREIGHTOS_*, ICEGATE_*)

# Signing secrets: set to test-only values so webhooks can be signed
TAZAPAY_WEBHOOK_SECRET=qa-tazapay
ESCROW_COM_WEBHOOK_SECRET=qa-escrowcom
STRIPE_WEBHOOK_SECRET=qa-stripe
PAYONEER_WEBHOOK_SECRET=qa-payoneer
SGS_WEBHOOK_SECRET=qa-sgs
QIMA_WEBHOOK_SECRET=qa-qima
BUREAU_VERITAS_WEBHOOK_SECRET=qa-bv
INTERTEK_WEBHOOK_SECRET=qa-intertek
TUV_SUD_WEBHOOK_SECRET=qa-tuv
COGOPORT_WEBHOOK_SECRET=qa-cogoport
FREIGHTOS_WEBHOOK_SECRET=qa-freightos
SENDGRID_WEBHOOK_PUBLIC_KEY=<test key pair>
MAILGUN_SIGNING_KEY=qa-mailgun

# Background sweeps are triggered by the tests, not by the clock
DEAL_WATCHER_ENABLED=false
DAILY_DIGEST_ENABLED=false
AUTO_FLOW_ENABLED=false
AUTO_DEAL_FROM_OPPORTUNITY_ENABLED=false
FOUR_EYES_THRESHOLD_USD=
```

Personas are sent as gateway headers (`X-User-ID`, `X-User-Email`, `X-User-Name`, `X-Account-ID`), so no real sign-in is involved. Scenarios about real tokens use a stub for the gateway's `GET /auth/me`.

| Persona | Identity |
|---|---|
| `staff` | `qa-staff@portless-qa.invalid`, PortLess account |
| `staff2` | `qa-staff2@portless-qa.invalid`, PortLess account (second approver) |
| `org_user` | Non-staff user of `qa-org-a` |
| `other_org_user` | Non-staff user of `qa-org-b` |
| `other_app` | Valid identity on another application's account |
| `anonymous` | No credentials |

### 3.2 Database and collections

All automation data lives in a dedicated database, `portless_automation`. The app needs no test mode: pointing `MONGO_DB_NAME` at it gives every service its normal collections there.

**Refuse to run** when `MONGO_DB_NAME` is not `portless_automation`, or when any Gmail or provider API credential is set.

Application collections to read for assertions or seed for fixtures:

| Area | Collections |
|---|---|
| Deals, approvals | `deals`, `approvals`, `exceptions`, `autonomy_policy` |
| Sourcing, matching | `rfqs`, `quotes`, `price_observations`, `match_outcomes`, `catalogue`, `opportunities`, `trade_counterparties` |
| Negotiation, mail | `sessions`, `rounds`, `inbound_emails` and the negotiation e-mail history |
| Contracts, documents | `proforma_invoices`, `purchase_orders`, `documents` |
| Logistics | `third_party_orders` |
| Money | `escrow_holds`, `payouts` |
| Compliance | `compliance_checks`, `compliance_hs_policy` |
| Identity | `users`, `organizations` |

New collections owned by the automation framework. The application never reads them.

| Collection | Purpose | Key fields |
|---|---|---|
| `qa_test_cases` | Catalogue loaded from this file | `_id` (`TC-…`), `feature`, `title`, `tags[]`, `priority`, `layer`, `phase`, `source_sha`, `steps[]`, `examples[]` |
| `qa_test_runs` | One run | `_id`, `git_sha_backend`, `git_sha_ui`, `env`, `tag_filter`, `started_at`, `finished_at`, `totals` |
| `qa_test_results` | One result per scenario or iteration | `run_id`, `tc_id`, `iteration`, `status` (`passed`, `failed`, `skipped`, `known_defect`), `duration_ms`, `error`, `artifacts[]`, `created_deal_ids[]` |
| `qa_fixtures` | Versioned fixture definitions and snapshots | `_id` (`FX-…`), `version`, `documents{collection: [...]}` |
| `qa_data_registry` | Every record a run created, for cleanup | `run_id`, `collection`, `doc_id`, `created_at` |

Suggested indexes: `qa_test_results(run_id, tc_id)`, `qa_test_results(tc_id, created_at)`, `qa_data_registry(run_id)`.

Isolation rules:

- Each scenario creates its own deal. Deal IDs and org IDs carry the run ID (`org_id = qa-<run_id>`), so parallel runs do not collide.
- Counterparties use the reserved `.invalid` domain, which cannot receive mail.
- Clean up through `qa_data_registry`, or drop the whole database between full runs.
- The event bus and repositories are process-wide, so run one worker per backend instance unless each worker has its own instance and database.

### 3.3 Webhook signing

Sign the exact raw body bytes. Set the header to the lowercase hex digest of HMAC-SHA256 unless noted.

| Provider | Header | Secret |
|---|---|---|
| Tazapay | `x-tazapay-signature` | `TAZAPAY_WEBHOOK_SECRET` |
| Escrow.com | `x-escrow-signature` | `ESCROW_COM_WEBHOOK_SECRET` |
| Stripe | `stripe-signature: t=<unix>,v1=<hex of t.body>` | `STRIPE_WEBHOOK_SECRET` |
| Payoneer | `x-payoneer-signature` | `PAYONEER_WEBHOOK_SECRET` |
| SGS | `x-sgs-signature` | `SGS_WEBHOOK_SECRET` |
| QIMA | `x-qima-signature` | `QIMA_WEBHOOK_SECRET` |
| Bureau Veritas | `x-bv-signature` | `BUREAU_VERITAS_WEBHOOK_SECRET` |
| Intertek | `x-intertek-signature` | `INTERTEK_WEBHOOK_SECRET` |
| TÜV SÜD | `x-tuvsud-signature` | `TUV_SUD_WEBHOOK_SECRET` |
| Cogoport | `x-cogoport-signature` | `COGOPORT_WEBHOOK_SECRET` |
| Freightos | `x-freightos-signature` | `FREIGHTOS_WEBHOOK_SECRET` |
| Mailgun | form field `signature` = HMAC of `timestamp + token` | `MAILGUN_SIGNING_KEY` |
| SendGrid | `x-twilio-email-event-webhook-signature` and `-timestamp` (ECDSA) | `SENDGRID_WEBHOOK_PUBLIC_KEY` |

These formats are the ones PortLess implements. They still need checking against each provider's sandbox.

### 3.4 Waiting for background work

Handlers run after the HTTP call returns. Poll until the expected state appears, for up to 15 seconds, using the API (deal, approvals, ledger). Never use fixed sleeps. For a `no gate` or `no email` assertion, wait until the deal's timeline has been stable for 2 seconds.

### 3.5 UI locators

The console has no `data-testid` attributes yet (see [DEF-05](#9-open-defects-and-decisions)). Until they exist, use accessible roles and the visible text quoted in the scenarios.

## 4. Step vocabulary

Implement each pattern once. `{D}`, `{PI}`, `{PO}`, `{ESC}`, `{QC}`, `{FRT}`, `{APPR}`, `{CAND}`, `{OFFER}` in paths are resolved from the scenario's fixture.

```text
SETUP
Given I am "<persona>"
Given a deal "<alias>" built from fixture "<FX>"
Given a deal "<alias>" at status "<STATUS>" from fixture "<FX>"
Given the setting "<ENV>" is "<value>"
Given the autonomy level of "<ACTION>" is "<L0|L1|L2|L3>"
Given the connector "<name>" fails with "<error>"
Given the record "<collection>" of deal "<alias>" has "<field>" set to "<value>"
Given "<field>" of the deal was set <n> <hours|days> ago

ACTION
When I call "<METHOD> <path>"
When I call "<METHOD> <path>" with body:
When I call with its approved gate "<METHOD> <path>" with body:
When the provider "<name>" sends "<path>" with signature "<valid|wrong|none>" and body:
When "<persona>" approves the pending "<ACTION>" gate
When "<persona>" rejects the pending "<ACTION>" gate
When "<persona>" approves the pending "<ACTION>" gate signing as "<name>"
When the sweep "<deal_watcher|escrow_watcher|hitl_timeout>" runs

API RESULT
Then the response status is <code>
Then the response field "<path>" is "<value>"
Then the response field "<path>" is present
Then the response detail contains "<text>"

STATE RESULT
Then deal "<alias>" has status "<STATUS>"
Then deal "<alias>" is unchanged
Then a pending gate "<ACTION>" exists at level "<L>"
Then a gate "<ACTION>" exists with status "<STATUS>"
Then no gate "<ACTION>" exists
Then the gate "<ACTION>" has risk flag "<flag>"
Then the gate "<ACTION>" does not have risk flag "<flag>"
Then the gate "<ACTION>" amount is <usd>
Then an e-mail containing "<subject>" is composed for the "<buyer|supplier>"
Then no e-mail containing "<subject>" is composed
Then an exception is recorded with "<field>" equal to "<value>"
Then no exception is recorded
Then the ledger field "<field>" is <value>
Then the escrow status is "<STATUS>"
Then tranche "<T1..T4>" has status "<STATUS>"
Then the compliance hold "<code>" is open
Then the compliance hold "<code>" is not open
Then the collection "<name>" has <n> records for the deal
Then exactly <n> exception(s) is/are recorded with "<field>" equal to "<value>"

SUPPLIER COMMITMENT (supplier "A" is the fixture's supplier)
Given standby supplier "<KEY>" quotes <usd> with score <n> [and no e-mail address]
When the PI leg runs for deal "<alias>"
When supplier "<KEY>" replies "<text>" from its own address
When the supplier order sweep runs <n> hour(s) later
Then the order request is "<requested|confirmed|exhausted>"
Then there is no order request
Then supplier "<KEY>" holds the order request
Then the order request to supplier "<KEY>" is "<requested|declined|no_reply|confirmed>"
Then supplier "<KEY>" is skipped with a reason containing "<text>"
Then an order request is composed for supplier "<KEY>" at its own price
Then no order request is composed for supplier "<KEY>"
Then no order request names the buyer or the buyer's price
Then the deal's supplier is supplier "<KEY>"
Then the gate "<ACTION>" is for supplier "<KEY>"
Then the committed supplier is "<KEY>" at its own price

UI
Given the browser is signed in as "<persona>"
Given the API answers <code> for "<METHOD> <path>"
Given the API is slow for "<METHOD> <path>"
When I open "<route>"
When I click "<name>"
When I fill "<label>" with "<value>"
When I open the tab "<name>"
Then I am on "<route>"
Then I see "<text>"
Then I do not see "<text>"
Then a toast says "<text>"
Then the button "<name>" is disabled
Then the button "<name>" is not shown
```

## 5. Fixtures

A fixture may be built by driving the API from the start or by seeding the database. Use seeding only to reach a phase that the scenario is not testing.

| Fixture | Buyer USD/MT | Supplier USD/MT | Qty MT | PI / escrow | PO | Margin guard |
|---|---:|---:|---:|---:|---:|---:|
| `FX-STD` | 1250 | 1000 | 20 | 25,000 | 20,000 | 20.00% |
| `FX-THIN` | 1100 | 1000 | 20 | 22,000 | 20,000 | 9.09% |
| `FX-BELOW-FLOOR` | 1050 | 1000 | 20 | 21,000 | 20,000 | 4.76% |

Common fixture facts: HS `10063020`, market `AE`, Tazapay escrow (1% fee), QC provider `sgs`, freight provider `cogoport`, buyer and supplier screened `clear` today, addresses on `.invalid`, supplier `SUP-<run>`.

Default tranches: T1 30% on `PO_ISSUED`, T2 40% on `QC_PASSED`, T3 20% on `CLEARED`, T4 10% on `ACCEPTED`.

| Money after each step | FX-STD | FX-THIN |
|---|---:|---:|
| T1 / T2 / T3 / T4 | 7,500 / 10,000 / 5,000 / 2,500 | 6,600 / 8,800 / 4,400 / 2,200 |
| Net received after T1+T2 | 17,325 | 15,246 |
| Net received after T1+T2+T3 | 22,275 | 19,602 |
| Net received after T4 | 24,750 | 21,780 |
| Supplier holdback until acceptance | 2,000 | 2,000 |
| Payout ceiling before acceptance | 18,000 | 18,000 |
| Payout gates (T3 approved after the CLEARED gate) | 17,325 → 675 → 2,000 | 15,246 → 2,754 → 2,000 |
| Fees total | 250 | 220 |
| Final realised margin | 4,750 | 1,780 |

## 6. Backend and flow scenarios

### Phase 0: Cross-cutting rules

```gherkin
@phase:platform @api @security
Feature: Authentication and access on every endpoint
  Every route needs an identity, and only PortLess staff reach anything but
  the organization's own deal list.

  @TC-AUTH-001 @P0 @negative
  Scenario Outline: Anonymous and non-staff callers are refused
    Given a deal "D" at status "ESCROW_FUNDED" from fixture "FX-STD"
    And I am "anonymous"
    When I call "<method> <path>"
    Then the response status is 401
    Given I am "org_user"
    When I call "<method> <path>"
    Then the response status is 403

    Examples: Deals
      | method | path                                                  |
      | GET    | /deals/compliance/hs-policy                           |
      | PUT    | /deals/compliance/hs-policy/10063020                  |
      | POST   | /deals                                                |
      | GET    | /deals/{D}                                            |
      | PATCH  | /deals/{D}/status                                     |
      | PATCH  | /deals/{D}/financials                                 |
      | PATCH  | /deals/{D}/parties/buyer                              |
      | POST   | /deals/{D}/enrich                                     |
      | GET    | /deals/{D}/compliance                                 |
      | POST   | /deals/{D}/compliance/check                           |
      | POST   | /deals/{D}/compliance/holds/seal_mismatch/clear       |
      | GET    | /deals/{D}/timeline                                   |
      | GET    | /deals/{D}/evidence-pack                              |
      | POST   | /deals/{D}/buyer-candidates                           |
      | POST   | /deals/{D}/outreach/start                             |
      | POST   | /deals/{D}/buyer-candidates/{CAND}/skip               |
      | GET    | /deals/{D}/match-board                                |
      | PUT    | /deals/{D}/buyer-candidates/{CAND}/requirement        |
      | POST   | /deals/{D}/supplier-offers                            |
      | POST   | /deals/{D}/match/evaluate                             |
      | POST   | /deals/{D}/match/indicative-offer                     |
      | POST   | /deals/{D}/match/confirm                              |
      | GET    | /deals/{D}/match-scores                               |

    Examples: Sourcing and match engine
      | method | path                             |
      | POST   | /sourcing/rfq                    |
      | GET    | /sourcing/rfqs/RFQ-1             |
      | GET    | /sourcing/deals/{D}/rfqs         |
      | POST   | /sourcing/quotes                 |
      | GET    | /sourcing/quotes/QTE-1           |
      | GET    | /sourcing/deals/{D}/quotes       |
      | POST   | /sourcing/verify/SUP-1           |
      | POST   | /match-engine/score              |
      | GET    | /match-engine/scores             |
      | GET    | /match-engine/scores/{D}         |

    Examples: Negotiation
      | method | path                                        |
      | POST   | /negotiation-agent/negotiations             |
      | GET    | /negotiation-agent/negotiations             |
      | POST   | /negotiation-agent/negotiations/NEG-1/round |
      | GET    | /negotiation-agent/negotiations/NEG-1       |
      | GET    | /negotiation-agent/negotiations/NEG-1/rounds |
      | GET    | /negotiation-agent/negotiations/NEG-1/emails |
      | GET    | /negotiation-agent/email-threads            |

    Examples: DealMaker and DocuGen
      | method | path                            |
      | POST   | /dealmaker/pi                   |
      | POST   | /dealmaker/pi/PI-1/accept       |
      | GET    | /dealmaker/pi/PI-1              |
      | GET    | /dealmaker/pi/PI-1/file         |
      | POST   | /dealmaker/po                   |
      | GET    | /dealmaker/po/PO-1              |
      | GET    | /dealmaker/po/PO-1/file         |
      | GET    | /dealmaker/deals/{D}/documents  |
      | POST   | /docugen/documents              |
      | GET    | /docugen/documents/DOC-1        |
      | GET    | /docugen/documents/DOC-1/file   |
      | GET    | /docugen/deals/{D}/documents    |
      | POST   | /docugen/customs/file           |

    Examples: Escrow Guard
      | method | path                                  |
      | POST   | /escrow-guard/holds                   |
      | GET    | /escrow-guard/holds/ESC-1             |
      | GET    | /escrow-guard/deals/{D}/hold          |
      | GET    | /escrow-guard/providers               |
      | POST   | /escrow-guard/escrows                 |
      | GET    | /escrow-guard/escrows/ESC-1           |
      | POST   | /escrow-guard/escrows/ESC-1/release   |
      | POST   | /escrow-guard/escrows/ESC-1/accept    |
      | POST   | /escrow-guard/escrows/ESC-1/request-acceptance |
      | POST   | /escrow-guard/escrows/ESC-1/dispute   |
      | POST   | /escrow-guard/escrows/ESC-1/open-window |
      | POST   | /escrow-guard/escrows/ESC-1/clear-dispute |
      | POST   | /escrow-guard/escrows/ESC-1/refund    |
      | POST   | /escrow-guard/escrows/ESC-1/request-refund |
      | POST   | /escrow-guard/payouts                 |
      | GET    | /escrow-guard/payouts/PAY-1           |
      | GET    | /escrow-guard/deals/{D}/summary       |
      | GET    | /escrow-guard/deals/{D}/ledger        |
      | GET    | /escrow-guard/deals/{D}/journal       |
      | GET    | /escrow-guard/journals                |
      | GET    | /escrow-guard/ledgers                 |

    Examples: Shield-Link
      | method | path                              |
      | GET    | /shield-link/qc/providers         |
      | POST   | /shield-link/qc/quote             |
      | POST   | /shield-link/qc                   |
      | POST   | /shield-link/freight/quote        |
      | POST   | /shield-link/freight              |
      | POST   | /shield-link/freight/REF-1/milestone |
      | POST   | /shield-link/deals/{D}/loading    |
      | POST   | /shield-link/deals/{D}/track      |
      | POST   | /shield-link/cha                  |
      | GET    | /shield-link/tpo/TPO-1            |
      | GET    | /shield-link/deals/{D}/orders     |

    Examples: Approvals, exceptions, e-mail
      | method | path                                     |
      | GET    | /hitl-approval/learning                  |
      | GET    | /hitl-approval/autonomy-policy           |
      | PUT    | /hitl-approval/autonomy-policy           |
      | POST   | /hitl-approval/approvals                 |
      | GET    | /hitl-approval/approvals                 |
      | GET    | /hitl-approval/approvals/APPR-1          |
      | POST   | /hitl-approval/approvals/APPR-1/resolve  |
      | GET    | /hitl-approval/digest                    |
      | POST   | /hitl-approval/digest/send               |
      | GET    | /hitl-approval/stream                    |
      | POST   | /exception-resolver/exceptions           |
      | GET    | /exception-resolver/exceptions           |
      | GET    | /exception-resolver/exceptions/EXC-1     |
      | POST   | /email-processor/inbound                 |
      | GET    | /email-processor/inbound                 |
      | POST   | /email-processor/inbound/manual          |
      | GET    | /email-processor/inbound/INB-1           |
      | POST   | /email-gateway/send                      |

    Examples: Opportunity intake and identity
      | method | path                                  |
      | GET    | /trade-min/catalogue                  |
      | PUT    | /trade-min/catalogue                  |
      | GET    | /trade-min/sources                    |
      | POST   | /trade-min/scan                       |
      | POST   | /trade-min/knowledge/notes            |
      | GET    | /trade-min/knowledge/search           |
      | GET    | /trade-min/price-book/10063020        |
      | GET    | /trade-min/buyers                     |
      | GET    | /trade-min/suppliers                  |
      | GET    | /trade-min/opportunities              |
      | GET    | /trade-min/opportunities/OPP-1        |
      | GET    | /trade-min/opportunities/OPP-1/match-seed |
      | POST   | /trade-min/opportunities/OPP-1/convert |
      | POST   | /identity/organizations               |
      | GET    | /identity/organizations               |
      | GET    | /identity/users                       |
      | PATCH  | /identity/users/USR-1/organization    |

  @TC-AUTH-002 @P1 @positive
  Scenario Outline: Public health endpoints need no identity
    Given I am "anonymous"
    When I call "GET <path>"
    Then the response status is 200

    Examples:
      | path                       |
      | /health                    |
      | /deals/health              |
      | /sourcing/health           |
      | /match-engine/health       |
      | /negotiation-agent/health  |
      | /dealmaker/health          |
      | /docugen/health            |
      | /escrow-guard/health       |
      | /shield-link/health        |
      | /hitl-approval/health      |
      | /exception-resolver/health |
      | /email-processor/health    |
      | /email-gateway/health      |
      | /trade-min/health          |
      | /identity/health           |

  @TC-AUTH-003 @P1 @security @negative
  Scenario Outline: The deeper health reports need an identity
    Given I am "anonymous"
    When I call "GET <path>"
    Then the response status is 401

    Examples:
      | path               |
      | /health/readiness  |
      | /health/downstream |

  @TC-AUTH-004 @P0 @security @negative
  Scenario: An identity from another application is refused
    Given I am "other_app"
    When I call "GET /deals"
    Then the response status is 403

  @TC-AUTH-005 @P0 @security @negative
  Scenario: A revoked or unknown bearer token is refused
    Given the setting "AUTH_TRUST_GATEWAY_HEADERS" is "false"
    When I call "GET /deals" with bearer token "revoked"
    Then the response status is 401

  @TC-AUTH-006 @P1 @exception
  Scenario: The sign-in service being unreachable is not reported as a bad token
    Given the setting "AUTH_TRUST_GATEWAY_HEADERS" is "false"
    And the connector "auth_gateway" fails with "timeout"
    When I call "GET /deals" with bearer token "valid"
    Then the response status is 503

  @TC-AUTH-007 @P0 @security @negative
  Scenario: Only the SSE stream accepts a token in the query string
    Given the setting "AUTH_TRUST_GATEWAY_HEADERS" is "false"
    When I call "GET /hitl-approval/approvals?token=valid"
    Then the response status is 401

  @TC-AUTH-008 @P0 @security @negative
  Scenario: Forged identity headers are ignored unless the gateway is trusted
    Given the setting "AUTH_TRUST_GATEWAY_HEADERS" is "false"
    When I call "GET /hitl-approval/approvals" with header "X-User-Email" set to "qa-staff@portless-qa.invalid"
    Then the response status is 401

  @TC-AUTH-009 @P0 @security @positive
  Scenario: An organization user lists only deals their organization is party to
    Given a deal "A" at status "MATCHING" from fixture "FX-STD" owned by "qa-org-a"
    And a deal "B" at status "MATCHING" from fixture "FX-STD" owned by "qa-org-b"
    And I am "org_user"
    When I call "GET /deals"
    Then the response status is 200
    And the response contains deal "A"
    And the response does not contain deal "B"

  @TC-AUTH-010 @P1 @positive
  Scenario: An organization user can read their own identity
    Given I am "org_user"
    When I call "GET /identity/me"
    Then the response status is 200
    And the response field "is_portless" is "false"
```

```gherkin
@phase:platform @api @security @money
Feature: Provider webhooks prove themselves with a signature
  Providers have no PortLess identity. A webhook without a token passes only
  when its signature is valid, and an unset secret means refused.

  @TC-HOOK-001 @P0 @positive
  Scenario Outline: A correctly signed webhook needs no token
    Given a deal "D" at status "<status>" from fixture "FX-STD"
    And I am "anonymous"
    When the provider "<provider>" sends "<path>" with signature "valid" and body:
      """
      <body>
      """
    Then the response status is 200

    Examples:
      | status                  | provider | path                                 | body                                                     |
      | ESCROW_PENDING          | tazapay  | /escrow-guard/webhooks/escrow-funded | {"ext_ref":"{ESC_REF}","status":"FUNDED","amount":25000} |
      | SUPPLIER_DISPATCH_READY | sgs      | /shield-link/qc/webhook              | {"ext_ref":"{QC_REF}","result":"FAIL"}                   |
      | IN_TRANSIT              | cogoport | /shield-link/freight/webhook         | {"ext_ref":"{FRT_REF}","milestone":"DEPARTED"}           |

  @TC-HOOK-002 @P0 @negative
  Scenario Outline: A wrong or missing signature moves nothing
    Given a deal "D" at status "<status>" from fixture "FX-STD"
    And I am "anonymous"
    When the provider "<provider>" sends "<path>" with signature "<signature>" and body:
      """
      <body>
      """
    Then the response status is 401
    And deal "D" is unchanged

    Examples:
      | status                  | provider | signature | path                                 | body                                                     |
      | ESCROW_PENDING          | tazapay  | wrong     | /escrow-guard/webhooks/escrow-funded | {"ext_ref":"{ESC_REF}","status":"FUNDED","amount":25000} |
      | ESCROW_PENDING          | tazapay  | none      | /escrow-guard/webhooks/escrow-funded | {"ext_ref":"{ESC_REF}","status":"FUNDED","amount":25000} |
      | SUPPLIER_DISPATCH_READY | sgs      | wrong     | /shield-link/qc/webhook              | {"ext_ref":"{QC_REF}","result":"PASS"}                   |
      | IN_TRANSIT              | cogoport | wrong     | /shield-link/freight/webhook         | {"ext_ref":"{FRT_REF}","milestone":"ARRIVED"}            |
      | CLEARED                 | payoneer | wrong     | /escrow-guard/webhooks/payout        | {"ext_ref":"{PAY_REF}","status":"COMPLETED"}             |

  @TC-HOOK-003 @P0 @negative
  Scenario Outline: An anonymous webhook is refused when its secret is unset
    Given a deal "D" at status "<status>" from fixture "FX-STD"
    And the setting "<secret>" is ""
    And I am "anonymous"
    When the provider "<provider>" sends "<path>" with signature "none" and body:
      """
      <body>
      """
    Then the response status is 401
    And deal "D" is unchanged

    Examples:
      | status                  | provider | secret                  | path                                 | body                                                     |
      | ESCROW_PENDING          | tazapay  | TAZAPAY_WEBHOOK_SECRET  | /escrow-guard/webhooks/escrow-funded | {"ext_ref":"{ESC_REF}","status":"FUNDED","amount":25000} |
      | SUPPLIER_DISPATCH_READY | sgs      | SGS_WEBHOOK_SECRET      | /shield-link/qc/webhook              | {"ext_ref":"{QC_REF}","result":"PASS"}                   |
      | IN_TRANSIT              | cogoport | COGOPORT_WEBHOOK_SECRET | /shield-link/freight/webhook         | {"ext_ref":"{FRT_REF}","milestone":"ARRIVED"}            |
      | CLEARED                 | payoneer | PAYONEER_WEBHOOK_SECRET | /escrow-guard/webhooks/payout        | {"ext_ref":"{PAY_REF}","status":"COMPLETED"}             |

  @TC-HOOK-004 @P1 @positive
  Scenario: A signed-in operator can post a webhook when the secret is unset
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD"
    And the setting "TAZAPAY_WEBHOOK_SECRET" is ""
    And I am "staff"
    When I call "POST /escrow-guard/webhooks/escrow-funded" with body:
      """
      {"ext_ref":"{ESC_REF}","status":"FUNDED","amount":25000}
      """
    Then the response status is 200
    And deal "D" has status "ESCROW_FUNDED"

  @TC-HOOK-005 @P1 @negative
  Scenario: A signed-in non-staff user cannot use a webhook
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD"
    And I am "org_user"
    When I call "POST /escrow-guard/webhooks/escrow-funded" with body:
      """
      {"ext_ref":"{ESC_REF}","status":"FUNDED","amount":25000}
      """
    Then the response status is 403
    And deal "D" is unchanged

  @TC-HOOK-006 @P1 @negative
  Scenario Outline: A malformed webhook body is the sender's error
    Given I am "anonymous"
    When the provider "tazapay" sends "/escrow-guard/webhooks/escrow-funded" with signature "valid" and body:
      """
      <body>
      """
    Then the response status is <code>

    Examples:
      | body             | code |
      | not json         | 400  |
      | {"status":"x"}   | 422  |

  @TC-HOOK-007 @P1 @negative
  Scenario Outline: A webhook for an unknown reference answers 404
    Given I am "staff"
    When I call "POST <path>" with body:
      """
      <body>
      """
    Then the response status is 404

    Examples:
      | path                                 | body                                        |
      | /escrow-guard/webhooks/escrow-funded | {"ext_ref":"NOPE","status":"FUNDED"}        |
      | /escrow-guard/webhooks/payout        | {"ext_ref":"NOPE","status":"COMPLETED"}     |
      | /shield-link/qc/webhook              | {"ext_ref":"NOPE","result":"PASS"}          |
      | /shield-link/freight/webhook         | {"ext_ref":"NOPE","milestone":"ARRIVED"}    |

  @TC-HOOK-008 @P0 @security @negative
  Scenario: A Stripe signature outside the replay window is refused
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD" using escrow provider "stripe"
    And I am "anonymous"
    When the provider "stripe" sends "/escrow-guard/webhooks/escrow-funded" with a valid signature timestamped 10 minutes ago
    Then the response status is 401
    And deal "D" is unchanged

  @TC-HOOK-009 @P0 @negative @email
  Scenario Outline: Inbound e-mail webhooks need a signing key from anonymous callers
    Given the setting "<key>" is ""
    And I am "anonymous"
    When I post the form "<path>" with field "from" set to "a@buyer.invalid"
    Then the response status is 403
    And the collection "inbound_emails" has 0 records for the deal

    Examples:
      | key                          | path                              |
      | SENDGRID_WEBHOOK_PUBLIC_KEY  | /email-processor/inbound/sendgrid |
      | MAILGUN_SIGNING_KEY          | /email-processor/inbound/mailgun  |

  @TC-HOOK-010 @P0 @negative @email
  Scenario Outline: Mailgun signature rules
    Given I am "anonymous"
    When I post a Mailgun message <variant>
    Then the response status is <code>

    Examples:
      | variant                   | code |
      | signed just now           | 200  |
      | with a wrong signature    | 403  |
      | signed 1 hour ago         | 403  |
      | missing the token field   | 400  |
      | with a non-numeric time   | 400  |
```

```gherkin
@phase:platform @api
Feature: Deal state machine
  Topology is enforced for every status pair.

  @TC-FSM-001 @P0 @positive
  Scenario Outline: Operator statuses can be set by hand
    Given a deal "D" at status "<from>" from fixture "FX-STD"
    And the setting "EVENT_CONTINUATION_ENABLED" is "false"
    And I am "staff"
    When I call "PATCH /deals/{D}/status" with body:
      """
      {"target_status":"<to>","reason":"automation"}
      """
    Then the response status is 200
    And deal "D" has status "<to>"

    Examples:
      | from               | to                 |
      | DRAFT              | MATCHING           |
      | MATCHING           | NEGOTIATING        |
      | NEGOTIATING        | NEGOTIATING        |
      | NEGOTIATING        | NEGOTIATION_FAILED |
      | NEGOTIATION_FAILED | MATCHING           |
      | NEGOTIATION_FAILED | CANCELLED          |
      | PI_SENT            | CANCELLED          |
      | PI_ACCEPTED        | CANCELLED          |
      | ESCROW_PENDING     | CANCELLED          |
      | QC_FAILED          | DISPUTE            |

  @TC-FSM-002 @P0 @negative
  Scenario Outline: Forbidden transitions answer 422 and change nothing
    Given a deal "D" at status "<from>" from fixture "FX-STD"
    And I am "staff"
    When I call "PATCH /deals/{D}/status" with body:
      """
      {"target_status":"<to>"}
      """
    Then the response status is 422
    And deal "D" is unchanged

    Examples: Skipping ahead
      | from                    | to                 |
      | DRAFT                   | NEGOTIATING        |
      | DRAFT                   | PAID               |
      | MATCHING                | PI_SENT            |
      | NEGOTIATING             | PI_SENT            |
      | NEGOTIATION_AGREED      | PI_ACCEPTED        |
      | PI_SENT                 | ESCROW_PENDING     |
      | PI_ACCEPTED             | ESCROW_FUNDED      |
      | ESCROW_PENDING          | PO_ISSUED          |
      | ESCROW_FUNDED           | QC_PASSED          |
      | PO_ISSUED               | QC_PASSED          |
      | QC_PASSED               | CLEARED            |
      | QC_PASSED               | PAID               |
      | IN_TRANSIT              | PAID               |
      | QC_FAILED               | IN_TRANSIT         |
      | DISPUTE                 | PAID               |

    Examples: Going backwards or cancelling after money committed
      | from                    | to                 |
      | MATCHING                | DRAFT              |
      | NEGOTIATION_AGREED      | NEGOTIATING        |
      | ESCROW_FUNDED           | ESCROW_PENDING     |
      | ESCROW_FUNDED           | CANCELLED          |
      | PO_ISSUED               | CANCELLED          |
      | IN_TRANSIT              | QC_PASSED          |
      | CLEARED                 | CANCELLED          |

    Examples: Terminal states stay terminal
      | from      | to       |
      | PAID      | CLEARED  |
      | PAID      | DISPUTE  |
      | CANCELLED | MATCHING |
      | REFUNDED  | PAID     |

  @TC-FSM-003 @P1 @negative
  Scenario Outline: An invalid status value is rejected by validation
    Given a deal "D" at status "MATCHING" from fixture "FX-STD"
    And I am "staff"
    When I call "PATCH /deals/{D}/status" with body:
      """
      <body>
      """
    Then the response status is 422

    Examples:
      | body                      |
      | {}                        |
      | {"target_status":"NOPE"}  |

  @TC-FSM-004 @P1 @negative
  Scenario: A status change on an unknown deal answers 404
    Given I am "staff"
    When I call "PATCH /deals/DEAL-NOPE/status" with body:
      """
      {"target_status":"MATCHING"}
      """
    Then the response status is 404

  @TC-FSM-005 @P0 @money @negative
  Scenario Outline: A status owned by a gate or a provider cannot be set by hand
    Given a deal "D" at status "<from>" from fixture "FX-STD"
    And I am "staff"
    When I call "PATCH /deals/{D}/status" with body:
      """
      {"target_status":"<to>"}
      """
    Then the response status is 422
    And the response detail contains "not by hand"
    And deal "D" is unchanged

    Examples:
      | from                    | to                      |
      | NEGOTIATING             | NEGOTIATION_AGREED      |
      | NEGOTIATION_AGREED      | PI_SENT                 |
      | PI_SENT                 | PI_ACCEPTED             |
      | PI_ACCEPTED             | ESCROW_PENDING          |
      | ESCROW_PENDING          | ESCROW_FUNDED           |
      | ESCROW_FUNDED           | PO_ISSUED               |
      | PO_ISSUED               | SUPPLIER_DISPATCH_READY |
      | SUPPLIER_DISPATCH_READY | QC_PASSED               |
      | SUPPLIER_DISPATCH_READY | QC_FAILED               |
      | QC_PASSED               | IN_TRANSIT              |
      | IN_TRANSIT              | CLEARED                 |
      | CLEARED                 | PAID                    |
      | DISPUTE                 | SUPPLIER_DISPATCH_READY |
      | DISPUTE                 | REFUNDED                |

  @TC-FSM-006 @P0 @money @negative
  Scenario: A deal holding buyer money cannot be cancelled by hand
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD" funded with 15000
    And I am "staff"
    When I call "PATCH /deals/{D}/status" with body:
      """
      {"target_status":"CANCELLED"}
      """
    Then the response status is 422
    And the response detail contains "playbook"
    And deal "D" is unchanged
```

```gherkin
@phase:platform @flow
Feature: Duplicate events, replay and the automation switch
  Events can be delivered twice and handlers must be safe to repeat.

  @TC-EVT-001 @P0 @positive @money
  Scenario Outline: A duplicate event creates no second gate or record
    Given a deal "D" at status "<status>" from fixture "FX-STD"
    When the event "<topic>" for the deal is published twice
    Then exactly 1 gate "<gate>" exists

    Examples:
      | status        | topic          | gate         |
      | ESCROW_FUNDED | escrow.funded  | ISSUE_PO     |
      | QC_PASSED     | qc.passed      | BOOK_FREIGHT |
      | IN_TRANSIT    | freight.booked | FILE_CUSTOMS |

  @TC-EVT-002 @P0 @positive
  Scenario: The status echo of an agreement does not issue a PI or log an error
    Given a deal "D" at status "NEGOTIATION_AGREED" from fixture "FX-STD"
    When the event "negotiation.agreed" without a price and without a negotiation id is published
    Then the collection "proforma_invoices" has 0 records for the deal
    And no error is logged for the deal

  @TC-EVT-003 @P1 @negative
  Scenario: A real agreement event without a price is reported and issues no PI
    Given a deal "D" at status "NEGOTIATION_AGREED" from fixture "FX-STD"
    When the event "negotiation.agreed" with negotiation id "NEG-1" and no price is published
    Then the collection "proforma_invoices" has 0 records for the deal
    And an error containing "missing/invalid agreed_price" is logged

  @TC-EVT-004 @P1 @edge
  Scenario Outline: An event for a deal that has moved on is ignored
    Given a deal "D" at status "<status>" from fixture "FX-STD"
    When the event "<topic>" for the deal is published
    Then deal "D" is unchanged
    And no exception is recorded

    Examples:
      | status    | topic              |
      | PAID      | escrow.funded      |
      | CLEARED   | qc.passed          |
      | CANCELLED | negotiation.agreed |

  @TC-EVT-005 @P0 @exception
  Scenario Outline: The automation switch stops every continuation
    Given a deal "D" at status "<status>" from fixture "FX-STD"
    And the setting "EVENT_CONTINUATION_ENABLED" is "false"
    When the event "<topic>" for the deal is published
    Then no gate "<gate>" exists

    Examples:
      | status        | topic         | gate         |
      | ESCROW_FUNDED | escrow.funded | ISSUE_PO     |
      | QC_PASSED     | qc.passed     | BOOK_FREIGHT |

  @TC-EVT-006 @P1 @exception
  Scenario: A failing handler becomes a deal exception
    Given a deal "D" at status "PO_ISSUED" from fixture "FX-STD"
    And the connector "sgs" fails with "HTTP 500"
    When the event "po.issued" for the deal is published
    Then an exception is recorded with "source_event" equal to "PO_ISSUED→QC_BOOKED"
```

### Phase 1: Matching (prospect)

Endpoints: `/deals`, `/deals/{id}/enrich`, `/deals/{id}/parties/{role}`, outreach, match board, `/sourcing/*`, `/match-engine/*`.

```gherkin
@phase:matching @api
Feature: Creating and reading a deal

  Background:
    Given I am "staff"

  @TC-MAT-001 @P0 @positive
  Scenario: Create a deal without an opportunity
    When I call "POST /deals" with body:
      """
      {"operator_id":"ignored","hs_code":"10063020","market":"AE"}
      """
    Then the response status is 201
    And the response field "status" is "DRAFT"
    And the response field "operator_id" is "the caller's user id"

  @TC-MAT-002 @P0 @positive
  Scenario: Create a deal from an opportunity
    Given an opportunity "O" with a scored supplier and buyer candidates
    When I call "POST /deals" with body:
      """
      {"operator_id":"x","hs_code":"10063020","market":"AE","opportunity_id":"{O}"}
      """
    Then the response status is 201
    And the response field "status" is "MATCHING"
    And the response field "buyer_candidates" is present
    And the opportunity "O" is marked converted

  @TC-MAT-003 @P1 @edge
  Scenario: An unknown opportunity leaves the deal in DRAFT
    When I call "POST /deals" with body:
      """
      {"operator_id":"x","hs_code":"10063020","market":"AE","opportunity_id":"OPP-NOPE"}
      """
    Then the response status is 201
    And the response field "status" is "DRAFT"

  @TC-MAT-004 @P1 @negative
  Scenario Outline: Deal creation validates its body
    When I call "POST /deals" with body:
      """
      <body>
      """
    Then the response status is 422

    Examples:
      | body                                                             |
      | {}                                                               |
      | {"operator_id":"x","market":"AE"}                                |
      | {"operator_id":"x","hs_code":"1006"}                             |
      | {"operator_id":"x","hs_code":"1006","market":"AE","autonomy_profile":"L9"} |

  @TC-MAT-005 @P0 @security @positive
  Scenario: The caller's identity overrides operator_id in the body
    When I call "POST /deals" with body:
      """
      {"operator_id":"somebody-else","hs_code":"10063020","market":"AE"}
      """
    Then the response field "operator_id" is "the caller's user id"

  @TC-MAT-006 @P1 @positive
  Scenario Outline: List deals with filters
    Given a deal "D" at status "MATCHING" from fixture "FX-STD"
    When I call "GET /deals<query>"
    Then the response status is <code>

    Examples:
      | query             | code |
      | ?status=MATCHING  | 200  |
      | ?limit=1          | 200  |
      | ?limit=500        | 200  |
      | ?limit=0          | 422  |
      | ?limit=501        | 422  |
      | ?status=NOPE      | 422  |

  @TC-MAT-007 @P1 @negative
  Scenario: An unknown deal answers 404
    When I call "GET /deals/DEAL-NOPE"
    Then the response status is 404

  @TC-MAT-008 @P0 @security @negative
  Scenario: A staff-only sub-resource is not reachable by an organization's user even for their own deal
    Given a deal "A" at status "MATCHING" from fixture "FX-STD" owned by "qa-org-a"
    And I am "org_user"
    When I call "GET /deals/{A}"
    Then the response status is 403

  @TC-MAT-009 @P1 @positive
  Scenario: Update deal financials
    Given a deal "D" at status "NEGOTIATING" from fixture "FX-STD"
    When I call "PATCH /deals/{D}/financials" with body:
      """
      {"buyer_target_price_usd":1300}
      """
    Then the response status is 200
    And the response field "buyer_target_price_usd" is "1300"

  @TC-MAT-010 @P2 @negative
  Scenario: Financial update on an unknown deal answers 404
    When I call "PATCH /deals/DEAL-NOPE/financials" with body:
      """
      {"buyer_target_price_usd":1300}
      """
    Then the response status is 404

  @TC-MAT-011 @P1 @positive
  Scenario: An operator-corrected contact survives a later enrichment
    Given a deal "D" at status "MATCHING" from fixture "FX-STD"
    When I call "PATCH /deals/{D}/parties/buyer" with body:
      """
      {"email":"right@buyer.invalid"}
      """
    Then the response status is 200
    When I call "POST /deals/{D}/enrich"
    Then the buyer party e-mail is "right@buyer.invalid"
    And the buyer party contact source is "operator"

  @TC-MAT-012 @P1 @negative
  Scenario: Updating a party with an unknown role answers 422
    Given a deal "D" at status "MATCHING" from fixture "FX-STD"
    When I call "PATCH /deals/{D}/parties/broker" with body:
      """
      {"email":"a@b.invalid"}
      """
    Then the response status is 422

  @TC-MAT-013 @P1 @exception
  Scenario: Enrichment without actor access finishes without failing the deal
    Given a deal "D" at status "MATCHING" from fixture "FX-STD"
    When I call "POST /deals/{D}/enrich"
    Then the response status is 200
    And deal "D" is unchanged

  @TC-MAT-014 @P0 @compliance @exception @needs:fake-connector
  Scenario: A BLOCK screening verdict raises an exception and forces the RFQ gate to a human
    Given a deal "D" at status "MATCHING" from fixture "FX-STD"
    And the connector "counterparty_check" answers verdict "block"
    When the event "deal.created" for the deal is published
    Then an exception is recorded with "deal_id" equal to "{D}"
    And a pending gate "DISPATCH_RFQ" exists at level "L0"
```

```gherkin
@phase:matching @flow @email
Feature: Buyer outreach funnel
  Identified buyers are introduced to before any price is discussed.

  Background:
    Given I am "staff"
    And a deal "D" at status "MATCHING" from fixture "FX-STD" with 3 buyer candidates

  @TC-OUT-001 @P0 @positive
  Scenario: Outreach contacts the first candidates in parallel
    When I call "POST /deals/{D}/outreach/start"
    Then the response status is 200
    And the response field "contacted" is "2"
    And the response field "waiting_for_buyer" is "true"
    And a gate "SEND_BUYER_OUTREACH" exists with status "AUTO_APPROVED"
    And an e-mail containing "[BUY:" is composed for the "buyer"

  @TC-OUT-002 @P1 @positive
  Scenario: Add a buyer to the funnel
    When I call "POST /deals/{D}/buyer-candidates" with body:
      """
      {"candidate_id":"C9","legal_name":"Added Buyer LLC","country":"AE","email":"a@added.invalid"}
      """
    Then the response status is 201

  @TC-OUT-003 @P1 @negative
  Scenario: A candidate body without required fields answers 422
    When I call "POST /deals/{D}/buyer-candidates" with body:
      """
      {}
      """
    Then the response status is 422

  @TC-OUT-004 @P1 @positive
  Scenario: Skipping a candidate moves the funnel on
    When I call "POST /deals/{D}/buyer-candidates/{CAND}/skip"
    Then the response status is 200

  @TC-OUT-005 @P1 @negative
  Scenario: Skipping an unknown candidate answers 422
    When I call "POST /deals/{D}/buyer-candidates/NOPE/skip"
    Then the response status is 422

  @TC-OUT-006 @P0 @positive
  Scenario: An interested reply from the contacted address selects the buyer
    Given the matching feature is off
    Given outreach was sent to candidate "C1"
    When "C1" replies "We are interested, please send details" from its own address with the "[BUY:" subject token
    Then candidate "C1" has status "selected"
    And the buyer party e-mail is candidate "C1"'s address

  @TC-OUT-007 @P1 @positive
  Scenario: A declined reply moves to the next candidate
    Given outreach was sent to candidate "C1"
    When "C1" replies "Not interested, thanks" from its own address with the "[BUY:" subject token
    Then candidate "C1" has status "declined"
    And the next pending candidate is contacted

  @TC-OUT-008 @P1 @edge
  Scenario: An unclear reply goes to a human
    Given outreach was sent to candidate "C1"
    When "C1" replies "Who is this?" from its own address with the "[BUY:" subject token
    Then a pending gate "PARSE_INBOUND_EMAIL" exists at level "L0"

  @TC-OUT-009 @P0 @security @negative
  Scenario: A valid subject token from a stranger's address is not believed
    Given outreach was sent to candidate "C1"
    When "stranger@other.invalid" replies "We are interested" with the "[BUY:" subject token for "C1"
    Then candidate "C1" does not have status "selected"
    And the buyer party is not set

  @TC-OUT-010 @P1 @positive @needs:time
  Scenario: Silence earns one follow-up and then the next buyer
    Given outreach was sent to candidate "C1" 3 days ago
    When the sweep "deal_watcher" runs
    Then an e-mail containing "[BUY:" is composed for the "buyer"
    Given the follow-up was sent 7 days ago
    When the sweep "deal_watcher" runs
    Then candidate "C1" has status "no_reply"
    And the next pending candidate is contacted

  @TC-OUT-011 @P0 @exception
  Scenario: Every candidate declining raises an exception
    Given every candidate has declined
    When the funnel is evaluated
    Then an exception is recorded with "source_event" equal to "OUTREACH:exhausted"

  @TC-OUT-012 @P1 @exception
  Scenario: A candidate with no address is not contacted
    Given a deal "E" at status "MATCHING" from fixture "FX-STD" with 1 buyer candidate without an e-mail
    When I call "POST /deals/{E}/outreach/start"
    Then the response field "contacted" is "0"

  @TC-OUT-013 @P1 @edge
  Scenario: Without a mailbox the candidate stays pending and shows the send error
    Given the mail transport is log-only
    When I call "POST /deals/{D}/outreach/start"
    Then candidate "C1" has status "pending"
    And candidate "C1" has a send error

  @TC-OUT-014 @P0 @compliance @negative
  Scenario: An embargoed destination stops outreach
    Given a deal "E" built from fixture "FX-STD" with market "IR"
    When the event "deal.created" for the deal is published
    Then the compliance hold "destination_embargoed" is open
    And no e-mail containing "[BUY:" is composed
```

```gherkin
@phase:matching @api @flow
Feature: RFQ, supplier quotes and verification

  Background:
    Given I am "staff"
    And a deal "D" at status "MATCHING" from fixture "FX-STD"

  @TC-RFQ-001 @P0 @positive
  Scenario: Dispatch an RFQ
    When I call "POST /sourcing/rfq" with body:
      """
      {"deal_id":"{D}","hs_code":"10063020","suppliers":[{"supplier_id":"S1","name":"Mill","email":"s1@mill.invalid"}],"quantity_mt":20}
      """
    Then the response status is 201
    And the response field "rfq_id" is present
    And an e-mail containing "[RFQ:" is composed for the "supplier"

  @TC-RFQ-002 @P1 @negative
  Scenario Outline: RFQ validation
    When I call "POST /sourcing/rfq" with body:
      """
      <body>
      """
    Then the response status is 422

    Examples:
      | body                                                                 |
      | {}                                                                   |
      | {"deal_id":"{D}","hs_code":"1006"}                                   |
      | {"deal_id":"{D}","hs_code":"1006","suppliers":[],"validity_days":0}  |
      | {"deal_id":"{D}","hs_code":"1006","suppliers":[],"validity_days":61} |

  @TC-RFQ-003 @P1 @negative
  Scenario Outline: Unknown RFQ and quote lookups
    When I call "GET <path>"
    Then the response status is 404

    Examples:
      | path                  |
      | /sourcing/rfqs/NOPE   |
      | /sourcing/quotes/NOPE |

  @TC-RFQ-004 @P1 @positive
  Scenario Outline: Deal-level RFQ and quote lists
    When I call "GET <path>"
    Then the response status is 200

    Examples:
      | path                       |
      | /sourcing/deals/{D}/rfqs   |
      | /sourcing/deals/{D}/quotes |

  @TC-RFQ-005 @P0 @positive
  Scenario: A quote is verified and scored
    Given an RFQ "R" was dispatched to supplier "S1"
    When I call "POST /sourcing/quotes" with body:
      """
      {"rfq_id":"{R}","deal_id":"{D}","supplier_id":"S1","unit_price_usd":1000,"gstin":"00AAAAA0000A0Z0","iec":"0000000000"}
      """
    Then the response status is 201
    And the response field "quote_id" is present

  @TC-RFQ-006 @P1 @negative
  Scenario Outline: Quote validation
    When I call "POST /sourcing/quotes" with body:
      """
      <body>
      """
    Then the response status is 422

    Examples:
      | body                                                                             |
      | {}                                                                               |
      | {"deal_id":"{D}","supplier_id":"S1","unit_price_usd":0}                          |
      | {"deal_id":"{D}","supplier_id":"S1","unit_price_usd":-1}                         |
      | {"deal_id":"{D}","supplier_id":"S1","unit_price_usd":10,"validity_days":0}       |
      | {"deal_id":"{D}","supplier_id":"S1","unit_price_usd":10,"validity_days":91}      |

  @TC-RFQ-007 @P0 @compliance @positive
  Scenario: A quote that failed GST or IEC verification needs a human
    Given the matching feature is off
    And the verification connector answers "gst_verified=false"
    When a quote from supplier "S1" is received
    Then a pending gate "CONFIRM_SUPPLIER_SELECTION" exists at level "L0"
    And deal "D" has status "MATCHING"

  @TC-RFQ-008 @P1 @positive
  Scenario: A verified supplier inside the price corridor is selected automatically
    Given the matching feature is off
    And the setting "SOURCING_AUTO_SOURCE_VERIFIED" is "true"
    When a verified quote inside the buyer's corridor is received
    Then a gate "CONFIRM_SUPPLIER_SELECTION" exists with status "AUTO_APPROVED"
    And the gate "CONFIRM_SUPPLIER_SELECTION" has risk flag "auto_sourced_verified_in_corridor"

  @TC-RFQ-009 @P0 @email @positive
  Scenario: An e-mailed quote from the dispatched address enters the pipeline
    Given an RFQ "R" was dispatched to "s1@mill.invalid"
    When "s1@mill.invalid" replies with a price of "1000" and the "[RFQ:" subject token
    Then the collection "quotes" has 1 records for the deal

  @TC-RFQ-010 @P0 @email @security @negative
  Scenario: An RFQ reply from another address is not recorded as that supplier's quote
    Given an RFQ "R" was dispatched to "s1.mill@gmail.com"
    When "other.mill@gmail.com" replies with a price of "900" and the "[RFQ:" subject token
    Then the collection "quotes" has 0 records for the deal
    And a pending gate "PARSE_INBOUND_EMAIL" exists at level "L0"

  @TC-RFQ-011 @P1 @email @edge
  Scenario: An RFQ reply with no readable price goes to a human
    Given an RFQ "R" was dispatched to "s1@mill.invalid"
    When "s1@mill.invalid" replies "We can supply, call us" with the "[RFQ:" subject token
    Then a pending gate "PARSE_INBOUND_EMAIL" exists at level "L0"

  @TC-RFQ-012 @P1 @positive
  Scenario: Verify a supplier directly
    When I call "POST /sourcing/verify/S1" with body:
      """
      {"gstin":"00AAAAA0000A0Z0","iec":"0000000000"}
      """
    Then the response status is 200

  @TC-RFQ-013 @P1 @exception
  Scenario: A rejected supplier-selection gate leaves the prospect open
    Given a pending gate "CONFIRM_SUPPLIER_SELECTION" exists for the deal
    When "staff" rejects the pending "CONFIRM_SUPPLIER_SELECTION" gate
    Then no exception is recorded
    And deal "D" has status "MATCHING"
```

```gherkin
@phase:matching @api
Feature: Match engine scoring

  Background:
    Given I am "staff"

  @TC-CMS-001 @P1 @positive
  Scenario: Score a supplier
    When I call "POST /match-engine/score" with body:
      """
      {"deal_id":"D1","supplier_id":"S1","buyer_target_price":1250,"supplier_quote_price":1000,"currency":"USD","verification":{"gst_verified":true,"iec_active":true,"dgft_denied":false},"supplier_to_port_km":300}
      """
    Then the response status is 200
    And the response field "decision" is present

  @TC-CMS-002 @P0 @negative
  Scenario: A DGFT-denied supplier is never sourced
    When I call "POST /match-engine/score" with body:
      """
      {"deal_id":"D1","supplier_id":"S1","buyer_target_price":1250,"supplier_quote_price":1000,"currency":"USD","verification":{"gst_verified":true,"iec_active":true,"dgft_denied":true},"supplier_to_port_km":300}
      """
    Then the response status is 200
    And the response field "decision" is not "SOURCE"

  @TC-CMS-003 @P1 @negative
  Scenario Outline: Scoring validation
    When I call "POST /match-engine/score" with body:
      """
      <body>
      """
    Then the response status is 422

    Examples:
      | body |
      | {}   |
      | {"deal_id":"D1","supplier_id":"S1","buyer_target_price":0,"supplier_quote_price":10,"currency":"USD","supplier_to_port_km":1} |
      | {"deal_id":"D1","supplier_id":"S1","buyer_target_price":10,"supplier_quote_price":-1,"currency":"USD","supplier_to_port_km":1} |
      | {"deal_id":"D1","supplier_id":"S1","buyer_target_price":10,"supplier_quote_price":1,"currency":"USD","supplier_to_port_km":-1} |

  @TC-CMS-004 @P2 @positive
  Scenario: List scores
    When I call "GET /match-engine/scores"
    Then the response status is 200

  @TC-CMS-005 @P2 @negative
  Scenario: A deal with no score answers 404
    When I call "GET /match-engine/scores/DEAL-NOPE"
    Then the response status is 404

  @TC-CMS-006 @P2 @positive
  Scenario: A deal's match scores are listed
    Given a deal "D" at status "MATCHING" from fixture "FX-STD"
    When I call "GET /deals/{D}/match-scores"
    Then the response status is 200
```

```gherkin
@phase:matching @api @flow
Feature: Match board, indicative offers and confirming a match

  Background:
    Given I am "staff"
    And a deal "D" at status "MATCHING" from fixture "FX-STD" with buyer "C1" and supplier offer "OF1"

  @TC-BRD-001 @P0 @positive
  Scenario: The board scores every buyer and supplier pair
    When I call "GET /deals/{D}/match-board"
    Then the response status is 200
    And the response field "pairs" is present

  @TC-BRD-002 @P1 @positive
  Scenario: Setting a buyer's requirement re-scores the board and feeds the price book
    When I call "PUT /deals/{D}/buyer-candidates/C1/requirement" with body:
      """
      {"grade":"1121","quantity_mt":20,"target_price_usd_per_mt":1250,"target_price_incoterm":"CIF"}
      """
    Then the response status is 200
    And the collection "price_observations" has 1 records for the deal

  @TC-BRD-003 @P1 @negative
  Scenario: A requirement for an unknown buyer answers 422
    When I call "PUT /deals/{D}/buyer-candidates/NOPE/requirement" with body:
      """
      {"grade":"1121"}
      """
    Then the response status is 422

  @TC-BRD-004 @P1 @positive
  Scenario: An operator adds a supplier offer
    When I call "POST /deals/{D}/supplier-offers" with body:
      """
      {"offer_id":"OF2","supplier_id":"mill.invalid","fob_price_usd_per_mt":990}
      """
    Then the response status is 201

  @TC-BRD-005 @P1 @negative
  Scenario: A supplier offer without ids answers 422
    When I call "POST /deals/{D}/supplier-offers" with body:
      """
      {"fob_price_usd_per_mt":990}
      """
    Then the response status is 422

  @TC-BRD-006 @P0 @positive
  Scenario: Evaluate opens a confirm gate for a firm pair
    Given pair "C1" × "OF1" has a chance of closing above the threshold
    When I call "POST /deals/{D}/match/evaluate"
    Then the response status is 200
    And a pending gate "CONFIRM_MATCH" exists at level "L0"

  @TC-BRD-007 @P1 @positive
  Scenario: Evaluate opens an indicative offer when the buyer has no price
    Given buyer "C1" is interested but has stated no price
    When I call "POST /deals/{D}/match/evaluate"
    Then a pending gate "SEND_INDICATIVE_OFFER" exists at level "L0"

  @TC-BRD-008 @P1 @edge
  Scenario: Evaluate with nothing to propose opens no gate
    Given a deal "E" at status "MATCHING" from fixture "FX-STD" with no buyer and no offer
    When I call "POST /deals/{E}/match/evaluate"
    Then the response status is 200
    And no gate "CONFIRM_MATCH" exists

  @TC-BRD-009 @P0 @positive
  Scenario: Approving a match sets the parties and opens the negotiation
    Given a pending gate "CONFIRM_MATCH" exists for pair "C1" × "OF1"
    When "staff" approves the pending "CONFIRM_MATCH" gate
    Then deal "D" has status "NEGOTIATING"
    And the buyer party is candidate "C1"
    And the supplier party is offer "OF1"

  @TC-BRD-010 @P1 @positive
  Scenario: Rejecting a match keeps the prospect open
    Given a pending gate "CONFIRM_MATCH" exists for pair "C1" × "OF1"
    When "staff" rejects the pending "CONFIRM_MATCH" gate
    Then deal "D" has status "MATCHING"

  @TC-BRD-011 @P1 @negative
  Scenario Outline: A match or offer for a pair not on the board answers 404
    When I call "POST /deals/{D}/match/<action>" with body:
      """
      {"candidate_id":"NOPE","offer_id":"NOPE"}
      """
    Then the response status is 404

    Examples:
      | action           |
      | confirm          |
      | indicative-offer |

  @TC-BRD-012 @P0 @negative
  Scenario Outline: A pair with a hard blocker cannot be proposed
    Given pair "C1" × "OF1" has the condition "<condition>"
    When I call "POST /deals/{D}/match/confirm" with body:
      """
      {"candidate_id":"C1","offer_id":"OF1"}
      """
    Then the response status is 422
    And the response detail contains "<detail>"
    And no gate "CONFIRM_MATCH" exists

    Examples:
      | condition                                | detail            |
      | supplier failed GST verification         | GST / IEC         |
      | buyer quantity below the supplier's MOQ  | minimum           |
      | supplier capacity cannot meet the window | cannot deliver    |
      | conflicting grades                       | grade mismatch    |

  @TC-BRD-013 @P1 @negative
  Scenario: A match without both prices answers 422
    Given pair "C1" × "OF1" has no buyer price
    When I call "POST /deals/{D}/match/confirm" with body:
      """
      {"candidate_id":"C1","offer_id":"OF1"}
      """
    Then the response status is 422
    And the response detail contains "both prices"

  @TC-BRD-014 @P1 @negative
  Scenario: An indicative offer needs a real supplier quote
    Given offer "OF1" has only an estimated price
    When I call "POST /deals/{D}/match/indicative-offer" with body:
      """
      {"candidate_id":"C1","offer_id":"OF1"}
      """
    Then the response status is 422
    And the response detail contains "quoted price"

  @TC-BRD-015 @P1 @negative
  Scenario: An indicative offer needs the buyer's address
    Given candidate "C1" has no e-mail address
    And offer "OF1" has a quoted price
    When I call "POST /deals/{D}/match/indicative-offer" with body:
      """
      {"candidate_id":"C1","offer_id":"OF1"}
      """
    Then the response status is 422
    And the response detail contains "no e-mail"

  @TC-BRD-016 @P1 @email @positive
  Scenario Outline: The buyer's reaction to an indicative offer is recorded
    Given an indicative offer was sent to "C1"
    When "C1" replies "<reply>" from its own address
    Then the indicative offer has outcome "<outcome>"

    Examples:
      | reply                       | outcome   |
      | We accept this price range  | accepted  |
      | We can pay 1200 per MT      | countered |
      | Too expensive, no thanks    | declined  |

  @TC-BRD-017 @P2 @positive
  Scenario: Price book
    When I call "GET /trade-min/price-book/10063020"
    Then the response status is 200
```

### Phase 2: Negotiation

```gherkin
@phase:negotiation @api @flow
Feature: Negotiation sessions and rounds
  The code decides the price and only an operator can confirm an agreement.

  Background:
    Given I am "staff"
    And a deal "D" at status "NEGOTIATING" from fixture "FX-STD"

  @TC-NEG-001 @P0 @positive
  Scenario: Start a negotiation
    When I call "POST /negotiation-agent/negotiations" with body:
      """
      {"deal_id":"{D}","trade_match_id":"TM1","buyer_id":"B1","seller_id":"S1","product_code":"10063020","buyer_country_code":"AE","buyer_target_price":1250,"supplier_quote_price":1000}
      """
    Then the response status is 201
    And the response field "negotiation_id" is present

  @TC-NEG-002 @P1 @negative
  Scenario Outline: Start validation
    When I call "POST /negotiation-agent/negotiations" with body:
      """
      <body>
      """
    Then the response status is 422

    Examples:
      | body |
      | {}   |
      | {"deal_id":"{D}","trade_match_id":"T","buyer_id":"B","seller_id":"S","product_code":"1","buyer_country_code":"AE","buyer_target_price":0,"supplier_quote_price":1} |
      | {"deal_id":"{D}","trade_match_id":"T","buyer_id":"B","seller_id":"S","product_code":"1","buyer_country_code":"AE","buyer_target_price":1,"supplier_quote_price":-1} |

  @TC-NEG-003 @P0 @positive
  Scenario: A round records the counter-price
    Given a negotiation "N" in status "ROUND"
    When I call "POST /negotiation-agent/negotiations/{N}/round" with body:
      """
      {"counter_price":1200,"party":"buyer"}
      """
    Then the response status is 200

  @TC-NEG-004 @P1 @negative
  Scenario Outline: Round validation
    Given a negotiation "N" in status "ROUND"
    When I call "POST /negotiation-agent/negotiations/{N}/round" with body:
      """
      <body>
      """
    Then the response status is 422

    Examples:
      | body                 |
      | {}                   |
      | {"counter_price":0}  |
      | {"counter_price":-5} |

  @TC-NEG-005 @P1 @negative
  Scenario: A round on an unknown negotiation answers 404
    When I call "POST /negotiation-agent/negotiations/NOPE/round" with body:
      """
      {"counter_price":1200,"party":"buyer"}
      """
    Then the response status is 404

  @TC-NEG-006 @P0 @negative
  Scenario Outline: A round on a finished negotiation answers 409
    Given a negotiation "N" in status "<status>"
    When I call "POST /negotiation-agent/negotiations/{N}/round" with body:
      """
      {"counter_price":1200,"party":"buyer"}
      """
    Then the response status is 409

    Examples:
      | status    |
      | AGREED    |
      | FAILED    |
      | ESCALATED |

  @TC-NEG-007 @P1 @positive
  Scenario Outline: Read negotiations
    Given a negotiation "N" in status "ROUND"
    When I call "GET <path>"
    Then the response status is 200

    Examples:
      | path                                   |
      | /negotiation-agent/negotiations        |
      | /negotiation-agent/negotiations/{N}    |
      | /negotiation-agent/negotiations/{N}/rounds |
      | /negotiation-agent/negotiations/{N}/emails |
      | /negotiation-agent/email-threads       |

  @TC-NEG-008 @P2 @negative
  Scenario: An unknown negotiation answers 404
    When I call "GET /negotiation-agent/negotiations/NOPE"
    Then the response status is 404

  @TC-NEG-009 @P0 @exception
  Scenario: With the LLM down the draft falls back to a template and stays with a human
    Given a negotiation "N" in status "STARTED"
    And the LLM gateway is not configured
    When a round is drafted
    Then a pending gate "SEND_NEGOTIATION_EMAIL" exists at level "L0"

  @TC-NEG-010 @P0 @positive
  Scenario: Detected acceptance waits for an operator
    Given a negotiation "N" in status "ROUND"
    When the buyer accepts the current price by e-mail
    Then a pending gate "CONFIRM_NEGOTIATION_AGREEMENT" exists at level "L0"
    And the negotiation "N" has status "HITL_PENDING"
    And deal "D" has status "NEGOTIATING"

  @TC-NEG-011 @P0 @positive
  Scenario: Approving the agreement closes the negotiation
    Given a pending gate "CONFIRM_NEGOTIATION_AGREEMENT" exists for the deal
    When "staff" approves the pending "CONFIRM_NEGOTIATION_AGREEMENT" gate
    Then deal "D" has status "NEGOTIATION_AGREED"

  @TC-NEG-012 @P1 @positive
  Scenario: Rejecting the agreement reopens the negotiation without an exception
    Given a pending gate "CONFIRM_NEGOTIATION_AGREEMENT" exists for the deal
    When "staff" rejects the pending "CONFIRM_NEGOTIATION_AGREEMENT" gate
    Then the negotiation has status "ROUND"
    And no exception is recorded

  @TC-NEG-013 @P0 @money @negative
  Scenario: An out-of-band model price is corrected and held for a human
    Given a negotiation "N" in status "ROUND"
    And the LLM proposes a price below the margin floor
    When a round is drafted
    Then the drafted e-mail shows the corrected price
    And the gate "SEND_NEGOTIATION_EMAIL" has risk flag "price_out_of_band"
    And a pending gate "SEND_NEGOTIATION_EMAIL" exists at level "L0"

  @TC-NEG-014 @P0 @email @security @negative
  Scenario Outline: A draft that leaks the other side is held for a human
    Given a negotiation "N" in status "ROUND"
    And the draft to the buyer contains <leak>
    When a round is drafted
    Then the gate "SEND_NEGOTIATION_EMAIL" has risk flag "counterparty_leak"
    And a pending gate "SEND_NEGOTIATION_EMAIL" exists at level "L0"

    Examples:
      | leak                      |
      | the supplier's name       |
      | the supplier's address    |
      | the supplier's domain     |
      | the supplier's price      |

  @TC-NEG-015 @P0 @email @exception
  Scenario Outline: The send-time safety check blocks an approved e-mail
    Given an approved e-mail draft where <condition>
    When the e-mail is sent
    Then the e-mail has status "blocked"
    And the e-mail has a block reason
    And an exception is recorded with "deal_id" equal to "{D}"

    Examples:
      | condition                                     |
      | the recipient is the other party              |
      | the recipient is not a current contact        |
      | the buyer and supplier addresses are the same |
      | the party replied after the draft was written |
      | the session has failed                        |

  @TC-NEG-016 @P1 @email @edge
  Scenario: Approving an already sent e-mail sends nothing more
    Given an e-mail that was already sent
    When the send is repeated
    Then no second e-mail is composed

  @TC-NEG-017 @P0 @email @exception
  Scenario: A failed negotiation offers the next candidate
    Given a negotiation "N" that fails after the maximum rounds
    Then deal "D" has status "NEGOTIATION_FAILED"
    And an exception is recorded with "deal_id" equal to "{D}"
    And a pending gate "CONFIRM_SUPPLIER_SELECTION" exists at level "L0"

  @TC-NEG-018 @P1 @exception @needs:time
  Scenario: A stale negotiation does not remain open forever
    Given a negotiation "N" in status "ROUND" idle past its limit
    When the sweep "negotiation_stale" runs
    Then the negotiation "N" has status "FAILED"

  @TC-NEG-019 @P0 @email @positive
  Scenario: An acceptance with no price accepts our last offer, for a person to confirm
    Given a deal "D" at status "NEGOTIATING" from fixture "FX-STD" with an offer of 1294.09 sent to the buyer
    When the buyer replies "We accept your offer and wish to proceed." from the known address
    Then a pending gate "CONFIRM_NEGOTIATION_AGREEMENT" exists at level "L0"
    And the negotiation has status "HITL_PENDING"

  @TC-NEG-020 @P0 @email @negative
  Scenario: A reply with no price that is not an acceptance goes to a person
    Given a deal "D" at status "NEGOTIATING" from fixture "FX-STD" with an offer of 1294.09 sent to the buyer
    When the buyer replies "Could you tell us more about the packaging?" from the known address
    Then a pending gate "PARSE_INBOUND_EMAIL" exists at level "L0"

  @TC-NEG-021 @P0 @positive
  Scenario: A draft saying escrow money is already there waits for a person
    Given a negotiation draft saying "The buyer's funds are already deposited in escrow."
    Then the gate "SEND_NEGOTIATION_EMAIL" has risk flag "premature_funds_claim"
    And a pending gate "SEND_NEGOTIATION_EMAIL" exists at level "L0"
```

```gherkin
@phase:negotiation @api @flow @email
Feature: Inbound e-mail is mapped to the right counterparty

  Background:
    Given I am "staff"
    And a deal "D" at status "NEGOTIATING" from fixture "FX-STD"
    And a negotiation "N" in status "ROUND"

  @TC-INB-001 @P0 @positive
  Scenario: A reply from the known address advances the negotiation
    When I call "POST /email-processor/inbound" with body:
      """
      {"email":{"from_address":"buyer@buyer.invalid","to_address":"trade@portless-qa.invalid","subject":"Re: offer [NEG:{N}]","body_text":"We can do 1200 per MT","source":"imap","received_at":"2026-11-09T10:00:00Z"}}
      """
    Then the response status is 201
    And the inbound record has sender check "verified"

  @TC-INB-002 @P0 @security @negative
  Scenario Outline: An unverified sender does not advance the negotiation
    When an e-mail arrives from "<sender>" with subject "Re: offer [NEG:{N}]"
    Then the inbound record has status "NEEDS_OPERATOR"
    And a pending gate "PARSE_INBOUND_EMAIL" exists at level "L0"
    And the gate "PARSE_INBOUND_EMAIL" has risk flag "sender_not_verified"
    And the e-mail is not in the agent's history

    Examples:
      | sender                     |
      | stranger@elsewhere.invalid |

  @TC-INB-003 @P1 @edge
  Scenario: The same company domain is accepted for a business domain
    When an e-mail arrives from "colleague@buyer.invalid" with subject "Re: offer [NEG:{N}]"
    Then the inbound record has sender check "verified"

  @TC-INB-004 @P0 @security @negative
  Scenario: A colleague on a free-mail domain is not assumed to be the buyer
    Given the buyer's address is on a free-mail domain
    When an e-mail arrives from another address on that domain with subject "Re: offer [NEG:{N}]"
    Then the inbound record does not have sender check "verified"

  @TC-INB-005 @P1 @edge
  Scenario: A duplicate message is processed once
    When the same message id arrives twice
    Then the collection "inbound_emails" has 1 records for the deal

  @TC-INB-006 @P1 @positive
  Scenario: An operator submits a reply manually
    When I call "POST /email-processor/inbound/manual" with body:
      """
      {"from_address":"buyer@buyer.invalid","subject":"phone call","body_text":"1200","negotiation_id":"{N}","party":"buyer","counter_price":1200}
      """
    Then the response status is 201

  @TC-INB-007 @P1 @negative
  Scenario Outline: Manual submission validation
    When I call "POST /email-processor/inbound/manual" with body:
      """
      <body>
      """
    Then the response status is 422

    Examples:
      | body                                       |
      | {}                                         |
      | {"from_address":"a@b.invalid"}             |
      | {"subject":"x","body_text":"y"}            |

  @TC-INB-008 @P2 @positive
  Scenario: List inbound e-mail
    When I call "GET /email-processor/inbound"
    Then the response status is 200

  @TC-INB-009 @P2 @negative
  Scenario: Unknown inbound record
    When I call "GET /email-processor/inbound/NOPE"
    Then the response status is 404

  @TC-INB-010 @P1 @positive
  Scenario: Without a mailbox the gateway logs the message and reports it
    Given the mail transport is log-only
    When I call "POST /email-gateway/send" with body:
      """
      {"to_address":"a@b.invalid","subject":"s","body":"b"}
      """
    Then the response status is 200
    And the response field "outcome" is "LOGGED"

  @TC-INB-011 @P1 @negative
  Scenario: The send endpoint validates its body
    When I call "POST /email-gateway/send" with body:
      """
      {"subject":"s"}
      """
    Then the response status is 422

  @TC-INB-012 @P1 @exception @needs:fake-connector
  Scenario: A configured SMTP server that fails answers 502
    Given the connector "smtp" fails with "connection refused"
    When I call "POST /email-gateway/send" with body:
      """
      {"to_address":"a@b.invalid","subject":"s","body":"b"}
      """
    Then the response status is 502
```

### Phase 3: Trade compliance

```gherkin
@phase:compliance @flow @compliance
Feature: Trade compliance before the Proforma Invoice

  Background:
    Given I am "staff"

  @TC-CMP-001 @P0 @positive
  Scenario: A clean, confirmed trade goes straight to the PI
    Given a deal "D" at status "NEGOTIATING" from fixture "FX-STD"
    And HS code "10063020" is confirmed "free"
    When the negotiation is finalized at "1250"
    Then deal "D" has status "PI_SENT"
    And no gate "CONFIRM_TRADE_COMPLIANCE" exists

  @TC-CMP-002 @P0 @positive
  Scenario: A restricted HS code asks a human
    Given a deal "D" at status "NEGOTIATING" from fixture "FX-STD"
    When the negotiation is finalized at "1250"
    Then a pending gate "CONFIRM_TRADE_COMPLIANCE" exists at level "L0"
    And the gate "CONFIRM_TRADE_COMPLIANCE" has risk flag "compliance:hs_restricted"
    And deal "D" has status "NEGOTIATION_AGREED"
    And the collection "proforma_invoices" has 0 records for the deal

  @TC-CMP-003 @P0 @positive
  Scenario: Approving the review issues the PI
    Given a deal "D" at status "NEGOTIATION_AGREED" from fixture "FX-STD" with a pending compliance review
    When "staff" approves the pending "CONFIRM_TRADE_COMPLIANCE" gate
    Then deal "D" has status "PI_SENT"

  @TC-CMP-004 @P0 @negative
  Scenario: Rejecting the review withholds the PI and raises an exception
    Given a deal "D" at status "NEGOTIATION_AGREED" from fixture "FX-STD" with a pending compliance review
    When "staff" rejects the pending "CONFIRM_TRADE_COMPLIANCE" gate
    Then deal "D" has status "NEGOTIATION_AGREED"
    And an exception is recorded with "deal_id" equal to "{D}"

  @TC-CMP-005 @P0 @negative
  Scenario Outline: A block withholds the PI
    Given a deal "D" at status "NEGOTIATING" from fixture "FX-STD" with <condition>
    When the negotiation is finalized at "1250"
    Then deal "D" has status "NEGOTIATION_AGREED"
    And the collection "proforma_invoices" has 0 records for the deal
    And an exception is recorded with "source_event" equal to "NEGOTIATION_AGREED→COMPLIANCE_BLOCK"

    Examples:
      | condition                     |
      | market "KP"                   |
      | market "IR"                   |
      | market "SY"                   |
      | market "CU"                   |
      | a DGFT-denied supplier        |
      | a prohibited HS code          |

  @TC-CMP-006 @P0 @positive
  Scenario Outline: A review condition asks a human before the PI
    Given a deal "D" at status "NEGOTIATING" from fixture "FX-STD" with <condition>
    And HS code "10063020" is confirmed "free"
    When the negotiation is finalized at "1250"
    Then a pending gate "CONFIRM_TRADE_COMPLIANCE" exists at level "L0"
    And the gate "CONFIRM_TRADE_COMPLIANCE" has risk flag "compliance:<flag>"

    Examples:
      | condition                                     | flag                  |
      | market "PK"                                   | destination_high_risk |
      | buyer and supplier sharing a domain           | related_parties       |
      | buyer and supplier sharing a phone            | related_parties       |
      | a supplier company younger than 1 year        | supplier_new_company  |
      | a buyer/supplier price ratio above 1.8        | spread_too_wide       |

  @TC-CMP-007 @P0 @positive
  Scenario Outline: The screening verdict decides what happens at agreement
    Given a deal "D" at status "NEGOTIATING" from fixture "FX-STD" with a fresh buyer screening of "<verdict>"
    And HS code "10063020" is confirmed "free"
    When the negotiation is finalized at "1250"
    Then deal "D" has status "<status>"
    And the gate check "<gate>" holds

    Examples:
      | verdict | status             | gate                                |
      | clear   | PI_SENT            | none                                |
      | review  | NEGOTIATION_AGREED | pending CONFIRM_COUNTERPARTY_RISK   |
      | block   | NEGOTIATION_AGREED | none                                |

  @TC-CMP-008 @P0 @positive
  Scenario: Approving the counterparty risk issues the PI
    Given a pending gate "CONFIRM_COUNTERPARTY_RISK" exists for a confirmed-HS deal
    When "staff" approves the pending "CONFIRM_COUNTERPARTY_RISK" gate
    Then the deal reaches "PI_SENT"

  @TC-CMP-009 @P1 @positive @needs:time
  Scenario: A screening older than 30 days is repeated
    Given a deal "D" at status "NEGOTIATING" from fixture "FX-STD"
    And "sanctions_checked_at" of the deal was set 31 days ago
    When the negotiation is finalized at "1250"
    Then the buyer is screened again

  @TC-CMP-010 @P1 @positive
  Scenario: Confirm an HS code's export policy
    Given I am "staff"
    When I call "PUT /deals/compliance/hs-policy/10063020" with body:
      """
      {"status":"free","note":"checked against ITC-HS"}
      """
    Then the response status is 200

  @TC-CMP-011 @P1 @negative
  Scenario Outline: HS policy validation
    When I call "PUT /deals/compliance/hs-policy/10063020" with body:
      """
      <body>
      """
    Then the response status is 422

    Examples:
      | body                    |
      | {}                      |
      | {"status":"nonsense"}   |
      | {"status":"licensed"}   |

  @TC-CMP-012 @P2 @positive
  Scenario: List the HS policy
    When I call "GET /deals/compliance/hs-policy"
    Then the response status is 200
    And the response field "seed" is present

  @TC-CMP-013 @P1 @positive
  Scenario: Read and re-run a deal's compliance
    Given a deal "D" at status "NEGOTIATION_AGREED" from fixture "FX-STD"
    When I call "POST /deals/{D}/compliance/check"
    Then the response status is 200
    When I call "GET /deals/{D}/compliance"
    Then the response status is 200

  @TC-CMP-014 @P2 @negative
  Scenario Outline: Compliance on an unknown deal answers 404
    When I call "<call>"
    Then the response status is 404

    Examples:
      | call                                |
      | GET /deals/DEAL-NOPE/compliance     |
      | POST /deals/DEAL-NOPE/compliance/check |

  @TC-CMP-015 @P1 @exception
  Scenario: Compliance switched off skips the check
    Given the setting "COMPLIANCE_ENABLED" is "false"
    And a deal "D" at status "NEGOTIATING" from fixture "FX-STD"
    When the negotiation is finalized at "1250"
    Then deal "D" has status "PI_SENT"
```

### Phase 4: Proforma Invoice

```gherkin
@phase:pi @api @flow
Feature: Proforma Invoice issue and acceptance

  Background:
    Given I am "staff"

  @TC-PI-001 @P0 @positive
  Scenario: Issue a PI
    Given a deal "D" at status "NEGOTIATION_AGREED" from fixture "FX-STD"
    When I call "POST /dealmaker/pi" with body:
      """
      {"deal_id":"{D}","agreed_price":1250,"quantity_mt":20,"port_of_destination":"Jebel Ali, AE","payment_terms":"Staged escrow","idempotency_key":"pi-{D}"}
      """
    Then the response status is 201
    And the response field "status" is "SENT"
    And the response field "total_value" is "25000"
    And deal "D" has status "PI_SENT"
    And an e-mail containing "Proforma Invoice" is composed for the "buyer"

  @TC-PI-002 @P0 @positive
  Scenario: The same idempotency key returns the same PI
    Given a deal "D" at status "PI_SENT" from fixture "FX-STD" with PI idempotency key "pi-1"
    When I call "POST /dealmaker/pi" with body:
      """
      {"deal_id":"{D}","agreed_price":1250,"quantity_mt":20,"port_of_destination":"Jebel Ali, AE","payment_terms":"Staged escrow","idempotency_key":"pi-1"}
      """
    Then the response status is 201
    And the collection "proforma_invoices" has 1 records for the deal

  @TC-PI-003 @P0 @negative
  Scenario Outline: A PI needs an agreed negotiation
    Given a deal "D" at status "<status>" from fixture "FX-STD"
    When I call "POST /dealmaker/pi" with body:
      """
      {"deal_id":"{D}","agreed_price":1250,"quantity_mt":20,"port_of_destination":"Jebel Ali, AE","payment_terms":"Staged escrow"}
      """
    Then the response status is 422
    And deal "D" is unchanged

    Examples:
      | status         |
      | MATCHING       |
      | NEGOTIATING    |
      | ESCROW_PENDING |
      | CANCELLED      |

  @TC-PI-004 @P1 @negative
  Scenario: A PI for an unknown deal answers 404
    When I call "POST /dealmaker/pi" with body:
      """
      {"deal_id":"NOPE","agreed_price":1250,"quantity_mt":20,"port_of_destination":"X","payment_terms":"Y"}
      """
    Then the response status is 404

  @TC-PI-005 @P1 @negative
  Scenario Outline: PI validation
    When I call "POST /dealmaker/pi" with body:
      """
      <body>
      """
    Then the response status is 422

    Examples:
      | body                                       |
      | {}                                         |
      | {"deal_id":"D","agreed_price":"abc","quantity_mt":1,"port_of_destination":"X"} |
      | {"deal_id":"D","agreed_price":1,"port_of_destination":"X"} |

  @TC-PI-006 @P0 @positive
  Scenario: Buyer acceptance opens the escrow and sends funding instructions
    Given a deal "D" at status "PI_SENT" from fixture "FX-STD"
    When I call "POST /dealmaker/pi/{PI}/accept" with body:
      """
      {"accepted_by":"buyer portal"}
      """
    Then the response status is 200
    And the response field "status" is "ACCEPTED"
    And deal "D" has status "ESCROW_PENDING"
    And the escrow status is "PENDING"
    And the escrow amount is 25000
    And an e-mail containing "Escrow funding instructions" is composed for the "buyer"

  @TC-PI-007 @P0 @negative
  Scenario: A PI cannot be accepted twice
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD"
    When I call "POST /dealmaker/pi/{PI}/accept" with body:
      """
      {}
      """
    Then the response status is 422

  @TC-PI-008 @P1 @negative
  Scenario: Accepting an unknown PI answers 404
    When I call "POST /dealmaker/pi/NOPE/accept" with body:
      """
      {}
      """
    Then the response status is 404

  @TC-PI-009 @P0 @money @negative @needs:time
  Scenario: An expired PI cannot be accepted
    Given a deal "D" at status "PI_SENT" from fixture "FX-STD"
    And "expires_at" of the deal's PI was set 1 days ago
    When I call "POST /dealmaker/pi/{PI}/accept" with body:
      """
      {}
      """
    Then the response status is 422
    And deal "D" has status "PI_SENT"

  @TC-PI-019 @P0 @money @positive @needs:time
  Scenario: An extended PI can be accepted
    Given a deal "D" at status "PI_SENT" from fixture "FX-STD"
    And "expires_at" of the deal's PI was set 1 days ago
    When I call "POST /dealmaker/pi/{PI}/extend" with body:
      """
      {"validity_days":5,"reason":"buyer asked for more time"}
      """
    Then the response status is 200
    When I call "POST /dealmaker/pi/{PI}/accept" with body:
      """
      {}
      """
    Then the response status is 200

  @TC-PI-020 @P1 @negative
  Scenario: An extension needs a reason
    Given a deal "D" at status "PI_SENT" from fixture "FX-STD"
    When I call "POST /dealmaker/pi/{PI}/extend" with body:
      """
      {"validity_days":5,"reason":""}
      """
    Then the response status is 422

  @TC-PI-021 @P0 @email @exception @needs:time
  Scenario: A buyer's acceptance after expiry goes to a person
    Given a deal "D" at status "PI_SENT" from fixture "FX-STD"
    And "expires_at" of the deal's PI was set 1 days ago
    When the buyer replies "We accept the Proforma Invoice" from the known address
    Then no gate "CONFIRM_PI_ACCEPTANCE" exists
    And the gate "PARSE_INBOUND_EMAIL" has risk flag "pi_expired"

  @TC-PI-022 @P0 @positive
  Scenario: The PI prints the port the buyer named
    Given a deal "D" at status "NEGOTIATION_AGREED" from fixture "FX-STD"
    And the buyer has written "Our destination port is Jebel Ali, UAE."
    When the Proforma Invoice is issued
    Then the response field "port_of_destination" is "Jebel Ali, AE"

  @TC-PI-023 @P1 @edge
  Scenario: The PI says the port is open when the buyer never named one
    Given a deal "D" at status "NEGOTIATION_AGREED" from fixture "FX-STD"
    When the Proforma Invoice is issued
    Then the response field "port_of_destination" is "Port to be confirmed by buyer, AE"

  @TC-PI-010 @P0 @email @positive
  Scenario: The buyer's e-mailed acceptance needs operator confirmation
    Given a deal "D" at status "PI_SENT" from fixture "FX-STD"
    When the buyer replies "We accept the Proforma Invoice" from the known address
    Then a pending gate "CONFIRM_PI_ACCEPTANCE" exists at level "L0"
    And deal "D" has status "PI_SENT"
    When "staff" approves the pending "CONFIRM_PI_ACCEPTANCE" gate
    Then deal "D" has status "ESCROW_PENDING"

  @TC-PI-011 @P1 @email @edge
  Scenario: A conditional reply is not read as acceptance
    Given a deal "D" at status "PI_SENT" from fixture "FX-STD"
    When the buyer replies "We accept if you lower the price" from the known address
    Then no gate "CONFIRM_PI_ACCEPTANCE" exists
    And a pending gate "PARSE_INBOUND_EMAIL" exists at level "L0"

  @TC-PI-012 @P1 @positive @needs:time
  Scenario Outline: Buyer reminders then an exception
    Given a deal "D" at status "<status>" from fixture "FX-STD"
    And "updated_at" of the deal was set <age> hours ago
    When the sweep "deal_watcher" runs
    Then a reminder is composed for the "buyer"
    And the reminder count never exceeds "DEAL_REMINDER_MAX"

    Examples:
      | status         | age |
      | PI_SENT        | 72  |
      | ESCROW_PENDING | 72  |

  @TC-PI-013 @P1 @needs:time @exception
  Scenario: A PI that expires unanswered raises an exception
    Given a deal "D" at status "PI_SENT" from fixture "FX-STD"
    And "expires_at" of the deal's PI was set 1 days ago
    When the sweep "deal_watcher" runs
    Then an exception is recorded with "deal_id" equal to "{D}"

  @TC-PI-014 @P1 @positive
  Scenario Outline: Read contracts
    Given a deal "D" at status "ESCROW_FUNDED" from fixture "FX-STD"
    When I call "GET <path>"
    Then the response status is 200

    Examples:
      | path                           |
      | /dealmaker/pi/{PI}             |
      | /dealmaker/pi/{PI}/file        |
      | /dealmaker/deals/{D}/documents |

  @TC-PI-015 @P2 @negative
  Scenario Outline: Unknown contract lookups
    When I call "GET <path>"
    Then the response status is 404

    Examples:
      | path                      |
      | /dealmaker/pi/NOPE        |
      | /dealmaker/pi/NOPE/file   |
      | /dealmaker/po/NOPE        |
      | /dealmaker/po/NOPE/file   |

  @TC-PI-016 @P1 @exception @needs:fake-connector
  Scenario: A Drive failure on download answers 502
    Given a deal "D" at status "ESCROW_FUNDED" from fixture "FX-STD"
    And the connector "google_drive" fails with "HTTP 500"
    When I call "GET /dealmaker/pi/{PI}/file"
    Then the response status is 502

  @TC-PI-017 @P1 @needs:fake-connector @exception
  Scenario: A missing local file answers 404
    Given a deal "D" at status "ESCROW_FUNDED" from fixture "FX-STD"
    And the stored PI file has been removed
    When I call "GET /dealmaker/pi/{PI}/file"
    Then the response status is 404

  @TC-PI-018 @P0 @money @exception
  Scenario: Unfunded-deal playbook cancels with an operator's approval
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD" unfunded past the grace period
    When the exception is analysed
    Then a pending gate "RESOLVE_EXCEPTION" exists at level "L0"
    When "staff" approves the pending "RESOLVE_EXCEPTION" gate
    Then deal "D" has status "CANCELLED"
    And an e-mail is composed for both parties
```

```gherkin
@phase:pi @flow @email
Feature: The supplier commits before the buyer gets a PI
  When the buyer agrees, the chosen supplier gets a firm order request at its own stated price and
  the PI waits. A decline, or no reply within SUPPLIER_CONFIRMATION_HOURS (24), sends the request
  to the next-ranked standby supplier whose own price still clears the margin floor. Only one
  supplier holds the request at a time. Supplier "A" is the fixture's supplier (1000); the standbys
  are ranked by score: "B" breaks the 8% floor, "E" has no e-mail address.

  Background:
    Given I am "staff"
    And the setting "SUPPLIER_CONFIRMATION_REQUIRED" is "true"
    And the setting "COMPLIANCE_ENABLED" is "false"
    And a deal "D" at status "NEGOTIATION_AGREED" from fixture "FX-STD"
    And standby supplier "B" quotes 1240 with score 90
    And standby supplier "C" quotes 900 with score 85
    And standby supplier "E" quotes 880 with score 80 and no e-mail address
    And standby supplier "D" quotes 890 with score 75

  @TC-SUP-001 @P0 @positive @money
  Scenario: The buyer's agreement sends the supplier a firm order request and holds the PI
    When the PI leg runs for deal "D"
    Then the order request is "requested"
    And supplier "A" holds the order request
    And an order request is composed for supplier "A" at its own price
    And no order request names the buyer or the buyer's price
    And deal "D" has status "NEGOTIATION_AGREED"
    And the collection "proforma_invoices" has 0 records for the deal

  @TC-SUP-002 @P0 @positive @money
  Scenario: The supplier's confirmation needs an operator, then the PI goes out
    When the PI leg runs for deal "D"
    And supplier "A" replies "We confirm the order at USD 1,000 per MT FOB." from its own address
    Then a pending gate "CONFIRM_SUPPLIER_ORDER" exists at level "L0"
    And the gate "CONFIRM_SUPPLIER_ORDER" is for supplier "A"
    And deal "D" has status "NEGOTIATION_AGREED"
    When "staff" approves the pending "CONFIRM_SUPPLIER_ORDER" gate
    Then deal "D" has status "PI_SENT"
    And the committed supplier is "A" at its own price

  @TC-SUP-003 @P0 @positive
  Scenario: A decline moves the order to the best standby that keeps the margin
    When the PI leg runs for deal "D"
    And supplier "A" replies "We regret we cannot supply this order, our stock is committed." from its own address
    Then the order request to supplier "A" is "declined"
    And supplier "B" is skipped with a reason containing "floor"
    And supplier "C" holds the order request
    And the deal's supplier is supplier "C"
    And an order request is composed for supplier "C" at its own price
    And no order request is composed for supplier "B"
    And no order request names the buyer or the buyer's price
    And deal "D" has status "NEGOTIATION_AGREED"
    And the collection "proforma_invoices" has 0 records for the deal

  @TC-SUP-004 @P1 @negative
  Scenario: A new price or a question is read by an operator, not as a confirmation
    When the PI leg runs for deal "D"
    And supplier "A" replies "Could you do USD 1,050 per MT instead?" from its own address
    Then the inbound record has status "NEEDS_OPERATOR"
    And no gate "CONFIRM_SUPPLIER_ORDER" exists
    And supplier "A" holds the order request

  @TC-SUP-005 @P0 @negative @security @money
  Scenario: A late confirmation from the replaced supplier is left for an operator
    When the PI leg runs for deal "D"
    And supplier "A" replies "We regret we cannot supply this order, our stock is committed." from its own address
    Then supplier "C" holds the order request
    When supplier "A" replies "We have reconsidered: we confirm the order at USD 1,000 per MT FOB." from its own address
    Then no gate "CONFIRM_SUPPLIER_ORDER" exists
    And supplier "C" holds the order request
    And the inbound record has status "NEEDS_OPERATOR"
    And the gate "PARSE_INBOUND_EMAIL" has risk flag "supplier_replaced"

  @TC-SUP-006 @P0 @positive @money
  Scenario: Two declines, a standby with no address passed over, the third supplier confirms
    When the PI leg runs for deal "D"
    And supplier "A" replies "We regret we cannot supply this order, our stock is committed." from its own address
    And supplier "C" replies "We regret we cannot take this order, the mill is fully booked." from its own address
    Then the order request to supplier "C" is "declined"
    And supplier "E" is skipped with a reason containing "e-mail"
    And supplier "D" holds the order request
    And an order request is composed for supplier "D" at its own price
    When supplier "D" replies "We confirm the order at USD 890 per MT FOB." from its own address
    Then the gate "CONFIRM_SUPPLIER_ORDER" is for supplier "D"
    When "staff" approves the pending "CONFIRM_SUPPLIER_ORDER" gate
    Then deal "D" has status "PI_SENT"
    And the committed supplier is "D" at its own price
    And no order request is composed for supplier "B"

  @TC-SUP-007 @P1 @positive @needs:time
  Scenario: Silence past the reply window moves the order to the standby
    When the PI leg runs for deal "D"
    And the supplier order sweep runs 1 hour later
    Then supplier "A" holds the order request
    When the supplier order sweep runs 25 hours later
    Then the order request to supplier "A" is "no_reply"
    And supplier "C" holds the order request
    And an order request is composed for supplier "C" at its own price

  @TC-SUP-008 @P0 @exception @money
  Scenario: Nobody left: one exception, the PI stays back
    When the PI leg runs for deal "D"
    And supplier "A" replies "We regret we cannot supply this order, our stock is committed." from its own address
    And supplier "C" replies "We regret we cannot take this order, the mill is fully booked." from its own address
    And supplier "D" replies "We regret we are unable to supply, sorry." from its own address
    Then the order request is "exhausted"
    And exactly 1 exception is recorded with "stage" equal to "SUPPLIER:no_confirmation"
    And deal "D" has status "NEGOTIATION_AGREED"
    And the collection "proforma_invoices" has 0 records for the deal
    When the supplier order sweep runs 100 hours later
    Then exactly 1 exception is recorded with "stage" equal to "SUPPLIER:no_confirmation"

  @TC-SUP-009 @P1 @negative @money
  Scenario: A confirmation after the request ran out issues no PI
    When the PI leg runs for deal "D"
    And the supplier order sweep runs 25 hours later
    And the supplier order sweep runs 50 hours later
    And the supplier order sweep runs 75 hours later
    Then the order request is "exhausted"
    When supplier "D" replies "Sorry for the delay: we confirm the order at USD 890 per MT FOB." from its own address
    Then no gate "CONFIRM_SUPPLIER_ORDER" exists
    And the inbound record has status "NEEDS_OPERATOR"
    And the collection "proforma_invoices" has 0 records for the deal

  @TC-SUP-010 @P1 @positive
  Scenario: Switched off, the PI goes out without an order request
    Given the setting "SUPPLIER_CONFIRMATION_REQUIRED" is "false"
    When the PI leg runs for deal "D"
    Then deal "D" has status "PI_SENT"
    And there is no order request
    And no e-mail containing "Order confirmation" is composed
```

### Phase 5: Escrow funding

```gherkin
@phase:escrow @api @flow @money
Feature: Opening and funding the escrow

  Background:
    Given I am "staff"

  @TC-ESC-001 @P0 @positive
  Scenario: Create a staged escrow
    Given a deal "D" at status "PI_ACCEPTED" from fixture "FX-STD"
    When I call "POST /escrow-guard/escrows" with body:
      """
      {"deal_id":"{D}","amount":25000,"provider":"tazapay","idempotency_key":"esc-{D}"}
      """
    Then the response status is 201
    And the response field "status" is "PENDING"
    And deal "D" has status "ESCROW_PENDING"

  @TC-ESC-002 @P0 @positive
  Scenario: The same idempotency key returns the same escrow
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD" with escrow idempotency key "esc-1"
    When I call "POST /escrow-guard/escrows" with body:
      """
      {"deal_id":"{D}","amount":25000,"provider":"tazapay","idempotency_key":"esc-1"}
      """
    Then the response status is 201
    And exactly 1 escrow exists for the deal

  @TC-ESC-003 @P0 @negative
  Scenario Outline: An escrow needs an accepted PI
    Given a deal "D" at status "<status>" from fixture "FX-STD"
    When I call "POST /escrow-guard/escrows" with body:
      """
      {"deal_id":"{D}","amount":25000,"provider":"tazapay"}
      """
    Then the response status is 422

    Examples:
      | status             |
      | NEGOTIATION_AGREED |
      | PI_SENT            |

  @TC-ESC-004 @P1 @negative
  Scenario: An escrow for an unknown deal answers 404
    When I call "POST /escrow-guard/escrows" with body:
      """
      {"deal_id":"NOPE","amount":25000,"provider":"tazapay"}
      """
    Then the response status is 404

  @TC-ESC-005 @P1 @exception @needs:fake-connector
  Scenario: A provider failure answers 502 and leaves an audit record
    Given a deal "D" at status "PI_ACCEPTED" from fixture "FX-STD"
    And the connector "tazapay" fails with "HTTP 500"
    When I call "POST /escrow-guard/escrows" with body:
      """
      {"deal_id":"{D}","amount":25000,"provider":"tazapay"}
      """
    Then the response status is 502

  @TC-ESC-006 @P1 @positive
  Scenario: Legacy hold on an open escrow step
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD" with no hold
    When I call "POST /escrow-guard/holds" with body:
      """
      {"deal_id":"{D}","amount":25000,"currency":"USD","idempotency_key":"h1"}
      """
    Then the response status is 201

  @TC-ESC-007 @P1 @negative
  Scenario Outline: A legacy hold needs ESCROW_PENDING
    Given a deal "D" at status "<status>" from fixture "FX-STD"
    When I call "POST /escrow-guard/holds" with body:
      """
      {"deal_id":"{D}","amount":25000,"currency":"USD","idempotency_key":"h1"}
      """
    Then the response status is 422

    Examples:
      | status        |
      | PI_SENT       |

  @TC-ESC-008 @P2 @positive
  Scenario: List escrow providers
    When I call "GET /escrow-guard/providers"
    Then the response status is 200

  @TC-ESC-009 @P0 @positive
  Scenario: Full funding moves the deal and opens the PO gate
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD"
    When the provider "tazapay" sends "/escrow-guard/webhooks/escrow-funded" with signature "valid" and body:
      """
      {"ext_ref":"{ESC_REF}","status":"FUNDED","amount":25000,"event_id":"ev1","payer_name":"Sandbox Buyer LLC","payer_country":"AE"}
      """
    Then the response status is 200
    And deal "D" has status "ESCROW_FUNDED"
    And the escrow status is "FUNDED"
    And a pending gate "ISSUE_PO" exists at level "L0"

  @TC-ESC-010 @P0 @negative
  Scenario: A shortfall keeps the deal waiting and asks the buyer for the balance
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD"
    When the provider "tazapay" sends "/escrow-guard/webhooks/escrow-funded" with signature "valid" and body:
      """
      {"ext_ref":"{ESC_REF}","status":"FUNDED","amount":15000,"event_id":"ev1"}
      """
    Then the response status is 200
    And the response field "status" is "shortfall"
    And the response field "shortfall" is "10000"
    And deal "D" has status "ESCROW_PENDING"
    And the escrow status is "PARTIALLY_FUNDED"
    And no gate "ISSUE_PO" exists
    And an exception is recorded with "category" equal to "PAYMENT_FAILED"
    And an e-mail containing "partially funded" is composed for the "buyer"

  @TC-ESC-011 @P0 @positive
  Scenario: The provider's next full total completes the funding
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD" funded with 15000
    When the provider "tazapay" sends "/escrow-guard/webhooks/escrow-funded" with signature "valid" and body:
      """
      {"ext_ref":"{ESC_REF}","status":"FUNDED","amount":25000,"event_id":"ev2"}
      """
    Then deal "D" has status "ESCROW_FUNDED"
    And the escrow status is "FUNDED"

  @TC-ESC-012 @P0 @edge
  Scenario: A replayed event id is acknowledged and not applied again
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD" funded with 15000 by event "ev1"
    When the provider "tazapay" sends "/escrow-guard/webhooks/escrow-funded" with signature "valid" and body:
      """
      {"ext_ref":"{ESC_REF}","status":"FUNDED","amount":15000,"event_id":"ev1"}
      """
    Then the response status is 200
    And the response field "replayed" is "true"
    And exactly 1 exception is recorded for the shortfall

  @TC-ESC-013 @P1 @edge
  Scenario: A funded webhook on a funded escrow changes nothing
    Given a deal "D" at status "ESCROW_FUNDED" from fixture "FX-STD"
    When the provider "tazapay" sends "/escrow-guard/webhooks/escrow-funded" with signature "valid" and body:
      """
      {"ext_ref":"{ESC_REF}","status":"FUNDED","amount":25000,"event_id":"ev9"}
      """
    Then the response status is 200
    And no gate "ISSUE_PO" exists

  @TC-ESC-014 @P0 @exception
  Scenario: Over-funding proceeds and flags the surplus
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD"
    When the provider "tazapay" sends "/escrow-guard/webhooks/escrow-funded" with signature "valid" and body:
      """
      {"ext_ref":"{ESC_REF}","status":"FUNDED","amount":26000,"event_id":"ev1"}
      """
    Then deal "D" has status "ESCROW_FUNDED"
    And an exception is recorded with "reason" equal to "funding_amount_mismatch"

  @TC-ESC-015 @P1 @positive
  Scenario Outline: Stripe amounts arrive in minor units
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD" using escrow provider "stripe"
    When the provider "stripe" sends "/escrow-guard/webhooks/escrow-funded" with signature "valid" and body:
      """
      {"ext_ref":"{ESC_REF}","status":"FUNDED","amount":<amount>,"event_id":"ev1"}
      """
    Then the escrow status is "<result>"

    Examples:
      | amount  | result           |
      | 2500000 | FUNDED           |
      | 1500000 | PARTIALLY_FUNDED |

  @TC-ESC-016 @P0 @compliance @positive
  Scenario: The buyer paying leaves no payer warning on the PO gate
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD"
    When the buyer funds the escrow in full as the payer
    Then the gate "ISSUE_PO" does not have risk flag "compliance:payer_unknown"
    And the gate "ISSUE_PO" does not have risk flag "compliance:payer_not_buyer"

  @TC-ESC-017 @P0 @compliance @negative
  Scenario Outline: A payer problem names the real reason and keeps a human on the PO
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD"
    And the autonomy level of "ISSUE_PO" is "L2"
    When the escrow is funded in full with <payer>
    Then a pending gate "ISSUE_PO" exists at level "L0"
    And the gate "ISSUE_PO" has risk flag "compliance:<flag>"

    Examples:
      | payer                                | flag                   |
      | payer "Unrelated Trading FZE"        | payer_not_buyer        |
      | no payer reported                    | payer_unknown          |
      | the buyer paying from country "DE"   | payer_country_mismatch |

  @TC-ESC-018 @P1 @positive
  Scenario Outline: Read escrow
    Given a deal "D" at status "ESCROW_FUNDED" from fixture "FX-STD"
    When I call "GET <path>"
    Then the response status is 200

    Examples:
      | path                            |
      | /escrow-guard/escrows/{ESC}     |
      | /escrow-guard/holds/{ESC}       |
      | /escrow-guard/deals/{D}/hold    |
      | /escrow-guard/deals/{D}/summary |

  @TC-ESC-019 @P2 @negative
  Scenario Outline: Unknown escrow lookups
    When I call "GET <path>"
    Then the response status is 404

    Examples:
      | path                            |
      | /escrow-guard/escrows/NOPE      |
      | /escrow-guard/holds/NOPE        |
      | /escrow-guard/deals/NOPE/hold   |
      | /escrow-guard/payouts/NOPE      |

  @TC-ESC-020 @P0 @money @needs:time @exception
  Scenario: Unfunded past the grace period is offered for cancellation
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD"
    And "created_at" of the deal's escrow was set 15 days ago
    When the sweep "deal_watcher" runs
    Then an exception is recorded with "category" equal to "PAYMENT_FAILED"
    And the pending gate "RESOLVE_EXCEPTION" has playbook "cancel_unfunded"

  @TC-ESC-021 @P0 @money @exception
  Scenario: Cancelling a partly funded deal refunds the money received
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD" funded with 15000
    And a pending gate "RESOLVE_EXCEPTION" has playbook "cancel_unfunded"
    When "staff" approves the pending "RESOLVE_EXCEPTION" gate
    Then deal "D" has status "CANCELLED"
    And the escrow status is "REFUNDED"
    And the ledger field "refunded_total" is 15000
```

### Phase 6: Purchase Order

```gherkin
@phase:po @api @flow @money
Feature: Purchase Order and the first release

  Background:
    Given I am "staff"

  @TC-PO-001 @P0 @positive
  Scenario: Approving the PO gate issues the PO and books QC
    Given a deal "D" at status "ESCROW_FUNDED" from fixture "FX-STD"
    When "staff" approves the pending "ISSUE_PO" gate
    Then deal "D" has status "SUPPLIER_DISPATCH_READY"
    And the collection "purchase_orders" has 1 records for the deal
    And an e-mail containing "Purchase Order" is composed for the "supplier"
    And an e-mail containing "Pre-shipment inspection booked" is composed for the "supplier"
    And a pending gate "RELEASE_ESCROW_TRANCHE" exists at level "L0"
    And the gate "RELEASE_ESCROW_TRANCHE" amount is 7500

  @TC-PO-002 @P0 @negative
  Scenario: Rejecting the PO gate raises an exception and issues nothing
    Given a deal "D" at status "ESCROW_FUNDED" from fixture "FX-STD"
    When "staff" rejects the pending "ISSUE_PO" gate
    Then deal "D" has status "ESCROW_FUNDED"
    And the collection "purchase_orders" has 0 records for the deal
    And an exception is recorded with "deal_id" equal to "{D}"

  @TC-PO-003 @P0 @positive
  Scenario Outline: Margin guard at the floor
    Given a deal "D" at status "ESCROW_FUNDED" from fixture "FX-STD" with buyer price <buyer>
    When I call "POST /dealmaker/po" with body:
      """
      {"deal_id":"{D}","agreed_price":<supplier>,"quantity_mt":20,"port_of_loading":"Chennai, IN","delivery_deadline":"2027-01-31","payment_terms":"Staged"}
      """
    Then the response status is <code>

    Examples:
      | buyer | supplier | code |
      | 1250  | 1000     | 201  |
      | 1000  | 920      | 201  |
      | 1000  | 921      | 422  |
      | 1050  | 1000     | 422  |
      | 1000  | 1000     | 422  |
      | 1000  | 1100     | 422  |

  @TC-PO-004 @P0 @money @negative
  Scenario: A PO with no buyer price to check its margin against is refused
    Given a deal "D" at status "ESCROW_FUNDED" from fixture "FX-STD" with no buyer target price and no PI
    When I call "POST /dealmaker/po" with body:
      """
      {"deal_id":"{D}","agreed_price":5000,"quantity_mt":20,"port_of_loading":"Chennai, IN","delivery_deadline":"2027-01-31","payment_terms":"Staged"}
      """
    Then the response status is 422

  @TC-PO-005 @P0 @negative
  Scenario Outline: A PO needs funded money
    Given a deal "D" at status "<status>" from fixture "FX-STD"
    When I call "POST /dealmaker/po" with body:
      """
      {"deal_id":"{D}","agreed_price":1000,"quantity_mt":20,"port_of_loading":"Chennai, IN","delivery_deadline":"2027-01-31","payment_terms":"Staged"}
      """
    Then the response status is 422
    And deal "D" is unchanged

    Examples:
      | status         |
      | PI_SENT        |
      | ESCROW_PENDING |
      | PO_ISSUED      |
      | CLEARED        |

  @TC-PO-006 @P1 @negative
  Scenario: A PO for an unknown deal answers 404
    When I call "POST /dealmaker/po" with body:
      """
      {"deal_id":"NOPE","agreed_price":1000,"quantity_mt":20,"port_of_loading":"X","delivery_deadline":"2027-01-31","payment_terms":"Y"}
      """
    Then the response status is 404

  @TC-PO-007 @P1 @negative
  Scenario Outline: PO validation
    When I call "POST /dealmaker/po" with body:
      """
      <body>
      """
    Then the response status is 422

    Examples:
      | body                                                                                     |
      | {}                                                                                       |
      | {"deal_id":"D","agreed_price":1,"quantity_mt":1,"port_of_loading":"X"}                   |
      | {"deal_id":"D","agreed_price":1,"quantity_mt":1,"port_of_loading":"X","delivery_deadline":"soon"} |

  @TC-PO-008 @P0 @edge
  Scenario: The first release cannot run before money exists
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD"
    When I call with its approved gate "POST /escrow-guard/escrows/{ESC}/release" with body:
      """
      {"tranche_id":"T1","proof_refs":["x"]}
      """
    Then the response status is 422

  @TC-PO-009 @P0 @positive
  Scenario: Approving T1 releases the production advance
    Given a deal "D" at status "SUPPLIER_DISPATCH_READY" from fixture "FX-STD" with T1 unreleased
    When "staff" approves the pending "RELEASE_ESCROW_TRANCHE" gate
    Then tranche "T1" has status "RELEASED"
    And the ledger field "released_total" is 7500
    And the ledger field "fees_total" is 75

  @TC-PO-010 @P0 @negative
  Scenario: Rejecting T1 keeps the money in escrow
    Given a deal "D" at status "SUPPLIER_DISPATCH_READY" from fixture "FX-STD" with T1 unreleased
    When "staff" rejects the pending "RELEASE_ESCROW_TRANCHE" gate
    Then tranche "T1" has status "PENDING"
    And an exception is recorded with "deal_id" equal to "{D}"

  @TC-PO-011 @P1 @positive @needs:time
  Scenario: A silent supplier is reminded
    Given a deal "D" at status "PO_ISSUED" from fixture "FX-STD"
    And "updated_at" of the deal was set 72 hours ago
    When the sweep "deal_watcher" runs
    Then a reminder is composed for the "supplier"

  @TC-PO-013 @P0 @money @negative
  Scenario Outline: A PO uses only a price the supplier itself stated
    Given a deal "D" at status "ESCROW_FUNDED" from a negotiation with supplier quote <quote>, our counter 960 and supplier's last reply <reply>
    Then the gate "ISSUE_PO" amount is <amount>

    Examples:
      | quote | reply | amount |
      | 1000  | none  | 20000  |
      | 1000  | 1010  | 20200  |

  @TC-PO-012 @P1 @positive
  Scenario: Read a PO
    Given a deal "D" at status "SUPPLIER_DISPATCH_READY" from fixture "FX-STD"
    When I call "GET /dealmaker/po/{PO}"
    Then the response status is 200
    When I call "GET /dealmaker/po/{PO}/file"
    Then the response status is 200
```

### Phase 7: Quality control and loading

```gherkin
@phase:qc @api @flow
Feature: Inspection booking, result and supervised loading

  Background:
    Given I am "staff"

  @TC-QC-001 @P1 @positive
  Scenario: QC providers and quotes
    When I call "GET /shield-link/qc/providers"
    Then the response status is 200
    When I call "POST /shield-link/qc/quote" with body:
      """
      {"provider":"sgs","quantity_mt":20,"hs_code":"10063020"}
      """
    Then the response status is 200

  @TC-QC-002 @P1 @negative
  Scenario: Quoting an unknown provider answers 422
    When I call "POST /shield-link/qc/quote" with body:
      """
      {"provider":"nope","quantity_mt":20}
      """
    Then the response status is 422

  @TC-QC-003 @P0 @positive
  Scenario Outline: Book with each provider
    Given a deal "D" at status "PO_ISSUED" from fixture "FX-STD"
    When I call "POST /shield-link/qc" with body:
      """
      {"deal_id":"{D}","provider":"<provider>","idempotency_key":"qc-{D}"}
      """
    Then the response status is 201
    And deal "D" has status "SUPPLIER_DISPATCH_READY"

    Examples:
      | provider |
      | sgs      |
      | qima     |
      | bv       |
      | intertek |
      | tuv_sud  |

  @TC-QC-004 @P1 @positive
  Scenario: The buyer-opted provider is used by default
    Given a deal "D" at status "PO_ISSUED" from fixture "FX-STD" whose buyer opted for "qima"
    When I call "POST /shield-link/qc" with body:
      """
      {"deal_id":"{D}"}
      """
    Then the response field "provider" is "qima"

  @TC-QC-005 @P0 @negative
  Scenario Outline: QC needs an issued PO
    Given a deal "D" at status "<status>" from fixture "FX-STD"
    When I call "POST /shield-link/qc" with body:
      """
      {"deal_id":"{D}","provider":"sgs"}
      """
    Then the response status is 422

    Examples:
      | status        |
      | ESCROW_FUNDED |
      | QC_PASSED     |
      | IN_TRANSIT    |

  @TC-QC-006 @P1 @negative
  Scenario Outline: QC booking errors
    Given a deal "D" at status "PO_ISSUED" from fixture "FX-STD"
    When I call "POST /shield-link/qc" with body:
      """
      <body>
      """
    Then the response status is <code>

    Examples:
      | body                                  | code |
      | {}                                    | 422  |
      | {"deal_id":"{D}","provider":"nope"}   | 422  |
      | {"deal_id":"NOPE","provider":"sgs"}   | 404  |

  @TC-QC-007 @P1 @exception @needs:fake-connector
  Scenario: A provider failure on booking answers 502
    Given a deal "D" at status "PO_ISSUED" from fixture "FX-STD"
    And the connector "sgs" fails with "HTTP 500"
    When I call "POST /shield-link/qc" with body:
      """
      {"deal_id":"{D}","provider":"sgs"}
      """
    Then the response status is 502
    And a "FAILED" order exists for the deal

  @TC-QC-008 @P0 @positive
  Scenario: A pass with the sealed container releases the next gates
    Given a deal "D" at status "SUPPLIER_DISPATCH_READY" from fixture "FX-STD" with T1 released
    When the inspector reports PASS with container "SBXU0000001", seal "SEAL-1", 20 MT, HS "10063020" and a photo
    Then deal "D" has status "QC_PASSED"
    And the collection "documents" has 5 records for the deal
    And a pending gate "RELEASE_ESCROW_TRANCHE" exists at level "L0"
    And the gate "RELEASE_ESCROW_TRANCHE" amount is 10000
    And a pending gate "BOOK_FREIGHT" exists at level "L0"

  @TC-QC-009 @P0 @compliance @positive
  Scenario: A pass without a seal waits for the loading record
    Given a deal "D" at status "SUPPLIER_DISPATCH_READY" from fixture "FX-STD"
    When the inspector reports PASS with no loading details
    Then deal "D" has status "QC_PASSED"
    And no gate "BOOK_FREIGHT" exists
    And the compliance hold "loading_not_verified" is open

  @TC-QC-010 @P0 @compliance @positive
  Scenario: The loading record opens the gates
    Given a deal "D" at status "QC_PASSED" from fixture "FX-STD" waiting for loading
    When I call "POST /shield-link/deals/{D}/loading" with body:
      """
      {"container_numbers":["SBXU0000001"],"seal_numbers":["SEAL-1"],"loaded_quantity_mt":20,"hs_code":"10063020","photos":["https://x.invalid/1.jpg"]}
      """
    Then the response status is 200
    And a pending gate "BOOK_FREIGHT" exists at level "L0"

  @TC-QC-011 @P1 @negative
  Scenario Outline: Loading validation
    Given a deal "D" at status "QC_PASSED" from fixture "FX-STD" waiting for loading
    When I call "POST /shield-link/deals/{D}/loading" with body:
      """
      <body>
      """
    Then the response status is 422

    Examples:
      | body                                                     |
      | {}                                                       |
      | {"container_numbers":[],"seal_numbers":["S"]}            |
      | {"container_numbers":["C"],"seal_numbers":[]}            |

  @TC-QC-012 @P2 @negative
  Scenario: Loading for an unknown deal answers 404
    When I call "POST /shield-link/deals/NOPE/loading" with body:
      """
      {"container_numbers":["C"],"seal_numbers":["S"]}
      """
    Then the response status is 404

  @TC-QC-013 @P0 @compliance @negative
  Scenario: A wrong product in the container stops the shipment
    Given a deal "D" at status "QC_PASSED" from fixture "FX-STD" waiting for loading
    When the loading is recorded with HS "85171300"
    Then the compliance hold "shipment_hs_mismatch" is open
    And no gate "BOOK_FREIGHT" exists
    And an exception is recorded with "category" equal to "QC_FAILED"

  @TC-QC-014 @P0 @compliance @positive
  Scenario Outline: Loading review findings keep gates with a human
    Given a deal "D" at status "QC_PASSED" from fixture "FX-STD" waiting for loading
    And the autonomy level of "BOOK_FREIGHT" is "L2"
    When the loading is recorded with <loading>
    Then a pending gate "BOOK_FREIGHT" exists at level "L0"
    And the gate "BOOK_FREIGHT" has risk flag "compliance:<flag>"

    Examples:
      | loading                       | flag                   |
      | 25 MT loaded against 20 MT    | shipment_qty_mismatch  |
      | no loaded quantity            | shipment_qty_unknown   |
      | no photos                     | shipment_no_photos     |

  @TC-QC-015 @P2 @edge
  Scenario Outline: Quantity tolerance boundary
    Given a deal "D" at status "QC_PASSED" from fixture "FX-STD" waiting for loading
    When the loading is recorded with <mt> MT
    Then the compliance hold "shipment_qty_mismatch" <state>

    Examples:
      | mt   | state       |
      | 21.0 | is not open |
      | 19.0 | is not open |
      | 21.1 | is open     |
      | 18.9 | is open     |

  @TC-QC-016 @P0 @negative
  Scenario: A failed inspection opens a dispute with a re-inspection plan
    Given a deal "D" at status "SUPPLIER_DISPATCH_READY" from fixture "FX-STD" with T1 released
    When the inspector reports FAIL
    Then deal "D" has status "DISPUTE"
    And no gate "BOOK_FREIGHT" exists
    And the pending gate "RESOLVE_EXCEPTION" has playbook "reinspect"

  @TC-QC-017 @P0 @money @negative
  Scenario: No release while in dispute
    Given a deal "D" at status "DISPUTE" from fixture "FX-STD"
    When I call with its approved gate "POST /escrow-guard/escrows/{ESC}/release" with body:
      """
      {"tranche_id":"T2","proof_refs":["x"],"approval_id":"{APPR}"}
      """
    Then the response status is 422
    And tranche "T2" has status "PENDING"

  @TC-QC-018 @P0 @positive
  Scenario: Re-inspection books a fresh inspection
    Given a deal "D" at status "DISPUTE" from fixture "FX-STD" with the re-inspection gate pending
    When "staff" approves the pending "RESOLVE_EXCEPTION" gate
    Then deal "D" has status "SUPPLIER_DISPATCH_READY"
    And 2 QC orders exist for the deal
    And an e-mail containing "rework" is composed for the "supplier"

  @TC-QC-019 @P0 @exception
  Scenario: A second failure beyond the limit is offered for cancellation
    Given a deal "D" whose inspections have already failed the allowed number of times
    When the inspector reports FAIL
    Then an exception is recorded with "category" equal to "QC_FAILED"
    And the exception decision is "CANCEL"

  @TC-QC-020 @P0 @money @positive
  Scenario: Refund after a failed inspection returns only undisbursed money
    Given a deal "D" at status "DISPUTE" from fixture "FX-STD" with T1 released
    When I call with its approved gate "POST /escrow-guard/escrows/{ESC}/refund" with body:
      """
      {"reason":"failed inspection"}
      """
    Then the response status is 200
    And the ledger field "refunded_total" is 17500
    And deal "D" has status "REFUNDED"

  @TC-QC-021 @P1 @positive
  Scenario: Read logistics orders
    Given a deal "D" at status "QC_PASSED" from fixture "FX-STD"
    When I call "GET /shield-link/deals/{D}/orders"
    Then the response status is 200
    When I call "GET /shield-link/tpo/{QC}"
    Then the response status is 200

  @TC-QC-022 @P2 @negative
  Scenario: Unknown order
    When I call "GET /shield-link/tpo/NOPE"
    Then the response status is 404
```

#### Inspection providers: QIMA by API, SGS and BV by portal

QIMA publishes a REST API (developers.qima.com): the booking goes to `POST /v1.0/inspection`, the status and the report are polled, and the report PDF is downloaded. SGS SMART and BV OneSource Connect are portals with no public API: the booking request goes to the provider's booking desk, and the confirmation and report come back on the signed QC webhook or from staff. The automation worker runs these providers on the in-process sandbox (`services/qc_sandbox`, `QC_SANDBOX_PROVIDERS`). The sandbox uses QIMA's documented requests and responses, and PortLess's own webhook contract for the portals. `{READY_DATE}` is a working day 8 to 10 days out, set by the Given step.

```gherkin
@phase:qc @api @flow
Feature: Inspection providers, the goods-ready date and the inspection status

  Background:
    Given I am "staff"

  @TC-QC-023 @P0 @positive @sandbox
  Scenario: A QIMA booking sends QIMA's documented order for the supplier's ready date
    Given a deal "D" at status "PO_ISSUED" from fixture "FX-STD"
    When the inspection is booked with "qima" for goods ready in 8 working days
    Then deal "D" has status "SUPPLIER_DISPATCH_READY"
    And the QC order has provider status "REQUESTED"
    And the QC order is booked for the goods-ready date
    And QIMA received the booking with the deal's PO number and the supplier's name
    And an e-mail containing "Pre-shipment inspection booked" is composed for the "supplier"

  @TC-QC-024 @P0 @positive @sandbox
  Scenario: QIMA's validation and passing report reach the deal by polling
    Given a deal "D" whose "qima" inspection is booked
    When QIMA validates the order for the inspection date
    And the QC status is polled
    Then the QC order has provider status "CONFIRMED"
    When QIMA reports "PASS" for the order
    And the QC status is polled
    Then deal "D" has status "QC_PASSED"
    And the QC order has provider status "REPORT_ISSUED"
    And the QC order keeps 1 report PDF whose hash is the CRF hash

  @TC-QC-025 @P0 @negative @sandbox
  Scenario: A failing QIMA report opens a dispute
    Given a deal "D" whose "qima" inspection is booked
    When QIMA reports "FAIL" for the order
    And the QC status is polled
    Then deal "D" has status "DISPUTE"
    And an exception is recorded with "category" equal to "QC_FAILED"

  @TC-QC-026 @P1 @exception @sandbox
  Scenario: A QIMA report that leaves the decision to the client goes to a person
    Given a deal "D" whose "qima" inspection is booked
    When QIMA reports "PENDING" for the order
    And the QC status is polled
    Then deal "D" has status "SUPPLIER_DISPATCH_READY"
    And an exception is recorded with "source_event" equal to "QC_RESULT_PENDING"

  @TC-QC-027 @P1 @positive @sandbox
  Scenario: A repeated poll after the report changes nothing
    Given a deal "D" whose "qima" inspection is booked
    When QIMA reports "PASS" for the order
    And the QC status is polled
    And the QC status is polled
    Then deal "D" has status "QC_PASSED"
    And the QC order keeps 1 report PDF whose hash is the CRF hash

  @TC-QC-028 @P0 @positive @sandbox
  Scenario Outline: A portal provider confirms and reports by signed webhook
    Given a deal "D" whose "<provider>" inspection is booked
    When the "<provider>" desk confirms the booking by webhook
    Then the response status is 200
    And the QC order has provider status "CONFIRMED"
    When the "<provider>" desk reports "PASS" with container "SBXU0000001" and seal "SEAL-1" by webhook
    Then deal "D" has status "QC_PASSED"
    And the QC order keeps 1 report PDF whose hash is the CRF hash

    Examples:
      | provider |
      | sgs      |
      | bv       |

  @TC-QC-029 @P1 @negative @sandbox
  Scenario: A portal webhook with a wrong signature is refused
    Given a deal "D" whose "sgs" inspection is booked
    When the "sgs" desk confirms the booking by webhook with a wrong signature
    Then the response status is 401
    And the QC order has provider status "REQUESTED"

  @TC-QC-030 @P1 @positive
  Scenario: Staff relay the result read on the provider's portal
    Given a deal "D" whose "bv" inspection is booked
    When I call "POST /shield-link/qc/{QC}/status" with body:
      """
      {"status":"REPORT_ISSUED","result":"FAIL","notes":"broken grains above the limit"}
      """
    Then the response status is 200
    And deal "D" has status "DISPUTE"

  @TC-QC-031 @P1 @negative
  Scenario Outline: Status relay validation
    Given a deal "D" whose "bv" inspection is booked
    When I call "POST /shield-link/qc/<ref>/status" with body:
      """
      <body>
      """
    Then the response status is <code>

    Examples:
      | ref  | body                        | code |
      | {QC} | {"status":"REPORT_ISSUED"}  | 422  |
      | {QC} | {"status":"DONE"}           | 422  |
      | NOPE | {"status":"CONFIRMED"}      | 404  |

  @TC-QC-032 @P0 @positive
  Scenario: The PO acknowledgement names the goods-ready date and the inspection is booked for it
    Given a deal "D" at status "ESCROW_FUNDED" from fixture "FX-STD"
    And the supplier must acknowledge the PO and give a goods-ready date
    When "staff" approves the pending "ISSUE_PO" gate
    Then deal "D" has status "PO_ISSUED"
    And an e-mail containing "100% produced" is composed for the "supplier"
    When the supplier replies "We acknowledge the purchase order. The goods will be ready for inspection on {READY_DATE}." from the known address
    Then deal "D" has status "SUPPLIER_DISPATCH_READY"
    And the QC order is booked for the goods-ready date

  @TC-QC-033 @P0 @positive
  Scenario: An acknowledgement without a date is followed by the date question
    Given a deal "D" at status "ESCROW_FUNDED" from fixture "FX-STD"
    And the supplier must acknowledge the PO and give a goods-ready date
    When "staff" approves the pending "ISSUE_PO" gate
    And the supplier replies "We acknowledge the purchase order." from the known address
    Then deal "D" has status "PO_ISSUED"
    And no QC order exists for the deal
    And an e-mail containing "ready for inspection" is composed for the "supplier"
    When the supplier replies "The lot will be packed and ready on {READY_DATE}." from the known address
    Then deal "D" has status "SUPPLIER_DISPATCH_READY"
    And the QC order is booked for the goods-ready date

  @TC-QC-034 @P1 @positive
  Scenario: Staff record a goods-ready date heard by phone
    Given a deal "D" at status "ESCROW_FUNDED" from fixture "FX-STD"
    And the supplier must acknowledge the PO and give a goods-ready date
    When "staff" approves the pending "ISSUE_PO" gate
    And I call "POST /shield-link/deals/{D}/qc-readiness" with body:
      """
      {"ready_date":"{READY_DATE}","packed_pct":90}
      """
    Then the response status is 200
    And the response field "status" is "booked"
    And deal "D" has status "SUPPLIER_DISPATCH_READY"
    And the QC order is booked for the goods-ready date

  @TC-QC-035 @P1 @negative
  Scenario Outline: Goods-ready date validation
    Given a deal "D" at status "PO_ISSUED" from fixture "FX-STD"
    When I call "POST /shield-link/deals/<deal>/qc-readiness" with body:
      """
      <body>
      """
    Then the response status is <code>

    Examples:
      | deal | body                                          | code |
      | {D}  | {"ready_date":"soon"}                         | 422  |
      | {D}  | {"ready_date":"2026-12-01","packed_pct":150}  | 422  |
      | {D}  | {}                                            | 422  |
      | NOPE | {"ready_date":"2026-12-01"}                   | 404  |

  @TC-QC-036 @P1 @positive @sandbox
  Scenario: Moving the inspection cancels the QIMA order and books the new date
    Given a deal "D" whose "qima" inspection is booked
    When I call "POST /shield-link/qc/{QC}/reschedule" with body:
      """
      {"inspection_date":"{READY_DATE}","reason":"supplier delayed packing"}
      """
    Then the response status is 201
    And QIMA cancelled the first order with reason "supplier delayed packing"
    And deal "D" has status "SUPPLIER_DISPATCH_READY"
    And 2 QC orders exist for the deal

  @TC-QC-037 @P1 @positive
  Scenario: The deal's inspection view
    Given a deal "D" whose "qima" inspection is booked
    When I call "GET /shield-link/deals/{D}/qc"
    Then the response status is 200
    And the response field "orders.0.provider" is "qima"
    And the response field "orders.0.provider_status" is "REQUESTED"
    And the response field "orders.0.channel" is "sandbox"

  @TC-QC-038 @P2 @positive @sandbox
  Scenario: The kept report is served as a PDF
    Given a deal "D" whose "qima" inspection is booked
    When QIMA reports "PASS" for the order
    And the QC status is polled
    And I call "GET /shield-link/qc/{QC}/documents/0"
    Then the response status is 200
    And the response is a PDF
```

### Phase 8: Documents

```gherkin
@phase:documents @api
Feature: Export documents and hand-over records

  Background:
    Given I am "staff"
    And a deal "D" at status "QC_PASSED" from fixture "FX-STD"

  @TC-DOC-001 @P1 @positive
  Scenario Outline: Render a document
    When I call "POST /docugen/documents" with body:
      """
      {"deal_id":"{D}","doc_type":"<type>","idempotency_key":"doc-{D}-<type>"}
      """
    Then the response status is 201
    And the response field "status" is "RENDERED"

    Examples:
      | type          |
      | CI            |
      | PL            |
      | CoO           |
      | SDF           |
      | SHIPPING_BILL |

  @TC-DOC-002 @P1 @edge
  Scenario: Rendering with the same key returns the same document
    When the document "CI" is requested twice with the same key
    Then the collection "documents" has no duplicate "CI"

  @TC-DOC-003 @P1 @negative
  Scenario Outline: Document request errors
    When I call "POST /docugen/documents" with body:
      """
      <body>
      """
    Then the response status is <code>

    Examples:
      | body                                  | code |
      | {}                                    | 422  |
      | {"deal_id":"{D}","doc_type":"NOPE"}   | 422  |
      | {"deal_id":"NOPE","doc_type":"CI"}    | 404  |

  @TC-DOC-004 @P1 @positive
  Scenario Outline: Read documents
    Given a document "DOC" exists for the deal
    When I call "GET <path>"
    Then the response status is 200

    Examples:
      | path                         |
      | /docugen/documents/{DOC}     |
      | /docugen/documents/{DOC}/file |
      | /docugen/deals/{D}/documents |

  @TC-DOC-005 @P2 @negative
  Scenario Outline: Unknown documents
    When I call "GET <path>"
    Then the response status is 404

    Examples:
      | path                        |
      | /docugen/documents/NOPE     |
      | /docugen/documents/NOPE/file |

  @TC-DOC-006 @P1 @exception @needs:fake-connector
  Scenario: A Drive failure on download answers 502
    Given a document "DOC" exists for the deal
    And the connector "google_drive" fails with "HTTP 500"
    When I call "GET /docugen/documents/{DOC}/file"
    Then the response status is 502

  @TC-DOC-007 @P1 @exception
  Scenario: Without Drive access documents still render to local storage
    When I call "POST /docugen/documents" with body:
      """
      {"deal_id":"{D}","doc_type":"CI"}
      """
    Then the response status is 201
    And a warning about local storage is logged

  @TC-DOC-008 @P1 @positive
  Scenario: Customs filing through DocuGen
    When I call "POST /docugen/customs/file" with body:
      """
      {"deal_id":"{D}"}
      """
    Then the response status is 201

  @TC-DOC-009 @P2 @negative
  Scenario Outline: Customs filing errors
    When I call "POST /docugen/customs/file" with body:
      """
      <body>
      """
    Then the response status is <code>

    Examples:
      | body                | code |
      | {}                  | 422  |

  @TC-DOC-013 @P2 @negative
  Scenario: Customs filing for an unknown deal answers 404
    When I call "POST /docugen/customs/file" with body:
      """
      {"deal_id":"NOPE"}
      """
    Then the response status is 404

  @TC-DOC-010 @P1 @positive
  Scenario Outline: Timeline and evidence pack
    When I call "GET <path>"
    Then the response status is 200

    Examples:
      | path                                 |
      | /deals/{D}/timeline                  |
      | /deals/{D}/evidence-pack?format=json |
      | /deals/{D}/evidence-pack?format=html |

  @TC-DOC-011 @P1 @positive
  Scenario: The evidence pack carries a content hash
    When I call "GET /deals/{D}/evidence-pack?format=json"
    Then the response field "content_hash" is present

  @TC-DOC-012 @P2 @negative
  Scenario Outline: Timeline and evidence for an unknown deal
    When I call "GET <path>"
    Then the response status is 404

    Examples:
      | path                        |
      | /deals/NOPE/timeline        |
      | /deals/NOPE/evidence-pack   |
```

### Phase 9: Freight and customs

```gherkin
@phase:freight @api @flow
Feature: Freight, customs and carrier milestones

  Background:
    Given I am "staff"

  @TC-FRT-001 @P1 @positive
  Scenario: Quote freight
    When I call "POST /shield-link/freight/quote" with body:
      """
      {"origin_port":"Chennai","destination_port":"Jebel Ali","quantity_mt":20}
      """
    Then the response status is 200

  @TC-FRT-002 @P1 @exception @needs:fake-connector
  Scenario: A provider failure on the quote answers 502
    Given the connector "cogoport" fails with "HTTP 500"
    When I call "POST /shield-link/freight/quote" with body:
      """
      {"origin_port":"Chennai","destination_port":"Jebel Ali","quantity_mt":20}
      """
    Then the response status is 502

  @TC-FRT-003 @P0 @positive
  Scenario: Approving freight ships the goods and opens the customs gate
    Given a deal "D" at status "QC_PASSED" from fixture "FX-STD" with loading recorded
    When "staff" approves the pending "BOOK_FREIGHT" gate
    Then deal "D" has status "IN_TRANSIT"
    And a pending gate "FILE_CUSTOMS" exists at level "L0"
    And an e-mail containing "Shipment booked" is composed for the "buyer"

  @TC-FRT-004 @P0 @negative
  Scenario: Rejecting freight raises an exception
    Given a deal "D" at status "QC_PASSED" from fixture "FX-STD" with loading recorded
    When "staff" rejects the pending "BOOK_FREIGHT" gate
    Then deal "D" has status "QC_PASSED"
    And an exception is recorded with "deal_id" equal to "{D}"

  @TC-FRT-005 @P0 @negative
  Scenario Outline: Freight needs a passed inspection
    Given a deal "D" at status "<status>" from fixture "FX-STD"
    When I call "POST /shield-link/freight" with body:
      """
      {"deal_id":"{D}","provider":"cogoport"}
      """
    Then the response status is 422

    Examples:
      | status                  |
      | SUPPLIER_DISPATCH_READY |
      | DISPUTE                 |
      | IN_TRANSIT              |

  @TC-FRT-006 @P1 @negative
  Scenario Outline: Freight booking errors
    When I call "POST /shield-link/freight" with body:
      """
      <body>
      """
    Then the response status is <code>

    Examples:
      | body                | code |
      | {}                  | 422  |
      | {"deal_id":"NOPE"}  | 404  |

  @TC-FRT-007 @P1 @exception @needs:fake-connector
  Scenario: A carrier failure on booking answers 502
    Given a deal "D" at status "QC_PASSED" from fixture "FX-STD"
    And the connector "cogoport" fails with "HTTP 500"
    When I call "POST /shield-link/freight" with body:
      """
      {"deal_id":"{D}","provider":"cogoport"}
      """
    Then the response status is 502
    And deal "D" has status "QC_PASSED"

  @TC-FRT-008 @P0 @positive
  Scenario: Approving customs files the Shipping Bill
    Given a deal "D" at status "IN_TRANSIT" from fixture "FX-STD"
    When "staff" approves the pending "FILE_CUSTOMS" gate
    Then a "CHA" order exists for the deal

  @TC-FRT-009 @P1 @negative
  Scenario Outline: Customs booking errors
    When I call "POST /shield-link/cha" with body:
      """
      <body>
      """
    Then the response status is <code>

    Examples:
      | body                | code |
      | {}                  | 422  |
      | {"deal_id":"NOPE"}  | 404  |

  @TC-FRT-010 @P0 @compliance
  Scenario Outline: Customs is filed only between QC pass and clearance
    Given a deal "D" at status "<status>" from fixture "FX-STD"
    When I call "POST /shield-link/cha" with body:
      """
      {"deal_id":"{D}"}
      """
    Then the response status is <code>

    Examples:
      | status     | code |
      | MATCHING   | 422  |
      | CANCELLED  | 422  |
      | PAID       | 422  |
      | QC_PASSED  | 201  |
      | IN_TRANSIT | 201  |
      | CLEARED    | 201  |

  @TC-FRT-011 @P0 @positive
  Scenario Outline: Carrier milestones
    Given a deal "D" at status "IN_TRANSIT" from fixture "FX-STD"
    When the carrier reports "<milestone>"
    Then deal "D" has status "<status>"

    Examples:
      | milestone  | status     |
      | DEPARTED   | IN_TRANSIT |
      | IN_TRANSIT | IN_TRANSIT |
      | ARRIVED    | CLEARED    |
      | CLEARED    | CLEARED    |
      | DELIVERED  | CLEARED    |

  @TC-FRT-012 @P0 @positive
  Scenario: Arrival opens the T3 and payout gates
    Given a deal "D" at status "IN_TRANSIT" from fixture "FX-STD" with T1 and T2 released
    When the carrier reports "ARRIVED"
    Then deal "D" has status "CLEARED"
    And a pending gate "RELEASE_ESCROW_TRANCHE" exists at level "L0"
    And the gate "RELEASE_ESCROW_TRANCHE" amount is 5000
    And a pending gate "RELEASE_SUPPLIER_PAYOUT" exists at level "L0"
    And an e-mail containing "Cargo cleared" is composed for the "buyer"

  @TC-FRT-013 @P1 @edge
  Scenario: An older milestone after a newer one is ignored
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" at milestone "ARRIVED"
    When I call "POST /shield-link/freight/{FRT_REF}/milestone" with body:
      """
      {"milestone":"DEPARTED"}
      """
    Then the response status is 200
    And the response field "status" is "ignored"

  @TC-FRT-014 @P1 @negative
  Scenario Outline: Manual milestone errors
    When I call "POST /shield-link/freight/<ref>/milestone" with body:
      """
      <body>
      """
    Then the response status is <code>

    Examples:
      | ref       | body                     | code |
      | NOPE      | {"milestone":"ARRIVED"}  | 404  |
      | {FRT_REF} | {"milestone":"LOST"}     | 422  |
      | {FRT_REF} | {}                       | 422  |

  @TC-FRT-015 @P0 @compliance @negative
  Scenario: A different seal on the bill of lading stops the money
    Given a deal "D" at status "IN_TRANSIT" from fixture "FX-STD" with inspector seal "SEAL-1"
    When the carrier reports "DEPARTED" with seal "SEAL-999"
    Then the compliance hold "seal_mismatch" is open
    And an exception is recorded with "source_event" equal to "FREIGHT_MILESTONE→SEAL_MISMATCH"

  @TC-FRT-016 @P0 @compliance @positive
  Scenario: The matching seal raises nothing
    Given a deal "D" at status "IN_TRANSIT" from fixture "FX-STD" with inspector seal "SEAL-1"
    When the carrier reports "DEPARTED" with seal "SEAL-1"
    Then the compliance hold "seal_mismatch" is not open
    And no exception is recorded

  @TC-FRT-017 @P0 @compliance @money @negative
  Scenario Outline: A block-level hold stops every money path
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with an open "seal_mismatch" hold
    When <action>
    Then the response status is <code>
    And the ledger is unchanged

    Examples:
      | action                                                                 | code |
      | "staff" tries to approve the pending "RELEASE_ESCROW_TRANCHE" gate     | 409  |
      | "staff" tries to approve the pending "RELEASE_SUPPLIER_PAYOUT" gate    | 409  |
      | I call the tranche release directly for "T3"                           | 422  |
      | I call the payout directly for 1000                                    | 422  |
      | I call the buyer acceptance directly                                   | 422  |

  @TC-FRT-018 @P0 @compliance @money @positive
  Scenario: A refund stays possible during a block
    Given a deal "D" at status "DISPUTE" from fixture "FX-STD" with an open "seal_mismatch" hold
    When I call with its approved gate "POST /escrow-guard/escrows/{ESC}/refund" with body:
      """
      {"reason":"seal"}
      """
    Then the response status is 200

  @TC-FRT-019 @P0 @compliance @positive
  Scenario: A false seal alarm is cleared with an audit trail
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with an open "seal_mismatch" hold
    When I call "POST /deals/{D}/compliance/holds/seal_mismatch/clear" with body:
      """
      {"reason":"Carrier typed the seal wrongly; confirmed by e-mail","evidence_ref":"mail-1"}
      """
    Then the response status is 200
    And the compliance hold "seal_mismatch" is not open
    And the clearance is recorded with the staff user's e-mail

  @TC-FRT-020 @P0 @compliance @negative
  Scenario Outline: Clearing a hold follows the rules
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with an open "seal_mismatch" hold
    When I call "POST /deals/{D}/compliance/holds/<code>/clear" with body:
      """
      <body>
      """
    Then the response status is <status>

    Examples:
      | code                  | body                                     | status |
      | seal_mismatch         | {"reason":"short"}                       | 422    |
      | seal_mismatch         | {}                                       | 422    |
      | destination_embargoed | {"reason":"please let this one through"} | 409    |
      | supplier_dgft_denied  | {"reason":"please let this one through"} | 409    |

  @TC-FRT-021 @P1 @negative
  Scenario: Clearing a seal hold that is not open answers 404
    Given a deal "D" at status "CLEARED" from fixture "FX-STD"
    When I call "POST /deals/{D}/compliance/holds/seal_mismatch/clear" with body:
      """
      {"reason":"Nothing is actually open on this deal"}
      """
    Then the response status is 404

  @TC-FRT-022 @P1 @positive
  Scenario Outline: Tracking poll
    Given a deal "D" at status "<status>" from fixture "FX-STD"
    When I call "POST /shield-link/deals/{D}/track"
    Then the response status is 200
    And the response field "status" is "<result>"

    Examples:
      | status   | result     |
      | MATCHING | no_booking |

  @TC-FRT-023 @P1 @exception @needs:time
  Scenario: A long stall raises an exception
    Given a deal "D" at status "IN_TRANSIT" from fixture "FX-STD"
    And "updated_at" of the deal was set 169 hours ago
    When the sweep "deal_watcher" runs
    Then an exception is recorded with "source_event" equal to "IN_TRANSIT:stalled"
```

### Phase 10: Escrow release and buyer acceptance

```gherkin
@phase:settlement @api @flow @money
Feature: Staged release and the buyer's acceptance window

  Background:
    Given I am "staff"

  @TC-REL-001 @P0 @negative
  Scenario: Tranches release strictly in order
    Given a deal "D" at status "QC_PASSED" from fixture "FX-STD" with T1 unreleased
    When I call with its approved gate "POST /escrow-guard/escrows/{ESC}/release" with body:
      """
      {"tranche_id":"T2","proof_refs":["x"]}
      """
    Then the response status is 422
    And the response detail contains "T1"
    And tranche "T2" has status "PENDING"

  @TC-REL-002 @P0 @negative
  Scenario: The final tranche cannot be released as a normal tranche
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with T1, T2 and T3 released
    When I call with its approved gate "POST /escrow-guard/escrows/{ESC}/release" with body:
      """
      {"tranche_id":"T4","proof_refs":["x"]}
      """
    Then the response status is 422
    And tranche "T4" has status "PENDING"

  @TC-REL-003 @P1 @negative
  Scenario Outline: Release errors with an approved gate named
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with an approved tranche gate "{APPR}"
    When I call "POST /escrow-guard/escrows/<escrow>/release" with body:
      """
      <body>
      """
    Then the response status is <code>

    Examples:
      | escrow | body                                       | code |
      | NOPE   | {"tranche_id":"T3"}                        | 403  |
      | {ESC}  | {"tranche_id":"T9","approval_id":"{APPR}"} | 403  |
      | {ESC}  | {}                                         | 422  |

  @TC-REL-004 @P0 @negative
  Scenario Outline: No release in a blocking deal status
    Given a deal "D" at status "<status>" from fixture "FX-STD"
    When I call with its approved gate "POST /escrow-guard/escrows/{ESC}/release" with body:
      """
      {"tranche_id":"T1","proof_refs":["x"]}
      """
    Then the response status is 422

    Examples:
      | status    |
      | DISPUTE   |
      | CANCELLED |
      | REFUNDED  |
      | QC_FAILED |

  @TC-REL-005 @P1 @edge
  Scenario: A release is safe to repeat
    Given a deal "D" at status "SUPPLIER_DISPATCH_READY" from fixture "FX-STD" with T1 released
    When the tranche callback for "T1" runs again
    Then the ledger field "released_total" is 7500

  @TC-REL-006 @P0 @money @security @negative
  Scenario Outline: Money cannot be moved around its approval gate
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with T1 and T2 released
    When I call "POST <path>" with body:
      """
      <body>
      """
    Then the response status is 403
    And the ledger is unchanged

    Examples:
      | path                               | body                                                                  |
      | /escrow-guard/escrows/{ESC}/release | {"tranche_id":"T3","proof_refs":["x"]}                               |
      | /escrow-guard/escrows/{ESC}/accept | {}                                                                    |
      | /escrow-guard/payouts              | {"deal_id":"{D}","payee_id":"S","amount":100,"idempotency_key":"p"}   |

  @TC-REL-025 @P0 @money @positive
  Scenario: A failed approved release can be retried by naming its gate
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with an approved "RELEASE_ESCROW_TRANCHE" gate for "T3" whose callback failed
    When I call with its approved gate "POST /escrow-guard/escrows/{ESC}/release" with body:
      """
      {"tranche_id":"T3","proof_refs":["x"],"approval_id":"{APPR}"}
      """
    Then the response status is 200
    And tranche "T3" has status "RELEASED"

  @TC-REL-026 @P0 @money @security @negative
  Scenario Outline: An approval covers only the money it named
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with an approval "{APPR}" in state "<state>"
    When I call "POST <path>" with body:
      """
      <body>
      """
    Then the response status is 403

    Examples:
      | state             | path                                | body                                                                                     |
      | pending           | /escrow-guard/escrows/{ESC}/release | {"tranche_id":"T3","approval_id":"{APPR}"}                                               |
      | rejected          | /escrow-guard/escrows/{ESC}/release | {"tranche_id":"T3","approval_id":"{APPR}"}                                               |
      | approved for T3   | /escrow-guard/escrows/{ESC}/release | {"tranche_id":"T4","approval_id":"{APPR}"}                                               |
      | approved for 1000 | /escrow-guard/payouts               | {"deal_id":"{D}","payee_id":"S","amount":9000,"idempotency_key":"k","approval_id":"{APPR}"} |
      | for another action | /escrow-guard/escrows/{ESC}/accept | {"approval_id":"{APPR}"}                                                                 |

  @TC-REL-027 @P0 @money @positive
  Scenario: An operator relays the buyer's acceptance through a gate
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with the window open
    When I call "POST /escrow-guard/escrows/{ESC}/request-acceptance" with body:
      """
      {"notes":"buyer phoned"}
      """
    Then the response status is 202
    And a pending gate "CONFIRM_BUYER_ACCEPTANCE" exists at level "L0"
    And tranche "T4" has status "PENDING"
    When "staff" approves the pending "CONFIRM_BUYER_ACCEPTANCE" gate
    Then tranche "T4" has status "RELEASED"

  @TC-REL-028 @P1 @negative
  Scenario Outline: Relaying acceptance needs a live escrow
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with <state>
    When I call "POST /escrow-guard/escrows/<escrow>/request-acceptance" with body:
      """
      {}
      """
    Then the response status is <code>

    Examples:
      | state                 | escrow | code |
      | a disputed escrow     | {ESC}  | 422  |
      | the final tranche out | {ESC}  | 422  |
      | no change             | NOPE   | 404  |

  @TC-REL-007 @P1 @exception @needs:fake-connector
  Scenario: A provider failure on release answers 502 and releases nothing
    Given a deal "D" at status "SUPPLIER_DISPATCH_READY" from fixture "FX-STD" with T1 unreleased
    And the connector "tazapay" fails with "HTTP 500"
    When I call with its approved gate "POST /escrow-guard/escrows/{ESC}/release" with body:
      """
      {"tranche_id":"T1","proof_refs":["x"]}
      """
    Then the response status is 502
    And tranche "T1" has status "PENDING"

  @TC-REL-008 @P0 @positive
  Scenario: A later tranche approved first is re-gated once its predecessor releases
    Given a deal "D" at status "QC_PASSED" from fixture "FX-STD" with T1 pending approval
    When "staff" approves the pending "RELEASE_ESCROW_TRANCHE" gate
    Then tranche "T1" has status "RELEASED"
    And a gate for tranche "T2" is pending

  @TC-REL-009 @P0 @positive
  Scenario: Delivery opens the buyer's window
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with T1, T2 and T3 released
    When the carrier reports "DELIVERED"
    Then the escrow has an inspection deadline
    And an e-mail containing "please confirm acceptance" is composed for the "buyer"

  @TC-REL-010 @P1 @positive
  Scenario: An operator confirms delivery
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with T1, T2 and T3 released
    When I call "POST /escrow-guard/escrows/{ESC}/open-window" with body:
      """
      {"reason":"buyer phoned"}
      """
    Then the response status is 200
    And the response field "inspection_deadline" is present

  @TC-REL-011 @P1 @positive
  Scenario: Delivery reported before the last proof tranche opens the window on that release
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with T1 and T2 released and delivery recorded
    When "staff" approves the pending "RELEASE_ESCROW_TRANCHE" gate
    Then the escrow has an inspection deadline

  @TC-REL-012 @P1 @edge @needs:time
  Scenario: Clearance without delivery news opens the window after the fallback period
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with T1, T2 and T3 released
    And "updated_at" of the deal was set 22 days ago
    When the sweep "deal_watcher" runs
    Then the escrow has an inspection deadline

  @TC-REL-013 @P0 @positive
  Scenario: Buyer acceptance releases the final tranche
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with the window open
    When I call with its approved gate "POST /escrow-guard/escrows/{ESC}/accept" with body:
      """
      {"accepted_by":"buyer"}
      """
    Then the response status is 200
    And tranche "T4" has status "RELEASED"
    And the escrow status is "RELEASED"

  @TC-REL-014 @P0 @email @positive
  Scenario: "We accept the goods" by e-mail needs operator confirmation
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with the window open
    When the buyer replies "We accept the goods" from the known address
    Then a pending gate "CONFIRM_BUYER_ACCEPTANCE" exists at level "L0"
    And tranche "T4" has status "PENDING"
    When "staff" approves the pending "CONFIRM_BUYER_ACCEPTANCE" gate
    Then tranche "T4" has status "RELEASED"

  @TC-REL-015 @P1 @positive
  Scenario: Rejecting the acceptance keeps the window running
    Given a pending gate "CONFIRM_BUYER_ACCEPTANCE" exists on a deal with the window open
    When "staff" rejects the pending "CONFIRM_BUYER_ACCEPTANCE" gate
    Then tranche "T4" has status "PENDING"
    And no exception is recorded

  @TC-REL-016 @P0 @positive @needs:time
  Scenario: Silence until the deadline auto-releases the final tranche
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with the window open
    And "inspection_deadline" of the deal's escrow was set 1 hours ago
    When the sweep "escrow_watcher" runs
    Then tranche "T4" has status "RELEASED"

  @TC-REL-017 @P0 @positive
  Scenario: A dispute inside the window stops the release
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with the window open
    When I call "POST /escrow-guard/escrows/{ESC}/dispute" with body:
      """
      {"reason":"goods damaged","raised_by":"buyer"}
      """
    Then the response status is 200
    And the response field "disputed" is "true"
    Given "inspection_deadline" of the deal's escrow was set 1 hours ago
    When the sweep "escrow_watcher" runs
    Then tranche "T4" has status "PENDING"

  @TC-REL-018 @P0 @negative
  Scenario: Acceptance is refused while disputed
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with a disputed escrow
    When I call with its approved gate "POST /escrow-guard/escrows/{ESC}/accept" with body:
      """
      {}
      """
    Then the response status is 422

  @TC-REL-019 @P1 @positive
  Scenario: Clearing a dispute lets the flow continue
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with a disputed escrow
    When I call "POST /escrow-guard/escrows/{ESC}/clear-dispute" with body:
      """
      {"reason":"re-inspection agreed"}
      """
    Then the response status is 200
    And the response field "disputed" is "false"

  @TC-REL-020 @P1 @negative
  Scenario Outline: Escrow action errors
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD"
    When I call "POST /escrow-guard/escrows/<escrow>/<action>" with body:
      """
      <body>
      """
    Then the response status is <code>

    Examples:
      | escrow | action        | body               | code |
      | NOPE   | accept        | {}                 | 403  |
      | NOPE   | dispute       | {"reason":"x"}     | 404  |
      | NOPE   | open-window   | {"reason":"x"}     | 404  |
      | NOPE   | clear-dispute | {"reason":"x"}     | 404  |
      | NOPE   | refund        | {}                 | 403  |
      | {ESC}  | refund        | {"reason":"x"}     | 403  |
      | {ESC}  | accept        | {}                 | 403  |
      | {ESC}  | dispute       | {"reason":"x"}     | 422  |
      | {ESC}  | dispute       | {}                 | 422  |
      | {ESC}  | open-window   | {}                 | 422  |

  @TC-REL-021 @P0 @positive
  Scenario: Refund is limited to the undisbursed balance
    Given a deal "D" at status "DISPUTE" from fixture "FX-STD" with T1 and T2 released
    When I call with its approved gate "POST /escrow-guard/escrows/{ESC}/refund" with body:
      """
      {"reason":"cancelled"}
      """
    Then the ledger field "refunded_total" is 7500
    And the ledger field "outstanding" is 0

  @TC-REL-022 @P1 @edge
  Scenario: A second refund changes nothing
    Given a deal "D" at status "REFUNDED" from fixture "FX-STD"
    When I call with its approved gate "POST /escrow-guard/escrows/{ESC}/refund" with body:
      """
      {}
      """
    Then the response status is 200
    And the ledger is unchanged

  @TC-REL-023 @P1 @negative
  Scenario: A fully released escrow has nothing to refund
    Given a deal "D" at status "PAID" from fixture "FX-STD" fully settled
    When I call with its approved gate "POST /escrow-guard/escrows/{ESC}/refund" with body:
      """
      {}
      """
    Then the response status is 422

  @TC-REL-024 @P1 @positive
  Scenario: Inspection trigger set to proof restores the older timing
    Given the setting "INSPECTION_WINDOW_TRIGGER" is "proof"
    And a deal "D" at status "CLEARED" from fixture "FX-STD" with T1 and T2 released
    When "staff" approves the pending "RELEASE_ESCROW_TRANCHE" gate
    Then the escrow has an inspection deadline
```

### Phase 11: Supplier payout and cash reconciliation

```gherkin
@phase:cash @api @flow @money
Feature: Supplier payout, settlement and the books

  Background:
    Given I am "staff"

  @TC-PAY-001 @P0 @positive
  Scenario Outline: Payouts follow released money and the holdback
    Given a deal "D" at status "CLEARED" from fixture "<fx>" with the payout gate open
    When the deal is driven to settlement
    Then the payout gate amounts in order are "<gates>"
    And deal "D" has status "PAID"
    And the ledger field "payouts_total" is 20000
    And the ledger field "realized_margin" is <margin>
    And the ledger field "reconciled" is "true"

    Examples:
      | fx      | gates                  | margin |
      | FX-STD  | 17325, 675, 2000       | 4750   |
      | FX-THIN | 15246, 2754, 2000      | 1780   |

  @TC-PAY-002 @P0 @positive
  Scenario: The deal cannot reach PAID before the buyer accepts
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with T1, T2 and T3 released
    When every open payout gate is approved and completed
    Then deal "D" has status "CLEARED"
    And the ledger field "payouts_total" is 18000
    And the ledger field "retained_until_acceptance" is 2000
    And the ledger field "available_for_payout" is 0
    And no gate "RELEASE_SUPPLIER_PAYOUT" exists with status "PENDING"

  @TC-PAY-003 @P0 @negative
  Scenario Outline: The escrow service refuses to pay past its limits
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with T1, T2 and T3 released
    When I call with its approved gate "POST /escrow-guard/payouts" with body:
      """
      {"deal_id":"{D}","payee_id":"SUP","amount":<amount>,"total_owed":20000,"idempotency_key":"p-<amount>"}
      """
    Then the response status is 422
    And the response detail contains "<detail>"

    Examples:
      | amount   | detail                     |
      | 18000.02 | held back                  |
      | 20000    | held back                  |
      | 22275.02 | exceeds funds released     |
      | 0        | amount                     |
      | -1       | amount                     |

  @TC-PAY-004 @P0 @positive
  Scenario: A payout up to the holdback is accepted
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with T1, T2 and T3 released
    When I call with its approved gate "POST /escrow-guard/payouts" with body:
      """
      {"deal_id":"{D}","payee_id":"SUP","amount":18000,"total_owed":20000,"idempotency_key":"p1"}
      """
    Then the response status is 201
    And the response field "status" is "PROCESSING"

  @TC-PAY-005 @P0 @positive
  Scenario: Acceptance frees the holdback
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with 18000 paid and the window open
    When the buyer's acceptance is recorded
    Then the ledger field "retained_until_acceptance" is 0
    And a pending gate "RELEASE_SUPPLIER_PAYOUT" exists at level "L0"
    And the gate "RELEASE_SUPPLIER_PAYOUT" amount is 2000

  @TC-PAY-006 @P0 @positive @needs:time
  Scenario: A lapsed window frees the holdback
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with 18000 paid and the window open
    And "inspection_deadline" of the deal's escrow was set 1 hours ago
    When the sweep "escrow_watcher" runs
    Then the ledger field "retained_until_acceptance" is 0

  @TC-PAY-007 @P0 @negative
  Scenario: A dispute keeps the holdback and blocks payouts
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with 18000 paid and a disputed escrow
    When I call with its approved gate "POST /escrow-guard/payouts" with body:
      """
      {"deal_id":"{D}","payee_id":"SUP","amount":100,"total_owed":20000,"kind":"supplier_balance","idempotency_key":"p2"}
      """
    Then the response status is 422
    And the response detail contains "disputed"
    And the ledger field "outstanding" is 2500

  @TC-PAY-008 @P1 @positive
  Scenario: The holdback can be switched off
    Given the setting "SUPPLIER_RETENTION_ENABLED" is "false"
    And a deal "D" at status "CLEARED" from fixture "FX-STD" with T1, T2 and T3 released
    When I call with its approved gate "POST /escrow-guard/payouts" with body:
      """
      {"deal_id":"{D}","payee_id":"SUP","amount":20000,"total_owed":20000,"idempotency_key":"p3"}
      """
    Then the response status is 201

  @TC-PAY-009 @P1 @edge
  Scenario: No holdback applies when the PO value is unknown
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with T1, T2 and T3 released and no PO on record
    When I call with its approved gate "POST /escrow-guard/payouts" with body:
      """
      {"deal_id":"{D}","payee_id":"SUP","amount":22275,"idempotency_key":"p4"}
      """
    Then the response status is 201

  @TC-PAY-010 @P0 @edge
  Scenario: The same idempotency key pays once
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with T1, T2 and T3 released
    When the same payout request is sent twice with one idempotency key
    Then exactly 1 payout exists for the deal

  @TC-PAY-011 @P0 @negative
  Scenario Outline: A main payout needs cleared cargo
    Given a deal "D" at status "<status>" from fixture "FX-STD"
    When I call with its approved gate "POST /escrow-guard/payouts" with body:
      """
      {"deal_id":"{D}","payee_id":"SUP","amount":100,"idempotency_key":"p5"}
      """
    Then the response status is 422

    Examples:
      | status        |
      | ESCROW_FUNDED |
      | QC_PASSED     |
      | IN_TRANSIT    |
      | DISPUTE       |

  @TC-PAY-012 @P1 @negative
  Scenario Outline: Payout errors
    When I call "POST /escrow-guard/payouts" with body:
      """
      <body>
      """
    Then the response status is <code>

    Examples:
      | body                                                                 | code |
      | {}                                                                   | 422  |
      | {"deal_id":"NOPE","payee_id":"S","amount":1,"idempotency_key":"k"}   | 403  |

  @TC-PAY-013 @P1 @exception @needs:fake-connector
  Scenario: A rail failure marks the payout failed and frees the money
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with T1, T2 and T3 released
    And the connector "payoneer" fails with "HTTP 500"
    When I call with its approved gate "POST /escrow-guard/payouts" with body:
      """
      {"deal_id":"{D}","payee_id":"SUP","amount":1000,"total_owed":20000,"idempotency_key":"p6"}
      """
    Then the response status is 502
    And the payout has status "FAILED"
    And the ledger field "payouts_pending" is 0

  @TC-PAY-014 @P0 @positive
  Scenario: A partial completed payout leaves the deal CLEARED and gates the balance
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with a processing payout of 17325
    When the provider "payoneer" sends "/escrow-guard/webhooks/payout" with signature "valid" and body:
      """
      {"ext_ref":"{PAY_REF}","status":"COMPLETED","event_id":"p-ev1"}
      """
    Then the response status is 200
    And deal "D" has status "CLEARED"
    And a pending gate "RELEASE_SUPPLIER_PAYOUT" exists at level "L0"

  @TC-PAY-015 @P0 @positive
  Scenario: The completing payout closes the deal and tells both sides
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" fully released with a processing balance payout
    When the provider "payoneer" sends "/escrow-guard/webhooks/payout" with signature "valid" and body:
      """
      {"ext_ref":"{PAY_REF}","status":"COMPLETED","event_id":"p-ev2"}
      """
    Then deal "D" has status "PAID"
    And an e-mail containing "Order completed" is composed for the "buyer"
    And an e-mail containing "Order completed" is composed for the "supplier"

  @TC-PAY-016 @P0 @edge
  Scenario: A replayed payout webhook is not applied again
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with a payout completed by event "p-ev1"
    When the provider "payoneer" sends "/escrow-guard/webhooks/payout" with signature "valid" and body:
      """
      {"ext_ref":"{PAY_REF}","status":"COMPLETED","event_id":"p-ev1"}
      """
    Then the response status is 200
    And the response field "replayed" is "true"

  @TC-PAY-017 @P0 @exception
  Scenario: A failed payout raises an exception and leaves the deal for retry
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with a processing payout of 17325
    When the provider "payoneer" sends "/escrow-guard/webhooks/payout" with signature "valid" and body:
      """
      {"ext_ref":"{PAY_REF}","status":"FAILED","event_id":"p-ev3"}
      """
    Then the response status is 200
    And deal "D" has status "CLEARED"
    And an exception is recorded with "deal_id" equal to "{D}"
    And the ledger field "payouts_pending" is 0

  @TC-PAY-018 @P1 @positive
  Scenario: Rejecting a payout gate raises an exception and pays nothing
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with the payout gate open
    When "staff" rejects the pending "RELEASE_SUPPLIER_PAYOUT" gate
    Then the ledger field "payouts_pending" is 0
    And an exception is recorded with "deal_id" equal to "{D}"

  @TC-PAY-019 @P0 @positive
  Scenario: The holdback limits the gate and says so
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with T1, T2 and T3 released and 17325 paid
    Then the gate "RELEASE_SUPPLIER_PAYOUT" amount is 675
    And the gate "RELEASE_SUPPLIER_PAYOUT" has risk flag "supplier_retention_held"
    And the gate "RELEASE_SUPPLIER_PAYOUT" does not have risk flag "partial_payout_capped_by_escrow"

  @TC-LED-001 @P0 @positive
  Scenario: The ledger adds up after every step
    Given a deal "D" at status "PAID" from fixture "FX-STD" fully settled
    When I call "GET /escrow-guard/deals/{D}/ledger"
    Then the response status is 200
    And the response field "principal" is "25000"
    And the response field "released_total" is "25000"
    And the response field "fees_total" is "250"
    And the response field "net_to_portless" is "24750"
    And the response field "payouts_total" is "20000"
    And the response field "realized_margin" is "4750"
    And the response field "outstanding" is "0"
    And the response field "reconciled" is "true"
    And the deal's realized_margin_usd is 4750

  @TC-LED-002 @P0 @positive
  Scenario: The journal balances
    Given a deal "D" at status "PAID" from fixture "FX-STD" fully settled
    When I call "GET /escrow-guard/deals/{D}/journal"
    Then the response status is 200
    And the response field "balanced" is "true"
    And the journal debits equal its credits
    And the journal balance of "escrow_trust" is 0
    And the journal balance of "portless_cash" is 4750
    And the journal balance of "provider_fees" is 250
    And the journal balance of "cogs" is 20000
    And the journal balance of "supplier_payable" is 0

  @TC-LED-003 @P1 @positive
  Scenario Outline: Books for all deals and in CSV
    Given a deal "D" at status "PAID" from fixture "FX-STD" fully settled
    When I call "GET <path>"
    Then the response status is 200

    Examples:
      | path                                     |
      | /escrow-guard/journals                   |
      | /escrow-guard/journals?format=csv        |
      | /escrow-guard/ledgers                    |
      | /escrow-guard/deals/{D}/journal?format=csv |

  @TC-LED-004 @P1 @negative
  Scenario: The journal of a deal with no escrow answers 404
    When I call "GET /escrow-guard/deals/NOPE/journal"
    Then the response status is 404

  @TC-LED-005 @P1 @edge
  Scenario: The ledger of a deal with no escrow is empty, not an error
    When I call "GET /escrow-guard/deals/NOPE/ledger"
    Then the response status is 200
    And the response field "reconciled" is "true"

  @TC-LED-006 @P0 @exception @needs:time
  Scenario Outline: A broken ledger is reported
    Given a deal "D" at status "CLEARED" from fixture "FX-STD"
    And the deal's escrow has <corruption>
    When I call "GET /escrow-guard/deals/{D}/ledger"
    Then the response field "reconciled" is "false"
    And the response field "issues" is present
    When the sweep "escrow_watcher" runs
    Then an exception is recorded with "deal_id" equal to "{D}"

    Examples:
      | corruption                           |
      | released more than the principal     |
      | a tranche sum that differs           |
      | payouts above net funds received     |

  @TC-LED-007 @P1 @positive
  Scenario: Settlement notices go to both sides
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with the window open
    When the buyer's acceptance is recorded
    Then an e-mail containing "Escrow settled" is composed for the "buyer"
    And an e-mail containing "Escrow settled" is composed for the "supplier"

  @TC-LED-008 @P0 @email @security @positive
  Scenario: Neither side sees the other's name or price
    Given a deal "D" at status "PAID" from fixture "FX-STD" fully settled
    Then no e-mail to the buyer contains "Sandbox Mill", "1,000.00" or "20,000.00"
    And no e-mail to the supplier contains "Sandbox Buyer", "1,250.00" or "25,000.00"
```

### Phase 12: Approvals

```gherkin
@phase:approvals @api @flow
Feature: Approval gates, autonomy and four-eyes

  Background:
    Given I am "staff"
    And a deal "D" at status "ESCROW_FUNDED" from fixture "FX-STD"

  @TC-HIT-001 @P0 @positive
  Scenario Outline: The autonomy level decides how a gate behaves
    When I call "POST /hitl-approval/approvals" with body:
      """
      {"deal_id":"{D}","action":"SEND_DEAL_NOTIFICATION","autonomy_level":"<level>","idempotency_key":"a-<level>","callback_payload":{}}
      """
    Then the response status is 201
    And the response field "status" is "<status>"

    Examples:
      | level | status        |
      | L0    | PENDING       |
      | L1    | PENDING       |
      | L2    | AUTO_APPROVED |
      | L3    | AUTO_APPROVED |

  @TC-HIT-002 @P0 @edge
  Scenario: The same idempotency key returns the same approval
    Given an approval with idempotency key "k1" exists for the deal
    When I call "POST /hitl-approval/approvals" with body:
      """
      {"deal_id":"{D}","action":"ISSUE_PO","autonomy_level":"L0","idempotency_key":"k1"}
      """
    Then the response status is 201
    And the collection "approvals" has no duplicate key "k1"

  @TC-HIT-003 @P1 @negative
  Scenario Outline: Approval creation validation
    When I call "POST /hitl-approval/approvals" with body:
      """
      <body>
      """
    Then the response status is 422

    Examples:
      | body                                                              |
      | {}                                                                |
      | {"deal_id":"{D}","action":"NOPE"}                                 |
      | {"deal_id":"{D}","action":"ISSUE_PO","default_on_timeout":"MAYBE"} |

  @TC-HIT-004 @P1 @positive
  Scenario Outline: List and read approvals
    Given a pending gate "ISSUE_PO" exists for the deal
    When I call "GET <path>"
    Then the response status is 200

    Examples:
      | path                                     |
      | /hitl-approval/approvals                 |
      | /hitl-approval/approvals?status=PENDING  |
      | /hitl-approval/approvals?deal_id={D}     |
      | /hitl-approval/approvals/{APPR}          |

  @TC-HIT-005 @P2 @negative
  Scenario Outline: Unknown approval
    When I call "<call>" with body:
      """
      {"decision":"APPROVE","resolved_by":"x"}
      """
    Then the response status is 404

    Examples:
      | call                                    |
      | POST /hitl-approval/approvals/NOPE/resolve |

  @TC-HIT-006 @P0 @negative
  Scenario Outline: Resolve validation
    Given a pending gate "ISSUE_PO" exists for the deal
    When I call "POST /hitl-approval/approvals/{APPR}/resolve" with body:
      """
      <body>
      """
    Then the response status is 422

    Examples:
      | body                                  |
      | {}                                    |
      | {"decision":"APPROVE"}                |
      | {"decision":"MAYBE","resolved_by":"x"} |

  @TC-HIT-007 @P0 @negative
  Scenario: A resolved gate cannot be resolved again
    Given a resolved gate "ISSUE_PO" exists for the deal
    When I call "POST /hitl-approval/approvals/{APPR}/resolve" with body:
      """
      {"decision":"APPROVE","resolved_by":"x"}
      """
    Then the response status is 409

  @TC-HIT-008 @P0 @edge
  Scenario: Two operators deciding at once produce one result
    Given a pending gate "ISSUE_PO" exists for the deal
    When "staff" and "staff2" approve the gate at the same moment
    Then exactly one call answers 200
    And the collection "purchase_orders" has 1 records for the deal

  @TC-HIT-009 @P0 @money @positive
  Scenario: Four-eyes needs two different operators
    Given the setting "FOUR_EYES_THRESHOLD_USD" is "5000"
    And a deal "E" at status "SUPPLIER_DISPATCH_READY" from fixture "FX-STD" with T1 unreleased
    Then the gate "RELEASE_ESCROW_TRANCHE" has risk flag "four_eyes_required"
    When "staff" approves the pending "RELEASE_ESCROW_TRANCHE" gate
    Then the response field "status" is "PENDING"
    And the response field "approvals_remaining" is "1"
    And tranche "T1" has status "PENDING"
    When "staff2" approves the pending "RELEASE_ESCROW_TRANCHE" gate
    Then tranche "T1" has status "RELEASED"

  @TC-HIT-010 @P0 @money @negative
  Scenario: The same operator cannot sign twice
    Given the setting "FOUR_EYES_THRESHOLD_USD" is "5000"
    And a deal "E" at status "SUPPLIER_DISPATCH_READY" from fixture "FX-STD" with T1 unreleased
    And "staff" has approved the pending "RELEASE_ESCROW_TRANCHE" gate
    When "staff" approves the pending "RELEASE_ESCROW_TRANCHE" gate
    Then the response status is 409

  @TC-HIT-011 @P0 @money @security @negative
  Scenario: One signed-in user cannot satisfy four-eyes by changing the name in the request
    Given the setting "FOUR_EYES_THRESHOLD_USD" is "5000"
    And a deal "E" at status "SUPPLIER_DISPATCH_READY" from fixture "FX-STD" with T1 unreleased
    When "staff" approves the pending "RELEASE_ESCROW_TRANCHE" gate signing as "person-a"
    And "staff" approves the pending "RELEASE_ESCROW_TRANCHE" gate signing as "person-b"
    Then the response status is 409
    And tranche "T1" has status "PENDING"

  @TC-HIT-012 @P0 @money @positive
  Scenario: Four-eyes overrides auto-approval
    Given the setting "FOUR_EYES_THRESHOLD_USD" is "5000"
    And the autonomy level of "RELEASE_ESCROW_TRANCHE" is "L2"
    And a deal "E" at status "PO_ISSUED" from fixture "FX-STD"
    When the event "po.issued" for the deal is published
    Then a pending gate "RELEASE_ESCROW_TRANCHE" exists at level "L0"

  @TC-HIT-013 @P1 @edge
  Scenario Outline: Four-eyes boundary
    Given the setting "FOUR_EYES_THRESHOLD_USD" is "<threshold>"
    When a money gate of 7500 is created
    Then required approvals is <required>

    Examples:
      | threshold | required |
      | 7500      | 2        |
      | 7500.01   | 1        |

  @TC-HIT-014 @P0 @money @negative
  Scenario: One reject closes a four-eyes gate
    Given the setting "FOUR_EYES_THRESHOLD_USD" is "5000"
    And a deal "E" at status "SUPPLIER_DISPATCH_READY" from fixture "FX-STD" with T1 unreleased
    And "staff" has approved the pending "RELEASE_ESCROW_TRANCHE" gate
    When "staff2" rejects the pending "RELEASE_ESCROW_TRANCHE" gate
    Then a gate "RELEASE_ESCROW_TRANCHE" exists with status "REJECTED"
    And tranche "T1" has status "PENDING"

  @TC-HIT-015 @P0 @exception @needs:time
  Scenario Outline: An expired L1 gate takes its default
    Given an L1 gate "<action>" with default "<default>" expired 1 minutes ago
    When the sweep "hitl_timeout" runs
    Then a gate "<action>" exists with status "<status>"

    Examples:
      | action                  | default | status    |
      | SEND_NEGOTIATION_EMAIL  | APPROVE | APPROVED  |
      | RELEASE_ESCROW_TRANCHE  | REJECT  | TIMED_OUT |

  @TC-HIT-016 @P0 @money @positive
  Scenario: Money actions are capped by the money ceiling
    Given the setting "AUTONOMY_MONEY_CEILING" is "L1"
    And the autonomy level of "RELEASE_SUPPLIER_PAYOUT" is "L3"
    And a deal "E" at status "CLEARED" from fixture "FX-STD" with the payout gate open
    Then a pending gate "RELEASE_SUPPLIER_PAYOUT" exists at level "L1"

  @TC-HIT-017 @P0 @money @positive
  Scenario Outline: A value threshold auto-approves only with its preconditions
    Given the setting "AUTONOMY_MONEY_AUTO_APPROVE_BELOW_USD" is "<limit>"
    And a tranche gate of 7500 is created with <proof> and a <ledger> ledger
    Then the gate result is "<result>"

    Examples:
      | limit | proof    | ledger     | result        |
      | 10000 | proof    | reconciled | AUTO_APPROVED |
      | 7500  | proof    | reconciled | PENDING       |
      | 10000 | no proof | reconciled | PENDING       |
      | 10000 | proof    | broken     | PENDING       |

  @TC-HIT-018 @P1 @positive
  Scenario: Read and save the autonomy policy
    When I call "GET /hitl-approval/autonomy-policy"
    Then the response status is 200
    When I call "PUT /hitl-approval/autonomy-policy" with body:
      """
      {"levels":{"ISSUE_PO":"L1"},"updated_by":"qa"}
      """
    Then the response status is 200

  @TC-HIT-019 @P1 @negative
  Scenario Outline: Autonomy policy validation
    When I call "PUT /hitl-approval/autonomy-policy" with body:
      """
      <body>
      """
    Then the response status is 422

    Examples:
      | body                                            |
      | {"levels":{"ISSUE_PO":"L9"},"updated_by":"qa"}  |
      | {"levels":{"NOPE":"L1"},"updated_by":"qa"}      |

  @TC-HIT-020 @P0 @positive
  Scenario Outline: Soft rejects do not raise an exception
    Given a pending gate "<action>" exists for the deal
    When "staff" rejects the pending "<action>" gate
    Then no exception is recorded

    Examples:
      | action                         |
      | SEND_NEGOTIATION_EMAIL         |
      | CONFIRM_NEGOTIATION_AGREEMENT  |
      | SEND_BUYER_OUTREACH            |
      | CONFIRM_BUYER_ACCEPTANCE       |

  @TC-HIT-021 @P0 @exception
  Scenario: A failing approval callback is reported, not lost
    Given a pending gate "ISSUE_PO" whose callback will fail
    When "staff" approves the pending "ISSUE_PO" gate
    Then an exception is recorded with "action_required" equal to "manual_retry_or_intervention"

  @TC-HIT-022 @P1 @positive
  Scenario: The stream delivers gate events
    Given I am connected to "GET /hitl-approval/stream"
    When a gate is created
    Then an "approval_created" event arrives on the stream

  @TC-HIT-023 @P1 @positive
  Scenario: The stream replays recent events after a reconnect
    Given 3 gates were created while I was disconnected
    When I connect to "GET /hitl-approval/stream"
    Then I receive the 3 buffered events

  @TC-HIT-024 @P2 @positive
  Scenario Outline: Digest and learning
    When I call "<call>"
    Then the response status is 200

    Examples:
      | call                        |
      | GET /hitl-approval/digest   |
      | POST /hitl-approval/digest/send |
      | GET /hitl-approval/learning |

  @TC-HIT-025 @P0 @money @negative
  Scenario: A direct refund is refused
    Given a deal "D" at status "DISPUTE" from fixture "FX-STD" with T1 released
    When I call "POST /escrow-guard/escrows/{ESC}/refund" with body:
      """
      {"reason":"failed inspection"}
      """
    Then the response status is 403
    And the ledger field "refunded_total" is 0

  @TC-HIT-026 @P0 @money @positive
  Scenario: A refund is requested, approved, then paid
    Given a deal "D" at status "DISPUTE" from fixture "FX-STD" with T1 released
    When I call "POST /escrow-guard/escrows/{ESC}/request-refund" with body:
      """
      {"reason":"failed inspection"}
      """
    Then the response status is 202
    And a pending gate "REFUND_ESCROW" exists at level "L0"
    When "staff" approves the pending "REFUND_ESCROW" gate
    Then the ledger field "refunded_total" is 17500
    And deal "D" has status "REFUNDED"

  @TC-HIT-027 @P1 @negative
  Scenario: A refund request needs a reason
    Given a deal "D" at status "DISPUTE" from fixture "FX-STD" with T1 released
    When I call "POST /escrow-guard/escrows/{ESC}/request-refund" with body:
      """
      {"reason":" "}
      """
    Then the response status is 422

  @TC-HIT-028 @P0 @money @exception
  Scenario: Rejecting a refund changes nothing
    Given a deal "D" at status "DISPUTE" from fixture "FX-STD" with T1 released
    And a refund for the deal has been requested
    When "staff" rejects the pending "REFUND_ESCROW" gate
    Then the ledger field "refunded_total" is 0
    And deal "D" has status "DISPUTE"

  @TC-HIT-029 @P0 @money @negative
  Scenario: A standalone approval service refuses a two-approver gate without a token
    Given the setting "FOUR_EYES_THRESHOLD_USD" is "100"
    And the standalone approval service is running without sign-in middleware
    And a pending "RELEASE_SUPPLIER_PAYOUT" gate of 500 USD exists
    When an anonymous caller approves it signing as "alice"
    Then the response status is 401
    And the gate has no signatures

  @TC-HIT-030 @P0 @exception
  Scenario Outline: The Kill Switch stops what would leave PortLess
    Given the platform is paused by "staff" with reason "bank incident"
    When I call with its approved gate "<method> <path>" with body:
      """
      <body>
      """
    Then the response status is 423

    Examples:
      | method | path                                 | body                                    |
      | POST   | /escrow-guard/escrows/{ESC}/release  | {"tranche_id":"T1"}                     |
      | POST   | /escrow-guard/escrows/{ESC}/refund   | {"reason":"x"}                          |
      | POST   | /shield-link/cha                     | {"deal_id":"{D}"}                       |
      | POST   | /email-gateway/send                  | {"to_address":"a@b.invalid","subject":"s","body":"b"} |

  @TC-HIT-031 @P0 @exception
  Scenario: While paused no gate approves itself and approving is refused
    Given the platform is paused by "staff" with reason "bank incident"
    When I call "POST /hitl-approval/approvals" with body:
      """
      {"deal_id":"{D}","action":"SEND_DEAL_NOTIFICATION","autonomy_level":"L2","idempotency_key":"p1","callback_payload":{}}
      """
    Then the response field "status" is "PENDING"
    And the gate "SEND_DEAL_NOTIFICATION" has risk flag "platform_paused"
    When "staff" approves the pending "SEND_DEAL_NOTIFICATION" gate
    Then the response status is 423
    And a pending gate "SEND_DEAL_NOTIFICATION" exists at level "L0"

  @TC-HIT-032 @P0 @negative
  Scenario Outline: Only staff with a reason can use the Kill Switch
    Given I am "<persona>"
    When I call "POST /hitl-approval/kill-switch" with body:
      """
      <body>
      """
    Then the response status is <code>

    Examples:
      | persona | body                             | code |
      | org_user | {"paused":true,"reason":"abc"}   | 403  |
      | staff   | {"paused":true,"reason":""}      | 422  |
      | staff   | {"paused":true,"reason":"abc"}   | 200  |
```

### Phase 13: Exceptions

```gherkin
@phase:exceptions @api @flow
Feature: Exception Resolver

  Background:
    Given I am "staff"
    And a deal "D" at status "CLEARED" from fixture "FX-STD"

  @TC-EXC-001 @P0 @positive
  Scenario: Raising an exception persists it first
    When I call "POST /exception-resolver/exceptions" with body:
      """
      {"deal_id":"{D}","source_event":"manual","source_service":"qa","detail":"check"}
      """
    Then the response status is 201
    And the response field "exception_id" is present

  @TC-EXC-002 @P1 @negative
  Scenario: Exception validation
    When I call "POST /exception-resolver/exceptions" with body:
      """
      {}
      """
    Then the response status is 422

  @TC-EXC-003 @P1 @positive
  Scenario: List and read exceptions
    Given an exception "X" exists for the deal
    When I call "GET /exception-resolver/exceptions"
    Then the response status is 200
    When I call "GET /exception-resolver/exceptions/{X}"
    Then the response status is 200

  @TC-EXC-004 @P2 @negative
  Scenario: Unknown exception
    When I call "GET /exception-resolver/exceptions/NOPE"
    Then the response status is 404

  @TC-EXC-005 @P0 @positive
  Scenario Outline: Known failures use a fixed playbook without the LLM
    When an exception of category "<category>" with <context> is raised
    Then the exception decision is "<decision>"

    Examples:
      | category           | context                    | decision |
      | QC_FAILED          | first failure              | ESCALATE |
      | QC_FAILED          | failures beyond the limit  | CANCEL   |
      | PAYMENT_FAILED     | shortfall just now         | AUTO_FIX |
      | PAYMENT_FAILED     | unfunded past grace        | ESCALATE |
      | NEGOTIATION_FAILED | candidates remaining       | AUTO_FIX |
      | NEGOTIATION_FAILED | funnel exhausted           | ESCALATE |
      | DOCUMENT_ERROR     | a render failure           | AUTO_FIX |
      | CONNECTOR_ERROR    | a provider timeout         | AUTO_FIX |

  @TC-EXC-006 @P0 @exception
  Scenario Outline: With the LLM unavailable the case reaches an operator
    Given the LLM gateway <state>
    When an exception of category "UNKNOWN" is raised
    Then the exception status is "ESCALATED"
    And a pending gate "RESOLVE_EXCEPTION" exists at level "L0"

    Examples:
      | state             |
      | is not configured |
      | has no model left |

  @TC-EXC-007 @P1 @exception
  Scenario: An unreadable LLM answer is escalated
    Given the LLM gateway returns invalid JSON
    When an exception of category "UNKNOWN" is raised
    Then the exception status is "ESCALATED"

  @TC-EXC-008 @P1 @exception
  Scenario: A resolver crash leaves an UNRESOLVABLE record
    Given the resolver agent will crash
    When an exception of category "UNKNOWN" is raised
    Then the exception status is "UNRESOLVABLE"

  @TC-EXC-009 @P1 @edge
  Scenario: The resolver's own events do not start another resolver run
    When the event "deal.exception" carrying an exception id is published
    Then no new exception is recorded

  @TC-EXC-010 @P1 @positive
  Scenario: An acknowledged plan does not run anything
    Given a pending gate "RESOLVE_EXCEPTION" without a playbook
    When "staff" approves the pending "RESOLVE_EXCEPTION" gate
    Then deal "D" is unchanged
```

```gherkin
@phase:intake @api
Feature: Opportunity intake, identity and readiness
  Inputs to the matching phase and the supporting operator data.

  Background:
    Given I am "staff"

  @TC-INT-001 @P1 @positive
  Scenario Outline: Read intake data
    When I call "GET <path>"
    Then the response status is 200

    Examples:
      | path                      |
      | /trade-min/catalogue      |
      | /trade-min/sources        |
      | /trade-min/buyers         |
      | /trade-min/suppliers      |
      | /trade-min/buyers?country=IN |
      | /trade-min/opportunities  |

  @TC-INT-002 @P1 @positive
  Scenario: Save the catalogue
    When I call "PUT /trade-min/catalogue" with body:
      """
      {"lines":[{"product_id":"rice","name":"Basmati rice","hs_code":"10063020","markets":["AE"]}]}
      """
    Then the response status is 200

  @TC-INT-003 @P1 @positive
  Scenario: A scan without provider keys finishes without inventing opportunities
    When I call "POST /trade-min/scan" with body:
      """
      {"hs_codes":["10063020"],"markets":["AE"]}
      """
    Then the response status is 200
    And no simulated opportunity is returned

  @TC-INT-004 @P2 @negative
  Scenario: Scan validation
    When I call "POST /trade-min/scan" with body:
      """
      {"min_arbitrage_rank":101}
      """
    Then the response status is 422

  @TC-INT-005 @P2 @negative
  Scenario: Unknown opportunity
    When I call "GET /trade-min/opportunities/NOPE"
    Then the response status is 404

  @TC-INT-013 @P1 @positive
  Scenario: The match seed of an opportunity can be read
    Given an opportunity "O" with a scored supplier and buyer candidates
    When I call "GET /trade-min/opportunities/{O}/match-seed"
    Then the response status is 200

  @TC-INT-014 @P2 @negative
  Scenario Outline: Unknown opportunity actions
    When I call "<method> /trade-min/opportunities/NOPE/<action>"
    Then the response status is 404

    Examples:
      | method | action     |
      | GET    | match-seed |
      | POST   | convert    |

  @TC-INT-015 @P1 @positive
  Scenario: Converting an opportunity marks it converted
    Given an opportunity "O" with a scored supplier and buyer candidates
    When I call "POST /trade-min/opportunities/{O}/convert"
    Then the response status is 200

  @TC-INT-006 @P2 @negative
  Scenario: A knowledge note must be long enough
    When I call "POST /trade-min/knowledge/notes" with body:
      """
      {"text":"x"}
      """
    Then the response status is 422

  @TC-INT-007 @P2 @exception
  Scenario: A note with knowledge-service off answers 503
    When I call "POST /trade-min/knowledge/notes" with body:
      """
      {"subject_type":"buyer","subject_id":"buyer.invalid","text":"A note the agent should remember"}
      """
    Then the response status is 503

  @TC-INT-008 @P2 @positive
  Scenario: Knowledge search with the service off
    When I call "GET /trade-min/knowledge/search?q=rice"
    Then the response status is 200

  @TC-INT-009 @P2 @positive
  Scenario Outline: Organization administration
    When I call "<call>"
    Then the response status is 200

    Examples:
      | call                      |
      | GET /identity/organizations |
      | GET /identity/users       |

  @TC-INT-010 @P2 @negative
  Scenario: Creating an organization validates its name
    When I call "POST /identity/organizations" with body:
      """
      {"name":""}
      """
    Then the response status is 422

  @TC-INT-011 @P1 @positive
  Scenario: Readiness names every missing integration
    When I call "GET /health/readiness"
    Then the response status is 200
    And the response field "checks" is present

  @TC-INT-012 @P0 @security @negative
  Scenario: Production refuses to start while a readiness check fails
    Given the setting "ENV_PROFILE" is "production"
    When the backend starts without the escrow credentials
    Then the startup is refused
```

## 7. UI scenarios

Backend calls may be real or mocked. Scenarios tagged `@needs:route-mock` need a browser-level mock of the named response.

```gherkin
@phase:ui-shell @ui
Feature: Sign-in, navigation and access

  @TC-UI-001 @P0 @positive
  Scenario Outline: Each role lands on the right page
    Given the browser is signed in as "<persona>"
    When I open "/"
    Then I am on "<route>"

    Examples:
      | persona  | route  |
      | staff    | /radar |
      | org_user | /deals |

  @TC-UI-002 @P0 @security @negative
  Scenario Outline: Signed-out users are sent to sign-in
    When I open "<route>"
    Then I am on "/login"

    Examples:
      | route     |
      | /deals    |
      | /approvals |
      | /money    |

  @TC-UI-003 @P0 @security @negative
  Scenario Outline: An organization user cannot open staff pages
    Given the browser is signed in as "org_user"
    When I open "<route>"
    Then I am on "/deals"

    Examples:
      | route                |
      | /radar               |
      | /prospects           |
      | /counterparties      |
      | /deals/DEAL-1        |
      | /approvals           |
      | /money               |
      | /exceptions          |
      | /catalogue           |
      | /autonomy            |
      | /health              |
      | /admin/organizations |

  @TC-UI-004 @P1 @positive
  Scenario Outline: Staff navigation reaches every page
    Given the browser is signed in as "staff"
    When I click "<link>"
    Then I am on "<route>"

    Examples:
      | link               | route                |
      | Opportunity Radar  | /radar               |
      | Prospects          | /prospects           |
      | Buyers & Suppliers | /counterparties      |
      | Deal Board         | /deals               |
      | Approvals Inbox    | /approvals           |
      | Money & Margin     | /money               |
      | Exceptions         | /exceptions          |
      | Catalogue          | /catalogue           |
      | Autonomy Settings  | /autonomy            |
      | System Health      | /health              |
      | Organizations      | /admin/organizations |

  @TC-UI-005 @P1 @positive
  Scenario: An organization user sees only Deals in the menu
    Given the browser is signed in as "org_user"
    When I open "/deals"
    Then I see "Deals"
    And I do not see "Approvals Inbox"
    And I do not see "Money & Margin"

  @TC-UI-006 @P2 @edge
  Scenario: An unknown route returns to the home page for the role
    Given the browser is signed in as "staff"
    When I open "/not-a-page"
    Then I am on "/radar"

  @TC-UI-007 @P0 @needs:route-mock @exception
  Scenario: A rejected session signs the user out
    Given the browser is signed in as "staff"
    And the API answers 401 for "GET /deals"
    And the token refresh is refused
    When I open "/deals"
    Then I am on "/login"

  @TC-UI-008 @P1 @needs:route-mock @exception
  Scenario: A network failure during refresh keeps the user signed in
    Given the browser is signed in as "staff"
    And the token refresh fails with a network error
    When I open "/deals"
    Then I am not on "/login"

  @TC-UI-009 @P1 @edge
  Scenario: Two tabs refreshing at once keep the session
    Given two tabs signed in as "staff" with an expiring token
    When both tabs refresh the token at once
    Then exactly one refresh call is made
    And both tabs stay signed in

  @TC-UI-010 @P1 @negative
  Scenario: Wrong credentials show an error and stay on sign-in
    Given the sign-in service refuses the password
    When I open "/login"
    And I fill "Email" with "qa@portless-qa.invalid"
    And I fill "Password" with "wrong"
    And I click "Sign in"
    Then I am on "/login"
    And an error is shown

  @TC-UI-011 @P2 @positive
  Scenario: Signing out returns to sign-in
    Given the browser is signed in as "staff"
    When I click "Sign out"
    Then I am on "/login"
```

```gherkin
@phase:ui-pages @ui @needs:route-mock
Feature: Standard page states

  @TC-UI-020 @P1 @exception
  Scenario Outline: Each data page shows loading, error with retry, and empty states
    Given the browser is signed in as "staff"
    And the API is slow for "GET <api>"
    When I open "<route>"
    Then I see "Loading…"
    Given the API answers 500 for "GET <api>"
    When I open "<route>"
    Then I see "Service unavailable"
    And I see "Retry"
    Given the API answers 200 with an empty list for "GET <api>"
    When I open "<route>"
    Then I do not see "Service unavailable"

    Examples:
      | route          | api                      |
      | /radar         | /trade-min/opportunities |
      | /prospects     | /deals                   |
      | /deals         | /deals                   |
      | /approvals     | /hitl-approval/approvals |
      | /money         | /escrow-guard/ledgers    |
      | /exceptions    | /exception-resolver/exceptions |
      | /counterparties | /trade-min/buyers       |

  @TC-UI-021 @P2 @negative
  Scenario: The error state does not mention a developer address
    Given the browser is signed in as "staff"
    And the API answers 500 for "GET /deals"
    When I open "/deals"
    Then I do not see "localhost:8081"
```

```gherkin
@phase:ui-matching @ui
Feature: Radar, prospects and the match board

  Background:
    Given the browser is signed in as "staff"

  @TC-UI-030 @P1 @positive
  Scenario: Starting a prospect from an opportunity
    Given an opportunity exists
    When I open "/radar"
    And I click "Start deal"
    Then a toast says "Prospect started"
    And a deal exists at "MATCHING" for the opportunity

  @TC-UI-031 @P1 @needs:route-mock @exception
  Scenario: A failed start is reported
    Given an opportunity exists
    And the API answers 500 for "POST /deals"
    When I open "/radar"
    And I click "Start deal"
    Then a toast says "Could not start the prospect"

  @TC-UI-032 @P2 @needs:route-mock @exception
  Scenario: A failed scan is reported
    Given the API answers 500 for "POST /trade-min/scan"
    When I open "/radar"
    And I click "Scan"
    Then a toast says "Scan failed"

  @TC-UI-033 @P2 @positive
  Scenario: Prospects with nothing to show explain the next step
    Given no prospects exist
    When I open "/prospects"
    Then I see "No prospects."

  @TC-UI-034 @P1 @positive
  Scenario: The deal board shows negotiating deals onward and prospects elsewhere
    Given a deal "A" at status "MATCHING" from fixture "FX-STD"
    And a deal "B" at status "NEGOTIATING" from fixture "FX-STD"
    When I open "/deals"
    Then I see deal "B"
    And I do not see deal "A"

  @TC-UI-035 @P1 @positive
  Scenario: The organization list is read-only
    Given the browser is signed in as "org_user"
    And a deal "A" at status "PI_SENT" from fixture "FX-STD" owned by "qa-org-a"
    When I open "/deals"
    Then I see deal "A"
    And the deal "A" is not a link

  @TC-UI-040 @P1 @positive
  Scenario: Add a buyer to the funnel
    Given a deal "D" at status "MATCHING" from fixture "FX-STD"
    When I open "/deals/{D}"
    And I add a buyer named "New Buyer LLC" in "AE"
    Then a toast says "New Buyer LLC added to the funnel"

  @TC-UI-041 @P1 @negative
  Scenario: A buyer needs a name and country
    Given a deal "D" at status "MATCHING" from fixture "FX-STD"
    When I open "/deals/{D}"
    And I add a buyer with no name
    Then a toast says "Company name and country are required"

  @TC-UI-042 @P1 @positive
  Scenario: Contact next buyers
    Given a deal "D" at status "MATCHING" from fixture "FX-STD" with 3 buyer candidates
    When I open "/deals/{D}"
    And I click "Contact next buyers"
    Then a toast says "introductions queued"

  @TC-UI-043 @P2 @edge
  Scenario: No candidate left to contact
    Given a deal "D" at status "MATCHING" from fixture "FX-STD"
    When I open "/deals/{D}"
    And I click "Contact next buyers"
    Then a toast says "No candidate to contact"

  @TC-UI-044 @P1 @positive
  Scenario: Add a supplier offer
    Given a deal "D" at status "MATCHING" from fixture "FX-STD"
    When I open "/deals/{D}"
    And I add a supplier offer from "mill.invalid" at "990"
    Then a toast says "Offer added — the board is re-scored"

  @TC-UI-045 @P1 @negative
  Scenario: An offer needs a supplier and price
    Given a deal "D" at status "MATCHING" from fixture "FX-STD"
    When I open "/deals/{D}"
    And I add a supplier offer with no price
    Then a toast says "Supplier id (e.g. its domain) and FOB price are required"

  @TC-UI-046 @P1 @positive
  Scenario: Save a buyer's requirement
    Given a deal "D" at status "MATCHING" from fixture "FX-STD" with buyer "C1" and supplier offer "OF1"
    When I open "/deals/{D}"
    And I save a requirement for "C1" with grade "1121"
    Then a toast says "Requirement saved — the board is re-scored"

  @TC-UI-047 @P0 @positive
  Scenario: Propose a match from the board
    Given a deal "D" at status "MATCHING" from fixture "FX-STD" with buyer "C1" and supplier offer "OF1"
    When I open "/deals/{D}"
    And I propose the match for "C1" × "OF1"
    Then a toast says "Match waiting for your approval"
    And a pending gate "CONFIRM_MATCH" exists at level "L0"

  @TC-UI-048 @P1 @negative
  Scenario: The backend's reason for refusing a match is shown
    Given a deal "D" at status "MATCHING" from fixture "FX-STD" with a blocked pair
    When I open "/deals/{D}"
    And I propose the match for the blocked pair
    Then a toast shows the backend detail

  @TC-UI-049 @P2 @positive
  Scenario Outline: Re-score the board
    Given a deal "D" at status "MATCHING" from fixture "FX-STD" with <state>
    When I open "/deals/{D}"
    And I click "Re-score"
    Then a toast says "<message>"

    Examples:
      | state                         | message                         |
      | a firm pair                   | approval opened                 |
      | no buyer and no offer         | Re-scored — nothing to propose yet |
```

```gherkin
@phase:ui-deal @ui
Feature: Deal detail page

  Background:
    Given the browser is signed in as "staff"

  @TC-UI-060 @P1 @positive
  Scenario Outline: Every tab opens
    Given a deal "D" at status "IN_TRANSIT" from fixture "FX-STD"
    When I open "/deals/{D}"
    And I open the tab "<tab>"
    Then the tab "<tab>" is selected

    Examples:
      | tab         |
      | Overview    |
      | Negotiation |
      | Logistics   |
      | Documents   |
      | Payments    |
      | Timeline    |

  @TC-UI-061 @P1 @positive
  Scenario: Correct a party's contact
    Given a deal "D" at status "MATCHING" from fixture "FX-STD"
    When I open "/deals/{D}"
    And I change the buyer's e-mail to "right@buyer.invalid"
    Then a toast says "Buyer contact updated"

  @TC-UI-062 @P1 @needs:route-mock @exception
  Scenario: A blocked screening warns the operator
    Given a deal "D" at status "MATCHING" from fixture "FX-STD"
    And enrichment answers with a blocked party
    When I open "/deals/{D}"
    And I click "Enrich"
    Then a toast says "Counterparty screening returned BLOCK — a deal exception was raised"

  @TC-UI-063 @P1 @negative
  Scenario: A manual negotiation round needs a valid price
    Given a deal "D" at status "NEGOTIATING" from fixture "FX-STD"
    When I open "/deals/{D}"
    And I open the tab "Negotiation"
    And I submit a round with price "abc"
    Then a toast says "Enter a valid counter-price"

  @TC-UI-064 @P1 @positive
  Scenario: Submit a manual negotiation round
    Given a deal "D" at status "NEGOTIATING" from fixture "FX-STD" with an open negotiation
    When I open "/deals/{D}"
    And I open the tab "Negotiation"
    And I submit a round with price "1200"
    Then a toast says "Round submitted"

  @TC-UI-065 @P1 @positive
  Scenario Outline: Decide on an e-mail draft
    Given a deal "D" at status "NEGOTIATING" from fixture "FX-STD" with a draft waiting for approval
    When I open "/deals/{D}"
    And I open the tab "Negotiation"
    And I <action> the draft
    Then a toast says "<message>"

    Examples:
      | action             | message                |
      | approve            | Email approved and sent |
      | reject             | Email rejected         |
      | modify and send    | Modified email sent    |

  @TC-UI-066 @P0 @compliance @positive
  Scenario: Record the supervised loading
    Given a deal "D" at status "QC_PASSED" from fixture "FX-STD" waiting for loading
    When I open "/deals/{D}"
    And I record loading with container "C1" and seal "S1"
    Then a toast says "Loading recorded"

  @TC-UI-067 @P0 @compliance @negative
  Scenario: Loading needs a container and a seal
    Given a deal "D" at status "QC_PASSED" from fixture "FX-STD" waiting for loading
    When I open "/deals/{D}"
    And I record loading with no seal
    Then a toast says "Container and seal numbers are both required"

  @TC-UI-068 @P1 @compliance @positive
  Scenario: Re-run the trade check
    Given a deal "D" at status "NEGOTIATION_AGREED" from fixture "FX-STD"
    When I open "/deals/{D}"
    And I click "Re-check"
    Then a toast says "Trade check:"

  @TC-UI-069 @P1 @positive
  Scenario: Record a carrier milestone
    Given a deal "D" at status "IN_TRANSIT" from fixture "FX-STD"
    When I open "/deals/{D}"
    And I record the milestone "DEPARTED"
    Then a toast says "DEPARTED recorded"

  @TC-UI-070 @P2 @edge
  Scenario: An out-of-date milestone is reported as ignored
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" at milestone "ARRIVED"
    When I open "/deals/{D}"
    And I record the milestone "DEPARTED"
    Then a toast says "Ignored — already at ARRIVED"

  @TC-UI-071 @P1 @positive
  Scenario: Poll carrier tracking without a booking
    Given a deal "D" at status "IN_TRANSIT" from fixture "FX-STD"
    When I open "/deals/{D}"
    And I click "Poll carrier tracking"
    Then a toast is shown

  @TC-UI-072 @P1 @needs:route-mock @exception
  Scenario: A document that cannot be fetched is reported
    Given a deal "D" at status "QC_PASSED" from fixture "FX-STD"
    And the API answers 502 for "GET /docugen/documents/{DOC}/file"
    When I open "/deals/{D}"
    And I open the tab "Documents"
    And I open a document
    Then a toast says "Could not fetch the document file"

  @TC-UI-073 @P1 @positive
  Scenario: Download the evidence pack
    Given a deal "D" at status "PAID" from fixture "FX-STD" fully settled
    When I open "/deals/{D}"
    And I download the evidence pack
    Then a toast says "Evidence pack downloaded"

  @TC-UI-074 @P2 @needs:route-mock @exception
  Scenario: Timeline failure is contained
    Given a deal "D" at status "IN_TRANSIT" from fixture "FX-STD"
    And the API answers 500 for "GET /deals/{D}/timeline"
    When I open "/deals/{D}"
    And I open the tab "Timeline"
    Then I see "Timeline unavailable"

  @TC-UI-075 @P2 @negative
  Scenario: An unknown deal shows an error, not a blank page
    When I open "/deals/DEAL-NOPE"
    Then an error state is shown

  @TC-UI-080 @P0 @money @positive
  Scenario Outline: Escrow actions are offered only when valid
    Given a deal "D" with an escrow in state "<state>"
    When I open "/deals/{D}"
    And I open the tab "Payments"
    Then the button "<button>" is <visibility>

    Examples:
      | state                     | button                   | visibility |
      | funded, window not open   | Confirm delivery         | shown      |
      | funded, window open       | Confirm delivery         | not shown  |
      | funded                    | Record buyer acceptance  | shown      |
      | disputed                  | Record buyer acceptance  | not shown  |
      | disputed                  | Clear the dispute        | shown      |
      | not disputed              | Clear the dispute        | not shown  |
      | fully released            | Refund                   | not shown  |

  @TC-UI-081 @P0 @money @negative
  Scenario Outline: Actions that need a reason insist on one
    Given a deal "D" with an escrow in state "funded"
    When I open "/deals/{D}"
    And I open the tab "Payments"
    And I click "<open>"
    And I click "<confirm>" with an empty reason
    Then a toast says "A reason is required"

    Examples:
      | open            | confirm       |
      | Raise a dispute | Raise dispute |

  @TC-UI-082 @P0 @money @positive
  Scenario: Relaying the buyer's acceptance opens an approval and moves no money
    Given a deal "D" with an escrow in state "funded, window open"
    When I open "/deals/{D}"
    And I open the tab "Payments"
    And I confirm "Record buyer acceptance" with "Send for approval"
    Then a toast says "Buyer acceptance sent for approval"
    And a pending gate "CONFIRM_BUYER_ACCEPTANCE" exists at level "L0"
    And tranche "T4" has status "PENDING"

  @TC-UI-083 @P1 @needs:route-mock @negative
  Scenario: The backend's refusal of an escrow action is shown
    Given a deal "D" with an escrow in state "funded, window open"
    And the API answers 422 with detail "disputed" for "POST /escrow-guard/escrows/{ESC}/request-acceptance"
    When I open "/deals/{D}"
    And I open the tab "Payments"
    And I confirm "Record buyer acceptance" with "Send for approval"
    Then a toast says "disputed"

  @TC-UI-084 @P1 @money @edge
  Scenario: The confirm button cannot be pressed twice
    Given a deal "D" with an escrow in state "funded, window open"
    And the API is slow for "POST /escrow-guard/escrows/{ESC}/request-acceptance"
    When I open "/deals/{D}"
    And I open the tab "Payments"
    And I confirm "Record buyer acceptance" with "Send for approval"
    Then the button "Working…" is disabled

  @TC-UI-085 @P0 @money @security @positive
  Scenario: Escrow actions record the signed-in operator
    Given a deal "D" with an escrow in state "funded"
    When I open "/deals/{D}"
    And I open the tab "Payments"
    And I raise a dispute with reason "goods damaged"
    Then the request's actor is the signed-in user's e-mail

  @TC-UI-086 @P1 @money @positive
  Scenario: The ledger shows the supplier holdback
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with T1, T2 and T3 released
    When I open "/deals/{D}"
    And I open the tab "Payments"
    Then I see "held back"
```

```gherkin
@phase:ui-approvals @ui
Feature: Approvals inbox

  Background:
    Given the browser is signed in as "staff"

  @TC-UI-100 @P0 @positive
  Scenario: Approve a gate
    Given a deal "D" at status "ESCROW_FUNDED" from fixture "FX-STD"
    When I open "/approvals"
    And I click "Approve"
    Then a toast says "Approved: Issue Po"
    And deal "D" has status "SUPPLIER_DISPATCH_READY"

  @TC-UI-101 @P0 @positive
  Scenario: Reject a gate with a note
    Given a deal "D" at status "ESCROW_FUNDED" from fixture "FX-STD"
    When I open "/approvals"
    And I fill "Optional note..." with "supplier changed terms"
    And I click "Reject"
    Then a toast says "Rejected: Issue Po"

  @TC-UI-102 @P1 @needs:route-mock @edge
  Scenario: A gate someone else already resolved refreshes the list
    Given a pending gate is listed
    And the API answers 409 with detail "is already 'APPROVED'" for "POST /hitl-approval/approvals/{APPR}/resolve"
    When I open "/approvals"
    And I click "Approve"
    Then a toast says "Already resolved — refreshing"

  @TC-UI-103 @P0 @money @negative
  Scenario: A compliance refusal is shown as a compliance refusal
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with an open "seal_mismatch" hold
    When I open "/approvals"
    And I approve the "Release Escrow Tranche" gate
    Then a toast says "Blocked by a compliance hold"
    And I do not see "Already resolved — refreshing"
    And the gate is still listed as pending

  @TC-UI-104 @P1 @needs:route-mock @exception
  Scenario: A server error keeps the gate open
    Given a pending gate is listed
    And the API answers 500 for "POST /hitl-approval/approvals/{APPR}/resolve"
    When I open "/approvals"
    And I click "Approve"
    Then a toast says "Failed to resolve approval"

  @TC-UI-105 @P0 @money @positive
  Scenario: Four-eyes flow in the console
    Given the setting "FOUR_EYES_THRESHOLD_USD" is "5000"
    And a deal "E" at status "SUPPLIER_DISPATCH_READY" from fixture "FX-STD" with T1 unreleased
    When I open "/approvals"
    Then I see "0 of 2 approvals"
    When I click "Approve"
    Then a toast says "more operator must countersign"
    And I see "You already approved this gate"
    And the button "Countersign" is disabled
    Given the browser is signed in as "staff2"
    When I open "/approvals"
    And I click "Countersign"
    Then a toast says "Approved: Release Escrow Tranche"

  @TC-UI-106 @P0 @money @negative
  Scenario: A same-operator refusal gets its own message
    Given the API refuses a second signature from the same operator
    When I open "/approvals"
    And I click "Countersign"
    Then a toast says "Second approver must be a different operator"

  @TC-UI-107 @P1 @positive
  Scenario Outline: Money and risk details are shown
    Given a pending gate "<action>" is listed
    When I open "/approvals"
    Then I see "<label>"

    Examples:
      | action                     | label             |
      | RELEASE_ESCROW_TRANCHE     | Proof refs        |
      | RELEASE_SUPPLIER_PAYOUT    | Payee             |
      | CONFIRM_COUNTERPARTY_RISK  | Screening         |
      | CONFIRM_BUYER_ACCEPTANCE   | Accepted by       |
      | SEND_INDICATIVE_OFFER      | Our minimum       |
      | CONFIRM_MATCH              | Chance of closing |
      | SEND_BUYER_OUTREACH        | Candidate         |

  @TC-UI-108 @P1 @positive
  Scenario Outline: The playbook's effect is explained
    Given a pending gate "RESOLVE_EXCEPTION" with playbook "<playbook>" is listed
    When I open "/approvals"
    Then I see "<label>"

    Examples:
      | playbook        | label                |
      | reinspect       | Re-inspection        |
      | cancel_unfunded | Cancel unfunded deal |

  @TC-UI-109 @P1 @positive
  Scenario: A payout limited by the holdback is described correctly
    Given a pending payout gate limited by the supplier holdback is listed
    When I open "/approvals"
    Then I do not see "capped at funds released from escrow"
    And I see "held back"

  @TC-UI-110 @P1 @positive
  Scenario: A warning shows on a gate about to expire
    Given an L1 gate expiring in 5 minutes is listed
    When I open "/approvals"
    Then I see "Expires In"
    And I see "On timeout: auto-reject"

  @TC-UI-111 @P1 @positive
  Scenario: A new gate appears without a reload
    When I open "/approvals"
    And a new gate is created
    Then the gate appears in the list

  @TC-UI-112 @P2 @positive
  Scenario Outline: Filter the inbox
    Given 1 pending and 1 resolved gate exist
    When I open "/approvals"
    And I click "<filter>"
    Then I see <count> gates

    Examples:
      | filter   | count |
      | PENDING  | 1     |
      | RESOLVED | 1     |
      | ALL      | 2     |

  @TC-UI-113 @P2 @edge
  Scenario: The pending count in the header follows the inbox
    Given 2 pending gates exist
    When I open "/approvals"
    And I click "Approve"
    Then the pending count shows 1
```

```gherkin
@phase:ui-money @ui
Feature: Money, exceptions, settings and health

  Background:
    Given the browser is signed in as "staff"

  @TC-UI-120 @P0 @money @positive
  Scenario: Money & Margin agrees with the ledgers
    Given a deal "D" at status "PAID" from fixture "FX-STD" fully settled
    When I open "/money"
    Then I see "Held principal"
    And I see "Released to PortLess (net)"
    And I see "Supplier payouts"
    And I see "Realized margin so far"
    And the page totals equal the sum of "GET /escrow-guard/ledgers"

  @TC-UI-121 @P1 @money @positive
  Scenario: Download a deal's journal
    Given a deal "D" at status "PAID" from fixture "FX-STD" fully settled
    When I open "/deals/{D}"
    And I open the tab "Payments"
    And I download the journal as CSV
    Then a CSV file is saved

  @TC-UI-122 @P1 @needs:route-mock @exception
  Scenario: A failed journal download is reported
    Given a deal "D" at status "PAID" from fixture "FX-STD" fully settled
    And the API answers 500 for "GET /escrow-guard/deals/{D}/journal"
    When I open "/deals/{D}"
    And I open the tab "Payments"
    And I download the journal as CSV
    Then a toast says "Could not download the journal"

  @TC-UI-130 @P1 @positive
  Scenario: Exceptions are listed and can be looked up
    Given an exception "X" exists
    When I open "/exceptions"
    Then I see exception "X"
    When I fill "Exception ID lookup" with "{X}"
    Then I see the detail of "X"

  @TC-UI-131 @P2 @negative
  Scenario: An unknown exception ID is reported
    When I open "/exceptions"
    And I fill "Exception ID lookup" with "EXC-NOPE"
    Then an error is shown

  @TC-UI-140 @P1 @positive
  Scenario: Save the autonomy policy
    When I open "/autonomy"
    And I set "Issue Po" to "L1"
    And I click "Save"
    Then a toast says "Autonomy policy saved"

  @TC-UI-141 @P1 @negative
  Scenario: A money threshold must be a positive number
    When I open "/autonomy"
    And I fill the money threshold with "-5"
    And I click "Save"
    Then a toast says "Money threshold must be a positive number (or empty to disable)"

  @TC-UI-142 @P2 @positive
  Scenario: Clearing overrides needs a save
    When I open "/autonomy"
    And I clear the overrides
    Then a toast says "Overrides cleared — press Save to apply"

  @TC-UI-143 @P1 @needs:route-mock @exception
  Scenario: A refused policy shows the backend's reason
    Given the API answers 422 with detail "bad level" for "PUT /hitl-approval/autonomy-policy"
    When I open "/autonomy"
    And I click "Save"
    Then a toast says "Save failed"

  @TC-UI-150 @P1 @positive
  Scenario: Save the catalogue
    When I open "/catalogue"
    And I add a product "Rice" with HS "10063020"
    And I click "Save"
    Then a toast says "Catalogue saved"

  @TC-UI-151 @P1 @negative
  Scenario Outline: Catalogue validation
    When I open "/catalogue"
    And I add <line>
    And I click "Save"
    Then a toast says "<message>"

    Examples:
      | line                   | message                                               |
      | a product with no name | Every line needs a name and an HS code of at least 4 digits |
      | a product with HS "12" | Every line needs a name and an HS code of at least 4 digits |
      | two lines with one id  | Duplicate product id                                  |

  @TC-UI-160 @P2 @positive
  Scenario: System health lists readiness
    When I open "/health"
    Then the readiness checks are listed

  @TC-UI-161 @P2 @positive
  Scenario: Sending the digest with no channel explains why
    When I open "/health"
    And I click "Send"
    Then a toast says "No delivery channel configured"

  @TC-UI-170 @P2 @positive
  Scenario: Create an organization
    When I open "/admin/organizations"
    And I create an organization named "QA Org"
    Then a toast says "Organization created"

  @TC-UI-171 @P2 @needs:route-mock @exception
  Scenario: A failed assignment is reported
    Given the API answers 500 for "PATCH /identity/users/{USR}/organization"
    When I open "/admin/organizations"
    And I assign a user to an organization
    Then a toast says "Could not assign user to organization"
```

## 8. End-to-end journeys

Each journey starts from an empty automation database and uses the console for every operator decision.

```gherkin
@phase:e2e @e2e @money
Feature: Whole deals from matching to cash

  Background:
    Given the browser is signed in as "staff"

  @TC-E2E-001 @P0 @positive
  Scenario: Healthy margin deal, buyer accepts
    Given a prospect at "MATCHING" with buyer "C1" and supplier offer "OF1" at fixture "FX-STD"
    When the match is proposed and approved in the console
    And the negotiation is agreed at "1250" and approved in the console
    And the compliance review is approved in the console
    Then deal "D" has status "PI_SENT"
    When the buyer accepts the PI and funds 25000 as the payer
    And the PO gate and T1 gate are approved in the console
    And the inspector reports PASS with a sealed container
    And the T2, freight and customs gates are approved in the console
    And the carrier reports "ARRIVED"
    And the T3 gate and every payout gate are approved and completed
    Then deal "D" has status "CLEARED"
    And the ledger field "payouts_total" is 18000
    When the carrier reports "DELIVERED"
    And the buyer's acceptance is recorded
    And every payout gate is approved and completed
    Then deal "D" has status "PAID"
    And the ledger field "realized_margin" is 4750
    And the ledger field "reconciled" is "true"
    And the Money & Margin page shows the settled deal
    And the evidence pack downloads with a content hash
    And no gate is left pending
    And no exception is recorded

  @TC-E2E-002 @P0 @positive
  Scenario: Thin margin deal needs the final tranche to pay the supplier
    Given a deal "D" at status "CLEARED" from fixture "FX-THIN" with the payout gate open
    When the deal is driven to settlement
    Then the payout gate amounts in order are "15246, 2754, 2000"
    And deal "D" has status "PAID"
    And the ledger field "realized_margin" is 1780

  @TC-E2E-003 @P0 @positive
  Scenario: Buyer stays silent and the window lapses
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with 18000 paid and the window open
    And "inspection_deadline" of the deal's escrow was set 1 hours ago
    When the sweep "escrow_watcher" runs
    And every payout gate is approved and completed
    Then deal "D" has status "PAID"

  @TC-E2E-004 @P0 @exception
  Scenario: Failed inspection, rework, then success
    Given a deal "D" at status "SUPPLIER_DISPATCH_READY" from fixture "FX-STD" with T1 released
    When the inspector reports FAIL
    And the re-inspection playbook is approved in the console
    And the inspector reports PASS with a sealed container
    Then deal "D" has status "QC_PASSED"
    And a pending gate "BOOK_FREIGHT" exists at level "L0"

  @TC-E2E-005 @P0 @exception
  Scenario: Failed inspection ending in a refund
    Given a deal "D" at status "DISPUTE" from fixture "FX-STD" with T1 released
    When the undisbursed balance is sent for refund in the console
    And "staff" approves the pending "REFUND_ESCROW" gate
    Then deal "D" has status "REFUNDED"
    And the ledger field "refunded_total" is 17500
    And the ledger field "reconciled" is "true"

  @TC-E2E-006 @P0 @exception
  Scenario: Buyer disputes after delivery
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with 18000 paid and the window open
    When the buyer's dispute is recorded in the console
    Then deal "D" has status "CLEARED"
    And the ledger field "outstanding" is 2500
    And the ledger field "retained_until_acceptance" is 2000
    And a pending gate "RESOLVE_EXCEPTION" exists at level "L0"

  @TC-E2E-007 @P0 @exception
  Scenario: Buyer never funds
    Given a deal "D" at status "ESCROW_PENDING" from fixture "FX-STD"
    And "created_at" of the deal's escrow was set 15 days ago
    When the sweep "deal_watcher" runs
    And the cancellation playbook is approved in the console
    Then deal "D" has status "CANCELLED"

  @TC-E2E-008 @P0 @compliance @exception
  Scenario: Seal mismatch, false alarm cleared, money released
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with an open "seal_mismatch" hold
    When the hold is cleared with a written reason
    And the T3 gate is approved in the console
    Then tranche "T3" has status "RELEASED"

  @TC-E2E-009 @P0 @compliance @negative
  Scenario: Embargoed destination never reaches a PI
    Given a deal "D" built from fixture "FX-STD" with market "KP"
    When the negotiation is finalized at "1250"
    Then deal "D" has status "NEGOTIATION_AGREED"
    And the collection "proforma_invoices" has 0 records for the deal
    And no e-mail containing "Proforma Invoice" is composed

  @TC-E2E-010 @P1 @positive
  Scenario: Parallel deals never cross mail or money
    Given 5 deals at status "ESCROW_PENDING" from fixture "FX-STD"
    When all 5 are driven to "PAID" at the same time
    Then each ledger reconciles on its own
    And every e-mail went to its own deal's parties
```

## 9. Open defects and decisions

These came from reading the code. They are not changes already made.

| ID | Type | Finding | Test | Status |
|---|---|---|---|---|
| DEF-01 | Defect, money | Four-eyes trusted `resolved_by` from the request. | TC-HIT-011 | Fixed: the signed-in caller is the approver |
| DEF-02 | Defect, money | A compliance refusal showed "Already resolved — refreshing". | TC-UI-103 | Fixed |
| DEF-03 | Defect, audit | Escrow actions recorded `operator`, not the user. | TC-UI-085 | Fixed |
| DEF-04 | Defect, display | The console did not show the supplier holdback. | TC-UI-086, TC-UI-109 | Fixed |
| DEF-05 | Testability | No `data-testid` attributes in the console. | All `@ui` | Open |
| DEF-06 | Defect, display | The error state mentioned `localhost:8081`. | TC-UI-021 | Fixed |
| DEF-07 | Defect | Customs filing for an unknown deal answered 201. | TC-DOC-013 | Fixed: answers 404 |
| DEC-01 | Decision | `PATCH /deals/{id}/status` could mark a deal funded or paid. | TC-FSM-005, TC-FSM-006 | Fixed: only operator statuses can be set by hand |
| DEC-02 | Decision | Staff could release, pay out or accept directly. | TC-REL-006, TC-REL-025 to TC-REL-028 | Fixed: an approved gate is required |
| DEC-03 | Decision | An expired PI could still be accepted. | TC-PI-009, TC-PI-019 to TC-PI-021 | Fixed: refused until staff extend it |
| DEC-04 | Decision | The margin guard was skipped when the buyer's price was unset. | TC-PO-004 | Fixed: uses the PI price, refused with neither |
| DEC-05 | Decision | Customs filing had no deal-status check. | TC-FRT-010 | Fixed: only QC_PASSED, IN_TRANSIT, CLEARED |
| DEC-06 | Decision | The console's Kill Switch had no backend command. | TC-HIT-030 to TC-HIT-032 | Fixed: pauses every outward action |
| DEC-07 | Decision | Staff could refund directly. | TC-HIT-025 to TC-HIT-028 | Fixed: REFUND_ESCROW gate |
| DEF-08 | Defect, money | Four-eyes identity was checked only on the monolith. | TC-HIT-029 | Fixed: a standalone service checks the token |
| DEF-09 | Defect, document | The PI printed "Destination Port, AE". | TC-PI-022, TC-PI-023 | Fixed: the buyer's port, or says it is open |
| DEF-10 | Defect, wording | A draft could say escrow money was already there. | TC-NEG-021 | Fixed: prompt rule and a flag that forces review |
| DEF-11 | Workflow | "We accept your offer" with no price stopped at an operator. | TC-NEG-019, TC-NEG-020 | Fixed: accepts our last offer, still confirmed by a person |

Before automating, check these expected results against a running system, because the exact value could not be confirmed from the route alone: request body shapes for `/sourcing/verify/{id}`, `/shield-link/qc/quote`, `/shield-link/freight/quote`, `/trade-min/scan` and `/trade-min/catalogue`; console labels for "Scan", "Enrich", "Re-check", "Re-score", "Send", "Sign in" and "Sign out"; reminder timings that depend on `DEAL_*_AFTER_HOURS`.

## 10. Extracting the scenarios

Keep this file as the source of truth and generate `.feature` files from it in the automation repository:

```python
import pathlib
import re

source = pathlib.Path("docs/testing/TEST_CASES.md").read_text(encoding="utf-8")
out = pathlib.Path("features")
out.mkdir(exist_ok=True)
for number, block in enumerate(re.findall(r"```gherkin\n(.*?)```", source, re.S), 1):
    title = re.search(r"Feature: (.+)", block).group(1)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    (out / f"{number:02d}-{slug}.feature").write_text(block, encoding="utf-8")
```

Suggested stack for the separate repository: `pytest-bdd` with `httpx` for `@api` and `@flow`, Playwright for `@ui` and `@e2e`, and `motor` for fixtures and the `qa_*` collections. Load `qa_test_cases` from the generated files on each run, keeping `source_sha` so results can be traced to the catalogue version.
