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
  Users,
  Mail,
  Wrench,
} from "lucide-react";
import { cn, fmtUSD, countdown, timeAgo } from "@/lib/utils";
import { useAuth } from "@/lib/auth";
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
  "SEND_BUYER_OUTREACH",
  "RESOLVE_EXCEPTION",
]);

// What APPROVE actually runs for a RESOLVE_EXCEPTION playbook
// (services/hitl_approval/callbacks.py::_cb_resolve_exception).
const PLAYBOOK_EFFECT: Record<string, { label: string; effect: string; tone: "warn" | "bad" }> = {
  reinspect: {
    label: "Re-inspection",
    effect:
      "APPROVE moves the deal DISPUTE → SUPPLIER_DISPATCH_READY, clears any buyer dispute on the escrow, books a fresh QC inspection and sends the supplier a rework notice. No escrow tranche releases until a clean report.",
    tone: "warn",
  },
  cancel_unfunded: {
    label: "Cancel unfunded deal",
    effect:
      "APPROVE refunds any partially funded escrow amount to the buyer, moves the deal to CANCELLED and e-mails both parties. REJECT leaves the deal waiting.",
    tone: "bad",
  },
};

// Risk flags with a specific meaning get their own wording / colour.
const FLAG_INFO: Record<string, { label: string; hint: string; tone: "warn" | "info" | "ok" | "bad" }> = {
  four_eyes_required: {
    label: "four-eyes",
    hint: "Two different operators must approve (FOUR_EYES_THRESHOLD_USD)",
    tone: "bad",
  },
  auto_approved_below_value_threshold: {
    label: "auto-approved (below threshold)",
    hint: "Amount under the value threshold and every precondition held",
    tone: "ok",
  },
  templated_outreach: {
    label: "templated outreach",
    hint: "Introduction e-mail from the outreach template (no LLM)",
    tone: "info",
  },
  followup: { label: "follow-up", hint: "Second touch after silence", tone: "info" },
  auto_sourced_verified_in_corridor: {
    label: "auto-sourced",
    hint: "GST + IEC verified supplier inside the price corridor",
    tone: "ok",
  },
};

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
  if (action === "SEND_BUYER_OUTREACH") {
    const body = str(payload.body) ?? "";
    rows.push({ label: "To", value: <span className="font-mono">{str(payload.to_address) ?? "—"}</span> });
    rows.push({ label: "Subject", value: str(payload.subject) ?? "—" });
    rows.push({
      label: "Kind",
      value: payload.kind === "followup" ? "follow-up after silence" : "first introduction",
    });
    rows.push({ label: "Candidate", value: <span className="font-mono">{str(payload.candidate_id) ?? "—"}</span> });
    if (body) {
      rows.push({
        label: "Body",
        value: (
          <pre className="whitespace-pre-wrap font-sans text-[11px] text-muted-foreground max-h-40 overflow-auto">
            {body.length > 900 ? `${body.slice(0, 900)}…` : body}
          </pre>
        ),
      });
    }
    rows.push({ label: "Effect", value: "APPROVE sends the e-mail; REJECT skips this buyer and contacts the next candidate" });
  }
  if (action === "RESOLVE_EXCEPTION") {
    const playbook = str(payload.playbook);
    rows.push({ label: "Category", value: str(payload.category) ?? "—" });
    if (payload.severity) rows.push({ label: "Severity", value: str(payload.severity) ?? "—" });
    if (payload.root_cause) rows.push({ label: "Root cause", value: str(payload.root_cause) ?? "—" });
    const steps = Array.isArray(payload.remediation_steps) ? (payload.remediation_steps as unknown[]) : [];
    if (steps.length) {
      rows.push({
        label: "Plan",
        value: (
          <ol className="list-decimal ml-4 space-y-0.5">
            {steps.map((s, i) => <li key={i}>{String(s)}</li>)}
          </ol>
        ),
      });
    }
    if (playbook) {
      const info = PLAYBOOK_EFFECT[playbook];
      rows.push({
        label: "Playbook",
        value: (
          <span>
            <span className={cn("font-semibold", info?.tone === "bad" ? "text-[#ff5d6c]" : "text-[#f5a623]")}>
              <Wrench className="inline w-3 h-3 mr-1" />
              {info?.label ?? playbook}
            </span>
            {payload.reason ? <span className="text-muted-foreground"> — {str(payload.reason)}</span> : null}
            <p className="text-muted-foreground mt-0.5">
              {info?.effect ?? `APPROVE runs the '${playbook}' playbook; REJECT leaves the deal as it is.`}
            </p>
          </span>
        ),
      });
    } else {
      rows.push({
        label: "Effect",
        value: "APPROVE acknowledges the plan (no automatic action); REJECT raises a deal exception",
      });
    }
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

function RiskFlag({ flag }: { flag: string }) {
  const playbook = flag.startsWith("playbook:") ? flag.slice("playbook:".length) : null;
  const info = playbook
    ? { label: `playbook: ${PLAYBOOK_EFFECT[playbook]?.label ?? playbook}`, hint: PLAYBOOK_EFFECT[playbook]?.effect ?? "", tone: "warn" as const }
    : FLAG_INFO[flag];
  const tone = info?.tone ?? "warn";
  const color =
    tone === "bad" ? "#ff5d6c" : tone === "ok" ? "#2ec28a" : tone === "info" ? "#4f8cff" : "#f5a623";
  return (
    <span
      title={info?.hint}
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium border"
      style={{ color, borderColor: `${color}4d`, background: `${color}1a` }}
    >
      {playbook ? <Wrench className="w-2.5 h-2.5" /> : tone === "bad" ? <Users className="w-2.5 h-2.5" /> : <Shield className="w-2.5 h-2.5" />}
      {info?.label ?? flag}
    </span>
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
  const { user } = useAuth();
  const operator = user?.email ?? user?.name ?? "operator";

  const isPending = approval.status === "PENDING";
  const isExpiring =
    approval.expires_at &&
    new Date(approval.expires_at).getTime() - Date.now() < 10 * 60 * 1000; // < 10min

  const required = approval.required_approvals ?? 1;
  const signatures = approval.approvals ?? [];
  const remaining = approval.approvals_remaining ?? (isPending ? Math.max(0, required - signatures.length) : 0);
  const fourEyes = required > 1;
  const alreadySigned = signatures.some((s) => s.approved_by === operator);

  async function handleDecision(decision: "APPROVE" | "REJECT") {
    try {
      const res = await resolve.mutateAsync({
        approvalId: approval.approval_id,
        payload: {
          decision,
          resolved_by: operator,
          note: note || undefined,
        },
      });
      if (decision === "APPROVE" && res.status === "PENDING" && (res.approvals_remaining ?? 0) > 0) {
        toast.info(`Recorded your approval — ${res.approvals_remaining} more operator must countersign`, {
          description: `${formatAction(approval.action)} stays pending until a different operator approves.`,
        });
      } else {
        toast.success(
          decision === "APPROVE"
            ? `Approved: ${formatAction(approval.action)}`
            : `Rejected: ${formatAction(approval.action)}`
        );
      }
      onResolved();
    } catch (err) {
      // 409 = already resolved (double-click / another operator) — or, on a
      // four-eyes gate, the same operator trying to approve twice. The backend
      // says which in `detail`.
      const e = err as { response?: { status?: number; data?: { detail?: string } } };
      const status = e?.response?.status;
      const detail = e?.response?.data?.detail;
      if (status === 409 && detail && /different operator|countersign|already carries/i.test(detail)) {
        toast.error("Second approver must be a different operator", { description: detail });
      } else if (status === 409) {
        toast.info("Already resolved — refreshing");
        onResolved();
      } else {
        toast.error("Failed to resolve approval", { description: detail });
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
            {approval.action === "SEND_BUYER_OUTREACH" ? <Mail className="w-4 h-4" /> : <Zap className="w-4 h-4" />}
          </div>
          <div>
            <p className="text-sm font-semibold">{formatAction(approval.action)}</p>
            <p className="text-xs text-muted-foreground font-mono">
              Deal: {approval.deal_id.slice(0, 12)}…
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0 flex-wrap justify-end">
          {fourEyes && (
            <Badge variant={remaining > 0 && isPending ? "destructive" : "secondary"} className="gap-1">
              <Users className="w-3 h-3" />
              {isPending
                ? `${signatures.length} of ${required} approvals`
                : `${required} approvers`}
            </Badge>
          )}
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

            {/* Four-eyes state */}
            {fourEyes && (
              <div
                className={cn(
                  "rounded-lg border p-3 text-xs space-y-1",
                  isPending && remaining > 0 ? "border-[#ff5d6c]/40 bg-[#ff5d6c]/5" : "border-border bg-secondary"
                )}
              >
                <p className="font-semibold flex items-center gap-1.5">
                  <Users className="w-3.5 h-3.5" />
                  {isPending
                    ? signatures.length === 0
                      ? `Four-eyes gate — needs ${required} different operators`
                      : `${signatures.length} of ${required} approvals — needs ${remaining === 1 ? "a second, different operator" : `${remaining} more different operators`}`
                    : `Approved by ${required} operators`}
                </p>
                {signatures.length > 0 && (
                  <ul className="space-y-0.5 text-muted-foreground">
                    {signatures.map((s) => (
                      <li key={`${s.approved_by}-${s.approved_at}`}>
                        <span className="font-medium text-foreground">{s.approved_by}</span> signed{" "}
                        {timeAgo(s.approved_at)}
                        {s.note && <span className="italic"> — "{s.note}"</span>}
                      </li>
                    ))}
                  </ul>
                )}
                {isPending && alreadySigned && (
                  <p className="text-[#f5a623]">You already approved this gate — another operator has to countersign.</p>
                )}
              </div>
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
                    <RiskFlag key={`${flag}-${i}`} flag={flag} />
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
                    disabled={resolve.isPending || alreadySigned}
                    title={alreadySigned ? "You already approved — a different operator must countersign" : undefined}
                  >
                    <CheckCircle2 className="w-4 h-4" />
                    {fourEyes && signatures.length > 0 ? "Countersign" : "Approve"}
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
                <p className="text-[10px] text-muted-foreground">Deciding as {operator}</p>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
