import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import {
  Bell,
  BellRing,
  CheckCircle2,
  Clock,
  ExternalLink,
  Filter,
  Inbox,
  Mail,
  XCircle,
  Zap,
} from "lucide-react";
import { cn, fmtUSD, timeAgo, countdown } from "@/lib/utils";
import type { ApprovalRecord } from "@/api/types";
import { useApprovals, useApprovalStream, useNegotiationEmails } from "@/api/hooks";
import { HitlApprovalCard } from "@/components/HitlApprovalCard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import { ScrollArea } from "@/components/ui/scroll-area";

function statusVariant(status: string): "warning" | "success" | "destructive" | "secondary" | "outline" {
  switch (status) {
    case "PENDING": return "warning";
    case "APPROVED":
    case "AUTO_APPROVED": return "success";
    case "REJECTED": return "destructive";
    case "TIMED_OUT": return "secondary";
    default: return "outline";
  }
}

function actionLabel(action: string): string {
  return action.replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
}

function isEmailAction(action: string): boolean {
  return action.toLowerCase().includes("email") || action === "SEND_NEGOTIATION_EMAIL";
}

interface ApprovalRowProps {
  approval: ApprovalRecord;
  isSelected: boolean;
  onClick: () => void;
}

function ApprovalRow({ approval, isSelected, onClick }: ApprovalRowProps) {
  const isPending = approval.status === "PENDING";
  const isExpiring =
    approval.expires_at != null &&
    new Date(approval.expires_at).getTime() - Date.now() < 10 * 60 * 1000;

  return (
    <button
      onClick={onClick}
      className={cn(
        "w-full text-left px-4 py-3 border-b border-border/50 transition-all duration-150 group",
        isSelected
          ? "bg-primary/10 border-l-2 border-l-primary"
          : "hover:bg-card border-l-2 border-l-transparent"
      )}
    >
      <div className="flex items-start justify-between gap-2 mb-1.5">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs font-semibold">{actionLabel(approval.action)}</span>
          {isPending && isExpiring && (
            <span className="text-[10px] text-[#ff5d6c] font-medium flex items-center gap-0.5">
              <Clock className="w-3 h-3" /> {countdown(approval.expires_at)}
            </span>
          )}
        </div>
        <Badge variant={statusVariant(approval.status)} className="text-[10px] shrink-0">
          {approval.status}
        </Badge>
      </div>

      <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
        <span className="font-mono">{approval.deal_id.slice(0, 8)}…</span>
        {approval.financial_impact_usd != null && (
          <>
            <span className="text-border">·</span>
            <span className="text-[#f5a623] font-medium">
              {fmtUSD(approval.financial_impact_usd)}
            </span>
          </>
        )}
        <span className="text-border">·</span>
        <span>{timeAgo(approval.created_at)}</span>
      </div>

      {approval.summary && (
        <p className="text-[10px] text-muted-foreground mt-1 line-clamp-2 leading-relaxed">
          {approval.summary}
        </p>
      )}

      {approval.risk_flags.length > 0 && (
        <div className="flex items-center gap-1 mt-1.5 flex-wrap">
          {approval.risk_flags.slice(0, 3).map((flag, i) => (
            <span
              key={i}
              className="text-[9px] px-1.5 py-0.5 rounded bg-[#f5a623]/10 text-[#f5a623] border border-[#f5a623]/20"
            >
              {flag}
            </span>
          ))}
          {approval.risk_flags.length > 3 && (
            <span className="text-[9px] text-muted-foreground">
              +{approval.risk_flags.length - 3} more
            </span>
          )}
        </div>
      )}
    </button>
  );
}

export function ApprovalsInbox() {
  const { data: approvals, isLoading, isError, refetch } = useApprovals();
  const { connected } = useApprovalStream();
  const [selected, setSelected] = useState<ApprovalRecord | null>(null);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<"ALL" | "PENDING" | "RESOLVED">("ALL");
  const prevCountRef = useRef(0);

  // Toast on new approval
  useEffect(() => {
    const pending = (approvals ?? []).filter((a) => a.status === "PENDING");
    if (pending.length > prevCountRef.current && prevCountRef.current > 0) {
      toast("New approval requires action", {
        description: actionLabel(pending[0].action),
        action: { label: "View", onClick: () => setSelected(pending[0]) },
        icon: <BellRing className="w-4 h-4 text-[#f5a623]" />,
      });
    }
    prevCountRef.current = pending.length;
  }, [approvals]);

  const allApprovals = approvals ?? [];

  // Group: pending first, then resolved
  const pending = allApprovals.filter((a) => a.status === "PENDING");
  const resolved = allApprovals.filter((a) => a.status !== "PENDING");

  const filterFn = (a: ApprovalRecord) => {
    const matchesFilter =
      filter === "ALL" ||
      (filter === "PENDING" && a.status === "PENDING") ||
      (filter === "RESOLVED" && a.status !== "PENDING");
    const matchesSearch =
      !search ||
      a.action.toLowerCase().includes(search.toLowerCase()) ||
      a.deal_id.toLowerCase().includes(search.toLowerCase()) ||
      (a.summary ?? "").toLowerCase().includes(search.toLowerCase());
    return matchesFilter && matchesSearch;
  };

  const filteredPending = pending.filter(filterFn);
  const filteredResolved = resolved.filter(filterFn);

  // The approval's callback_payload only carries email_id/negotiation_id/party
  // (see negotiation_agent/service.py) — the full drafted subject/body lives
  // in the negotiation's email history, so fetch it separately to display.
  const selectedNegotiationId = selected?.callback_payload?.negotiation_id as string | undefined;
  const isSelectedEmailAction = isEmailAction(selected?.action ?? "");
  const { data: negotiationEmails, isLoading: emailsLoading } = useNegotiationEmails(
    isSelectedEmailAction ? selectedNegotiationId : undefined
  );
  const selectedEmailId = selected?.callback_payload?.email_id as string | undefined;
  // Prefer the exact drafted email; fall back to the latest email on the thread
  // so the operator always sees the full body rather than a stalled spinner if
  // the email_id can't be matched.
  const draftEmail =
    negotiationEmails?.find((e) => e.email_id === selectedEmailId) ??
    (negotiationEmails && negotiationEmails.length > 0
      ? negotiationEmails[negotiationEmails.length - 1]
      : undefined);
  // A modified body (operator edited before/after) wins over the original draft.
  const fullEmailBody = selected?.modified_body ?? draftEmail?.body ?? null;

  return (
    <div className="flex flex-col gap-3 h-[calc(100vh-120px)] min-h-0">
      {/* Header */}
      <div className="flex items-center justify-between gap-3 shrink-0">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <h1 className="text-lg font-bold">Approvals Inbox</h1>
            {pending.length > 0 && (
              <span className="flex items-center justify-center w-6 h-6 text-[11px] font-bold rounded-full bg-[#ff5d6c] text-white">
                {pending.length}
              </span>
            )}
          </div>
          {/* SSE connection indicator */}
          <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
            <span
              className={cn(
                "w-1.5 h-1.5 rounded-full",
                connected ? "bg-[#2ec28a] animate-pulse" : "bg-muted-foreground"
              )}
            />
            {connected ? "Live" : "Polling"}
          </div>
        </div>
        <Button variant="outline" size="sm" className="h-8 gap-1.5 text-xs" onClick={() => refetch()}>
          Refresh
        </Button>
      </div>

      <div className="flex flex-col md:flex-row gap-3 flex-1 min-h-0">
        {/* ── Left pane: list ── */}
        <div className="w-full md:w-[340px] md:shrink-0 flex flex-col bg-popover border border-border rounded-xl overflow-hidden min-h-[280px] md:min-h-0">
          {/* Filters */}
          <div className="p-3 border-b border-border space-y-2">
            <div className="relative">
              <Filter className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
              <Input
                placeholder="Search..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="pl-8 h-8 text-xs"
              />
            </div>
            <div className="flex gap-1">
              {(["ALL", "PENDING", "RESOLVED"] as const).map((f) => (
                <button
                  key={f}
                  onClick={() => setFilter(f)}
                  className={cn(
                    "flex-1 text-[10px] font-medium py-1.5 rounded-md transition-colors",
                    filter === f
                      ? "bg-primary/15 text-primary"
                      : "text-muted-foreground hover:bg-secondary"
                  )}
                >
                  {f}
                  {f === "PENDING" && filteredPending.length > 0 && (
                    <span className="ml-1 text-[#f5a623]">({filteredPending.length})</span>
                  )}
                </button>
              ))}
            </div>
          </div>

          {/* List */}
          <ScrollArea className="flex-1">
            {isLoading ? (
              <div className="p-3 space-y-2">
                {[1, 2, 3, 4].map((i) => (
                  <Skeleton key={i} className="h-20 w-full rounded-lg" />
                ))}
              </div>
            ) : isError ? (
              <div className="flex flex-col items-center py-12 gap-2 text-muted-foreground">
                <Bell className="w-6 h-6" />
                <p className="text-xs">Backend unreachable</p>
              </div>
            ) : filteredPending.length === 0 && filteredResolved.length === 0 ? (
              <div className="flex flex-col items-center py-12 gap-2 text-muted-foreground">
                <Inbox className="w-6 h-6" />
                <p className="text-xs">No approvals</p>
              </div>
            ) : (
              <>
                {filteredPending.length > 0 && (
                  <>
                    <div className="px-4 py-2 text-[10px] font-semibold uppercase tracking-widest text-[#f5a623] bg-[#f5a623]/5 border-b border-[#f5a623]/20">
                      Pending · {filteredPending.length}
                    </div>
                    {filteredPending.map((a) => (
                      <ApprovalRow
                        key={a.approval_id}
                        approval={a}
                        isSelected={selected?.approval_id === a.approval_id}
                        onClick={() => setSelected(a)}
                      />
                    ))}
                  </>
                )}
                {filteredResolved.length > 0 && (
                  <>
                    <div className="px-4 py-2 text-[10px] font-semibold uppercase tracking-widest text-muted-foreground bg-secondary/30 border-b border-border">
                      Resolved · {filteredResolved.length}
                    </div>
                    {filteredResolved.map((a) => (
                      <ApprovalRow
                        key={a.approval_id}
                        approval={a}
                        isSelected={selected?.approval_id === a.approval_id}
                        onClick={() => setSelected(a)}
                      />
                    ))}
                  </>
                )}
              </>
            )}
          </ScrollArea>
        </div>

        {/* ── Right pane: detail ── */}
        <div className="flex-1 min-w-0">
          {!selected ? (
            <div className="flex flex-col items-center justify-center h-full gap-3 text-muted-foreground border border-dashed border-border rounded-xl">
              <Zap className="w-10 h-10 opacity-30" />
              <p className="text-sm">Select an approval to review</p>
              {pending.length > 0 && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setSelected(pending[0])}
                  className="mt-2"
                >
                  Review oldest pending
                </Button>
              )}
            </div>
          ) : (
            <ScrollArea className="h-full pr-1">
              <div className="space-y-4">
                {/* Context links */}
                <div className="flex items-center gap-3 text-xs text-muted-foreground">
                  <Link
                    to={`/deals/${selected.deal_id}`}
                    className="flex items-center gap-1 text-[#4f8cff] hover:underline"
                  >
                    View Deal <ExternalLink className="w-3 h-3" />
                  </Link>
                  {selected.deep_link && (
                    <a
                      href={selected.deep_link}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex items-center gap-1 hover:underline"
                    >
                      Deep Link <ExternalLink className="w-3 h-3" />
                    </a>
                  )}
                </div>

                {/* Full HITL card */}
                <HitlApprovalCard
                  approval={selected}
                  onResolved={() => {
                    refetch();
                    // Re-select with updated data
                    setTimeout(() => {
                      const updated = approvals?.find(
                        (a) => a.approval_id === selected.approval_id
                      );
                      if (updated) setSelected(updated);
                    }, 500);
                  }}
                />

                {/* Full email content for email actions — fetched from negotiation
                    email history since callback_payload only carries
                    email_id/negotiation_id. Always shows the complete body (no
                    truncation), unlike the approval's 150-char summary field. */}
                {isSelectedEmailAction && selected.callback_payload && (
                  <div className="rounded-xl border border-border bg-popover">
                    <div className="flex items-center gap-2 px-4 py-3 border-b border-border">
                      <Mail className="w-4 h-4 text-[#4f8cff]" />
                      <span className="text-sm font-semibold">Full Email Content</span>
                    </div>
                    <div className="p-4 space-y-2">
                      {emailsLoading ? (
                        <p className="text-xs text-muted-foreground">Loading draft…</p>
                      ) : fullEmailBody ? (
                        <>
                          {draftEmail && (
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1">
                              <div>
                                <p className="text-[10px] text-muted-foreground">Subject</p>
                                <p className="text-sm font-medium break-words">{draftEmail.subject}</p>
                              </div>
                              <div>
                                <p className="text-[10px] text-muted-foreground">Recipient</p>
                                <p className="text-sm font-medium break-words">
                                  {draftEmail.to_address}
                                  <span className="ml-1 text-[10px] text-muted-foreground capitalize">
                                    ({draftEmail.party})
                                  </span>
                                </p>
                              </div>
                            </div>
                          )}
                          <Separator />
                          {/* whitespace-pre-wrap + break-words: render the entire
                              body verbatim, wrapping long lines instead of clipping. */}
                          <div className="text-sm text-foreground whitespace-pre-wrap break-words leading-relaxed">
                            {fullEmailBody}
                          </div>
                          {selected.modified_body && draftEmail?.body && selected.modified_body !== draftEmail.body && (
                            <p className="text-[10px] text-muted-foreground italic pt-1">
                              Showing the operator-modified body.
                            </p>
                          )}
                        </>
                      ) : (
                        // Loaded but no matching email on the thread — show the
                        // approval summary rather than a permanent spinner.
                        <p className="text-sm text-muted-foreground whitespace-pre-wrap break-words leading-relaxed">
                          {selected.summary ?? "Drafted email content is not available for this approval."}
                        </p>
                      )}
                    </div>
                  </div>
                )}

                {/* Resolution record */}
                {selected.status !== "PENDING" && (
                  <div className="rounded-xl border border-border bg-popover p-4 space-y-2">
                    <p className="text-xs font-semibold text-muted-foreground">
                      Resolution
                    </p>
                    <div className="flex items-center gap-2">
                      {selected.status === "APPROVED" || selected.status === "AUTO_APPROVED" ? (
                        <CheckCircle2 className="w-4 h-4 text-[#2ec28a]" />
                      ) : (
                        <XCircle className="w-4 h-4 text-[#ff5d6c]" />
                      )}
                      <span className="text-sm font-medium">
                        {selected.status} by {selected.resolved_by ?? "System"}
                      </span>
                      <span className="text-xs text-muted-foreground ml-auto">
                        {timeAgo(selected.resolved_at)}
                      </span>
                    </div>
                    {selected.resolution_note && (
                      <p className="text-xs text-muted-foreground italic pl-6">
                        "{selected.resolution_note}"
                      </p>
                    )}
                    {selected.modified_body && (
                      <div className="pl-6">
                        <p className="text-[10px] text-muted-foreground mb-1">
                          Modified email body:
                        </p>
                        <div className="text-xs bg-card border border-border rounded p-2 whitespace-pre-wrap">
                          {selected.modified_body}
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </ScrollArea>
          )}
        </div>
      </div>
    </div>
  );
}
