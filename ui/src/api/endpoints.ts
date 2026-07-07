import { api } from "./client";
import type {
  ApprovalRecord,
  CreateDealRequest,
  Deal,
  DealException,
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
  QcRecord,
  RagIngestResponse,
  RagJobStatus,
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
  list: () => api.get<Opportunity[]>("/opportunities").then((r) => r.data),
  scan: (payload: { hs_codes?: string[]; target_markets?: string[] }) =>
    api.post<Opportunity[]>("/opportunities/scan", payload).then((r) => r.data),
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

export const EscrowApi = {
  getHolds: (dealId: string) =>
    api
      .get<EscrowHold[]>("/escrow-guard/holds", { params: { deal_id: dealId } })
      .then((r) => r.data),
};

export const ShieldApi = {
  getQc: (dealId: string) =>
    api
      .get<QcRecord[]>("/shield-link/qc", { params: { deal_id: dealId } })
      .then((r) => r.data),
  getFreight: (dealId: string) =>
    api
      .get<FreightRecord[]>("/shield-link/freight", { params: { deal_id: dealId } })
      .then((r) => r.data),
};

export const DocugenApi = {
  getDocuments: (dealId: string) =>
    api
      .get<DocumentRecord[]>("/docugen/documents", { params: { deal_id: dealId } })
      .then((r) => r.data),
};

export const MatchApi = {
  getScore: (dealId: string) =>
    api
      .get<MatchScore>("/match-engine/matches", { params: { deal_id: dealId } })
      .then((r) => r.data),
};

export const RagApi = {
  stats: () => api.get<RagStats>("/rag/stats").then((r) => r.data),
  jobStatus: (jobId: string) =>
    api.get<RagJobStatus>(`/rag/ingest/${jobId}`).then((r) => r.data),
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
};
