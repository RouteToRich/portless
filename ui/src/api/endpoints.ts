import { api } from "./client";
import type {
  Approval,
  CreateDealRequest,
  Deal,
  DealException,
  DownstreamHealth,
  Opportunity,
} from "./types";

// Thin wrappers around the gateway endpoints (services/api_gateway/router.py).

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
      .get<Approval[]>("/approvals", { params: status ? { status } : undefined })
      .then((r) => r.data),
  resolve: (approvalId: string, decision: "APPROVE" | "REJECT", resolvedBy: string) =>
    api
      .post<Approval>(`/approvals/${approvalId}/resolve`, {
        decision,
        resolved_by: resolvedBy,
      })
      .then((r) => r.data),
};

export const ExceptionsApi = {
  get: (exceptionId: string) =>
    api.get<DealException>(`/exceptions/${exceptionId}`).then((r) => r.data),
};
