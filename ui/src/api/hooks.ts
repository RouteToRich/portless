import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApprovalsApi,
  DealsApi,
  DocugenApi,
  EscrowApi,
  ExceptionsApi,
  HealthApi,
  MatchApi,
  NegotiationApi,
  OpportunitiesApi,
  RagApi,
  ShieldApi,
} from "./endpoints";
import type { ApprovalRecord, CreateDealRequest, ResolveApprovalRequest } from "./types";

// ── Health ──────────────────────────────────────────────────────────────────

export function useGatewayHealth() {
  return useQuery({ queryKey: ["health", "gateway"], queryFn: HealthApi.gateway });
}

export function useDownstreamHealth() {
  return useQuery({
    queryKey: ["health", "downstream"],
    queryFn: HealthApi.downstream,
    refetchInterval: 10000,
  });
}

// ── Deals ───────────────────────────────────────────────────────────────────

export function useDeals(status?: string) {
  return useQuery({
    queryKey: ["deals", status ?? "all"],
    queryFn: () => DealsApi.list(status),
    refetchInterval: 15000,
  });
}

export function useDeal(dealId: string | undefined) {
  return useQuery({
    queryKey: ["deal", dealId],
    queryFn: () => DealsApi.get(dealId as string),
    enabled: Boolean(dealId),
  });
}

export function useCreateDeal() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: CreateDealRequest) => DealsApi.create(payload),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["deals"] }),
  });
}

export function useUpdateDealStatus() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      dealId,
      targetStatus,
      reason,
    }: {
      dealId: string;
      targetStatus: string;
      reason?: string;
    }) => DealsApi.updateStatus(dealId, targetStatus, reason),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ["deals"] });
      qc.invalidateQueries({ queryKey: ["deal", vars.dealId] });
    },
  });
}

// ── Opportunities ────────────────────────────────────────────────────────────

export function useOpportunities() {
  return useQuery({ queryKey: ["opportunities"], queryFn: OpportunitiesApi.list });
}

export function useScanOpportunities() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => OpportunitiesApi.scan({}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["opportunities"] }),
  });
}

// ── Approvals ────────────────────────────────────────────────────────────────

export function useApprovals(status?: string) {
  return useQuery({
    queryKey: ["approvals", status ?? "all"],
    queryFn: () => ApprovalsApi.list(status),
    refetchInterval: 10000,
  });
}

export function useApproval(approvalId: string | undefined) {
  return useQuery({
    queryKey: ["approval", approvalId],
    queryFn: () => ApprovalsApi.get(approvalId as string),
    enabled: Boolean(approvalId),
  });
}

export function useResolveApproval() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      approvalId,
      payload,
    }: {
      approvalId: string;
      payload: ResolveApprovalRequest;
    }) => ApprovalsApi.resolve(approvalId, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["approvals"] });
    },
  });
}

// ── Approvals SSE stream ─────────────────────────────────────────────────────

export function useApprovalStream() {
  const [events, setEvents] = useState<ApprovalRecord[]>([]);
  const [connected, setConnected] = useState(false);
  const qc = useQueryClient();
  const baseURL = import.meta.env.VITE_API_BASE_URL ?? "";

  useEffect(() => {
    const url = baseURL
      ? `${baseURL}/hitl-approval/stream`
      : "/api/hitl-approval/stream";

    let es: EventSource;
    let retryTimeout: ReturnType<typeof setTimeout>;

    const connect = () => {
      es = new EventSource(url);

      es.onopen = () => setConnected(true);

      es.onmessage = (e) => {
        try {
          const approval = JSON.parse(e.data) as ApprovalRecord;
          setEvents((prev) => {
            const exists = prev.find((x) => x.approval_id === approval.approval_id);
            if (exists) {
              return prev.map((x) =>
                x.approval_id === approval.approval_id ? approval : x
              );
            }
            return [approval, ...prev].slice(0, 50);
          });
          qc.invalidateQueries({ queryKey: ["approvals"] });
        } catch {
          // ignore parse errors
        }
      };

      es.onerror = () => {
        setConnected(false);
        es.close();
        retryTimeout = setTimeout(connect, 5000);
      };
    };

    connect();

    return () => {
      es?.close();
      clearTimeout(retryTimeout);
    };
  }, [baseURL, qc]);

  return { events, connected };
}

// ── Exceptions ───────────────────────────────────────────────────────────────

export function useException(exceptionId: string | null) {
  return useQuery({
    queryKey: ["exception", exceptionId],
    queryFn: () => ExceptionsApi.get(exceptionId as string),
    enabled: Boolean(exceptionId),
  });
}

export function useExceptions() {
  return useQuery({
    queryKey: ["exceptions"],
    queryFn: ExceptionsApi.list,
    refetchInterval: 15000,
  });
}

// ── Negotiation ───────────────────────────────────────────────────────────────

export function useNegotiationSessions(dealId: string | undefined) {
  return useQuery({
    queryKey: ["negotiation-sessions", dealId],
    queryFn: () => NegotiationApi.getSessions(dealId as string),
    enabled: Boolean(dealId),
  });
}

export function useNegotiationRounds(negotiationId: string | undefined) {
  return useQuery({
    queryKey: ["negotiation-rounds", negotiationId],
    queryFn: () => NegotiationApi.getRounds(negotiationId as string),
    enabled: Boolean(negotiationId),
  });
}

export function useNegotiationEmails(negotiationId: string | undefined) {
  return useQuery({
    queryKey: ["negotiation-emails", negotiationId],
    queryFn: () => NegotiationApi.getEmails(negotiationId as string),
    enabled: Boolean(negotiationId),
    refetchInterval: 10000,
  });
}

export function useNegotiationEmailThreads(dealId?: string) {
  return useQuery({
    queryKey: ["negotiation-email-threads", dealId ?? "all"],
    queryFn: () => NegotiationApi.getEmailThreads(dealId),
    refetchInterval: 15000,
  });
}

export function useRunNegotiationRound(negotiationId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (req: { inbound_price?: number | null; inbound_message?: string | null; party?: string }) =>
      NegotiationApi.runRound(negotiationId, req),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["negotiation-rounds", negotiationId] });
      qc.invalidateQueries({ queryKey: ["negotiation-emails", negotiationId] });
      qc.invalidateQueries({ queryKey: ["negotiation-sessions"] });
    },
  });
}

// ── Escrow ────────────────────────────────────────────────────────────────────

export function useEscrowHolds(dealId: string | undefined) {
  return useQuery({
    queryKey: ["escrow-holds", dealId],
    queryFn: () => EscrowApi.getHolds(dealId as string),
    enabled: Boolean(dealId),
  });
}

// ── Shield ─────────────────────────────────────────────────────────────────────

export function useQcRecords(dealId: string | undefined) {
  return useQuery({
    queryKey: ["qc-records", dealId],
    queryFn: () => ShieldApi.getQc(dealId as string),
    enabled: Boolean(dealId),
  });
}

export function useFreightRecords(dealId: string | undefined) {
  return useQuery({
    queryKey: ["freight-records", dealId],
    queryFn: () => ShieldApi.getFreight(dealId as string),
    enabled: Boolean(dealId),
  });
}

// ── Docugen ─────────────────────────────────────────────────────────────────────

export function useDocuments(dealId: string | undefined) {
  return useQuery({
    queryKey: ["documents", dealId],
    queryFn: () => DocugenApi.getDocuments(dealId as string),
    enabled: Boolean(dealId),
  });
}

// ── Match Engine ──────────────────────────────────────────────────────────────

export function useMatchScore(dealId: string | undefined) {
  return useQuery({
    queryKey: ["match-score", dealId],
    queryFn: () => MatchApi.getScore(dealId as string),
    enabled: Boolean(dealId),
  });
}

// ── RAG Knowledge Base ────────────────────────────────────────────────────────

export function useRagStats() {
  return useQuery({
    queryKey: ["rag", "stats"],
    queryFn: RagApi.stats,
    refetchInterval: 15000,
  });
}

export function useRagUpload() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ files, chunkStrategy }: { files: File[]; chunkStrategy?: string }) =>
      RagApi.upload(files, chunkStrategy),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["rag", "stats"] });
      qc.invalidateQueries({ queryKey: ["rag", "files"] });
    },
  });
}

// Files durably persisted in MongoDB GridFS — proves ingestion survives restarts.
export function useRagFiles() {
  return useQuery({
    queryKey: ["rag", "files"],
    queryFn: () => RagApi.files(),
    refetchInterval: 15000,
  });
}

// Test-chat: runs the full RAG pipeline for one question (no server-side history).
export function useRagQuery() {
  return useMutation({
    mutationFn: (question: string) => RagApi.query(question),
  });
}

// Polls a running ingestion job until it completes or fails.
export function useRagJob(jobId: string | null) {
  const qc = useQueryClient();
  return useQuery({
    queryKey: ["rag", "job", jobId],
    queryFn: () => RagApi.jobStatus(jobId as string),
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      const s = query.state.data?.status;
      if (s === "completed" || s === "failed") {
        qc.invalidateQueries({ queryKey: ["rag", "stats"] });
        qc.invalidateQueries({ queryKey: ["rag", "files"] });
        return false;
      }
      return 1500;
    },
  });
}
