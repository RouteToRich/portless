# PortLess operator console (UI)

React + TypeScript + Vite console for the operator. It talks to the backend monolith
(or the API Gateway in microservice mode) over REST with a JWT; counterparties never use
it — buyers and suppliers act by e-mail.

## Stack

React 18, TypeScript, Vite, TanStack Query, React Router, Axios, Tailwind + Radix
primitives. Types, endpoints and hooks live in `src/api/`; pages in `src/pages/`;
`src/mocks/adapter.ts` serves `backend/mocked/` fixtures in mock mode.

## Screens

| Route | Screen | What the operator does there |
|---|---|---|
| `/radar` | Opportunity Radar | Ranked HS-code × market opportunities from the scan (own Apify actors), source health, start a deal |
| `/catalogue` | Catalogue | Product lines and corridors that drive scans, actor inputs and outreach copy; scan the catalogue |
| `/deals` | Deal Board | Kanban across the deal state machine |
| `/deals/:id` | Deal Detail | Overview (parties, enrichment, buyer outreach funnel), Negotiation (rounds, e-mail thread, drafts), Logistics (QC, freight, milestones, tracking), Documents (PI/PO, document pack, evidence pack), Payments (escrow, tranches, acceptance window, ledger, journal + CSV), Timeline |
| `/approvals` | Approvals Inbox | Every waiting gate with payload, risk flags, timeout, four-eyes state; approve / reject / edit a draft |
| `/money` | Money & Margin | Projected vs realised margin, escrow ledgers across deals |
| `/exceptions` | Exceptions | Open exceptions, the resolver's plan, the playbook an approval will run |
| `/autonomy` | Autonomy Settings | Per-action level, timeout, on-timeout default, value threshold; money ceiling and four-eyes |
| `/health` | System Health | Downstream health, production readiness checks, data sources and Apify run budget, operator digest |
| `/knowledge` | Knowledge Base | RAG corpus upload / query |
| `/admin/organizations` | Organizations | Buyer / supplier organizations and users |

Live updates: the Approvals Inbox subscribes to `GET /hitl-approval/stream` (SSE) with the
token as a query parameter.

## Configure and run

```bash
npm install
npm run dev          # http://localhost:5173 → backend on http://localhost:8081
npm run dev:mock     # same UI on fixtures, no backend needed
npm run lint         # tsc --noEmit
npm run build        # production bundle in dist/
```

`VITE_API_BASE_URL` points the client at a different backend (default the monolith on
8081; the Vite dev proxy forwards `/api` otherwise). Log in with an account whose e-mail is
on the backend's `AUTH_PORTLESS_EMAILS` allowlist to see the operator screens; other
accounts see only their own organization's deals.
