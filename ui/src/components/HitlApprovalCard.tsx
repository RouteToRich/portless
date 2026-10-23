import { useState } from "react";
import { toast } from "sonner";
import {
  CheckCircle2,
  XCircle,
  Clock,
  AlertTriangle,
  DollarSign,
  Shield,
  Zap,
} from "lucide-react";
import { cn, fmtUSD, countdown, timeAgo } from "@/lib/utils";
import type { ApprovalRecord } from "@/api/types";
import { useResolveApproval } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import { Separator } from "@/components/ui/separator";

function autonomyBadge(level: string) {
  const map: Record<string, { label: string; variant: "destructive" | "warning" | "info" | "success" }> = {
    L0: { label: "L0 Always Ask", variant: "destructive" },
    L1: { label: "L1 With Timeout", variant: "warning" },
    L2: { label: "L2 Auto + Notify", variant: "info" },
    L3: { label: "L3 Autonomous", variant: "success" },
  };
  const cfg = map[level] ?? { label: level, variant: "secondary" as "info" };
  return <Badge variant={cfg.variant}>{cfg.label}</Badge>;
}

function statusBadge(status: string) {
  switch (status) {
    case "PENDING":
      return <Badge variant="warning">Pending</Badge>;
    case "APPROVED":
    case "AUTO_APPROVED":
      return <Badge variant="success">Approved</Badge>;
    case "REJECTED":
      return <Badge variant="destructive">Rejected</Badge>;
    case "TIMED_OUT":
      return <Badge variant="secondary">Timed Out</Badge>;
    default:
      return <Badge variant="outline">{status}</Badge>;
  }
}

function formatAction(action: string): string {
  return action
    .replace(/_/g, " ")
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// Money / risk actions carry the facts the operator is deciding on in the
// saga callback payload — show them instead of making them read the summary.
const DETAIL_ACTIONS = new Set([
  "RELEASE_ESCROW_TRANCHE",
  "RELEASE_SUPPLIER_PAYOUT",
  "CONFIRM_COUNTERPARTY_RISK",
  "CONFIRM_BUYER_ACCEPTANCE",
]);

function str(v: unknown): string | null {
  if (v == null || v === "") return null;
  return typeof v === "number" ? String(v) : String(v);
}

function PayloadDetails({ action, payload }: { action: string; payload: Record<string, unknown> }) {
  if (!DETAIL_ACTIONS.has(action)) return null;
  const rows: { label: string; value: React.ReactNode }[] = [];
  const num = (k: string) => (typeof payload[k] === "number" ? (payload[k] as number) : null);

  if (action === "RELEASE_ESCROW_TRANCHE") {
    rows.push({ label: "Escrow", value: <span className="font-mono">{str(payload.escrow_id) ?? "—"}</span> });
    rows.push({
      label: "Tranche",
      value: `${str(payload.tranche_id) ?? "—"}${payload.trigger ? ` · on ${String(payload.trigger).replace(/_/g, " ")}` : ""}`,
    });
    const proofs = Array.isArray(payload.proof_refs) ? (payload.proof_refs as unknown[]).filter(Boolean) : [];
    rows.push({
      label: "Proof refs",
      value: proofs.length ? (
        <ul className="space-y-0.5">
          {proofs.map((p, i) => <li key={i} className="font-mono break-all">{String(p)}</li>)}
        </ul>
      ) : (
        <span className="text-[#f5a623]">none attached</span>
      ),
    });
  }
  if (action === "RELEASE_SUPPLIER_PAYOUT") {
    const amount = num("amount");
    const owed = num("total_owed");
    rows.push({ label: "Payee", value: <span className="font-mono">{str(payload.payee_id) ?? "—"}</span> });
    rows.push({
      label: "Amount",
      value: (
        <span>
          <span className="font-semibold">{amount != null ? fmtUSD(amount) : "—"}</span>
          {owed != null && (
            <span className="text-muted-foreground"> of {fmtUSD(owed)} owed</span>
          )}
          {amount != null && owed != null && amount + 0.01 < owed && (
            <span className="text-[#f5a623]"> · partial (capped at funds released from escrow)</span>
          )}
        </span>
      ),
    });
    rows.push({
      label: "Kind",
      value: payload.kind === "supplier_balance" ? "balance after final tranche" : "main supplier payout",
    });
  }
  if (action === "CONFIRM_COUNTERPARTY_RISK") {
    rows.push({ label: "Buyer", value: str(payload.buyer) ?? "—" });
    rows.push({
      label: "Screening",
      value: <span className="text-[#f5a623]">{str(payload.screening_reason) ?? "review — see deal parties"}</span>,
    });
    const price = num("agreed_price");
    const qty = num("quantity_mt");
    rows.push({
      label: "PI to issue",
      value: price != null ? `${fmtUSD(price)}/MT${qty != null ? ` × ${qty} MT = ${fmtUSD(price * qty)}` : ""}` : "—",
    });
  }
  if (action === "CONFIRM_BUYER_ACCEPTANCE") {
    rows.push({ label: "Escrow", value: <span className="font-mono">{str(payload.escrow_id) ?? "—"}</span> });
    rows.push({ label: "Accepted by", value: str(payload.accepted_by) ?? "—" });
    rows.push({ label: "Effect", value: "releases the final ACCEPTED-trigger tranche to PortLess" });
  }

  if (rows.length === 0) return null;
  return (
    <div className="rounded-lg bg-secondary border border-border p-3">
      <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground mb-1.5">
        Details
      </p>
      <dl className="space-y-1 text-xs">
        {rows.map((r) => (
          <div key={r.label} className="grid grid-cols-[88px_1fr] gap-2">
            <dt className="text-muted-foreground">{r.label}</dt>
            <dd className="min-w-0 break-words">{r.value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

interface HitlApprovalCardProps {
  approval: ApprovalRecord;
  onResolved?: () => void;
  compact?: boolean;
}

export function HitlApprovalCard({
  approval,
  onResolved = () => {},
  compact = false,
}: HitlApprovalCardProps) {
  const [note, setNote] = useState("");
  const resolve = useResolveApproval();

  const isPending = approval.status === "PENDING";
  const isExpiring =
    approval.expires_at &&
    new Date(approval.expires_at).getTime() - Date.now() < 10 * 60 * 1000; // < 10min

  async function handleDecision(decision: "APPROVE" | "REJECT") {
    try {
      await resolve.mutateAsync({
        approvalId: approval.approval_id,
        payload: {
          decision,
          resolved_by: "operator",
          note: note || undefined,
        },
      });
      toast.success(
        decision === "APPROVE"
          ? `Approved: ${formatAction(approval.action)}`
          : `Rejected: ${formatAction(approval.action)}`
      );
      onResolved();
    } catch (err) {
      // 409 = already resolved (e.g. a double-click, or another operator/tab
      // got there first) — the request was correctly rejected, not failed.
      // Refresh instead of showing an error for state the UI just hasn't
      // caught up to yet.
      const status = (err as { response?: { status?: number } })?.response?.status;
      if (status === 409) {
        toast.info("Already resolved — refreshing");
        onResolved();
      } else {
        toast.error("Failed to resolve approval");
      }
    }
  }

  return (
    <div
      className={cn(
        "rounded-lg border bg-card",
        isPending && "border-[#f5a623]/30",
        !isPending && approval.status === "APPROVED" && "border-[#2ec28a]/30",
        !isPending && approval.status === "REJECTED" && "border-[#ff5d6c]/30",
        !isPending && approval.status !== "APPROVED" && approval.status !== "REJECTED" && "border-border"
      )}
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-3 p-4">
        <div className="flex items-center gap-2.5 flex-wrap">
          <div
            className={cn(
              "flex items-center justify-center w-8 h-8 rounded-lg shrink-0",
              isPending ? "bg-[#f5a623]/15 text-[#f5a623]" : "bg-secondary text-muted-foreground"
            )}
          >
            <Zap className="w-4 h-4" />
          </div>
          <div>
            <p className="text-sm font-semibold">{formatAction(approval.action)}</p>
            <p className="text-xs text-muted-foreground font-mono">
              Deal: {approval.deal_id.slice(0, 12)}…
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {autonomyBadge(approval.autonomy_level)}
          {statusBadge(approval.status)}
        </div>
      </div>

      {!compact && (
        <>
          <Separator />

          <div className="p-4 space-y-4">
            {/* Summary */}
            {approval.summary && (
              <p className="text-sm text-muted-foreground leading-relaxed">
                {approval.summary}
              </p>
            )}

            {/* Metrics row */}
            <div className="grid grid-cols-2 gap-3">
              {approval.financial_impact_usd != null && (
                <div className="flex items-center gap-2 rounded-lg bg-secondary border border-border p-2.5">
                  <DollarSign className="w-4 h-4 text-[#f5a623] shrink-0" />
                  <div>
                    <p className="text-[10px] text-muted-foreground">Financial Impact</p>
                    <p className="text-sm font-semibold">
                      {fmtUSD(approval.financial_impact_usd)}
                    </p>
                  </div>
                </div>
              )}

              {approval.expires_at && isPending && (
                <div
                  className={cn(
                    "flex items-center gap-2 rounded-lg border p-2.5",
                    isExpiring
                      ? "bg-[#ff5d6c]/10 border-[#ff5d6c]/30"
                      : "bg-secondary border-border"
                  )}
                >
                  <Clock
                    className={cn(
                      "w-4 h-4 shrink-0",
                      isExpiring ? "text-[#ff5d6c]" : "text-[#f5a623]"
                    )}
                  />
                  <div>
                    <p className="text-[10px] text-muted-foreground">Expires In</p>
                    <p
                      className={cn(
                        "text-sm font-semibold",
                        isExpiring ? "text-[#ff5d6c]" : "text-foreground"
                      )}
                    >
                      {countdown(approval.expires_at)}
                    </p>
                  </div>
                </div>
              )}
            </div>

            {/* Money / risk action details from the saga payload */}
            <PayloadDetails action={approval.action} payload={approval.callback_payload ?? {}} />

            {/* Risk flags */}
            {approval.risk_flags.length > 0 && (
              <div>
                <div className="flex items-center gap-1.5 mb-2">
                  <AlertTriangle className="w-3.5 h-3.5 text-[#f5a623]" />
                  <p className="text-xs font-medium text-muted-foreground">Risk Flags</p>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {approval.risk_flags.map((flag, i) => (
                    <span
                      key={i}
                      className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] bg-[#f5a623]/10 border border-[#f5a623]/30 text-[#f5a623] font-medium"
                    >
                      <Shield className="w-2.5 h-2.5" />
                      {flag}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* Timeout default */}
            {approval.expires_at && (
              <p className="text-[10px] text-muted-foreground flex items-center gap-1">
                <Clock className="w-3 h-3" />
                On timeout: auto-{approval.default_on_timeout.toLowerCase()}
              </p>
            )}

            {/* Resolved state */}
            {!isPending && (
              <div className="flex items-center gap-2 p-2.5 rounded-lg bg-secondary border border-border">
                {approval.status === "APPROVED" || approval.status === "AUTO_APPROVED" ? (
                  <CheckCircle2 className="w-4 h-4 text-[#2ec28a] shrink-0" />
                ) : (
                  <XCircle className="w-4 h-4 text-[#ff5d6c] shrink-0" />
                )}
                <div className="text-xs">
                  <span className="font-medium">
                    {approval.resolved_by ?? "System"}
                  </span>
                  {" · "}
                  <span className="text-muted-foreground">
                    {timeAgo(approval.resolved_at)}
                  </span>
                  {approval.resolution_note && (
                    <p className="text-muted-foreground mt-0.5 italic">
                      "{approval.resolution_note}"
                    </p>
                  )}
                </div>
              </div>
            )}

            {/* Approve / Reject actions */}
            {isPending && (
              <div className="space-y-2">
                <Textarea
                  placeholder="Optional note..."
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  className="text-xs min-h-[60px]"
                />
                <div className="flex gap-2">
                  <Button
                    variant="success"
                    size="sm"
                    className="flex-1 gap-1.5"
                    onClick={() => handleDecision("APPROVE")}
                    disabled={resolve.isPending}
                  >
                    <CheckCircle2 className="w-4 h-4" /> Approve
                  </Button>
                  <Button
                    variant="destructive"
                    size="sm"
                    className="flex-1 gap-1.5"
                    onClick={() => handleDecision("REJECT")}
                    disabled={resolve.isPending}
                  >
                    <XCircle className="w-4 h-4" /> Reject
                  </Button>
                </div>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
