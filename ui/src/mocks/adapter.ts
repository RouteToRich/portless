// Mock axios adapter — serves the fixtures in backend/mocked/ so the entire
// Ops Console (System Health included) runs without any backend.
//
// Enabled via VITE_USE_MOCKS=true (see api/client.ts). All matching happens at
// the axios *adapter* level, so no real network request is ever made.
//
// NOTE: the backend/mocked fixtures were authored independently of the UI's
// TypeScript types (src/api/types.ts) and disagree on nearly every entity, so
// each handler below maps the fixture shape into the shape the UI consumes.
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from "axios";

// Health
import gatewayOk from "@mocked/health/gateway_ok.json";
import downstreamAllOk from "@mocked/health/downstream_all_ok.json";
import downstreamDegraded from "@mocked/health/downstream_degraded.json";

// Top-level lists (wrapped fixtures → unwrapped to the arrays the UI consumes)
import dealList from "@mocked/deals/deal_list.json";
import scanResult from "@mocked/trade_min/scan_result.json";
import approvalList from "@mocked/hitl_approval/approval_list.json";
import exceptionList from "@mocked/exception_resolver/exception_list.json";

// Rich exception detail fixtures (share exception_id with the list above).
import excQc from "@mocked/exception_resolver/exception_qc_failed.json";
import excPayment from "@mocked/exception_resolver/exception_payment_failed.json";
import excFreight from "@mocked/exception_resolver/exception_freight_delay.json";
import excCustoms from "@mocked/exception_resolver/exception_customs_rejected.json";
import excDocument from "@mocked/exception_resolver/exception_document_error.json";
import excMargin from "@mocked/exception_resolver/exception_margin_breach.json";
import excConnector from "@mocked/exception_resolver/exception_connector_error.json";
import excNegotiation from "@mocked/exception_resolver/exception_negotiation_failed.json";

// Deal detail fixtures, keyed loosely by status for the detail page.
import dealNegotiating from "@mocked/deals/deal_detail_negotiating.json";
import dealEscrowFunded from "@mocked/deals/deal_detail_escrow_funded.json";
import dealInTransit from "@mocked/deals/deal_detail_in_transit.json";
import dealPaid from "@mocked/deals/deal_detail_paid.json";
import dealDispute from "@mocked/deals/deal_detail_dispute.json";

// RAG knowledge base.
import ragStats from "@mocked/rag/stats.json";
import ragQueryFixture from "@mocked/rag/query.json";

// Deal-detail sub-resources.
import matchScoreFixture from "@mocked/match_engine/score_result.json";
import holdCreated from "@mocked/escrow_guard/hold_created.json";
import ledgerFixture from "@mocked/escrow_guard/ledger.json";
import ledgersListFixture from "@mocked/escrow_guard/ledgers_list.json";
import qcBooking from "@mocked/shield_link/qc_booking.json";
import freightBooking from "@mocked/shield_link/freight_booking.json";
import documentsList from "@mocked/docugen/documents_list.json";

// Readiness gate, data-source reachability, Apify enrichment.
import readinessFixture from "@mocked/health/readiness.json";
import tradeSourcesFixture from "@mocked/trade_min/sources.json";
import enrichResultFixture from "@mocked/deals/enrich_result.json";

// Batch A/B/C: outreach funnel, timeline + evidence pack, journal, catalogue,
// digest, four-eyes approval, autonomy policy v2, carrier milestones.
import dealOutreach from "@mocked/deals/deal_detail_outreach.json";
import timelineFixture from "@mocked/deals/timeline.json";
import evidencePackFixture from "@mocked/deals/evidence_pack.json";
import journalFixture from "@mocked/escrow_guard/journal.json";
import catalogueFixture from "@mocked/trade_min/catalogue.json";
import digestFixture from "@mocked/hitl_approval/digest.json";
import approvalFourEyes from "@mocked/hitl_approval/approval_four_eyes.json";
import autonomyPolicyFixture from "@mocked/hitl_approval/autonomy_policy_v2.json";

type RawObj = Record<string, unknown>;

// Set VITE_MOCK_DEGRADED=true to exercise the "Partial Degradation" UI path
// (two services reported unreachable); defaults to all-healthy.
const DOWNSTREAM_FIXTURE =
  import.meta.env.VITE_MOCK_DEGRADED === "true" ? downstreamDegraded : downstreamAllOk;

type Handler = (ctx: {
  id: string;
  /** Every `:param` captured from the route, by name (e.g. { id, role }). */
  params: Record<string, string>;
  query: URLSearchParams;
  body: RawObj;
  ownerId: string;
}) => unknown;

// Handlers throw this to return a non-200 (e.g. 401 bad login, 409 email taken).
class MockError extends Error {
  constructor(public status: number, public detail: string) {
    super(detail);
  }
}

const unwrap = <T>(o: { items?: T[] } | T[]): T[] => (Array.isArray(o) ? o : (o.items ?? []));

// ── Deal ──────────────────────────────────────────────────────────────────
// List fixtures use flat buyer_id/supplier_id + origin/destination_country (no
// parties); detail fixtures carry `parties` as a {buyer, supplier} object or
// omit it. The UI's Deal type wants `parties: Party[]`, so normalize every shape.
function toParty(
  role: "buyer" | "supplier",
  src: RawObj | undefined,
  idFallback: unknown,
  countryFallback: unknown
) {
  const name = src?.name ?? src?.legal_name ?? idFallback;
  const country = src?.country ?? countryFallback;
  if (name == null && country == null) return null;
  return {
    role,
    legal_name: String(name ?? role),
    country: String(country ?? "—"),
    gstin: src?.gstin ?? null,
    iec: src?.iec ?? null,
    verification_status: src?.verification_status ?? null,
    risk_score: src?.risk_score ?? null,
  };
}

function normalizeDeal(d: RawObj) {
  let parties: unknown[];
  if (Array.isArray(d.parties)) {
    parties = d.parties;
  } else {
    const p = (d.parties && typeof d.parties === "object" ? d.parties : {}) as RawObj;
    // List fixtures put the buyer at destination_country, supplier at origin.
    const buyer = toParty("buyer", p.buyer as RawObj, d.buyer_id, d.destination_country);
    const supplier = toParty("supplier", p.supplier as RawObj, d.supplier_id, d.origin_country);
    parties = [buyer, supplier].filter(Boolean);
  }
  return {
    ...d,
    operator_id: d.operator_id ?? "OP-000",
    autonomy_profile: d.autonomy_profile ?? "L1",
    margin_floor_pct: d.margin_floor_pct ?? null,
    est_margin_usd: d.est_margin_usd ?? null,
    realized_margin_usd: d.realized_margin_usd ?? null,
    parties,
  };
}

// ── Exception ────────────────────────────────────────────────────────────
// ExceptionDetail reads `remediation_plan` (string[]); fixtures expose
// `remediation_steps` (objects). Flatten to descriptions so neither crashes.
function normalizeException(e: RawObj) {
  const steps = Array.isArray(e.remediation_steps) ? e.remediation_steps : [];
  const remediation_plan = Array.isArray(e.remediation_plan)
    ? e.remediation_plan
    : steps
        .map((s) => (typeof s === "string" ? s : (s as RawObj)?.description))
        .filter((s): s is string => typeof s === "string");
  return { ...e, remediation_plan };
}

const exceptionDetails = [
  excQc,
  excPayment,
  excFreight,
  excCustoms,
  excDocument,
  excMargin,
  excConnector,
  excNegotiation,
] as RawObj[];
const exceptionById = new Map(exceptionDetails.map((e) => [e.exception_id as string, e]));
const exceptionListItems = unwrap<RawObj>(exceptionList as never);

// ── Opportunity ──────────────────────────────────────────────────────────
// The scan fixture uses buyer_country + per-signal scores; the UI's Opportunity
// type wants `market` + a single 0–10 `arbitrage_rank`.
function toOpportunity(o: RawObj) {
  const scores = [o.demand_signal_score, o.supply_availability_score, o.trade_flow_score].filter(
    (n): n is number => typeof n === "number"
  );
  const rank = scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length / 10 : 0;
  return {
    opportunity_id: o.opportunity_id,
    hs_code: o.hs_code,
    market: o.market ?? o.buyer_country ?? "—",
    arbitrage_rank: Number(rank.toFixed(2)),
    source: Array.isArray(o.sources) ? o.sources[0] : (o.source ?? null),
  };
}

// ── Approval ─────────────────────────────────────────────────────────────
// ApprovalsInbox reads `risk_flags.length`; the fixture omits it (and several
// other required ApprovalRecord fields). Fill safe defaults.
function normalizeApproval(a: RawObj): RawObj {
  return {
    ...a,
    risk_flags: Array.isArray(a.risk_flags) ? a.risk_flags : [],
    autonomy_level: a.autonomy_level ?? "L1",
    default_on_timeout: a.default_on_timeout ?? "REJECT",
    callback_payload: a.callback_payload ?? {},
    decision: a.decision ?? null,
    resolved_by: a.resolved_by ?? null,
    resolved_at: a.resolved_at ?? null,
    resolution_note: a.resolution_note ?? null,
    modified_body: a.modified_body ?? null,
    expires_at: a.expires_at ?? null,
    updated_at: a.updated_at ?? a.created_at ?? null,
  };
}
const approvalListItems: RawObj[] = [
  { ...(approvalFourEyes as RawObj) },
  ...unwrap<RawObj>(approvalList as never).map(normalizeApproval),
];

// ── Match score ──────────────────────────────────────────────────────────
// Fixture exposes cms_score + a weighted `breakdown`; the UI's MatchScore wants
// five flat 0–100 scores. Map each breakdown bucket to a percentage.
function pct(b: unknown): number {
  const bucket = b as { score?: number; max?: number } | undefined;
  if (bucket && typeof bucket.score === "number" && bucket.max) {
    return Math.min(100, Number(((bucket.score / bucket.max) * 100).toFixed(1)));
  }
  return 0;
}
function normalizeMatchScore(m: RawObj) {
  const b = (m.breakdown ?? {}) as RawObj;
  return {
    composite_score: m.composite_score ?? m.cms_score ?? 0,
    price_score: m.price_score ?? pct(b.price_competitiveness),
    margin_score: m.margin_score ?? pct(b.financial_health),
    risk_score: m.risk_score ?? pct(b.supply_reliability),
    logistics_score: m.logistics_score ?? pct(b.logistics_fit),
  };
}

// ── Deal sub-resources (single-object fixtures → arrays the UI maps over) ──
function normalizeHold(h: RawObj) {
  return {
    hold_id: h.hold_id,
    deal_id: h.deal_id,
    amount_usd: h.amount_usd ?? 0,
    status: h.status ?? "PENDING",
    stripe_payment_intent_id: h.stripe_payment_intent_id ?? null,
    funded_at: h.funded_at ?? null,
  };
}
function normalizeQc(q: RawObj) {
  return {
    qc_id: q.qc_id ?? q.order_id,
    deal_id: q.deal_id,
    provider: q.provider ?? "SGS",
    status: q.status ?? "BOOKED",
    report_url: q.report_url ?? null,
    booked_at: q.booked_at ?? null,
  };
}
function normalizeFreight(f: RawObj) {
  return {
    tpo_id: f.tpo_id ?? f.order_id,
    deal_id: f.deal_id,
    provider: f.provider ?? "Cogoport",
    status: f.status ?? "BOOKED",
    bl_number: f.bl_number ?? null,
    eta: f.eta ?? null,
  };
}
function normalizeDoc(d: RawObj) {
  return {
    document_id: d.document_id,
    deal_id: d.deal_id,
    document_type: d.document_type,
    status: d.status,
    file_url: d.file_url ?? d.download_url ?? null,
    created_at: d.created_at ?? null,
  };
}

const dealDetails = [dealNegotiating, dealEscrowFunded, dealInTransit, dealPaid, dealDispute, dealOutreach];
const documents = ((documentsList as { documents?: RawObj[] }).documents ?? []).map(normalizeDoc);

// ── Staged escrow (neutral provider) — mutable so operator actions persist ──
// One escrow per deal id; seeded lazily from the ledger fixture so every deal
// detail page shows the same realistic 30/40/20/10 schedule.
type Tranche = { tranche_id: string; trigger: string; pct: number; amount: number | null;
  status: string; proof_refs: string[]; ext_ref: string | null; released_at: string | null };
type MockEscrow = RawObj & { escrow_id: string; deal_id: string; amount: number; status: string;
  released_total: number; refunded_total: number; fees_total: number; tranches: Tranche[];
  disputed: boolean; funded_amount: number | null };
const escrowsByDeal = new Map<string, MockEscrow>();

function seedEscrow(dealId: string): MockEscrow {
  const existing = escrowsByDeal.get(dealId);
  if (existing) return existing;
  const l = ledgerFixture as RawObj;
  const legacy = normalizeHold(holdCreated as RawObj);
  const escrow: MockEscrow = {
    escrow_id: `ESC-${dealId.replace(/[^A-Z0-9]/gi, "").slice(-8).toUpperCase()}`,
    deal_id: dealId,
    provider: (l.provider as string) ?? "tazapay",
    amount: (l.principal as number) ?? legacy.amount_usd,
    currency: "USD",
    status: (l.status as string) ?? "FUNDED",
    ext_ref: `tzp_${dealId.slice(-6).toLowerCase()}`,
    client_secret: null,
    funded_at: legacy.funded_at ?? new Date(Date.now() - 86_400_000 * 9).toISOString(),
    funded_amount: (l.funded_amount as number) ?? null,
    created_at: new Date(Date.now() - 86_400_000 * 10).toISOString(),
    payment_method: "bank_transfer",
    checkout_url: `https://pay.tazapay.com/checkout/tzp_${dealId.slice(-6).toLowerCase()}`,
    released_total: (l.released_total as number) ?? 0,
    refunded_total: (l.refunded_total as number) ?? 0,
    fee_pct: 1.0,
    fees_total: (l.fees_total as number) ?? 0,
    shortfall: (l.shortfall as number | null) ?? null,
    settled_at: null,
    tranches: ((l.tranches as Tranche[]) ?? []).map((t) => ({ ...t, proof_refs: [...t.proof_refs] })),
    inspection_deadline: null,
    delivery_confirmed_at: null,
    inspection_opened_at: null,
    inspection_trigger: null,
    buyer_accepted_at: null,
    disputed: false,
    disputed_at: null,
  };
  escrowsByDeal.set(dealId, escrow);
  return escrow;
}

function escrowByEscrowId(escrowId: string): MockEscrow {
  const found = [...escrowsByDeal.values()].find((e) => e.escrow_id === escrowId);
  if (!found) throw new MockError(404, `Escrow hold '${escrowId}' not found`);
  return found;
}

function ledgerFor(escrow: MockEscrow): RawObj {
  const l = ledgerFixture as RawObj;
  const payouts: RawObj[] = ((l.payouts as RawObj[]) ?? []).map((p): RawObj => ({ ...p, deal_id: escrow.deal_id }));
  const paid = payouts
    .filter((p) => p.status === "COMPLETED")
    .reduce((s, p) => s + Number(p.amount ?? 0), 0);
  const net = Number((escrow.released_total - escrow.fees_total).toFixed(2));
  const owed = (l.supplier_owed as number | null) ?? null;
  const settled = escrow.tranches.length > 0 && escrow.tranches.every((t) => t.status === "RELEASED");
  const issues: string[] = [];
  if (escrow.funded_amount != null && Math.abs(escrow.funded_amount - escrow.amount) > 0.01) {
    issues.push(`funded ${escrow.funded_amount} != principal ${escrow.amount}`);
  }
  return {
    deal_id: escrow.deal_id,
    currency: "USD",
    provider: escrow.provider,
    escrow_id: escrow.escrow_id,
    status: escrow.status,
    principal: escrow.amount,
    funded_amount: escrow.funded_amount,
    shortfall: escrow.shortfall,
    released_total: escrow.released_total,
    refunded_total: escrow.refunded_total,
    outstanding: Number((escrow.amount - escrow.released_total - escrow.refunded_total).toFixed(2)),
    fees_total: escrow.fees_total,
    net_to_portless: net,
    payouts_total: paid,
    payouts_pending: 0,
    available_for_payout: Math.max(0, Number((net - paid).toFixed(2))),
    supplier_owed: owed,
    supplier_balance_due: owed != null ? Number((owed - paid).toFixed(2)) : null,
    realized_margin: escrow.released_total || paid ? Number((net - paid).toFixed(2)) : null,
    tranches: escrow.tranches,
    payouts,
    inspection_deadline: escrow.inspection_deadline,
    disputed: escrow.disputed,
    settled,
    reconciled: issues.length === 0,
    issues,
  };
}

// Apply an enrichment result onto a deal's parties (mock POST /deals/:id/enrich).
function applyEnrichment(deal: RawObj): RawObj {
  const res = enrichResultFixture as RawObj;
  const parties = (Array.isArray(deal.parties) ? deal.parties : []) as RawObj[];
  const merged = parties.map((p) => {
    const src = (res[p.role as string] ?? {}) as RawObj;
    const { screening: _screening, legal_name: _ln, country: _c, ...contact } = src;
    void _screening; void _ln; void _c;
    return {
      ...p,
      ...contact,
      sanctions_checked_at: new Date().toISOString(),
      enriched_at: new Date().toISOString(),
    };
  });
  return { ...deal, parties: merged };
}

// ── Mock auth + per-user data ────────────────────────────────────────────
// A built-in demo account owns all the seed fixtures; freshly registered users
// start with an empty Deal Board and build their own flow — so the UI is unique
// per login, mirroring the real backend's operator-scoped deals.
interface MockUser {
  user_id: string;
  email: string;
  name: string;
  password: string;
}
const DEMO_OWNER = "USR-DEMO";
const mockUsers = new Map<string, MockUser>([
  ["demo@portless.io", { user_id: DEMO_OWNER, email: "demo@portless.io", name: "Demo Operator", password: "demo123" }],
]);

const seedDeals = [...unwrap<RawObj>(dealList as never), dealOutreach as RawObj]
  .map(normalizeDeal)
  .map((d) => ({ ...d, operator_id: DEMO_OWNER }));
const dealsByOwner = new Map<string, RawObj[]>([[DEMO_OWNER, seedDeals]]);

function encodeMockToken(u: MockUser): string {
  return "mock." + btoa(JSON.stringify({ sub: u.user_id, email: u.email, name: u.name }));
}
function ownerFromAuthHeader(header: string | undefined): string {
  if (!header) return "";
  const raw = header.replace(/^Bearer\s+/i, "");
  if (!raw.startsWith("mock.")) return "";
  try {
    return (JSON.parse(atob(raw.slice(5))) as { sub?: string }).sub ?? "";
  } catch {
    return "";
  }
}
function publicUser(u: MockUser) {
  return { user_id: u.user_id, email: u.email, name: u.name, created_at: new Date().toISOString() };
}

// ── RAG ingest job + GridFS file-store simulation ────────────────────────
// Track upload start times so a polled job reports running for ~2.5s then
// completes — mirroring the real background ingestion flow.
const ragJobs = new Map<string, number>();
function ragJobStatus(id: string) {
  const started = ragJobs.get(id);
  const done = !started || Date.now() - started > 2500;
  return {
    job_id: id,
    status: done ? "completed" : "running",
    inserted_count: done ? 42 : null,
    graph_inserted_count: done ? 18 : null,
    error: null,
    started_at: new Date(started ?? Date.now()).toISOString(),
    completed_at: done ? new Date().toISOString() : null,
  };
}

// Mirrors the real /rag/files list (MongoDB GridFS) so the Knowledge Base
// page's "Persisted Documents" panel reflects actual uploads in mock mode too.
interface MockRagFile {
  file_id: string;
  filename: string;
  size: number;
  backend: string;
  content_type: string | null;
  uploaded_at: string;
}
const ragFiles: MockRagFile[] = [
  {
    file_id: "gridfs-seed-0001",
    filename: "trade-playbook-rice-hs1006.pdf",
    size: 482_311,
    backend: "mongodb",
    content_type: "application/pdf",
    uploaded_at: new Date(Date.now() - 86_400_000 * 3).toISOString(),
  },
  {
    file_id: "gridfs-seed-0002",
    filename: "supplier-terms-glossary.md",
    size: 18_204,
    backend: "mongodb",
    content_type: "text/markdown",
    uploaded_at: new Date(Date.now() - 86_400_000).toISOString(),
  },
];

// Reuses the single realistic query.json fixture for every question, just
// swapping in the asked question so the test-chat panel feels responsive
// without needing a fixture per possible question.
function ragQueryAnswer(body: RawObj) {
  const question = String(body.question ?? "").trim() || "What documents are needed?";
  return { ...ragQueryFixture, question };
}

function ragStartJob(body: RawObj) {
  const id = `rag-job-${Date.now().toString(16)}`;
  ragJobs.set(id, Date.now());

  // The real endpoint is multipart/form-data; extract File entries so the
  // mock file store (below) grows with whatever was actually dropped in the
  // UI, just like the real MongoDB GridFS-backed store does.
  if (typeof FormData !== "undefined" && body instanceof FormData) {
    const files = body.getAll("files").filter((f): f is File => f instanceof File);
    files.forEach((f, i) => {
      ragFiles.push({
        file_id: `gridfs-${Date.now().toString(16)}-${i}`,
        filename: f.name,
        size: f.size,
        backend: "mongodb",
        content_type: f.type || null,
        uploaded_at: new Date().toISOString(),
      });
    });
  }

  return { job_id: id, status: "pending", message: `Accepted. Poll /rag/ingest/${id}.` };
}

// Route table: "METHOD /path" → handler. `:param` captures a path segment.
// ── Autonomy policy v2 (mutable so the settings page round-trips) ──────────
type PolicyAction = RawObj & { action: string };
const policyState = JSON.parse(JSON.stringify(autonomyPolicyFixture)) as {
  money_ceiling: string; money_auto_approve_below_usd: number | null; actions: PolicyAction[];
};
const LEVEL_RANK: Record<string, number> = { L0: 0, L1: 1, L2: 2, L3: 3 };

function recomputePolicy() {
  for (const a of policyState.actions) {
    const override = a.override as string | null;
    const env = a.env as string | null;
    let effective = override ?? env ?? (a.default as string);
    let source = override ? "override" : env ? "env" : "default";
    if (a.money_moving && LEVEL_RANK[effective] > LEVEL_RANK[policyState.money_ceiling]) {
      effective = policyState.money_ceiling;
      source = "money_ceiling";
    }
    a.effective = effective;
    a.source = source;
    a.timeout_seconds = (a.timeout_override as number | null) ?? a.default_timeout_seconds;
    a.default_on_timeout = (a.on_timeout_override as string | null) ?? (a.money_moving ? "REJECT" : a.default_on_timeout);
    const th = a.threshold_override as number | null;
    a.auto_approve_below_usd = th ?? (a.money_moving ? policyState.money_auto_approve_below_usd : null);
  }
}

// ── Freight orders: milestone state per deal ───────────────────────────────
const freightState = new Map<string, { last_milestone: string | null; last_milestone_at: string | null; delivered_at: string | null; status: string }>();
const MILESTONE_ORDER = ["DEPARTED", "IN_TRANSIT", "ARRIVED", "CLEARED", "DELIVERED"];
function freightFor(dealId: string) {
  const f = normalizeFreight(freightBooking as RawObj);
  const existing = freightState.get(dealId);
  if (existing) return existing;
  const init = { last_milestone: null as string | null, last_milestone_at: null as string | null, delivered_at: null as string | null, status: f.status as string };
  freightState.set(dealId, init);
  return init;
}
function applyMilestone(dealId: string, milestone: string) {
  const st = freightFor(dealId);
  if (st.last_milestone && MILESTONE_ORDER.indexOf(milestone) < MILESTONE_ORDER.indexOf(st.last_milestone)) {
    return { status: "ignored", milestone, deal_id: dealId, last_milestone: st.last_milestone };
  }
  const now = new Date().toISOString();
  st.last_milestone = milestone;
  st.last_milestone_at = now;
  st.status = milestone === "DEPARTED" || milestone === "IN_TRANSIT" ? "IN_PROGRESS" : "COMPLETED";
  if (milestone === "DELIVERED") {
    st.delivered_at = now;
    // DELIVERED → the escrow's acceptance window opens (once proof tranches are released).
    const e = seedEscrow(dealId);
    openWindow(e, "delivery");
  }
  return { status: "ok", milestone, deal_id: dealId };
}
function openWindow(e: MockEscrow, trigger: string): MockEscrow {
  if (e.inspection_deadline) return e;
  const pendingFinal = e.tranches.some((t) => t.trigger === "ACCEPTED" && t.status === "PENDING");
  if (!pendingFinal || (e.status !== "FUNDED" && e.status !== "RELEASED")) return e;
  const now = new Date();
  const earlierPending = e.tranches.some((t) => t.trigger !== "ACCEPTED" && t.status === "PENDING");
  e.delivery_confirmed_at = (e.delivery_confirmed_at as string | null) ?? now.toISOString();
  if (earlierPending) return e; // recorded; opens when the proof tranche releases
  e.inspection_deadline = new Date(now.getTime() + 72 * 3600_000).toISOString();
  e.inspection_opened_at = now.toISOString();
  e.inspection_trigger = trigger;
  return e;
}

// ── Catalogue (mutable) ─────────────────────────────────────────────────────
const catalogueState = JSON.parse(JSON.stringify(catalogueFixture)) as RawObj & { products: RawObj[]; defaults: { hs_codes: string[]; markets: string[] } };
function catalogueView() {
  const active = catalogueState.products.filter((p) => p.active !== false);
  const corridors = active.flatMap((p) =>
    ((p.markets as string[]) ?? []).map((m) => ({ hs_code: p.hs_code, name: p.name, market: m }))
  );
  return { ...catalogueState, corridors, active: active.length > 0 };
}

// ── Journal: CSV rendering for ?format=csv ────────────────────────────────
function journalCsv(j: RawObj): string {
  const rows = (j.entries as RawObj[]) ?? [];
  const head = "deal_id,seq,at,event,debit_account,credit_account,amount,currency,ref,memo";
  const body = rows.map((e) =>
    [j.deal_id, e.seq, e.at ?? "", e.event, e.debit, e.credit, Number(e.amount).toFixed(2), e.currency, e.ref ?? "", `"${String(e.memo ?? "").replace(/"/g, '""')}"`].join(",")
  );
  return [head, ...body].join("\n") + "\n";
}

function evidenceHtml(dealId: string): string {
  const pack: RawObj = { ...(evidencePackFixture as RawObj), deal_id: dealId };
  return `<!doctype html><html><head><meta charset="utf-8"><title>Evidence pack ${dealId}</title></head><body style="font:14px system-ui;margin:32px"><h1>Evidence pack — deal ${dealId}</h1><p>SHA-256 ${String(pack.content_hash)}</p><p>Mock render — the live backend produces the full page (parties, PI/PO, documents, ledger, journal, approvals, exceptions, correspondence, timeline).</p><pre>${JSON.stringify(pack, null, 2).slice(0, 4000)}</pre></body></html>`;
}

// Find (or seed from a fixture) the owned deal, persisting it on the owner's board.
function ownedDeal(ownerId: string, id: string): RawObj {
  const list = dealsByOwner.get(ownerId) ?? [];
  const existing = list.find((d) => d.deal_id === id);
  if (existing) return existing;
  const base = normalizeDeal((dealDetails as RawObj[]).find((d) => d.deal_id === id) ?? (dealNegotiating as RawObj));
  const deal = { ...base, deal_id: id };
  dealsByOwner.set(ownerId, [deal, ...list]);
  return deal;
}
function saveOwnedDeal(ownerId: string, deal: RawObj) {
  const list = dealsByOwner.get(ownerId) ?? [];
  dealsByOwner.set(ownerId, list.some((d) => d.deal_id === deal.deal_id)
    ? list.map((d) => (d.deal_id === deal.deal_id ? deal : d))
    : [deal, ...list]);
  return deal;
}

recomputePolicy();

const routes: Record<string, Handler> = {
  "GET /health": () => gatewayOk,
  "GET /health/downstream": () => DOWNSTREAM_FIXTURE,
  "GET /health/readiness": () => ({ ...readinessFixture, generated_at: new Date().toISOString() }),

  // Auth
  "POST /auth/register": ({ body }) => {
    const email = String(body.email ?? "").trim().toLowerCase();
    if (!email || !body.password) throw new MockError(422, "Email and password required");
    if (mockUsers.has(email)) throw new MockError(409, `An account with '${email}' already exists`);
    const user: MockUser = {
      user_id: `USR-${Math.random().toString(36).slice(2, 10).toUpperCase()}`,
      email,
      name: String(body.name ?? email),
      password: String(body.password),
    };
    mockUsers.set(email, user);
    dealsByOwner.set(user.user_id, []); // fresh, empty board
    return { access_token: encodeMockToken(user), token_type: "bearer", user: publicUser(user) };
  },
  "POST /auth/login": ({ body }) => {
    const email = String(body.email ?? "").trim().toLowerCase();
    const user = mockUsers.get(email);
    if (!user || user.password !== String(body.password ?? "")) {
      throw new MockError(401, "Invalid email or password");
    }
    return { access_token: encodeMockToken(user), token_type: "bearer", user: publicUser(user) };
  },
  "GET /auth/me": ({ ownerId }) => {
    const user = [...mockUsers.values()].find((u) => u.user_id === ownerId);
    if (!user) throw new MockError(401, "Invalid or expired token");
    return publicUser(user);
  },

  // Deals (scoped to the authenticated user)
  "GET /deals": ({ ownerId }) => dealsByOwner.get(ownerId) ?? [],
  "GET /deals/:id": ({ id, ownerId }) => {
    const owned = (dealsByOwner.get(ownerId) ?? []).find((d) => d.deal_id === id);
    if (owned) return owned;
    // Fall back to a detail fixture (demo deals map to these by id).
    return normalizeDeal((dealDetails as RawObj[]).find((d) => d.deal_id === id) ?? (dealNegotiating as RawObj));
  },
  "POST /deals": ({ body, ownerId }) => {
    const deal = normalizeDeal({
      deal_id: `DEAL-MOCK-${Date.now().toString(36).toUpperCase()}`,
      status: "DRAFT",
      market: "AE",
      ...body,
      operator_id: ownerId,
    });
    const list = dealsByOwner.get(ownerId) ?? [];
    dealsByOwner.set(ownerId, [deal, ...list]);
    return deal;
  },
  "PATCH /deals/:id/status": ({ id, body, ownerId }) => {
    const list = dealsByOwner.get(ownerId) ?? [];
    const existing = list.find((d) => d.deal_id === id);
    const updated = normalizeDeal({
      ...(existing ?? (dealNegotiating as RawObj)),
      deal_id: id,
      status: body.target_status ?? "MATCHING",
      operator_id: ownerId,
    });
    if (existing) dealsByOwner.set(ownerId, list.map((d) => (d.deal_id === id ? updated : d)));
    return updated;
  },
  // Operator correction of a counterparty — persists on the owned deal.
  "PATCH /deals/:id/parties/:role": ({ params, body, ownerId }) => {
    const id = params.id;
    const role = params.role;
    if (role !== "buyer" && role !== "supplier") throw new MockError(422, "role must be buyer or supplier");
    const list = dealsByOwner.get(ownerId) ?? [];
    const base = list.find((d) => d.deal_id === id)
      ?? normalizeDeal((dealDetails as RawObj[]).find((d) => d.deal_id === id) ?? (dealNegotiating as RawObj));
    const parties = (Array.isArray(base.parties) ? base.parties : []) as RawObj[];
    const clean = Object.fromEntries(Object.entries(body).filter(([, v]) => v != null && v !== ""));
    const touchesContact = ["email", "website", "phone", "address"].some((k) => k in clean);
    const stamped = touchesContact
      ? { ...clean, contact_source: "operator", contact_confidence: 1.0,
          email_status: clean.email ? "unverified" : undefined }
      : clean;
    const idx = parties.findIndex((p) => p.role === role);
    const nextParties = idx >= 0
      ? parties.map((p, i) => (i === idx ? { ...p, ...stamped } : p))
      : [...parties, { role, legal_name: clean.legal_name ?? `Unknown ${role}`, country: clean.country ?? "—", ...stamped }];
    const updated = { ...base, deal_id: id, parties: nextParties, updated_at: new Date().toISOString() };
    if (list.some((d) => d.deal_id === id)) {
      dealsByOwner.set(ownerId, list.map((d) => (d.deal_id === id ? updated : d)));
    } else {
      dealsByOwner.set(ownerId, [updated, ...list]);
    }
    return updated;
  },
  // Apify enrichment — applies the fixture contacts/screening onto the deal.
  "POST /deals/:id/enrich": ({ id, ownerId }) => {
    const list = dealsByOwner.get(ownerId) ?? [];
    const base = list.find((d) => d.deal_id === id)
      ?? normalizeDeal((dealDetails as RawObj[]).find((d) => d.deal_id === id) ?? (dealNegotiating as RawObj));
    const updated = applyEnrichment({ ...base, deal_id: id });
    if (list.some((d) => d.deal_id === id)) {
      dealsByOwner.set(ownerId, list.map((d) => (d.deal_id === id ? updated : d)));
    } else {
      dealsByOwner.set(ownerId, [updated, ...list]);
    }
    return { ...(enrichResultFixture as RawObj), deal_id: id, deal: updated };
  },

  // Deal timeline + evidence pack
  "GET /deals/:id/timeline": ({ id }) => ({ ...(timelineFixture as RawObj), deal_id: id }),
  "GET /deals/:id/evidence-pack": ({ id, query }) =>
    query.get("format") === "html"
      ? evidenceHtml(id)
      : { ...(evidencePackFixture as RawObj), deal_id: id, generated_at: new Date().toISOString() },

  // Buyer outreach funnel
  "POST /deals/:id/buyer-candidates": ({ id, body, ownerId }) => {
    const deal = ownedDeal(ownerId, id);
    const cands = (Array.isArray(deal.buyer_candidates) ? deal.buyer_candidates : []) as RawObj[];
    if (cands.some((c) => c.candidate_id === body.candidate_id)) {
      throw new MockError(422, `buyer candidate '${String(body.candidate_id)}' already on deal ${id}`);
    }
    return saveOwnedDeal(ownerId, { ...deal, buyer_candidates: [...cands, { status: "pending", ...body }], updated_at: new Date().toISOString() });
  },
  "POST /deals/:id/outreach/start": ({ id, ownerId }) => {
    const deal = ownedDeal(ownerId, id);
    const cands = (Array.isArray(deal.buyer_candidates) ? deal.buyer_candidates : []) as RawObj[];
    const active = cands.filter((c) => c.status === "contacted" || c.status === "followed_up").length;
    let slots = Math.max(0, 2 - active);
    let contacted = 0;
    const now = new Date().toISOString();
    const next = cands.map((c) => {
      if (slots > 0 && c.status === "pending" && c.email) {
        slots -= 1;
        contacted += 1;
        return { ...c, status: "contacted", contacted_at: now, email_id: `OUTI${id.replace(/-/g, "").slice(-10)}${String(c.candidate_id).toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 12)}` };
      }
      return c;
    });
    saveOwnedDeal(ownerId, { ...deal, buyer_candidates: next, updated_at: now });
    const waiting = next.length > 0 && !next.some((c) => c.status === "interested" || c.status === "selected");
    return { deal_id: id, contacted, waiting_for_buyer: waiting };
  },
  "POST /deals/:id/buyer-candidates/:cid/skip": ({ params, ownerId }) => {
    const deal = ownedDeal(ownerId, params.id);
    const cands = (Array.isArray(deal.buyer_candidates) ? deal.buyer_candidates : []) as RawObj[];
    if (!cands.some((c) => c.candidate_id === params.cid)) throw new MockError(422, `buyer candidate '${params.cid}' not on deal ${params.id}`);
    const now = new Date().toISOString();
    let advanced = 0;
    const next = cands.map((c) =>
      c.candidate_id === params.cid ? { ...c, status: "declined", replied_at: now, reply_preview: "skipped by operator" } : c
    );
    const active = next.filter((c) => c.status === "contacted" || c.status === "followed_up").length;
    if (active < 2) {
      const idx = next.findIndex((c) => c.status === "pending" && c.email);
      if (idx >= 0) {
        next[idx] = { ...next[idx], status: "contacted", contacted_at: now };
        advanced = 1;
      }
    }
    saveOwnedDeal(ownerId, { ...deal, buyer_candidates: next, updated_at: now });
    const open = next.some((c) => ["pending", "contacted", "followed_up", "interested", "selected"].includes(String(c.status)));
    return { deal_id: params.id, candidate_id: params.cid, advanced, exhausted: !open && next.length > 0 };
  },

  // Catalogue / corridors
  "GET /trade-min/catalogue": () => catalogueView(),
  "PUT /trade-min/catalogue": ({ body }) => {
    const products = (Array.isArray(body.products) ? body.products : []) as RawObj[];
    const seen = new Set<string>();
    for (const p of products) {
      const hs = String(p.hs_code ?? "").replace(/\D/g, "");
      if (hs.length < 4) throw new MockError(422, `HS code ${JSON.stringify(p.hs_code)} must have at least 4 digits`);
      if (!String(p.name ?? "").trim()) throw new MockError(422, `product line ${String(p.product_id)} needs a name`);
      if (seen.has(String(p.product_id))) throw new MockError(422, `duplicate product_id ${String(p.product_id)}`);
      seen.add(String(p.product_id));
      p.hs_code = hs;
      p.markets = [...new Set(((p.markets as string[]) ?? []).map((m) => m.trim().toUpperCase().slice(0, 2)).filter(Boolean))].sort();
      p.active = p.active !== false;
    }
    catalogueState.products = products;
    catalogueState.updated_by = body.updated_by ?? "operator";
    catalogueState.updated_at = new Date().toISOString();
    return catalogueView();
  },

  // Autonomy policy v2
  "GET /hitl-approval/autonomy-policy": () => policyState,
  "PUT /hitl-approval/autonomy-policy": ({ body }) => {
    const levels = (body.levels ?? {}) as Record<string, string | null>;
    const gates = (body.gates ?? {}) as Record<string, RawObj | null>;
    for (const a of policyState.actions) {
      if (a.action in levels) a.override = levels[a.action];
      const g = gates[a.action];
      if (g === null) {
        a.override = null; a.timeout_override = null; a.on_timeout_override = null; a.threshold_override = null;
      } else if (g) {
        if ("level" in g) a.override = g.level;
        if ("timeout_seconds" in g) a.timeout_override = g.timeout_seconds;
        if ("default_on_timeout" in g) a.on_timeout_override = g.default_on_timeout;
        if ("auto_approve_below_usd" in g) a.threshold_override = g.auto_approve_below_usd;
      }
    }
    if (body.set_money_threshold) {
      policyState.money_auto_approve_below_usd = (body.money_auto_approve_below_usd as number | null) ?? null;
    }
    recomputePolicy();
    return policyState;
  },

  // Operator digest
  "GET /hitl-approval/digest": () => ({ ...(digestFixture as RawObj), generated_at: new Date().toISOString() }),
  "POST /hitl-approval/digest/send": () => ({
    telegram: false,
    email: [],
    text: (digestFixture as RawObj).text,
  }),

  // Opportunities — the UI calls the trade-min prefix; the bare paths are kept
  // for the standalone gateway shape.
  "GET /opportunities": () =>
    ((scanResult as { opportunities?: RawObj[] }).opportunities ?? []).map(toOpportunity),
  "GET /trade-min/opportunities": () =>
    ((scanResult as { opportunities?: RawObj[] }).opportunities ?? []).map(toOpportunity),
  "GET /opportunities/:id": ({ id }) => {
    const all = (scanResult as { opportunities?: RawObj[] }).opportunities ?? [];
    const found = all.find((o) => o.opportunity_id === id);
    if (!found) throw new MockError(404, `Opportunity ${id} not found`);
    // Raw fixture fields (seller_candidates, buyer_signals, tariff_data, …)
    // plus the normalized list-shape fields the UI type expects.
    return { ...found, ...toOpportunity(found) };
  },
  "GET /trade-min/opportunities/:id": ({ id }) => {
    const all = (scanResult as { opportunities?: RawObj[] }).opportunities ?? [];
    const found = all.find((o) => o.opportunity_id === id);
    if (!found) throw new MockError(404, `Opportunity ${id} not found`);
    return { ...found, ...toOpportunity(found) };
  },
  "POST /opportunities/scan": () =>
    ((scanResult as { opportunities?: RawObj[] }).opportunities ?? []).map(toOpportunity),
  "POST /trade-min/scan": () =>
    ((scanResult as { opportunities?: RawObj[] }).opportunities ?? []).map(toOpportunity),
  "GET /trade-min/sources": () => tradeSourcesFixture,

  // Approvals (four-eyes aware: a gate with required_approvals=2 collects two
  // distinct approvers before it resolves; the same operator twice → 409).
  "GET /hitl-approval/approvals": ({ query }) => {
    const status = query.get("status");
    return status ? approvalListItems.filter((a) => a.status === status) : approvalListItems;
  },
  "GET /hitl-approval/approvals/:id": ({ id }) => {
    const found = approvalListItems.find((a) => a.approval_id === id);
    if (!found) throw new MockError(404, `Approval '${id}' not found`);
    return found;
  },
  "POST /hitl-approval/approvals/:id/resolve": ({ id, body }) => {
    const idx = approvalListItems.findIndex((a) => a.approval_id === id);
    const found = approvalListItems[idx >= 0 ? idx : 0];
    if (found.status !== "PENDING") throw new MockError(409, `Approval '${id}' is already ${String(found.status)}`);
    const decision = body.decision === "REJECT" ? "REJECT" : "APPROVE";
    const by = String(body.resolved_by ?? "ops@portless.io");
    const now = new Date().toISOString();
    const required = Number(found.required_approvals ?? 1);
    const sigs = [...((found.approvals as RawObj[]) ?? [])];
    if (decision === "APPROVE" && required > 1) {
      if (sigs.some((s) => s.approved_by === by)) {
        throw new MockError(409, `Approval '${id}' already carries '${by}'s approval — a different operator must countersign`);
      }
      sigs.push({ approved_by: by, approved_at: now, note: body.note ?? null });
      if (sigs.length < required) {
        const pending = { ...found, approvals: sigs, approvals_remaining: required - sigs.length, updated_at: now };
        if (idx >= 0) approvalListItems[idx] = pending;
        return pending;
      }
    }
    const resolved = {
      ...found,
      approval_id: id,
      status: decision === "REJECT" ? "REJECTED" : "APPROVED",
      decision,
      resolved_by: by,
      resolved_at: now,
      resolution_note: body.note ?? null,
      approvals: sigs,
      approvals_remaining: 0,
      updated_at: now,
    };
    if (idx >= 0) approvalListItems[idx] = resolved;
    return resolved;
  },

  // Exceptions
  "GET /exceptions": () => exceptionListItems.map(normalizeException),
  "GET /exceptions/:id": ({ id }) =>
    normalizeException(
      exceptionById.get(id) ??
        exceptionListItems.find((e) => e.exception_id === id) ??
        (excQc as RawObj)
    ),

  // RAG knowledge base
  "GET /rag/stats": () => ragStats,
  "GET /rag/health": () => ({ status: "ok", mongodb: "ok", vector_index: "ok" }),
  "POST /rag/upload": ({ body }) => ragStartJob(body),
  "POST /rag/ingest": ({ body }) => ragStartJob(body),
  "GET /rag/ingest/:id": ({ id }) => ragJobStatus(id),
  "GET /rag/files": () => ({ backend: "mongodb", count: ragFiles.length, files: ragFiles }),
  "POST /rag/query": ({ body }) => ragQueryAnswer(body),

  // Deal-detail sub-resources — emulate the REAL backend response shapes;
  // endpoints.ts owns the backend→UI mapping for both mock and live modes.
  "GET /match-engine/scores/:id": () => {
    const ui = normalizeMatchScore(matchScoreFixture as RawObj);
    return {
      composite_score: ui.composite_score,
      arbitrage_score: ui.price_score,
      proximity_score: ui.logistics_score,
      reliability_score: ui.risk_score,
      gatekeeper_passed: true,
    };
  },
  // Neutral staged escrow — full EscrowResponse shape (tranches, fees, window).
  "GET /escrow-guard/deals/:id/hold": ({ id }) => seedEscrow(id),
  "GET /escrow-guard/deals/:id/ledger": ({ id }) => ledgerFor(seedEscrow(id)),
  "GET /escrow-guard/ledgers": () => {
    const seeded = [...escrowsByDeal.values()].map(ledgerFor);
    const fixture = unwrap<RawObj>(ledgersListFixture as never)
      .filter((l) => !escrowsByDeal.has(l.deal_id as string));
    return [...seeded, ...fixture];
  },
  "POST /escrow-guard/escrows/:id/accept": ({ id, body }) => {
    const e = escrowByEscrowId(id);
    if (e.disputed) throw new MockError(422, `Deal ${e.deal_id} is 'disputed'; required 'not_disputed'`);
    const now = new Date().toISOString();
    let add = 0;
    e.tranches = e.tranches.map((t) => {
      if (t.trigger === "ACCEPTED" && t.status === "PENDING") {
        add += t.amount ?? e.amount * t.pct;
        return { ...t, status: "RELEASED", released_at: now, ext_ref: "tzp_rel_final" };
      }
      return t;
    });
    e.released_total = Number((e.released_total + add).toFixed(2));
    e.fees_total = Number((e.fees_total + add * 0.01).toFixed(2));
    e.buyer_accepted_at = now;
    e.inspection_deadline = null;
    if (e.tranches.every((t) => t.status === "RELEASED")) {
      e.status = "RELEASED";
      e.released_at = now;
      e.settled_at = now;
    }
    void body;
    return e;
  },
  "POST /escrow-guard/escrows/:id/dispute": ({ id, body }) => {
    const e = escrowByEscrowId(id);
    if (!body.reason) throw new MockError(422, "reason is required");
    e.disputed = true;
    e.disputed_at = new Date().toISOString();
    return e;
  },
  "POST /escrow-guard/escrows/:id/open-window": ({ id, body }) => {
    const e = escrowByEscrowId(id);
    if (e.status !== "FUNDED" && e.status !== "RELEASED") throw new MockError(422, `Deal ${e.deal_id} is 'escrow=${e.status}'; required 'escrow=FUNDED'`);
    void body;
    return openWindow(e, "operator");
  },
  "POST /escrow-guard/escrows/:id/clear-dispute": ({ id }) => {
    const e = escrowByEscrowId(id);
    e.disputed = false;
    return e;
  },
  "GET /escrow-guard/deals/:id/journal": ({ id, query }) => {
    const e = seedEscrow(id);
    const j = { ...(journalFixture as RawObj), deal_id: id, escrow_id: e.escrow_id, generated_at: new Date().toISOString() };
    return query.get("format") === "csv" ? journalCsv(j) : j;
  },
  "GET /escrow-guard/journals": () =>
    [...escrowsByDeal.values()].map((e) => ({ ...(journalFixture as RawObj), deal_id: e.deal_id, escrow_id: e.escrow_id })),
  "POST /escrow-guard/escrows/:id/refund": ({ id }) => {
    const e = escrowByEscrowId(id);
    const received = e.funded_amount ?? e.amount;
    const undisbursed = Number((received - e.released_total).toFixed(2));
    if (undisbursed <= 0.01) throw new MockError(422, "nothing left to refund");
    e.refunded_total = undisbursed;
    e.status = "REFUNDED";
    return e;
  },
  "GET /shield-link/deals/:id/orders": ({ id }) => {
    const q = normalizeQc(qcBooking as RawObj);
    const f = normalizeFreight(freightBooking as RawObj);
    const st = freightFor(id);
    return {
      deal_id: id,
      orders: [
        {
          tpo_id: q.qc_id, deal_id: id, order_type: "QC", provider: q.provider,
          status: q.status, ext_ref: null, eta: null, qc_result: null,
          created_at: q.booked_at ?? new Date().toISOString(),
        },
        {
          tpo_id: f.tpo_id, deal_id: id, order_type: "FREIGHT", provider: f.provider,
          status: st.status, ext_ref: f.bl_number, eta: f.eta, qc_result: null,
          container_number: `MSKU${id.replace(/\D/g, "").slice(-7).padStart(7, "0")}`,
          last_milestone: st.last_milestone, last_milestone_at: st.last_milestone_at, delivered_at: st.delivered_at,
          created_at: new Date().toISOString(),
        },
      ],
    };
  },
  // Operator-recorded carrier milestone (by TPO id or carrier ref) — the mock
  // keeps one freight booking per deal, so the ref resolves to every deal that
  // currently shows that booking.
  "POST /shield-link/freight/:ref/milestone": ({ params, body }) => {
    const f = normalizeFreight(freightBooking as RawObj);
    if (params.ref !== f.tpo_id && params.ref !== f.bl_number) throw new MockError(404, `Third-party order '${params.ref}' not found`);
    const dealId = [...freightState.keys()][0] ?? String(body.deal_id ?? "DEAL-20240102-002");
    return applyMilestone(dealId, String(body.milestone ?? "DELIVERED"));
  },
  "POST /shield-link/deals/:id/track": ({ id }) => {
    const st = freightFor(id);
    // The carrier-tracking actor is an external slot — mock it as "stub" until
    // a milestone has been recorded, then report the current one unchanged.
    return st.last_milestone
      ? { status: "unchanged", deal_id: id, milestone: st.last_milestone }
      : { status: "stub", deal_id: id, tpo_id: normalizeFreight(freightBooking as RawObj).tpo_id };
  },
  "GET /docugen/deals/:id/documents": ({ id }) => ({
    deal_id: id,
    documents: documents.map((d) => ({
      doc_id: d.document_id, deal_id: id, doc_type: d.document_type,
      status: d.status, storage_uri: d.file_url, created_at: d.created_at,
    })),
  }),
  "GET /dealmaker/deals/:id/documents": ({ id }) => ({ deal_id: id, pi: null, po: null }),
  // File streaming — mock serves a small HTML body (endpoints.ts wraps it in a Blob)
  "GET /docugen/documents/:id/file": ({ id }) =>
    `<html><body><h1>Mock document ${id}</h1><p>Rendered document preview.</p></body></html>`,
  "GET /dealmaker/pi/:id/file": ({ id }) =>
    `<html><body><h1>Mock Proforma Invoice ${id}</h1></body></html>`,
  "GET /dealmaker/po/:id/file": ({ id }) =>
    `<html><body><h1>Mock Purchase Order ${id}</h1></body></html>`,
};

// Match a concrete "METHOD /path" against the route table, capturing :params.
// `id` is the FIRST captured param (back-compat); `params` holds all of them by
// name so routes like "/deals/:id/parties/:role" see both.
function resolve(
  method: string,
  path: string
): { handler: Handler; id: string; params: Record<string, string> } | null {
  const reqParts = path.split("/").filter(Boolean);
  for (const key of Object.keys(routes)) {
    const [routeMethod, routePath] = key.split(" ");
    if (routeMethod !== method) continue;
    const routeParts = routePath.split("/").filter(Boolean);
    if (routeParts.length !== reqParts.length) continue;
    const params: Record<string, string> = {};
    let id = "";
    const matched = routeParts.every((seg, i) => {
      if (seg.startsWith(":")) {
        params[seg.slice(1)] = decodeURIComponent(reqParts[i]);
        if (!id) id = decodeURIComponent(reqParts[i]);
        return true;
      }
      return seg === reqParts[i];
    });
    if (matched) return { handler: routes[key], id, params };
  }
  return null;
}

function parseBody(data: unknown): RawObj {
  if (!data) return {};
  if (typeof data === "object") return data as RawObj;
  try {
    return JSON.parse(String(data)) as RawObj;
  } catch {
    return {};
  }
}

export const mockAdapter: AxiosAdapter = async (config: InternalAxiosRequestConfig) => {
  const method = (config.method ?? "get").toUpperCase();
  // config.url is relative (e.g. "/deals?status=PAID"); strip baseURL + query.
  const raw = (config.url ?? "").replace(config.baseURL ?? "", "");
  const [path, queryString = ""] = raw.split("?");
  const match = resolve(method, path);

  // Simulate a little latency so loading/skeleton states are visible.
  await new Promise((r) => setTimeout(r, 150));

  const response = (data: unknown, status = 200): AxiosResponse => ({
    data,
    status,
    statusText: status === 200 ? "OK" : "Not Found",
    headers: {},
    config,
  });

  if (!match) {
    // Unmapped GET → empty array so list pages degrade instead of crashing.
    if (method === "GET") return response([]);
    // Unmapped mutation → succeed quietly so action buttons don't error-toast.
    return response({ ok: true });
  }

  const headers = (config.headers ?? {}) as Record<string, unknown>;
  const authHeader = (headers.Authorization ?? headers.authorization) as string | undefined;

  try {
    const data = match.handler({
      id: match.id,
      params: match.params,
      query: new URLSearchParams(queryString),
      body: parseBody(config.data),
      ownerId: ownerFromAuthHeader(authHeader),
    });
    return response(data);
  } catch (err) {
    if (err instanceof MockError) {
      return Promise.reject(
        Object.assign(new Error(err.detail), {
          isAxiosError: true,
          config,
          response: response({ detail: err.detail }, err.status),
        })
      );
    }
    throw err;
  }
};
