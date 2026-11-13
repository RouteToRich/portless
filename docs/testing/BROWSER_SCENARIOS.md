---
title: PortLess browser scenarios and Playwright plan
scope: Operator console (portless-ui) driven in a real browser against a real backend
source_of_truth: docs/testing/TEST_CASES.md (the @ui and console-written @e2e cases) + section 4 of this file (new browser cases)
status: Not automated. To be built as its own task in a separate repository (portless-e2e).
---

# PortLess browser scenarios and Playwright plan

The backend runner (`services/automation`) drives the API and the workflow; it does not open the console. Every case that needs a person's view and clicks — approving money, seeing the right number, a page that must not break — needs a real browser. This file lists those cases and how to automate them.

## 1. Playwright or Cypress

**Use Playwright.** For this console the deciding points are:

| Need in PortLess | Playwright | Cypress |
|---|---|---|
| Four-eyes: two different operators approve the same gate in one test | Two browser contexts in one test, each signed in as a different person | One browser, one user at a time; needs workarounds (API call for the second approver) |
| Kill Switch / SSE: what one operator does must appear in another operator's tab | Several pages and contexts at once; waits on network events | Single tab; no multi-tab |
| Evidence pack, journal CSV, PI / PO documents | First-class download handling (file, name, content, hash) | Possible, more setup |
| Console (5173), backend (8081 / 8099) and automation view server (8082) on different origins | No origin restrictions | Cross-origin needs `cy.origin` per call |
| Seeding a deal at any stage before the browser opens | `request` API fixture in the same test runner | `cy.request`, fine but chained |
| Run the same Gherkin as the catalogue | `playwright-bdd` reads `.feature` files | `cypress-cucumber-preprocessor`, works |
| Browsers | Chromium, Firefox, WebKit (Safari) | Chromium family + Firefox; WebKit experimental |
| Parallel runs in CI | Built in, free | Paid dashboard for good parallelism |
| Debugging a failed CI run | Trace viewer (DOM, network, console per step), video, screenshots | Time travel in the runner, video |
| Language | TypeScript (same as the console) | JavaScript / TypeScript |

Cypress would be fine for single-user page tests, but the money cases (four-eyes, live updates across operators, downloads) are the ones that matter most here, and Playwright handles them without workarounds.

## 2. The separate repository (`portless-e2e`)

```text
portless-e2e/
  features/                 generated from TEST_CASES.md (@ui, console @e2e) + section 4 of this file
  steps/                    step definitions for the UI vocabulary (TEST_CASES.md section 4, "UI")
  fixtures/
    personas.ts             staff, staff2, org-user, compliance — one token each
    seed.ts                 builds a deal at any stage through the backend API / automation seed
    auth-stub.ts            tiny auth-service stand-in: GET /auth/me answers the persona for a token
  playwright.config.ts      projects: chromium (every commit), firefox + webkit (nightly)
  .github/workflows/e2e.yml start backend + console + auth stub, run, upload traces and the report
```

How it runs:

- [ ] **Backend** — the monolith with the catalogue's test settings (`TEST_CASES.md` section 3.1): `MONGO_DB_NAME=portless_automation`, mail log-only, no provider keys, `INTEGRATION_MODE=sandbox` with the QIMA / Tazapay / Maersk simulators, `AUTH_GATEWAY_URL` pointing at the auth stub, `FRONTEND_URL` = the console's address.
- [ ] **Sign-in** — the console keeps a bearer token in `localStorage` (`portless_token`) and the backend checks it with auth-service's `GET /auth/me`. In tests, the auth stub maps fixed tokens to personas, and Playwright starts each context with that token in its storage state. One test (TC-UI-001 / TC-UI-002) still goes through the real sign-in page against the stub.
- [ ] **Data** — seed with the backend API or the automation seed (`services/automation/seed.py`, any stage), never by clicking through the earlier stages unless that is what the case tests. Clear with `POST /automation/clear` (`test_data`).
- [ ] **Locators** — prerequisite: add `data-testid` to the console (defect DEF-05). Until then, use roles and the visible texts quoted in the scenarios.
- [ ] **Results** — write each result to the same `qa_*` collections as the backend runner (with `source_sha` of the catalogue), so the Automation dashboard shows browser and backend results together.
- [ ] **Runs** — `@P0 and @ui` on every pull request to `portless-ui` or `portless`; all `@ui` and `@e2e` nightly; full browser matrix before a release.
- [ ] **Never** point the suite at production data or a real mailbox; refuse to start unless the backend reports `MONGO_DB_NAME=portless_automation`.

## 3. Existing browser cases in the catalogue

81 `@ui` cases (17 of them P0) and the console-written end-to-end journeys, all in `TEST_CASES.md` sections 7 and 8. The backend runner reports the `@ui` ones as `needs_browser`; TC-E2E-001 … TC-E2E-010 are `not_automated` because their steps are console actions. TC-E2E-000 is played by the LLM journey UC-15 and needs no browser.
#### Shell, sign-in and navigation (`@phase:ui-shell`)

| Case | Priority | Type | Title |
|---|---|---|---|
| TC-UI-001 | P0 | positive | Each role lands on the right page |
| TC-UI-002 | P0 | negative | Signed-out users are sent to sign-in |
| TC-UI-003 | P0 | negative | An organization user cannot open staff pages |
| TC-UI-004 | P1 | positive | Staff navigation reaches every page |
| TC-UI-005 | P1 | positive | An organization user sees only Deals in the menu |
| TC-UI-006 | P2 | edge | An unknown route returns to the home page for the role |
| TC-UI-007 | P0 | exception | A rejected session signs the user out |
| TC-UI-008 | P1 | exception | A network failure during refresh keeps the user signed in |
| TC-UI-009 | P1 | edge | Two tabs refreshing at once keep the session |
| TC-UI-010 | P1 | negative | Wrong credentials show an error and stay on sign-in |
| TC-UI-011 | P2 | positive | Signing out returns to sign-in |

#### Pages (`@phase:ui-pages`)

| Case | Priority | Type | Title |
|---|---|---|---|
| TC-UI-020 | P1 | exception | Each data page shows loading, error with retry, and empty states |
| TC-UI-021 | P2 | negative | The error state does not mention a developer address |

#### Prospects and matching (`@phase:ui-matching`)

| Case | Priority | Type | Title |
|---|---|---|---|
| TC-UI-030 | P1 | positive | Starting a prospect from an opportunity |
| TC-UI-031 | P1 | exception | A failed start is reported |
| TC-UI-032 | P2 | exception | A failed scan is reported |
| TC-UI-033 | P2 | positive | Prospects with nothing to show explain the next step |
| TC-UI-034 | P1 | positive | The deal board shows negotiating deals onward and prospects elsewhere |
| TC-UI-035 | P1 | positive | The organization list is read-only |
| TC-UI-040 | P1 | positive | Add a buyer to the funnel |
| TC-UI-041 | P1 | negative | A buyer needs a name and country |
| TC-UI-042 | P1 | positive | Contact next buyers |
| TC-UI-043 | P2 | edge | No candidate left to contact |
| TC-UI-044 | P1 | positive | Add a supplier offer |
| TC-UI-045 | P1 | negative | An offer needs a supplier and price |
| TC-UI-046 | P1 | positive | Save a buyer's requirement |
| TC-UI-047 | P0 | positive | Propose a match from the board |
| TC-UI-048 | P1 | negative | The backend's reason for refusing a match is shown |
| TC-UI-049 | P2 | positive | Re-score the board |

#### Deal page (`@phase:ui-deal`)

| Case | Priority | Type | Title |
|---|---|---|---|
| TC-UI-060 | P1 | positive | Every tab opens |
| TC-UI-061 | P1 | positive | Correct a party's contact |
| TC-UI-062 | P1 | exception | A blocked screening warns the operator |
| TC-UI-063 | P1 | negative | A manual negotiation round needs a valid price |
| TC-UI-064 | P1 | positive | Submit a manual negotiation round |
| TC-UI-065 | P1 | positive | Decide on an e-mail draft |
| TC-UI-066 | P0 | positive | Record the supervised loading |
| TC-UI-067 | P0 | negative | Loading needs a container and a seal |
| TC-UI-068 | P1 | positive | Re-run the trade check |
| TC-UI-069 | P1 | positive | Record a carrier milestone |
| TC-UI-070 | P2 | edge | An out-of-date milestone is reported as ignored |
| TC-UI-071 | P1 | positive | Poll carrier tracking without a booking |
| TC-UI-072 | P1 | exception | A document that cannot be fetched is reported |
| TC-UI-073 | P1 | positive | Download the evidence pack |
| TC-UI-074 | P2 | exception | Timeline failure is contained |
| TC-UI-075 | P2 | negative | An unknown deal shows an error, not a blank page |
| TC-UI-080 | P0 | positive | Escrow actions are offered only when valid |
| TC-UI-081 | P0 | negative | Actions that need a reason insist on one |
| TC-UI-082 | P0 | positive | Relaying the buyer's acceptance opens an approval and moves no money |
| TC-UI-083 | P1 | negative | The backend's refusal of an escrow action is shown |
| TC-UI-084 | P1 | edge | The confirm button cannot be pressed twice |
| TC-UI-085 | P0 | positive | Escrow actions record the signed-in operator |
| TC-UI-086 | P1 | positive | The ledger shows the supplier holdback |

#### Approvals (`@phase:ui-approvals`)

| Case | Priority | Type | Title |
|---|---|---|---|
| TC-UI-100 | P0 | positive | Approve a gate |
| TC-UI-101 | P0 | positive | Reject a gate with a note |
| TC-UI-102 | P1 | edge | A gate someone else already resolved refreshes the list |
| TC-UI-103 | P0 | negative | A compliance refusal is shown as a compliance refusal |
| TC-UI-104 | P1 | exception | A server error keeps the gate open |
| TC-UI-105 | P0 | positive | Four-eyes flow in the console |
| TC-UI-106 | P0 | negative | A same-operator refusal gets its own message |
| TC-UI-107 | P1 | positive | Money and risk details are shown |
| TC-UI-108 | P1 | positive | The playbook's effect is explained |
| TC-UI-109 | P1 | positive | A payout limited by the holdback is described correctly |
| TC-UI-110 | P1 | positive | A warning shows on a gate about to expire |
| TC-UI-111 | P1 | positive | A new gate appears without a reload |
| TC-UI-112 | P2 | positive | Filter the inbox |
| TC-UI-113 | P2 | edge | The pending count in the header follows the inbox |

#### Money and margin (`@phase:ui-money`)

| Case | Priority | Type | Title |
|---|---|---|---|
| TC-UI-120 | P0 | positive | Money & Margin agrees with the ledgers |
| TC-UI-121 | P1 | positive | Download a deal's journal |
| TC-UI-122 | P1 | exception | A failed journal download is reported |
| TC-UI-130 | P1 | positive | Exceptions are listed and can be looked up |
| TC-UI-131 | P2 | negative | An unknown exception ID is reported |
| TC-UI-140 | P1 | positive | Save the autonomy policy |
| TC-UI-141 | P1 | negative | A money threshold must be a positive number |
| TC-UI-142 | P2 | positive | Clearing overrides needs a save |
| TC-UI-143 | P1 | exception | A refused policy shows the backend's reason |
| TC-UI-150 | P1 | positive | Save the catalogue |
| TC-UI-151 | P1 | negative | Catalogue validation |
| TC-UI-160 | P2 | positive | System health lists readiness |
| TC-UI-161 | P2 | positive | Sending the digest with no channel explains why |
| TC-UI-170 | P2 | positive | Create an organization |
| TC-UI-171 | P2 | exception | A failed assignment is reported |

#### End-to-end journeys written for the console (`@phase:e2e`)

| Case | Priority | Type | Title |
|---|---|---|---|
| TC-E2E-000 | P0 | positive | The whole positive flow, opportunity to cash, touching every service |
| TC-E2E-001 | P0 | positive | Healthy margin deal, buyer accepts |
| TC-E2E-002 | P0 | positive | Thin margin deal needs the final tranche to pay the supplier |
| TC-E2E-003 | P0 | positive | Buyer stays silent and the window lapses |
| TC-E2E-004 | P0 | exception | Failed inspection, rework, then success |
| TC-E2E-005 | P0 | exception | Failed inspection ending in a refund |
| TC-E2E-006 | P0 | exception | Buyer disputes after delivery |
| TC-E2E-007 | P0 | exception | Buyer never funds |
| TC-E2E-008 | P0 | exception | Seal mismatch, false alarm cleared, money released |
| TC-E2E-009 | P0 | negative | Embargoed destination never reaches a PI |
| TC-E2E-010 | P1 | positive | Parallel deals never cross mail or money |

## 4. New browser scenarios

Cases the catalogue does not have yet, chosen for what goes wrong in production in front of an operator: two people at once, stale screens, wrong numbers, lost connections, unsafe clicks, and the newest features (supplier address, currency, superseded gates, manual suppliers). Same Gherkin vocabulary as `TEST_CASES.md` section 4 ("UI"); `@known-defect` means the console does not do it yet.

```gherkin
@phase:ui-money @ui @money @browser
Feature: Money actions in the console are safe with several operators

  @TC-WEB-001 @P0 @security
  Scenario: Four-eyes needs two different operators in two browsers
    Given the setting "FOUR_EYES_THRESHOLD_USD" is "5000"
    And a deal "D" at status "PO_ISSUED" from fixture "FX-STD"
    And browser A is signed in as "staff" and browser B as "staff2"
    When browser A opens the pending "RELEASE_ESCROW_TRANCHE" gate and clicks "Approve"
    Then browser A sees "1 of 2 approvals"
    And browser B sees the gate waiting for a second approval within 5 seconds
    When browser B clicks "Approve"
    Then both browsers show the gate approved by "staff" and "staff2"

  @TC-WEB-002 @P0 @security
  Scenario: The same operator cannot sign twice from two tabs
    Given the setting "FOUR_EYES_THRESHOLD_USD" is "5000"
    And a deal "D" at status "PO_ISSUED" from fixture "FX-STD"
    And two tabs are signed in as "staff"
    When tab 1 approves the pending "RELEASE_ESCROW_TRANCHE" gate
    And tab 2 approves the same gate
    Then tab 2 shows a message that the same person cannot sign twice
    And the gate still needs 1 approval

  @TC-WEB-003 @P0 @edge
  Scenario: A double click on Approve sends one request
    Given a deal "D" at status "PO_ISSUED" from fixture "FX-STD"
    And the browser is signed in as "staff"
    When I double-click "Approve" on the pending "RELEASE_ESCROW_TRANCHE" gate
    Then exactly 1 resolve request is sent
    And the button "Approve" is disabled until the answer arrives

  @TC-WEB-004 @P0 @edge
  Scenario: Approving a gate another operator already resolved
    Given a deal "D" at status "PO_ISSUED" from fixture "FX-STD"
    And browser A and browser B show the same pending "RELEASE_ESCROW_TRANCHE" gate
    When browser B rejects it
    And browser A clicks "Approve" without refreshing
    Then browser A sees "already resolved" and the gate's real outcome
    And no money is requested from any provider

  @TC-WEB-005 @P0 @negative
  Scenario: The Kill Switch turned on in one browser stops approvals in another
    Given browser A is signed in as "staff" on the Approvals page and browser B as "staff2"
    When browser B turns the Kill Switch on with reason "provider incident"
    Then browser A shows the paused banner within 5 seconds
    And every "Approve" button in browser A is disabled
    And "Reject" stays available

  @TC-WEB-006 @P0 @exception
  Scenario: The session expires while an operator is approving money
    Given the browser is signed in as "staff" on the pending "RELEASE_SUPPLIER_PAYOUT" gate
    And the session token expires
    When I click "Approve"
    Then I am asked to sign in again
    And after signing in the gate is still pending and nothing was approved

  @TC-WEB-007 @P0 @positive
  Scenario: Every money figure on the deal page equals the ledger
    Given a deal "D" at status "CLEARED" from fixture "FX-STD" with payouts 17325 and 675
    When I open the deal's Money tab
    Then I see "25,000.00" as escrow principal
    And I see "18,000.00" paid to the supplier and "2,000.00" held until acceptance
    And every figure equals the API ledger for the deal

  @TC-WEB-008 @P0 @positive
  Scenario: The evidence pack downloads with its content hash
    Given a deal "D" at status "PAID" from fixture "FX-STD"
    When I click "Evidence pack"
    Then a file is downloaded
    And the SHA-256 shown in the console equals the hash inside the file

  @TC-WEB-009 @P1 @positive
  Scenario: The journal downloads as CSV with balanced totals
    Given a deal "D" at status "PAID" from fixture "FX-STD"
    When I download the deal's journal as CSV
    Then the debit total equals the credit total

  @TC-WEB-010 @P1 @positive
  Scenario: Superseded match gates are shown as closed by the system
    Given a deal "D" whose match was confirmed while two other pairs were proposed
    When I open the deal's Approvals tab
    Then the two other gates show "closed — another pair was confirmed"
    And they are not counted as rejected by an operator
```

```gherkin
@phase:ui-deal @ui @browser
Feature: The deal page tells the operator what the deal is waiting for

  @TC-WEB-011 @P0 @positive @known-defect
  Scenario: A missing facility address is visible and can be entered
    Given a deal "D" at status "PO_ISSUED" whose supplier address is "Thanjavur, Tamil Nadu" and the inspection waits for it
    And the browser is signed in as "staff"
    When I open the deal page
    Then I see "Inspection waits for the supplier's facility address"
    When I fill "Facility address" with "Plot 14, SIPCOT Industrial Estate, Thanjavur 613005" and click "Save"
    Then the QC status chip changes from "Awaiting address" to "Requested"

  @TC-WEB-012 @P1 @positive @known-defect
  Scenario: The supplier's price shows in INR with USD beside it
    Given a deal "D" at status "PO_ISSUED" from fixture "FX-STD" with an Indian supplier
    When I open the deal page
    Then I see the supplier price "USD 1,000.00" with "INR" beside it and the rate used

  @TC-WEB-013 @P1 @positive
  Scenario: The QC, escrow and freight chips follow the deal
    Given a deal "D" driven step by step from "PO_ISSUED" to "PAID"
    When I keep the Deal Board open
    Then the QC, escrow and freight chips of deal "D" change at each step without a page reload

  @TC-WEB-014 @P1 @positive
  Scenario: The shipment card shows a rolled vessel
    Given a deal "D" at status "IN_TRANSIT" booked on Maersk
    When Maersk moves the booking to another vessel
    Then the shipment card shows the new vessel and ETA

  @TC-WEB-015 @P1 @negative
  Scenario Outline: Actions that need a reason refuse an empty one
    Given a deal "D" at status "<status>" from fixture "FX-STD"
    When I click "<action>" and leave the reason empty
    Then the button "Confirm" is disabled

    Examples:
      | status    | action                  |
      | PI_SENT   | Extend PI               |
      | QC_PASSED | Clear seal mismatch     |
      | DISPUTE   | Request refund          |
      | PO_ISSUED | Turn on the Kill Switch |

  @TC-WEB-016 @P1 @edge
  Scenario: Long and non-Latin names do not break the layout
    Given a deal whose buyer is "شركة الخليج للتجارة العامة ذ.م.م" and supplier "Sri Venkateswara Agro Industries Private Limited"
    When I open the Deal Board and the deal page
    Then both names are fully readable or truncated with a tooltip
    And no element overflows the page

  @TC-WEB-017 @P0 @security
  Scenario: Script in a counterparty name is shown as text
    Given a deal whose buyer is "<img src=x onerror=alert(1)> Trading"
    When I open the deal page
    Then no dialog opens
    And I see the name as plain text

  @TC-WEB-018 @P1 @edge
  Scenario: Dates show the operator's local time with the zone
    Given a gate created at "2026-12-01T18:45:00Z"
    When an operator in India opens it
    Then I see "2 Dec 2026, 00:15 IST"
```

```gherkin
@phase:ui-shell @ui @browser
Feature: The console stays honest when the network or the backend misbehaves

  @TC-WEB-019 @P0 @exception
  Scenario: The live stream drops and comes back
    Given the browser is signed in as "staff" on the Approvals page
    When the event stream disconnects for 30 seconds while a new gate is created
    Then I see a reconnecting banner
    And after it reconnects the new gate is listed without a page reload

  @TC-WEB-020 @P1 @exception @needs:route-mock
  Scenario Outline: A failing backend call shows a clear error, never a blank page
    Given the API answers <code> for "<call>"
    When I open "<route>"
    Then I see an error message with a "Retry" button

    Examples:
      | code | call                        | route       |
      | 500  | GET /deals                  | /deals      |
      | 503  | GET /hitl-approval/approvals| /approvals  |
      | 504  | GET /escrow-guard/ledgers   | /money      |

  @TC-WEB-021 @P1 @exception @needs:route-mock
  Scenario Outline: Refusals are explained in plain words
    Given the API answers <code> with detail "<detail>" for "POST /hitl-approval/approvals/{APPR}/resolve"
    When I click "Approve"
    Then a toast says "<message>"

    Examples:
      | code | detail                         | message                              |
      | 423  | platform paused                | The platform is paused               |
      | 409  | compliance block open          | A compliance hold blocks this money  |
      | 409  | same approver                  | A second, different approver is needed |
      | 403  | not staff                      | You are not allowed to approve this  |

  @TC-WEB-022 @P1 @edge @needs:route-mock
  Scenario: A slow save cannot be submitted twice
    Given the API is slow for "PUT /deals/{D}/packing"
    When I click "Save" twice on the packing form
    Then exactly 1 request is sent

  @TC-WEB-023 @P0 @security
  Scenario: An organisation user cannot open another organisation's deal by URL
    Given a deal "E" of organisation "qa-org-b"
    And the browser is signed in as "org-user" of organisation "qa-org-a"
    When I open "/deals/{E}"
    Then I see "not found" and no data of deal "E"

  @TC-WEB-024 @P1 @positive
  Scenario: A deep link to an approval opens it after sign-in
    Given a signed-out browser
    When I open "/approvals/{APPR}"
    And I sign in as "staff"
    Then I am on "/approvals/{APPR}"

  @TC-WEB-025 @P0 @negative
  Scenario: Automation mode can never be confused with live deals
    Given the browser is signed in as "staff" with Automation mode on
    Then every page shows the Automation banner
    And no live deal is listed
    And on a backend with ENV_PROFILE=production the Automation toggle is not shown

  @TC-WEB-026 @P2 @positive
  Scenario: Approvals work on a phone-sized screen
    Given the browser window is 390 × 844
    When I approve a pending "SEND_NEGOTIATION_EMAIL" gate
    Then the approval completes without horizontal scrolling

  @TC-WEB-027 @P2 @positive
  Scenario: An approval can be done with the keyboard only
    When I reach the pending gate and its "Approve" button using Tab and press Enter
    Then the gate is approved
```

```gherkin
@phase:ui-matching @ui @browser
Feature: Adding a local supplier in the console

  @TC-WEB-028 @P0 @positive @known-defect
  Scenario: Add a local supplier and see its checks
    Given the browser is signed in as "staff"
    When I open "/suppliers" and click "Add supplier"
    And I fill the supplier form with name "Local Mill", GSTIN "33AAAAA0000A1Z5", phone, e-mail and "Plot 14, SIPCOT, Thanjavur 613005"
    And I click "Save"
    Then I see "Local Mill" in the supplier list with its verification result

  @TC-WEB-029 @P1 @negative @known-defect
  Scenario Outline: The supplier form refuses invalid details
    When I fill "<field>" with "<value>" in the supplier form and click "Save"
    Then I see "<message>"

    Examples:
      | field            | value            | message                         |
      | GSTIN            | 12345            | GSTIN must have 15 characters   |
      | Facility address | Thanjavur        | Give the street, town and PIN code |
      | E-mail           | not-an-email     | Enter a valid e-mail            |

  @TC-WEB-030 @P1 @negative @known-defect
  Scenario: A duplicate GSTIN is shown, not saved twice
    Given a local supplier with GSTIN "33AAAAA0000A1Z5" exists
    When I add another supplier with GSTIN "33AAAAA0000A1Z5"
    Then I see "A supplier with this GSTIN already exists" and a link to it

  @TC-WEB-031 @P1 @positive @known-defect
  Scenario: Import local suppliers from a CSV with a preview
    When I import a CSV of 10 suppliers where 2 have a duplicate GSTIN and 1 has no address
    Then the preview shows 7 to add, 2 duplicates and 1 to fix
    And confirming adds 7 suppliers

  @TC-WEB-032 @P0 @positive @known-defect
  Scenario: Add a local supplier to a prospect and record its phone quote
    Given a deal "D" at status "MATCHING" with an interested buyer
    When I open the deal, click "Add supplier to this deal" and pick "Local Mill"
    And I click "Record phone quote" and enter 900 USD per MT FOB
    Then the match board lists "Local Mill" at 900
    And a "CONFIRM_MATCH" gate appears in Approvals
```

## 5. Order of work

1. Add `data-testid` attributes to the console (DEF-05) — every button and figure used above.
2. Create `portless-e2e` with Playwright, `playwright-bdd`, the auth stub and the seed fixture.
3. Automate the 17 P0 `@ui` cases and TC-WEB-001 … TC-WEB-008, TC-WEB-017, TC-WEB-019, TC-WEB-023, TC-WEB-025 first (money, security, live updates).
4. Rewrite TC-E2E-001 … TC-E2E-010 as browser journeys (console for every decision, providers on the simulators).
5. The remaining `@ui` P1 / P2 cases and section 4.
6. The `@known-defect` cases as each feature lands (facility address field, currency display, manual suppliers).
