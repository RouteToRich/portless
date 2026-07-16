import { api } from "./client";
import type {
  ApprovalRecord,
  CreateDealRequest,
  Deal,
  DealException,
  DealmakerDealDocuments,
  DocumentRecord,
  DownstreamHealth,
  EmailHistoryRecord,
  EscrowHold,
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
  ResolveApprovalRequest,
  TokenResponse,
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
};

export const OpportunitiesApi = {
  list: () => api.get<Opportunity[]>("/trade-min/opportunities").then((r) => r.data),
  // Opportunities are marked CONVERTED (not deleted) when a deal is created,
  // so the originating record stays fetchable by id for the deal Overview tab.
  get: (opportunityId: string) =>
    api.get<Opportunity>(`/trade-min/opportunities/${opportunityId}`).then((r) => r.data),
  scan: (payload: { hs_codes?: string[]; target_markets?: string[] }) =>
    api.post<Opportunity[]>("/trade-min/scan", payload).then((r) => r.data),
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

interface RawEscrowHold {
  escrow_id: string;
  deal_id: string;
  amount: number;
  status: string;
  ext_ref?: string | null;
  funded_at?: string | null;
}

export const EscrowApi = {
  // Real API: GET /escrow-guard/deals/{deal_id}/hold → single hold, 404 when none.
  getHolds: (dealId: string) =>
    api
      .get<RawEscrowHold>(`/escrow-guard/deals/${dealId}/hold`)
      .then((r): EscrowHold[] => [{
        hold_id: r.data.escrow_id,
        deal_id: r.data.deal_id,
        amount_usd: r.data.amount,
        status: r.data.status as EscrowHold["status"],
        stripe_payment_intent_id: r.data.ext_ref ?? null,
        funded_at: r.data.funded_at ?? null,
      }])
      .catch(or404<EscrowHold[]>([])),
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
  const s = o.status.toUpperCase();
  if (s === "COMPLETED" || s === "CLEARED") return "CLEARED";
  if (s === "ARRIVED") return "ARRIVED";
  if (s === "IN_TRANSIT") return "IN_TRANSIT";
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
        }))
    ),
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
async function fetchFileBlob(path: string): Promise<Blob> {
  const r = await api.get(path, { responseType: "blob" });
  if (r.data instanceof Blob) return r.data;
  // Mock adapter returns a plain string — coerce so View/Download still work.
  return new Blob([typeof r.data === "string" ? r.data : JSON.stringify(r.data)], {
    type: "text/html",
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
