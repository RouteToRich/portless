import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApprovalsApi,
  DealsApi,
  HealthApi,
  OpportunitiesApi,
} from "./endpoints";
import type { CreateDealRequest } from "./types";

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

export function useDeals(status?: string) {
  return useQuery({ queryKey: ["deals", status ?? "all"], queryFn: () => DealsApi.list(status) });
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

export function useApprovals(status?: string) {
  return useQuery({
    queryKey: ["approvals", status ?? "all"],
    queryFn: () => ApprovalsApi.list(status),
  });
}

export function useResolveApproval() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      approvalId,
      decision,
      resolvedBy,
    }: {
      approvalId: string;
      decision: "APPROVE" | "REJECT";
      resolvedBy: string;
    }) => ApprovalsApi.resolve(approvalId, decision, resolvedBy),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["approvals"] }),
  });
}
