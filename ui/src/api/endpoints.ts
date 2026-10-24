import { api } from "./client";
import type {
  ApprovalRecord,
  AutonomyLevel,
  BuyerCandidate,
  CatalogueView,
  CreateDealRequest,
  Deal,
  DealException,
  DealJournal,
  DealLedger,
  DealTimeline,
  DigestSendResult,
  OperatorDigest,
  ProductLine,
  RecordMilestoneRequest,
  TrackingPollResult,
  DealmakerDealDocuments,
  DocumentRecord,
  DownstreamHealth,
  EmailHistoryRecord,
  EnrichmentResult,
  EscrowRecord,
  FreightRecord,
  MatchScore,
  NegotiationEmailThread,
  NegotiationResponse,
  NegotiationRound,
  NegotiationSession,
  AuthUser,
  Opportunity,
  Organization,
  QcRecord,
  RagFilesResponse,
  RagIngestResponse,
  RagJobStatus,
  RagQueryResponse,
  RagStats,
  ReadinessReport,
  ResolveApprovalRequest,
  TokenResponse,
  TradeSourcesResponse,
  UpdatePartyRequest,
} from "./types";

// Thin wrappers around the gateway endpoints (services/api_gateway/router.py).

export const AuthApi = {
  register: (payload: { email: string; name: string; password: string }) =>
    api.post<TokenResponse>("/auth/register", payload).then((r) => r.data),
  login: (payload: { email: string; password: string }) =>
    api.post<TokenResponse>("/auth/login", payload).then((r) => r.data),
  me: () => api.get<AuthUser>("/auth/me").then((r) => r.data),
  // Revokes the current bearer token server-side (shared/auth/blacklist.py)
  // so it can't be replayed even if it leaked before its natural expiry.
  logout: () => api.post<void>("/auth/logout").then(() => undefined),
};

// Organizations admin — Portless platform staff only (403 for everyone else).
export const OrganizationsApi = {
  list: () => api.get<Organization[]>("/auth/organizations").then((r) => r.data),
  create: (name: string) =>
    api.post<Organization>("/auth/organizations", { name }).then((r) => r.data),
  listUsers: () => api.get<AuthUser[]>("/auth/users").then((r) => r.data),
  assignUserOrg: (userId: string, orgId: string) =>
    api
      .patch<AuthUser>(`/auth/users/${userId}/organization`, { org_id: orgId })
      .then((r) => r.data),
};

export const HealthApi = {
  gateway: () => api.get<{ status: string; service: string }>("/health").then((r) => r.data),
  downstream: () => api.get<DownstreamHealth>("/health/downstream").then((r) => r.data),
  // Production readiness gate (shared/readiness.py): every stub / fallback the
  // pipeline is currently running on, ok / warn / fail per check.
  readiness: () => api.get<ReadinessReport>("/health/readiness").then((r) => r.data),
};

export const DealsApi = {
  list: (status?: string) =>
    api.get<Deal[]>("/deals", { params: status ? { status } : undefined }).then((r) => r.data),
  get: (dealId: string) => api.get<Deal>(`/deals/${dealId}`).then((r) => r.data),
  create: (payload: CreateDealRequest) => api.post<Deal>("/deals", payload).then((r) => r.data),
  updateStatus: (dealId: string, targetStatus: string, reason?: string) =>
    api
      .patch<Deal>(`/deals/${dealId}/status`, { target_status: targetStatus, reason })
      .then((r) => r.data),
  // Operator correction of a counterparty (stamped contact_source="operator",
  // never overwritten by a later actor run).
  updateParty: (dealId: string, role: "buyer" | "supplier", payload: UpdatePartyRequest) =>
    api.patch<Deal>(`/deals/${dealId}/parties/${role}`, payload).then((r) => r.data),
  // Identify + verify the parties on the PortLess Apify actors (contact finder,
  // e-mail check, OFAC/UN/EU/UK sanctions + LEI + VIES screening).
  enrich: (dealId: string, opts?: { screen?: boolean; contacts?: boolean }) =>
    api
      .post<EnrichmentResult>(`/deals/${dealId}/enrich`, undefined, {
        params: { screen: opts?.screen ?? true, contacts: opts?.contacts ?? true },
      })
      .then((r) => r.data),
  // Every dated fact of the deal from every service, merged and sorted.
  timeline: (dealId: string) =>
    api.get<DealTimeline>(`/deals/${dealId}/timeline`).then((r) => r.data),
  // Hand-over pack (deal, parties, PI/PO, document hashes, orders, ledger +
  // journal, approvals, exceptions, correspondence, timeline, SHA-256).
  evidencePackJson: (dealId: string) =>
    api
      .get<Record<string, unknown>>(`/deals/${dealId}/evidence-pack`, { params: { format: "json" } })
      .then((r) => r.data),
  evidencePackHtml: (dealId: string) =>
    fetchFileBlob(`/deals/${dealId}/evidence-pack?format=html`, "text/html"),
  // Buyer outreach funnel (shared/orchestration/outreach.py).
  addBuyerCandidate: (dealId: string, candidate: BuyerCandidate) =>
    api.post<Deal>(`/deals/${dealId}/buyer-candidates`, candidate).then((r) => r.data),
  startOutreach: (dealId: string) =>
    api
      .post<{ deal_id: string; contacted: number; waiting_for_buyer: boolean }>(
        `/deals/${dealId}/outreach/start`
      )
      .then((r) => r.data),
  skipBuyerCandidate: (dealId: string, candidateId: string) =>
    api
      .post<{ deal_id: string; candidate_id: string; advanced?: number; exhausted?: boolean }>(
        `/deals/${dealId}/buyer-candidates/${candidateId}/skip`
      )
      .then((r) => r.data),
};

export const OpportunitiesApi = {
  list: () => api.get<Opportunity[]>("/trade-min/opportunities").then((r) => r.data),
  // Opportunities are marked CONVERTED (not deleted) when a deal is created,
  // so the originating record stays fetchable by id for the deal Overview tab.
  get: (opportunityId: string) =>
    api.get<Opportunity>(`/trade-min/opportunities/${opportunityId}`).then((r) => r.data),
  scan: (payload: { hs_codes?: string[]; target_markets?: string[] }) =>
    api.post<Opportunity[]>("/trade-min/scan", payload).then((r) => r.data),
  // Which demand / buyer / seller / enrichment sources the scanner can reach
  // (live / stub / mock / open) — incl. the PortLess-owned Apify actors.
  sources: () => api.get<TradeSourcesResponse>("/trade-min/sources").then((r) => r.data),
};

export type OnTimeout = "APPROVE" | "REJECT";

export interface AutonomyActionPolicy {
  action: string;
  label: string;
  description: string;
  default: AutonomyLevel;
  override: AutonomyLevel | null;
  env: AutonomyLevel | null;
  effective: AutonomyLevel;
  source: "override" | "env" | "default" | "money_ceiling" | "value_threshold" | string;
  follows_deal_profile: boolean;
  money_moving: boolean;
  // v2 gate settings (timeouts, on-timeout default, value threshold)
  timeout_seconds: number;
  timeout_override: number | null;
  default_timeout_seconds: number;
  default_on_timeout: OnTimeout;
  on_timeout_override: OnTimeout | null;
  auto_approve_below_usd: number | null;
  threshold_override: number | null;
}

export interface AutonomyPolicyResponse {
  money_ceiling: AutonomyLevel;
  /** Global value threshold for money gates (null = every money gate waits for a human). */
  money_auto_approve_below_usd: number | null;
  actions: AutonomyActionPolicy[];
}

/** Per-action gate override; omit a key to leave it, null to clear it. */
export interface GateOverride {
  level?: AutonomyLevel | null;
  timeout_seconds?: number | null;
  default_on_timeout?: OnTimeout | null;
  auto_approve_below_usd?: number | null;
}

export interface AutonomyPolicyUpdate {
  levels?: Record<string, AutonomyLevel | null>;
  gates?: Record<string, GateOverride | null>;
  money_auto_approve_below_usd?: number | null;
  set_money_threshold?: boolean;
  updated_by?: string;
}

// Per-action autonomy policy (shared/orchestration/autonomy_policy.py) — the
// Autonomy Settings page reads the effective level / timeout / on-timeout
// default / value threshold per action class and writes overrides; null clears
// an override so env/default applies again.
export const AutonomyApi = {
  get: () =>
    api.get<AutonomyPolicyResponse>("/hitl-approval/autonomy-policy").then((r) => r.data),
  save: (update: AutonomyPolicyUpdate) =>
    api
      .put<AutonomyPolicyResponse>("/hitl-approval/autonomy-policy", {
        levels: update.levels ?? {},
        gates: update.gates ?? {},
        money_auto_approve_below_usd: update.money_auto_approve_below_usd ?? null,
        set_money_threshold: update.set_money_threshold ?? false,
        updated_by: update.updated_by ?? "operator",
      })
      .then((r) => r.data),
};

// Daily operator digest (shared/orchestration/digest.py): pending gates, deals
// per stage, stalls, open exceptions, money, Apify spend.
export const DigestApi = {
  get: () => api.get<OperatorDigest>("/hitl-approval/digest").then((r) => r.data),
  send: () => api.post<DigestSendResult>("/hitl-approval/digest/send").then((r) => r.data),
};

// Product catalogue / corridors (shared/catalogue.py) — what PortLess sells
// and where; drives scan defaults, actor inputs and outreach e-mails.
export const CatalogueApi = {
  get: () => api.get<CatalogueView>("/trade-min/catalogue").then((r) => r.data),
  save: (products: ProductLine[], updatedBy = "operator") =>
    api
      .put<CatalogueView>("/trade-min/catalogue", { products, updated_by: updatedBy })
      .then((r) => r.data),
};

export const ApprovalsApi = {
  list: (status?: string) =>
    api
      .get<ApprovalRecord[]>("/hitl-approval/approvals", {
        params: status ? { status } : undefined,
      })
      .then((r) => r.data),
  get: (approvalId: string) =>
    api.get<ApprovalRecord>(`/hitl-approval/approvals/${approvalId}`).then((r) => r.data),
  resolve: (approvalId: string, payload: ResolveApprovalRequest) =>
    api
      .post<ApprovalRecord>(`/hitl-approval/approvals/${approvalId}/resolve`, payload)
      .then((r) => r.data),
};

export const ExceptionsApi = {
  get: (exceptionId: string) =>
    api.get<DealException>(`/exceptions/${exceptionId}`).then((r) => r.data),
  list: () => api.get<DealException[]>("/exceptions").then((r) => r.data),
};

export const NegotiationApi = {
  getSessions: (dealId: string) =>
    api
      .get<NegotiationSession[]>("/negotiation-agent/negotiations", {
        params: { deal_id: dealId },
      })
      .then((r) => r.data),
  getSession: (negotiationId: string) =>
    api
      .get<NegotiationSession>(`/negotiation-agent/negotiations/${negotiationId}`)
      .then((r) => r.data),
  getRounds: (negotiationId: string) =>
    api
      .get<NegotiationRound[]>(`/negotiation-agent/negotiations/${negotiationId}/rounds`)
      .then((r) => r.data),
  getEmails: (negotiationId: string) =>
    api
      .get<EmailHistoryRecord[]>(`/negotiation-agent/negotiations/${negotiationId}/emails`)
      .then((r) => r.data),
  getEmailThreads: (dealId?: string) =>
    api
      .get<NegotiationEmailThread[]>("/negotiation-agent/email-threads", {
        params: dealId ? { deal_id: dealId } : undefined,
      })
      .then((r) => r.data),
  runRound: (
    negotiationId: string,
    req: { inbound_price?: number | null; inbound_message?: string | null; party?: string }
  ) =>
    api
      .post<NegotiationResponse>(`/negotiation-agent/negotiations/${negotiationId}/round`, req)
      .then((r) => r.data),
};

// ── Deal sub-resources ──────────────────────────────────────────────────────
// These map the REAL backend response shapes (each service's schemas.py) onto
// the UI types. The previous paths (/escrow-guard/holds?deal_id=…,
// /shield-link/qc, /docugen/documents, /match-engine/matches) only ever
// existed in the mock adapter — against the live backend they 404'd and every
// deal-detail tab rendered empty.

// swallow a 404 into `empty` — "no record yet" is a normal state, not an error
function or404<T>(empty: T) {
  return (err: unknown): T => {
    const status = (err as { response?: { status?: number } })?.response?.status;
    if (status === 404) return empty;
    throw err;
  };
}

// Fill the staged-escrow fields a legacy Stripe hold (or an older fixture)
// leaves out, so the Payments tab can rely on them being present.
function normalizeEscrow(raw: Partial<EscrowRecord> & { escrow_id: string; deal_id: string }): EscrowRecord {
  return {
    escrow_id: raw.escrow_id,
    deal_id: raw.deal_id,
    provider: raw.provider ?? "stripe",
    amount: raw.amount ?? 0,
    currency: raw.currency ?? "USD",
    status: raw.status ?? "PENDING",
    ext_ref: raw.ext_ref ?? null,
    client_secret: raw.client_secret ?? null,
    funded_at: raw.funded_at ?? null,
    funded_amount: raw.funded_amount ?? null,
    created_at: raw.created_at ?? new Date().toISOString(),
    payment_method: raw.payment_method ?? null,
    checkout_url: raw.checkout_url ?? null,
    released_total: raw.released_total ?? 0,
    refunded_total: raw.refunded_total ?? 0,
    fee_pct: raw.fee_pct ?? null,
    fees_total: raw.fees_total ?? 0,
    shortfall: raw.shortfall ?? null,
    settled_at: raw.settled_at ?? null,
    tranches: raw.tranches ?? [],
    inspection_deadline: raw.inspection_deadline ?? null,
    delivery_confirmed_at: raw.delivery_confirmed_at ?? null,
    inspection_opened_at: raw.inspection_opened_at ?? null,
    inspection_trigger: raw.inspection_trigger ?? null,
    buyer_accepted_at: raw.buyer_accepted_at ?? null,
    disputed: raw.disputed ?? false,
    disputed_at: raw.disputed_at ?? null,
  };
}

export const EscrowApi = {
  // Real API: GET /escrow-guard/deals/{deal_id}/hold → single escrow, 404 when none.
  getHold: (dealId: string) =>
    api
      .get<Partial<EscrowRecord> & { escrow_id: string; deal_id: string }>(
        `/escrow-guard/deals/${dealId}/hold`
      )
      .then((r): EscrowRecord | null => normalizeEscrow(r.data))
      .catch(or404<EscrowRecord | null>(null)),
  // Per-trade money ledger: held / released / fees / payouts / supplier balance +
  // reconciliation issues. Returns an empty ledger (no 404) when there is no escrow.
  getLedger: (dealId: string) =>
    api.get<DealLedger>(`/escrow-guard/deals/${dealId}/ledger`).then((r) => r.data),
  listLedgers: () => api.get<DealLedger[]>("/escrow-guard/ledgers").then((r) => r.data),
  // Buyer acceptance of the goods → releases the final ACCEPTED-trigger tranche.
  accept: (escrowId: string, payload: { accepted_by?: string; notes?: string }) =>
    api
      .post<EscrowRecord>(`/escrow-guard/escrows/${escrowId}/accept`, payload)
      .then((r) => normalizeEscrow(r.data)),
  // Buyer dispute within the inspection window — blocks every release.
  dispute: (escrowId: string, payload: { reason: string; raised_by?: string }) =>
    api
      .post<EscrowRecord>(`/escrow-guard/escrows/${escrowId}/dispute`, payload)
      .then((r) => normalizeEscrow(r.data)),
  // Refund the undisbursed balance to the buyer (DISPUTE → REFUNDED).
  refund: (escrowId: string, payload: { reason: string }) =>
    api
      .post<EscrowRecord>(`/escrow-guard/escrows/${escrowId}/refund`, payload)
      .then((r) => normalizeEscrow(r.data)),
  // Operator confirms delivery → the buyer's acceptance window starts
  // (idempotent; waits for pending proof tranches).
  openWindow: (escrowId: string, payload: { reason: string }) =>
    api
      .post<EscrowRecord>(`/escrow-guard/escrows/${escrowId}/open-window`, payload)
      .then((r) => normalizeEscrow(r.data)),
  // Lift a buyer dispute (re-inspection agreed / resolved in PortLess's favour).
  clearDispute: (escrowId: string, payload: { reason: string }) =>
    api
      .post<EscrowRecord>(`/escrow-guard/escrows/${escrowId}/clear-dispute`, payload)
      .then((r) => normalizeEscrow(r.data)),
  // Double-entry journal for the deal (escrow funding, tranche releases with
  // fees and revenue, refunds, supplier payouts). 404 when there is no escrow.
  getJournal: (dealId: string) =>
    api
      .get<DealJournal>(`/escrow-guard/deals/${dealId}/journal`)
      .then((r): DealJournal | null => r.data)
      .catch(or404<DealJournal | null>(null)),
  journalCsv: (dealId: string) =>
    fetchFileBlob(`/escrow-guard/deals/${dealId}/journal?format=csv`, "text/csv"),
};

interface RawShieldOrder {
  tpo_id: string;
  deal_id: string;
  order_type: "QC" | "FREIGHT" | "CHA";
  provider: string;
  status: string;
  ext_ref?: string | null;
  eta?: string | null;
  qc_result?: string | null;
  container_number?: string | null;
  last_milestone?: string | null;
  last_milestone_at?: string | null;
  delivered_at?: string | null;
  created_at: string;
}

// Real API: GET /shield-link/deals/{deal_id}/orders → all TPOs (QC + freight + CHA)
const getShieldOrders = (dealId: string) =>
  api
    .get<{ orders: RawShieldOrder[] }>(`/shield-link/deals/${dealId}/orders`)
    .then((r) => r.data.orders ?? []);

// Provider statuses → the UI's display vocabulary (SGS says PASS/FAIL, the
// freight flow ends at COMPLETED; the UI renders PASSED/FAILED/CLEARED).
function qcStatus(o: RawShieldOrder): QcRecord["status"] {
  const s = (o.qc_result ?? o.status).toUpperCase();
  if (s === "PASS" || s === "PASSED") return "PASSED";
  if (s === "FAIL" || s === "FAILED") return "FAILED";
  if (s === "IN_PROGRESS") return "IN_PROGRESS";
  return "BOOKED";
}
function freightStatus(o: RawShieldOrder): FreightRecord["status"] {
  // The carrier milestone is the most precise state when one was recorded.
  const m = (o.last_milestone ?? "").toUpperCase();
  if (o.delivered_at || m === "DELIVERED") return "DELIVERED";
  if (m === "CLEARED") return "CLEARED";
  if (m === "ARRIVED") return "ARRIVED";
  if (m === "DEPARTED" || m === "IN_TRANSIT") return "IN_TRANSIT";
  const s = o.status.toUpperCase();
  if (s === "COMPLETED" || s === "CLEARED") return "CLEARED";
  if (s === "ARRIVED") return "ARRIVED";
  if (s === "IN_TRANSIT" || s === "IN_PROGRESS") return "IN_TRANSIT";
  return "BOOKED";
}

export const ShieldApi = {
  getQc: (dealId: string) =>
    getShieldOrders(dealId).then((orders): QcRecord[] =>
      orders
        .filter((o) => o.order_type === "QC")
        .map((o) => ({
          qc_id: o.tpo_id,
          deal_id: o.deal_id,
          provider: o.provider,
          status: qcStatus(o),
          report_url: null,
          booked_at: o.created_at,
        }))
    ),
  getFreight: (dealId: string) =>
    getShieldOrders(dealId).then((orders): FreightRecord[] =>
      orders
        .filter((o) => o.order_type === "FREIGHT")
        .map((o) => ({
          tpo_id: o.tpo_id,
          deal_id: o.deal_id,
          provider: o.provider,
          status: freightStatus(o),
          bl_number: o.ext_ref ?? null,
          eta: o.eta ?? null,
          container_number: o.container_number ?? null,
          last_milestone: o.last_milestone ?? null,
          last_milestone_at: o.last_milestone_at ?? null,
          delivered_at: o.delivered_at ?? null,
        }))
    ),
  // Operator / tracking milestone by TPO id (no provider signature). DELIVERED
  // opens the buyer's acceptance window on the escrow.
  recordMilestone: (tpoId: string, payload: RecordMilestoneRequest) =>
    api
      .post<{ status: string; milestone: string; deal_id: string; last_milestone?: string }>(
        `/shield-link/freight/${tpoId}/milestone`,
        { source: "operator", ...payload }
      )
      .then((r) => r.data),
  // Poll the carrier-tracking actor for the deal's container now.
  pollTracking: (dealId: string) =>
    api.post<TrackingPollResult>(`/shield-link/deals/${dealId}/track`).then((r) => r.data),
};

interface RawDocugenDoc {
  doc_id: string;
  deal_id: string;
  doc_type: string;
  status: string;
  storage_uri?: string | null;
  created_at: string;
}

export const DocugenApi = {
  // Real API: GET /docugen/deals/{deal_id}/documents. storage_uri is a Google
  // Drive webViewLink (or a local file:// URI when Drive isn't configured).
  getDocuments: (dealId: string) =>
    api
      .get<{ documents: RawDocugenDoc[] }>(`/docugen/deals/${dealId}/documents`)
      .then((r): DocumentRecord[] =>
        (r.data.documents ?? []).map((d) => ({
          document_id: d.doc_id,
          deal_id: d.deal_id,
          document_type: d.doc_type,
          status: d.status,
          file_url: d.storage_uri ?? null,
          created_at: d.created_at,
        }))
      ),
};

export const DealmakerApi = {
  // PI + PO for the deal (storage_uri is the Drive link once issued).
  getDealDocuments: (dealId: string) =>
    api
      .get<DealmakerDealDocuments>(`/dealmaker/deals/${dealId}/documents`)
      .then((r) => r.data),
};

// ── Document file streaming ─────────────────────────────────────────────────
// The stored files live in the platform's private Google Drive; the backend
// streams them through its own credentials (GET …/file). Fetched as a Blob via
// the authenticated client because a plain <a href> can't send the bearer token.
async function fetchFileBlob(path: string, fallbackType = "text/html"): Promise<Blob> {
  const r = await api.get(path, { responseType: "blob" });
  if (r.data instanceof Blob) return r.data;
  // Mock adapter returns a plain string — coerce so View/Download still work.
  return new Blob([typeof r.data === "string" ? r.data : JSON.stringify(r.data)], {
    type: fallbackType,
  });
}

export const FilesApi = {
  documentFile: (docId: string) => fetchFileBlob(`/docugen/documents/${docId}/file`),
  piFile: (piId: string) => fetchFileBlob(`/dealmaker/pi/${piId}/file`),
  poFile: (poId: string) => fetchFileBlob(`/dealmaker/po/${poId}/file`),
};

interface RawMatchScore {
  composite_score: number;
  arbitrage_score: number;
  proximity_score: number;
  reliability_score: number;
  gatekeeper_passed: boolean;
}

export const MatchApi = {
  // Real API: GET /match-engine/scores/{deal_id} — 404 when the score store
  // has no entry (e.g. in-memory store reset); callers fall back to the
  // deal's own winning_cms snapshot.
  getScore: (dealId: string) =>
    api
      .get<RawMatchScore>(`/match-engine/scores/${dealId}`)
      .then((r): MatchScore => ({
        composite_score: r.data.composite_score,
        arbitrage_score: r.data.arbitrage_score,
        proximity_score: r.data.proximity_score,
        reliability_score: r.data.reliability_score,
        gatekeeper_passed: r.data.gatekeeper_passed,
      }))
      .catch(or404<MatchScore | null>(null)),
};

export const RagApi = {
  stats: () => api.get<RagStats>("/rag/stats").then((r) => r.data),
  jobStatus: (jobId: string) =>
    api.get<RagJobStatus>(`/rag/ingest/${jobId}`).then((r) => r.data),
  // Files durably persisted in the RAG file store (MongoDB GridFS).
  files: (limit = 100) =>
    api.get<RagFilesResponse>("/rag/files", { params: { limit } }).then((r) => r.data),
  upload: (files: File[], chunkStrategy = "recursive") => {
    const form = new FormData();
    files.forEach((f) => form.append("files", f));
    form.append("chunk_strategy", chunkStrategy);
    return api
      .post<RagIngestResponse>("/rag/upload", form, {
        headers: { "Content-Type": "multipart/form-data" },
      })
      .then((r) => r.data);
  },
  // Full retrieve → rerank → generate pipeline, used by the Knowledge Base test chat.
  query: (question: string) =>
    api.post<RagQueryResponse>("/rag/query", { question }).then((r) => r.data),
};
