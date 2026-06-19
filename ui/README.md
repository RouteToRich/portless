# PortLess Ops Console (UI)

React + TypeScript + Vite operator console for the PortLess platform. It talks to
the FastAPI **API Gateway** (BFF pattern) which fans out to the domain services
over REST.

## Stack
- React 18 + TypeScript + Vite
- TanStack Query (server cache)
- React Router (navigation)
- Axios (typed API client)

## Screens (blueprint §2.2)
- **Opportunity Radar** — ranked HS-code arbitrage cards, one-click "Start Deal"
- **Deal Board** — Kanban across the deal state-machine stages
- **Deal Detail** — overview, margin, parties, agent activity (timeline TODO)
- **Approvals Inbox** — pending HITL decisions with Approve / Reject
- **Money & Margin** — projected vs. realized margin, escrow/payouts (TODO)
- **Exceptions** — look up disputes / failed QC and remediation plans
- **Autonomy Settings** — per-action-class L0–L3 levels
- **System Health** — gateway + downstream service health
- **Kill Switch** — global pause (confirmation only; backend command TODO)

## Configure
The UI targets the gateway via `VITE_API_BASE_URL` (default `http://localhost:8000`).
Without it, the Vite dev proxy forwards `/api` to the gateway.

```bash
cp .env.example .env
```

## Run

```bash
npm install
npm run dev      # http://localhost:5173
```

Start the backend gateway first (from the `backend/` folder):

```bash
cd ../backend
uvicorn services.api_gateway.main:app --reload --port 8000
```

> **Status:** UI is wired to real gateway endpoints. Screens whose backend
> service methods are still `NotImplementedError` show clean loading / error /
> empty states until the corresponding service logic lands.
