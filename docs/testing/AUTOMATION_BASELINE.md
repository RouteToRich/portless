---
title: Automation baseline
scope: The Gherkin catalogue (TEST_CASES.md) run by the automation worker, backend + browser
---

# Automation baseline

## 2026-11-20 — local run, no Mongo, no LLM gateway

`worker_env(use_mongo=False)` with `LLM_GATEWAY_URL=""`, `--scope catalogue`, console bundle at
`../portless-ui/dist-qa` (browser runner on).

| | Backend | Browser (`@ui`) | Total |
|---|---:|---:|---:|
| Passed | 779 | 79 | 858 |
| Failed | 2 | 0 | 2 |
| Not automated | 160 | 56 | 216 |

The two failures are the LLM journeys `UC-15` and `TC-E2E-000`, which play the counterparties with the
LLM and fail by design without the gateway; run them on a backend with the gateway login.

Before this round (same machine, 2026-11-19): 665 passed, 4 failed, 186 not automated, 135 `needs_browser`.

### Found and fixed by these runs

- The daily data jobs (country feeds, regulation watch …) reached public sites inside automation runs —
  now off in the worker unless a scenario turns one on.
- `GET /deals/compliance/countries/{iso2}` returned an invented "standard" profile for any two-letter code
  instead of 404 (`country_profiles.get`).
- Console: the Deal Board card did not show the deal id; the Approvals Inbox and the gate card cut it to
  8 / 12 characters; loading skeletons said nothing to a screen reader or a test; error states were worded
  four different ways and three pages offered no Retry.
- Catalogue cases out of date with the product (legacy Stripe hold off by default, console labels,
  the paged directory endpoint, a too-short test password, an embargoed-destination expectation).

### Not automated yet (biggest groups)

- Browser forms: add buyer / supplier offer / requirement, propose a match, record loading / milestone,
  round entry, draft approve / reject / modify, open a document, autonomy level select, two tabs refreshing.
- Backend: concurrency at the same moment, SSE stream, ledger-breach fixtures, "driven to settlement"
  multi-step fixtures, `the app starts` under a production profile.

Run it again with `services/automation/worker.py` (see CLAUDE.md, Automation mode).
