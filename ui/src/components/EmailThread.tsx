import { useState } from "react";
import { toast } from "sonner";
import {
  CheckCheck,
  Clock,
  Edit2,
  X,
  ChevronDown,
  ChevronUp,
  Send,
  Mail,
} from "lucide-react";
import { cn, initials, timeAgo } from "@/lib/utils";
import type { EmailHistoryRecord } from "@/api/types";
import { useResolveApproval } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Skeleton } from "@/components/ui/skeleton";

function emailStatusBadge(status: EmailHistoryRecord["status"]) {
  switch (status) {
    case "pending_approval":
      return (
        <Badge variant="warning" className="gap-1">
          <Clock className="w-2.5 h-2.5" /> Pending
        </Badge>
      );
    case "sent":
    case "received":
      return (
        <Badge variant="success" className="gap-1">
          <CheckCheck className="w-2.5 h-2.5" /> Sent
        </Badge>
      );
    case "modified_sent":
      return (
        <Badge variant="info" className="gap-1">
          <Edit2 className="w-2.5 h-2.5" /> Modified
        </Badge>
      );
    case "rejected":
      return (
        <Badge variant="destructive" className="gap-1">
          <X className="w-2.5 h-2.5" /> Rejected
        </Badge>
      );
    case "failed":
      return (
        <Badge variant="destructive" className="gap-1">
          <X className="w-2.5 h-2.5" /> Failed
        </Badge>
      );
    case "draft":
      return <Badge variant="outline">Draft</Badge>;
    default:
      return <Badge variant="secondary">{status}</Badge>;
  }
}

interface EmailCardProps {
  email: EmailHistoryRecord;
  onResolved: () => void;
}

function EmailCard({ email, onResolved }: EmailCardProps) {
  const [expanded, setExpanded] = useState(false);
  const [showModify, setShowModify] = useState(false);
  const [modBody, setModBody] = useState(email.body);

  const resolve = useResolveApproval();
  const isOutbound = email.direction === "outbound";
  const isLong = email.body.length > 280;
  const displayBody =
    expanded || !isLong ? email.body : email.body.slice(0, 280) + "...";

  const senderName = isOutbound
    ? "PortLess AI"
    : email.party === "buyer"
    ? "Buyer"
    : "Supplier";

  async function handleApprove() {
    if (!email.approval_id) return;
    try {
      await resolve.mutateAsync({
        approvalId: email.approval_id,
        payload: { decision: "APPROVE", resolved_by: "operator" },
      });
      toast.success("Email approved and sent");
      onResolved();
    } catch {
      toast.error("Failed to approve email");
    }
  }

  async function handleReject() {
    if (!email.approval_id) return;
    try {
      await resolve.mutateAsync({
        approvalId: email.approval_id,
        payload: { decision: "REJECT", resolved_by: "operator" },
      });
      toast.success("Email rejected");
      onResolved();
    } catch {
      toast.error("Failed to reject email");
    }
  }

  async function handleModify() {
    if (!email.approval_id) return;
    try {
      await resolve.mutateAsync({
        approvalId: email.approval_id,
        payload: {
          decision: "APPROVE",
          resolved_by: "operator",
          modified_body: modBody,
        },
      });
      toast.success("Modified email sent");
      setShowModify(false);
      onResolved();
    } catch {
      toast.error("Failed to send modified email");
    }
  }

  return (
    <div
      className={cn(
        "flex gap-3 animate-fade-in",
        isOutbound ? "flex-row-reverse" : "flex-row"
      )}
    >
      {/* Avatar */}
      <Avatar className="w-8 h-8 shrink-0 mt-1">
        <AvatarFallback
          className={cn(
            "text-[10px]",
            isOutbound ? "bg-primary/20 text-primary" : "bg-secondary text-muted-foreground"
          )}
        >
          {initials(senderName)}
        </AvatarFallback>
      </Avatar>

      {/* Card */}
      <div
        className={cn(
          "flex-1 max-w-[80%] rounded-xl border p-3 space-y-2",
          isOutbound
            ? "bg-primary/10 border-primary/25"
            : "bg-card border-border"
        )}
      >
        {/* Header */}
        <div className="flex items-start justify-between gap-2 flex-wrap">
          <div>
            <span className="text-xs font-semibold">{senderName}</span>
            <div className="text-[10px] text-muted-foreground flex items-center gap-1 mt-0.5">
              <Mail className="w-3 h-3" />
              <span className="truncate max-w-[200px]">{isOutbound ? email.to_address : email.from_address}</span>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {emailStatusBadge(email.status)}
            <span className="text-[10px] text-muted-foreground">
              {timeAgo(email.sent_at ?? email.created_at)}
            </span>
          </div>
        </div>

        {/* Subject */}
        <p className="text-xs font-medium text-muted-foreground">
          Re: {email.subject}
        </p>

        {/* Body */}
        <div className="text-xs text-foreground whitespace-pre-wrap leading-relaxed">
          {displayBody}
        </div>

        {/* Show more */}
        {isLong && (
          <button
            onClick={() => setExpanded(!expanded)}
            className="flex items-center gap-1 text-[10px] text-[#4f8cff] hover:underline"
          >
            {expanded ? (
              <>
                <ChevronUp className="w-3 h-3" /> Show less
              </>
            ) : (
              <>
                <ChevronDown className="w-3 h-3" /> Show more
              </>
            )}
          </button>
        )}

        {/* Round badge */}
        <div className="flex items-center gap-2">
          <Badge variant="outline" className="text-[9px]">
            Round {email.round_no}
          </Badge>
          <Badge variant="secondary" className="text-[9px] capitalize">
            {email.party}
          </Badge>
        </div>

        {/* HITL actions for pending_approval */}
        {email.status === "pending_approval" && email.approval_id && (
          <div className="border-t border-border/50 pt-2 space-y-2">
            {!showModify ? (
              <div className="flex gap-2 flex-wrap">
                <Button
                  size="sm"
                  variant="success"
                  className="h-7 text-xs gap-1"
                  onClick={handleApprove}
                  disabled={resolve.isPending}
                >
                  <CheckCheck className="w-3 h-3" /> Approve & Send
                </Button>
                <Button
                  size="sm"
                  variant="destructive"
                  className="h-7 text-xs gap-1"
                  onClick={handleReject}
                  disabled={resolve.isPending}
                >
                  <X className="w-3 h-3" /> Reject
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-7 text-xs gap-1"
                  onClick={() => {
                    setShowModify(true);
                    setModBody(email.body);
                  }}
                >
                  <Edit2 className="w-3 h-3" /> Modify
                </Button>
              </div>
            ) : (
              <div className="space-y-2">
                <p className="text-[10px] text-muted-foreground font-medium">
                  Edit email body before sending:
                </p>
                <Textarea
                  value={modBody}
                  onChange={(e) => setModBody(e.target.value)}
                  className="text-xs min-h-[120px]"
                />
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="default"
                    className="h-7 text-xs gap-1"
                    onClick={handleModify}
                    disabled={resolve.isPending}
                  >
                    <Send className="w-3 h-3" /> Send Modified
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-7 text-xs"
                    onClick={() => setShowModify(false)}
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

interface EmailThreadProps {
  emails: EmailHistoryRecord[] | undefined;
  isLoading: boolean;
  onResolved?: () => void;
}

export function EmailThread({ emails, isLoading, onResolved = () => {} }: EmailThreadProps) {
  if (isLoading) {
    return (
      <div className="space-y-4 p-4">
        {[1, 2, 3].map((i) => (
          <div key={i} className="flex gap-3">
            <Skeleton className="w-8 h-8 rounded-full shrink-0" />
            <div className="flex-1 space-y-2">
              <Skeleton className="h-3 w-32" />
              <Skeleton className="h-16 w-full" />
            </div>
          </div>
        ))}
      </div>
    );
  }

  if (!emails || emails.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <Mail className="w-8 h-8 text-muted-foreground mb-3" />
        <p className="text-sm text-muted-foreground">No emails yet</p>
        <p className="text-xs text-muted-foreground mt-1">
          Email history will appear here once negotiations begin
        </p>
      </div>
    );
  }

  const sorted = [...emails].sort(
    (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
  );

  return (
    <div className="flex flex-col gap-4 p-4">
      {sorted.map((email) => (
        <EmailCard key={email.email_id} email={email} onResolved={onResolved} />
      ))}
    </div>
  );
}
