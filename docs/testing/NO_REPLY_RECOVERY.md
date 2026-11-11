---
title: Replies, silence and the next-ranked counterparty
scope: Buyer and supplier replies from outreach to the Purchase Order acknowledgement
status: Proposed. Section 2 describes the code as read on 2026-11-11. The scenarios tagged @proposed fail against today's code until section 4 is built.
related: docs/testing/TEST_CASES.md (TC-*), services/automation/journeys_index.py (UC-01 to UC-14)
---

# Replies, silence and the next-ranked counterparty

A deal only closes when both a buyer and a supplier keep answering. This document covers:

- what happens when a party accepts, or sends a conditional ("hidden") reply;
- what happens when a party goes quiet;
- how we move to the next-ranked buyer or supplier without losing the side that is still answering.

Each case lists the use case (UC) and test cases (TC) that already cover it. New cases start at **UC-15** and **TC-NRR-001**.

## 1. Answers to the three questions

**The supplier agrees during the negotiation.** That fixes our cost and nothing more. The orchestrator stores the supplier's price as `supplier_last_quote`, and only a buyer acceptance can close the negotiation (`agents/negotiation_agent/graph.py`, `party == "buyer"`). A supplier saying yes never moves the deal by itself.

**The buyer accepts the negotiation.** Today's flow:

1. The acceptance is detected and parks at the `CONFIRM_NEGOTIATION_AGREEMENT` gate (L0). An operator approves it and the deal moves to `NEGOTIATION_AGREED`. (TC-NEG-010, TC-NEG-011, TC-NEG-019)
2. The PI does **not** go out yet. The chosen supplier gets a firm order request at its own last price. (TC-SUP-001)
3. If the supplier confirms, the `CONFIRM_SUPPLIER_ORDER` gate opens. APPROVE issues the PI and the deal moves to `PI_SENT`. (TC-SUP-002, UC-13)
4. If the supplier declines, or stays silent for 24 h, the request goes to the next-ranked standby supplier whose own price still keeps the margin floor. (TC-SUP-003, TC-SUP-006, TC-SUP-007, UC-13, UC-14)
5. The buyer accepts the PI (`CONFIRM_PI_ACCEPTANCE`), then the escrow, PO and QC steps follow. (TC-PI-010)

So yes: when the first supplier does not confirm, the second supplier is contacted automatically. You never need to run the flow again for that.

**Nobody replies.** Some waits are handled well today and others are not. See the table in section 2. The biggest gaps are:

- A buyer who goes quiet **after** agreeing (PI or escrow funding) kills the deal. It is cancelled, the next buyer is never offered the lot, and the supplier who already committed is never told.
- A quiet negotiation waits **30 days** before it fails. When it fails, the code replaces **both** sides, whichever one was actually silent.

## 2. What happens today

| # | Waiting for | Silent party | Today | Coverage | Gap |
|---|---|---|---|---|---|
| 1 | Outreach reply | Buyer | Follow-up after 3 days, `no_reply` 7 days later, then the next candidate. When all are used: `OUTREACH:exhausted`, escalated to an operator | TC-OUT-010, TC-OUT-011 | None for silence. When exhausted there is no automatic new buyer scan |
| 2 | RFQ quote | Supplier | No reminder. The deal sits in `MATCHING` until the stall SLA raises a generic exception | none | No nudge and no deadline. Other shortlisted suppliers are not asked |
| 3 | Negotiation reply | Buyer | At most 2 follow-ups, each drafted at 2 days and **held for an operator** (L0). Then nothing until 30 days, when the session is FAILED (`sweep_stale_negotiations`) | TC-NEG-018 | Far too slow. Each nudge needs a click |
| 4 | Negotiation reply | Supplier | Same as row 3 | TC-NEG-018 | Same as row 3 |
| 5 | After a failed negotiation | Either | `on_negotiation_failed` opens a re-match gate for the **next supplier**, **and** the resolver playbook contacts the **next buyer** | TC-NEG-017 | Both sides are replaced even when only one was silent, so the healthy side is lost |
| 6 | Firm order confirmation | Supplier | 24 h, then the next standby that keeps the margin. Only one supplier holds the request at a time. When none are left: `SUPPLIER:no_confirmation` | TC-SUP-007, TC-SUP-008, UC-14 | The agreed buyer is never told about the delay. No new supplier search is started |
| 7 | PI acceptance | Buyer | Reminders at 72 h up to `DEAL_REMINDER_MAX`. When the PI expires, `PI_SENT:expired` is classed as a payment failure, then a cancel proposal | TC-PI-012, TC-PI-013 | No next buyer is offered the lot. The committed supplier is not told. The state machine only allows `PI_SENT → CANCELLED` |
| 8 | Escrow funding | Buyer | Reminders. After 14 days unfunded, the `cancel_unfunded` gate cancels the deal | TC-PI-012, TC-PI-018 | Same as row 7 |
| 9 | PO acknowledgement | Supplier | Reminder at 72 h, then a stall exception that the LLM triages. The buyer's money is in escrow | TC-PO-011 | No standby supplier and no way to withdraw the PO |
| 10 | Late reply from a replaced party | Supplier | Goes to an operator with flag `supplier_replaced` | TC-SUP-005 | No mirror rule for a replaced **buyer** |
| 11 | Delivery acceptance | Buyer | The window lapses and the final tranche releases | TC-REL (silence), journey "Buyer stays silent and the window lapses" | None |

### Replies that are not a clear yes or no ("hidden" replies)

| Reply | Today | Coverage |
|---|---|---|
| Buyer asks a question instead of quoting | `PARSE_INBOUND_EMAIL` for an operator | UC-02, TC-NEG-020 |
| Buyer is hesitant ("sounds good, let me check") | Not read as agreement | UC-09 |
| Conditional PI answer ("we accept if you lower the price") | Not an acceptance; goes to an operator | UC-10, TC-PI-011 |
| Supplier answers the firm order with a new price or a question | `NEEDS_OPERATOR`, no confirmation gate | TC-SUP-004 |
| Supplier accepts **with conditions** during the negotiation ("1,000 if 50 % advance", "ex-works", "only 15 MT") | Read as a counter-price. The condition is not captured | none (gap) |
| Reply from another address or another party | Not believed; goes to an operator | TC-OUT-009, TC-RFQ-010, UC-04, UC-05 |
| Buyer declines | Next candidate | UC-03, TC-OUT-007 |

## 3. Use cases

Existing use cases that this document relies on: UC-01 (whole deal), UC-02, UC-03, UC-09, UC-10, UC-13 (supplier declines, then standby), UC-14 (supplier silent, then standby, then nobody left).

| ID | Title | Actor / trigger | Expected result |
|---|---|---|---|
| UC-15 | Supplier accepts our counter during the negotiation | Supplier replies "we accept USD 960" | Cost fixed at 960. The deal stays `NEGOTIATING`. The next buyer offer is priced from 960. The supplier gets no PO yet |
| UC-16 | Supplier accepts with a hidden condition | Supplier replies "OK at 960 if 50 % advance" | The condition is captured (`payment_terms`, `incoterm`, `quantity`). A changed term goes to an operator with flag `conditional_acceptance`. The price is not treated as agreed |
| UC-17 | Supplier silent on the RFQ | No quote within `RFQ_REPLY_DAYS` | One reminder. At the deadline the RFQ closes for that supplier and the next shortlisted supplier is asked |
| UC-18 | Buyer silent during the negotiation | No reply after our offer | An automatic templated nudge (L2) on day 2 and day 4. On day 7 that buyer is set to `no_reply`. **The supplier stays.** The next buyer candidate is contacted with an offer that still clears against the same supplier |
| UC-19 | Supplier silent during the negotiation | No reply after our counter | Nudges on day 2 and day 4. On day 7 the next-ranked supplier is asked. **The buyer stays** and gets a short holding note if they are waiting on us |
| UC-20 | Both sides silent | Neither replies | The buyer is replaced first, because demand is scarcer. The supplier is replaced if it still has not answered when the new buyer engages |
| UC-21 | No supplier confirms after the buyer agreed | All standbys declined or silent | The buyer gets a holding note. A new supplier search starts for the product and corridor. An exception goes out only if the search finds nobody in `SUPPLIER_RESEARCH_HOURS` |
| UC-22 | Buyer silent on the PI | PI not accepted | Reminders at 72 h. When the PI expires, the PI is voided, the deal returns to `MATCHING` with the **supplier commitment kept**, and the supplier is told about the short delay. The next interested buyer is offered the lot at a price that clears against the committed supplier price |
| UC-23 | Buyer never funds the escrow | Unfunded past `ESCROW_FUNDING_GRACE_DAYS` | `cancel_unfunded` refunds any partial funds, then the deal is **re-offered** to the next buyer (as in UC-22) instead of being cancelled. It is cancelled only when no buyer is left |
| UC-24 | Supplier silent on the PO | No PO acknowledgement in `PO_ACK_HOURS` (money in escrow) | Reminder, then a `REPLACE_SUPPLIER` gate (L0, operator). APPROVE withdraws the PO, asks the next standby at a price that keeps the margin, issues a new PO, and tells the buyer the new date. No escrow money moves |
| UC-25 | Late reply from a replaced buyer | A dropped buyer replies after the next buyer was contacted | Not auto-accepted. Goes to an operator with flag `buyer_replaced`. The operator can take the buyer back as a candidate |
| UC-26 | Nobody left on a side | Funnel or standby list is empty | Starts a fresh scan (buyers or suppliers for the product and corridor). An operator exception is raised only if the scan adds nobody |

## 4. How we fix it

1. **Every wait has an owner, a clock and a next step.** Store one `waiting_on` record per deal: party, since, the outbound e-mail it answers, nudges sent, and deadline. The deal watcher reads it, and so do the console and the digest. Today the clocks are spread across four sweeps.
2. **The same ladder for every wait:** nudge, nudge, hand over to the next ranked party. Templated nudges are L2 (automatic). Only drafts that change a price stay with an operator.
3. **Replace only the silent side.** `on_negotiation_failed` gets a `silent_party` / `failed_side` and replaces just that side. The healthy side keeps its session, price and commitment, and gets a holding note.
4. **Keep the bench warm.** Buyers who said "interested" but were not chosen stay `standby` instead of being dropped. Suppliers who quoted stay ranked (they already do, for the firm order). The next pick is always re-priced so the margin floor still holds against the side we keep.
5. **Bring a deal back after agreement.** Add `PI_SENT → MATCHING` and `ESCROW_PENDING → MATCHING` (only while no buyer money is held). The PI is voided and the supplier commitment carries over.
6. **Rescan before giving up.** An empty funnel or standby list starts a scan for that product and corridor. A human is asked only when the scan finds nobody.
7. **Late replies always go to a person.** The `supplier_replaced` rule gets a buyer mirror.

Proposed timers (all overridable):

| Setting | Default | Wait |
|---|---|---|
| `RFQ_REPLY_DAYS` / `RFQ_REMINDER_DAYS` | 5 / 2 | RFQ |
| `NEGOTIATION_FOLLOWUP_AFTER_SECONDS` | 2 days (unchanged); nudges become L2 | Negotiation |
| `NEGOTIATION_HANDOVER_DAYS` (new; replaces the 30-day fail for silence) | 7 | Negotiation |
| `SUPPLIER_CONFIRMATION_HOURS` | 24 (unchanged), plus a nudge at 12 h | Firm order |
| PI validity | unchanged; expiry hands over to the next buyer | PI |
| `ESCROW_FUNDING_GRACE_DAYS` | 14 (unchanged); expiry hands over to the next buyer | Escrow |
| `PO_ACK_HOURS` | 48 to the reminder, 96 to the `REPLACE_SUPPLIER` gate | PO |
| `SUPPLIER_RESEARCH_HOURS` | 48 | Rescan |

### Decisions needed

| ID | Question | Recommendation |
|---|---|---|
| NRR-D1 | May a deal go back from `PI_SENT` / `ESCROW_PENDING` to `MATCHING`, or should the next buyer get a new deal that copies the supplier commitment? | Go back to `MATCHING` on the same deal, so the history, supplier commitment and documents stay in one place |
| NRR-D2 | Negotiation nudges: automatic (L2), or keep them with an operator? | L2 for templated nudges that state no new price |
| NRR-D3 | When a buyer drops after the supplier confirmed, how long does the supplier's committed price stay valid? | Ask the supplier to hold it for 7 days in the holding note. Re-confirm after that |
| NRR-D4 | Replacing a supplier after the PO (UC-24): withdraw the PO automatically, or always ask an operator? | Always ask an operator. It is a contract |

## 5. Scenarios

Steps follow the vocabulary of `TEST_CASES.md` section 4. New steps are listed at the end. Scenarios tagged `@proposed` fail on today's code. The rest pin down current behaviour that has no test yet.

```gherkin
@phase:negotiation @flow @email
Feature: Supplier replies during the negotiation
  A supplier's yes fixes our cost; only the buyer closes the sale. A yes with a condition
  attached is not a yes. FX-STD: buyer target 1250, supplier quote 1000, 20 MT.

  Background:
    Given I am "staff"
    And a deal "D" at status "NEGOTIATING" from fixture "FX-STD"
    And a negotiation "N" in status "ROUND"
    And a counter of 960 was sent to the supplier

  @TC-NRR-001 @P1 @positive
  Scenario: A supplier accepting our counter fixes the cost and nothing more
    When the supplier replies "We accept USD 960 per MT FOB." from the known address
    Then the negotiation field "supplier_last_quote" is 960
    And no gate "CONFIRM_NEGOTIATION_AGREEMENT" exists
    And deal "D" has status "NEGOTIATING"
    And the collection "purchase_orders" has 0 records for the deal

  @TC-NRR-002 @P0 @money @negative @proposed
  Scenario Outline: A supplier yes with a condition goes to a person
    When the supplier replies "<reply>" from the known address
    Then the negotiation field "supplier_last_quote" is not 960
    And a pending gate "PARSE_INBOUND_EMAIL" exists at level "L0"
    And the gate "PARSE_INBOUND_EMAIL" has risk flag "conditional_acceptance"
    And the inbound record names the condition "<condition>"

    Examples:
      | reply                                                   | condition     |
      | We accept 960 provided you pay 50% in advance.          | payment_terms |
      | OK at 960, ex-works our factory.                        | incoterm      |
      | 960 is fine but we can only ship 15 MT this month.      | quantity      |
```

```gherkin
@phase:matching @flow @email
Feature: A supplier silent on the RFQ is replaced by the next shortlisted supplier
  Background:
    Given I am "staff"
    And a deal "D" at status "MATCHING" from fixture "FX-STD"
    And an RFQ was dispatched to suppliers "S1" and "S2" with "S3" next on the shortlist

  @TC-NRR-003 @P1 @positive @needs:time @proposed
  Scenario: Reminder, then the next shortlisted supplier
    Given the RFQ to "S1" was sent 2 days ago without a quote
    When the sweep "deal_watcher" runs
    Then an e-mail containing "[RFQ:" is composed for supplier "S1"
    Given the RFQ to "S1" was sent 5 days ago without a quote
    When the sweep "deal_watcher" runs
    Then the RFQ to "S1" has status "no_reply"
    And an e-mail containing "[RFQ:" is composed for supplier "S3"

  @TC-NRR-004 @P1 @negative @proposed
  Scenario: A quote that arrives on time stops the reminders
    Given the RFQ to "S1" was sent 2 days ago without a quote
    And supplier "S1" quotes 980 by e-mail from its own address
    When the sweep "deal_watcher" runs
    Then no reminder is composed for supplier "S1"
```

```gherkin
@phase:negotiation @flow @email
Feature: Silence during the negotiation replaces only the silent side
  Two nudges, then the next-ranked party. The side that is still answering keeps its session,
  its price and its place in the deal.

  Background:
    Given I am "staff"
    And the setting "NEGOTIATION_HANDOVER_DAYS" is "7"
    And a deal "D" at status "NEGOTIATING" from fixture "FX-STD" with 3 buyer candidates
    And standby supplier "C" quotes 990 with score 85
    And a negotiation "N" in status "ROUND"

  @TC-NRR-005 @P1 @positive @needs:time @proposed
  Scenario: Nudges to a silent buyer go out on their own
    Given our last offer to the buyer was sent 2 days ago without a reply
    When the sweep "negotiation_stale" runs
    Then a gate "SEND_NEGOTIATION_EMAIL" exists with status "AUTO_APPROVED"
    And an e-mail containing "[NEG:" is composed for the "buyer"

  @TC-NRR-006 @P0 @positive @needs:time @proposed
  Scenario: A buyer silent past the hand-over moves to the next buyer, the supplier stays
    Given our last offer to the buyer was sent 7 days ago with 2 follow-ups unanswered
    When the sweep "negotiation_stale" runs
    Then the selected buyer candidate has status "no_reply"
    And the next pending candidate is contacted
    And the deal's supplier is the fixture's supplier
    And no gate "CONFIRM_SUPPLIER_SELECTION" exists
    And deal "D" has status "MATCHING"

  @TC-NRR-007 @P0 @positive @needs:time @proposed
  Scenario: A supplier silent past the hand-over moves to the next supplier, the buyer stays
    Given our last counter to the supplier was sent 7 days ago with 2 follow-ups unanswered
    And the buyer replied 1 day ago
    When the sweep "negotiation_stale" runs
    Then the deal's supplier is supplier "C"
    And the buyer party is unchanged
    And an e-mail containing "[NEG:" is composed for supplier "C"
    And a holding note is composed for the "buyer"
    And no order request names the buyer or the buyer's price
    And deal "D" has status "NEGOTIATING"

  @TC-NRR-008 @P1 @edge @needs:time @proposed
  Scenario: Both silent: the buyer is replaced first
    Given our last offer to the buyer was sent 7 days ago with 2 follow-ups unanswered
    And our last counter to the supplier was sent 7 days ago with 2 follow-ups unanswered
    When the sweep "negotiation_stale" runs
    Then the next pending candidate is contacted
    And the deal's supplier is the fixture's supplier

  @TC-NRR-009 @P0 @negative @proposed
  Scenario: A failed negotiation does not replace the side that kept answering
    Given the negotiation "N" fails because the buyer declined
    Then no gate "CONFIRM_SUPPLIER_SELECTION" exists
    And the next pending candidate is contacted

  @TC-NRR-010 @P1 @exception @needs:time
  Scenario: Today, a quiet negotiation only fails after the 30-day cooling period
    Given a negotiation "N" in status "ROUND" idle for 29 days
    When the sweep "negotiation_stale" runs
    Then the negotiation "N" has status "ROUND"
```

TC-NRR-010 records today's behaviour. Delete it when TC-NRR-006 and TC-NRR-007 pass.

```gherkin
@phase:pi @flow @email
Feature: No supplier confirms after the buyer agreed
  Builds on TC-SUP-008 (exhausted). The buyer is kept informed and a new search starts
  before anyone is told the deal failed.

  Background:
    Given I am "staff"
    And the setting "SUPPLIER_CONFIRMATION_REQUIRED" is "true"
    And a deal "D" at status "NEGOTIATION_AGREED" from fixture "FX-STD"
    And every standby supplier has declined the order request

  @TC-NRR-011 @P0 @positive @proposed
  Scenario: The buyer gets a holding note and a new supplier search starts
    Then the order request is "exhausted"
    And a holding note is composed for the "buyer"
    And no holding note names a supplier or a supplier price
    And a supplier scan is started for the deal's HS code and corridor
    And no exception is recorded with "stage" equal to "SUPPLIER:no_confirmation"

  @TC-NRR-012 @P0 @positive @proposed
  Scenario: A supplier from the new search gets the firm order request
    Given the supplier scan found supplier "F" quoting 950 with an e-mail address
    When the PI leg runs for deal "D"
    Then supplier "F" holds the order request
    And an order request is composed for supplier "F" at its own price

  @TC-NRR-013 @P0 @exception @needs:time @proposed
  Scenario: The new search finds nobody: one exception
    Given the supplier scan found no supplier
    And 48 hours have passed
    When the supplier order sweep runs
    Then exactly 1 exception is recorded with "stage" equal to "SUPPLIER:no_confirmation"
    And deal "D" has status "NEGOTIATION_AGREED"
```

```gherkin
@phase:pi @flow @email @money
Feature: A buyer who goes quiet after agreeing hands the lot to the next buyer
  The supplier already committed. Losing the buyer must not lose the supplier: the PI is voided,
  the deal returns to MATCHING with the supplier commitment kept, and the next interested buyer
  is offered the lot at a price that clears the margin floor against the committed supplier price.

  Background:
    Given I am "staff"
    And a deal "D" at status "PI_SENT" from fixture "FX-STD"
    And the supplier "A" confirmed the order at 1000
    And buyer candidate "C2" replied "interested" and is on standby

  @TC-NRR-014 @P0 @positive @needs:time @proposed
  Scenario: An expired PI goes to the next buyer, not to cancellation
    Given "expires_at" of the deal's PI was set 1 days ago
    When the sweep "deal_watcher" runs
    Then the deal's PI has status "VOID"
    And deal "D" has status "MATCHING"
    And the committed supplier is "A" at its own price
    And an e-mail is composed for candidate "C2"
    And a holding note is composed for supplier "A"
    And no holding note names the buyer or the buyer's price
    And no gate "RESOLVE_EXCEPTION" with playbook "cancel_unfunded" exists

  @TC-NRR-015 @P0 @money @positive @needs:time @proposed
  Scenario: An unfunded escrow refunds any partial money, then goes to the next buyer
    Given a deal "E" at status "ESCROW_PENDING" from fixture "FX-STD" partially funded with 5000 past the grace period
    And buyer candidate "C2" replied "interested" and is on standby
    When the exception is analysed
    And "staff" approves the pending "RESOLVE_EXCEPTION" gate
    Then a refund of 5000 is recorded for the buyer
    And deal "E" has status "MATCHING"
    And an e-mail is composed for candidate "C2"

  @TC-NRR-016 @P0 @money @exception @proposed
  Scenario: No buyer left: cancel, and tell the supplier
    Given a deal "F" at status "ESCROW_PENDING" from fixture "FX-STD" unfunded past the grace period
    And the deal has no buyer candidate left
    When the exception is analysed
    And "staff" approves the pending "RESOLVE_EXCEPTION" gate
    Then deal "F" has status "CANCELLED"
    And an e-mail is composed for both parties
    And a buyer scan is started for the deal's HS code and corridor

  @TC-NRR-017 @P0 @money @security @negative @proposed
  Scenario: A deal holding buyer money never returns to MATCHING
    Given a deal "G" at status "ESCROW_PENDING" from fixture "FX-STD" partially funded with 5000
    When the deal is handed to the next buyer without a refund
    Then the request is refused
    And deal "G" has status "ESCROW_PENDING"

  @TC-NRR-018 @P0 @security @negative @proposed
  Scenario: A late acceptance from the replaced buyer goes to a person
    Given "expires_at" of the deal's PI was set 1 days ago
    And the sweep "deal_watcher" has handed the deal to candidate "C2"
    When the first buyer replies "Sorry for the delay, we accept the PI." from the known address
    Then no gate "CONFIRM_PI_ACCEPTANCE" exists
    And a pending gate "PARSE_INBOUND_EMAIL" exists at level "L0"
    And the gate "PARSE_INBOUND_EMAIL" has risk flag "buyer_replaced"
```

```gherkin
@phase:po @flow @email @money
Feature: A supplier silent on the Purchase Order can be replaced with an operator's approval
  The buyer's money is in escrow, so replacing the supplier is a contract decision: always L0,
  and no escrow money moves.

  Background:
    Given I am "staff"
    And the setting "PO_ACK_REQUIRED" is "true"
    And a deal "D" at status "PO_ISSUED" from fixture "FX-STD"
    And standby supplier "C" quotes 990 with score 85

  @TC-NRR-019 @P1 @positive @needs:time @proposed
  Scenario: Reminder at 48 hours, replacement gate at 96 hours
    Given the PO was sent 48 hours ago without an acknowledgement
    When the sweep "deal_watcher" runs
    Then a reminder is composed for the "supplier"
    Given the PO was sent 96 hours ago without an acknowledgement
    When the sweep "deal_watcher" runs
    Then a pending gate "REPLACE_SUPPLIER" exists at level "L0"
    And the gate "REPLACE_SUPPLIER" is for supplier "C"

  @TC-NRR-020 @P0 @money @positive @proposed
  Scenario: Approving the replacement withdraws the PO and asks the standby
    Given a pending gate "REPLACE_SUPPLIER" exists for the deal
    When "staff" approves the pending "REPLACE_SUPPLIER" gate
    Then the first PO has status "WITHDRAWN"
    And an e-mail is composed for the "supplier" containing "withdrawn"
    And an order request is composed for supplier "C" at its own price
    And the ledger field "released_total" is unchanged
    And a holding note is composed for the "buyer"

  @TC-NRR-021 @P0 @money @negative @proposed
  Scenario: A standby that breaks the margin floor is never offered
    Given standby supplier "C" quotes 1200 with score 85
    And the PO was sent 96 hours ago without an acknowledgement
    When the sweep "deal_watcher" runs
    Then no gate "REPLACE_SUPPLIER" is for supplier "C"
    And an exception is recorded with "deal_id" equal to "{D}"

  @TC-NRR-022 @P1 @negative @proposed
  Scenario: Rejecting the replacement keeps the first supplier
    Given a pending gate "REPLACE_SUPPLIER" exists for the deal
    When "staff" rejects the pending "REPLACE_SUPPLIER" gate
    Then the first PO has status "ISSUED"
    And no order request is composed for supplier "C"
```

### New steps to implement

| Step | Used by |
|---|---|
| `a counter of <price> was sent to the supplier` | TC-NRR-001, TC-NRR-002 |
| `the negotiation field "<f>" is [not] <value>` | TC-NRR-001, TC-NRR-002 |
| `the inbound record names the condition "<c>"` | TC-NRR-002 |
| `an RFQ was dispatched to suppliers … with "<s>" next on the shortlist`, `the RFQ to "<s>" was sent <n> days ago without a quote`, `the RFQ to "<s>" has status "<st>"` | TC-NRR-003, TC-NRR-004 |
| `our last offer to the buyer / counter to the supplier was sent <n> days ago [with <k> follow-ups unanswered]` | TC-NRR-005 to TC-NRR-008 |
| `the selected buyer candidate has status "<st>"`, `the buyer party is unchanged`, `the deal's supplier is the fixture's supplier` | TC-NRR-006 to TC-NRR-008 |
| `a holding note is composed for …`, `no holding note names …` | TC-NRR-007, TC-NRR-011, TC-NRR-014, TC-NRR-020 |
| `a supplier / buyer scan is started for the deal's HS code and corridor`, `the supplier scan found …` | TC-NRR-011 to TC-NRR-013, TC-NRR-016 |
| `buyer candidate "<c>" replied "interested" and is on standby`, `the supplier "<s>" confirmed the order at <p>` | TC-NRR-014 to TC-NRR-018 |
| `the deal's PI has status "<st>"`, `the first PO has status "<st>"`, `the PO was sent <n> hours ago without an acknowledgement` | TC-NRR-014, TC-NRR-019 to TC-NRR-022 |
