import { useState } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Clock,
  ExternalLink,
  Filter,
  Inbox,
  MessageSquare,
  Send,
  Users,
  XCircle,
} from "lucide-react";
import { cn, fmtUSD, timeAgo } from "@/lib/utils";
import type { NegotiationEmailThread } from "@/api/types";
import {
  useNegotiationEmailThreads,
  useRunNegotiationRound,
} from "@/api/hooks";
import { EmailThread } from "@/components/EmailThread";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";

// ── Helpers ──────────────────────────────────────────────────────────────────

type StatusFilter = "all" | "active" | "pending" | "closed";
type PartyFilter = "all" | "buyer" | "supplier";

const TERMINAL = new Set(["AGREED", "FAILED", "ESCALATED"]);

function isActive(s: string) {
  return s === "STARTED" || s === "ROUND";
}
function isPending(s: string) {
  return s === "HITL_PENDING";
}
function isClosed(s: string) {
  return TERMINAL.has(s);
}

function statusBadge(status: string) {
  if (status === "AGREED")
    return (
      <Badge variant="success" className="gap-1 text-[10px]">
        <CheckCircle2 className="w-2.5 h-2.5" /> Agreed
      </Badge>
    );
  if (status === "HITL_PENDING")
    return (
      <Badge variant="warning" className="gap-1 text-[10px]">
        <Clock className="w-2.5 h-2.5" /> Pending Review
      </Badge>
    );
  if (status === "FAILED" || status === "ESCALATED")
    return (
      <Badge variant="destructive" className="gap-1 text-[10px]">
        <XCircle className="w-2.5 h-2.5" />{" "}
        {status === "ESCALATED" ? "Escalated" : "Failed"}
      </Badge>
    );
  if (status === "ROUND" || status === "STARTED")
    return (
      <Badge variant="secondary" className="gap-1 text-[10px]">
        <MessageSquare className="w-2.5 h-2.5" /> Round {status === "STARTED" ? "0" : ""}
      </Badge>
    );
  return <Badge variant="outline" className="text-[10px]">{status}</Badge>;
}

// ── Thread list item ──────────────────────────────────────────────────────────

function ThreadCard({
  thread,
  selected,
  onClick,
}: {
  thread: NegotiationEmailThread;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "w-full text-left px-4 py-3 border-b border-border/40 transition-all group",
        selected
          ? "bg-primary/10 border-l-2 border-l-primary"
          : "hover:bg-card border-l-2 border-l-transparent"
      )}
    >
      {/* Top row */}
      <div className="flex items-start justify-between gap-2 mb-1.5">
        <div className="min-w-0">
          <p className="text-xs font-semibold truncate">
            {thread.deal_id}
          </p>
          <p className="text-[10px] text-muted-foreground">
            HS {thread.product_code} · Round {thread.round_no}
          </p>
        </div>
        <div className="flex flex-col items-end gap-1 shrink-0">
          {statusBadge(thread.status)}
          {thread.pending_approval_count > 0 && (
            <Badge className="text-[9px] bg-amber-500/20 text-amber-400 border-amber-500/30 gap-1">
              <AlertTriangle className="w-2 h-2" />
              {thread.pending_approval_count} pending
            </Badge>
          )}
        </div>
      </div>

      {/* Price row */}
      <div className="flex items-center gap-3 text-[10px] text-muted-foreground">
        <span>Bid {thread.current_bid ? fmtUSD(thread.current_bid) : "—"}</span>
        <ArrowRight className="w-3 h-3" />
        <span>Ask {thread.current_ask ? fmtUSD(thread.current_ask) : "—"}</span>
        {thread.margin_at_current != null && (
          <span
            className={cn(
              "ml-auto font-medium",
              thread.margin_at_current >= 8
                ? "text-emerald-400"
                : "text-[#ff5d6c]"
            )}
          >
            {thread.margin_at_current.toFixed(1)}%
          </span>
        )}
      </div>

      <p className="text-[10px] text-muted-foreground mt-1">
        {timeAgo(thread.updated_at)}
      </p>
    </button>
  );
}

// ── Thread detail header ──────────────────────────────────────────────────────

function ThreadHeader({ thread }: { thread: NegotiationEmailThread }) {
  const gap =
    thread.current_bid != null && thread.current_ask != null
      ? Math.abs(thread.current_ask - thread.current_bid)
      : null;

  return (
    <div className="px-5 py-3 border-b space-y-1.5">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-semibold">{thread.deal_id}</h2>
            {statusBadge(thread.status)}
            <Link
              to={`/deals/${thread.deal_id}`}
              className="text-[10px] text-[#4f8cff] hover:underline flex items-center gap-0.5"
            >
              View deal <ExternalLink className="w-3 h-3" />
            </Link>
          </div>
          <p className="text-[10px] text-muted-foreground mt-0.5">
            {thread.buyer_id} → {thread.seller_id} · HS {thread.product_code}
          </p>
        </div>
        <p className="text-[10px] text-muted-foreground">
          Updated {timeAgo(thread.updated_at)}
        </p>
      </div>

      {/* Price stats */}
      <div className="flex items-center gap-4 text-xs flex-wrap">
        <Stat label="Target" value={fmtUSD(thread.buyer_target_price)} />
        <Stat label="Quote" value={fmtUSD(thread.supplier_quote_price)} />
        <Separator orientation="vertical" className="h-4" />
        <Stat
          label="Bid"
          value={thread.current_bid != null ? fmtUSD(thread.current_bid) : "—"}
          highlight
        />
        <Stat
          label="Ask"
          value={thread.current_ask != null ? fmtUSD(thread.current_ask) : "—"}
          highlight
        />
        {gap != null && (
          <Stat label="Gap" value={`$${gap.toFixed(2)}`} />
        )}
        {thread.agreed_price != null && (
          <Stat label="Agreed" value={fmtUSD(thread.agreed_price)} ok />
        )}
        {thread.margin_at_current != null && (
          <Stat
            label="Margin"
            value={`${thread.margin_at_current.toFixed(2)}%`}
            ok={thread.margin_at_current >= 8}
            warn={thread.margin_at_current < 8}
          />
        )}
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  highlight,
  ok,
  warn,
}: {
  label: string;
  value: string;
  highlight?: boolean;
  ok?: boolean;
  warn?: boolean;
}) {
  return (
    <div>
      <p className="text-[9px] text-muted-foreground uppercase tracking-wide">
        {label}
      </p>
      <p
        className={cn(
          "text-xs font-semibold",
          ok && "text-emerald-400",
          warn && "text-[#ff5d6c]",
          highlight && !ok && !warn && "text-foreground"
        )}
      >
        {value}
      </p>
    </div>
  );
}

// ── HITL banner ───────────────────────────────────────────────────────────────

function HitlBanner({ count }: { count: number }) {
  return (
    <div className="mx-4 my-2 flex items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2">
      <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0" />
      <p className="text-xs text-amber-300 flex-1">
        <span className="font-semibold">{count}</span> email draft
        {count !== 1 ? "s" : ""} awaiting operator approval — review the
        conversation below and click{" "}
        <span className="font-semibold">Approve &amp; Send</span>,{" "}
        <span className="font-semibold">Modify</span>, or{" "}
        <span className="font-semibold">Reject</span>.
      </p>
      <Link to="/approvals">
        <Button variant="ghost" size="sm" className="h-7 text-xs text-amber-300 gap-1 hover:text-amber-200">
          Approvals <ExternalLink className="w-3 h-3" />
        </Button>
      </Link>
    </div>
  );
}

// ── Manual round submission form ─────────────────────────────────────────────

function SubmitRoundForm({
  negotiationId,
  onSubmit,
}: {
  negotiationId: string;
  onSubmit: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [party, setParty] = useState<"buyer" | "supplier">("buyer");
  const [price, setPrice] = useState("");
  const [msg, setMsg] = useState("");
  const runRound = useRunNegotiationRound(negotiationId);

  async function handleSubmit() {
    const counterPrice = parseFloat(price);
    if (!price || isNaN(counterPrice) || counterPrice <= 0) {
      toast.error("Enter a valid counter-price");
      return;
    }
    try {
      await runRound.mutateAsync({
        inbound_price: counterPrice,
        inbound_message: msg || undefined,
        party,
      });
      toast.success(`Round submitted (${party}, $${counterPrice})`);
      setPrice("");
      setMsg("");
      setOpen(false);
      onSubmit();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Failed to submit round";
      toast.error(msg);
    }
  }

  return (
    <div className="border-t bg-card/50">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-2 px-4 py-2.5 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
      >
        <Send className="w-3.5 h-3.5" />
        Submit Inbound Reply
        <span className="ml-auto text-[10px] text-muted-foreground">
          {open ? "▲ collapse" : "▼ expand"}
        </span>
      </button>

      {open && (
        <div className="px-4 pb-4 space-y-3 animate-fade-in">
          <Separator />
          <p className="text-[10px] text-muted-foreground">
            Use this to manually submit an inbound counter-offer when IMAP
            auto-detection is not configured.
          </p>

          {/* Party selector */}
          <div className="flex gap-2">
            {(["buyer", "supplier"] as const).map((p) => (
              <Button
                key={p}
                variant={party === p ? "default" : "outline"}
                size="sm"
                className="h-7 text-xs capitalize"
                onClick={() => setParty(p)}
              >
                <Users className="w-3 h-3 mr-1" />
                {p}
              </Button>
            ))}
          </div>

          {/* Price */}
          <div className="space-y-1">
            <Label className="text-[11px]">Counter Price (USD/unit) *</Label>
            <Input
              type="number"
              min={0}
              step={0.01}
              placeholder="e.g. 445.00"
              value={price}
              onChange={(e) => setPrice(e.target.value)}
              className="h-8 text-xs"
            />
          </div>

          {/* Message */}
          <div className="space-y-1">
            <Label className="text-[11px]">Inbound Message (optional)</Label>
            <Textarea
              placeholder="Paste the buyer / supplier reply here…"
              value={msg}
              onChange={(e) => setMsg(e.target.value)}
              className="min-h-[80px] text-xs"
            />
          </div>

          <div className="flex gap-2">
            <Button
              size="sm"
              className="h-8 text-xs gap-1"
              onClick={handleSubmit}
              disabled={runRound.isPending}
            >
              <Send className="w-3.5 h-3.5" />
              {runRound.isPending ? "Submitting…" : "Submit Round"}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="h-8 text-xs"
              onClick={() => setOpen(false)}
            >
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Empty states ──────────────────────────────────────────────────────────────

function EmptySelect() {
  return (
    <div className="flex flex-col items-center justify-center h-full text-center px-8">
      <MessageSquare className="w-10 h-10 text-muted-foreground/40 mb-4" />
      <p className="text-sm font-medium text-muted-foreground">
        Select a negotiation
      </p>
      <p className="text-xs text-muted-foreground/60 mt-1">
        Pick a thread on the left to view its full email conversation
      </p>
    </div>
  );
}

function EmptyList() {
  return (
    <div className="flex flex-col items-center justify-center py-16 px-4 text-center">
      <Inbox className="w-8 h-8 text-muted-foreground/40 mb-3" />
      <p className="text-sm text-muted-foreground">No negotiations found</p>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export function NegotiationInbox() {
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [partyFilter, setPartyFilter] = useState<PartyFilter>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const { data: threads, isLoading, refetch } = useNegotiationEmailThreads();

  const selected = threads?.find((t) => t.negotiation_id === selectedId) ?? null;

  // Filter thread list
  const filtered = (threads ?? []).filter((t) => {
    if (search) {
      const q = search.toLowerCase();
      if (
        !t.deal_id.toLowerCase().includes(q) &&
        !t.product_code.toLowerCase().includes(q) &&
        !t.buyer_id.toLowerCase().includes(q) &&
        !t.seller_id.toLowerCase().includes(q)
      )
        return false;
    }
    if (statusFilter === "active" && !isActive(t.status)) return false;
    if (statusFilter === "pending" && !isPending(t.status)) return false;
    if (statusFilter === "closed" && !isClosed(t.status)) return false;
    return true;
  });

  const totalPending = (threads ?? []).reduce(
    (s, t) => s + t.pending_approval_count,
    0
  );

  // Filter emails by party
  const visibleEmails =
    partyFilter === "all"
      ? selected?.emails
      : selected?.emails.filter((e) => e.party === partyFilter);

  const STATUS_FILTERS: { key: StatusFilter; label: string }[] = [
    { key: "all", label: "All" },
    { key: "active", label: "Active" },
    { key: "pending", label: "Pending" },
    { key: "closed", label: "Closed" },
  ];

  const PARTY_FILTERS: { key: PartyFilter; label: string }[] = [
    { key: "all", label: "All" },
    { key: "buyer", label: "Buyer" },
    { key: "supplier", label: "Supplier" },
  ];

  return (
    <div className="flex h-full overflow-hidden">
      {/* ── Left: thread list ── */}
      <div className="w-80 shrink-0 border-r flex flex-col overflow-hidden">
        {/* Header */}
        <div className="px-4 py-3 border-b space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-sm font-semibold">Threads</span>
            {totalPending > 0 && (
              <Badge className="text-[10px] bg-amber-500/20 text-amber-400 border-amber-500/30 gap-1">
                <AlertTriangle className="w-3 h-3" />
                {totalPending} pending
              </Badge>
            )}
          </div>
          <Input
            placeholder="Search deal, product, party…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="h-8 text-xs"
          />
          {/* Status filter */}
          <div className="flex gap-1">
            {STATUS_FILTERS.map(({ key, label }) => (
              <button
                key={key}
                onClick={() => setStatusFilter(key)}
                className={cn(
                  "flex-1 text-[10px] py-1 rounded-md border transition-colors",
                  statusFilter === key
                    ? "bg-primary text-primary-foreground border-primary"
                    : "border-border/50 text-muted-foreground hover:text-foreground"
                )}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {/* List */}
        <ScrollArea className="flex-1">
          {isLoading ? (
            <div className="space-y-px p-2">
              {[1, 2, 3, 4].map((i) => (
                <div key={i} className="space-y-2 p-3">
                  <Skeleton className="h-3 w-32" />
                  <Skeleton className="h-2 w-48" />
                  <Skeleton className="h-2 w-24" />
                </div>
              ))}
            </div>
          ) : filtered.length === 0 ? (
            <EmptyList />
          ) : (
            filtered.map((t) => (
              <ThreadCard
                key={t.negotiation_id}
                thread={t}
                selected={selectedId === t.negotiation_id}
                onClick={() => {
                  setSelectedId(t.negotiation_id);
                  setPartyFilter("all");
                }}
              />
            ))
          )}
        </ScrollArea>
      </div>

      {/* ── Right: conversation panel ── */}
      <div className="flex-1 flex flex-col overflow-hidden min-w-0">
        {!selected ? (
          <EmptySelect />
        ) : (
          <>
            {/* Session header */}
            <ThreadHeader thread={selected} />

            {/* HITL warning banner */}
            {selected.hitl_required && selected.pending_approval_count > 0 && (
              <HitlBanner count={selected.pending_approval_count} />
            )}

            {/* Party filter + info bar */}
            <div className="px-4 py-2 border-b flex items-center gap-3">
              <Filter className="w-3.5 h-3.5 text-muted-foreground" />
              <span className="text-[11px] text-muted-foreground">Show:</span>
              <div className="flex gap-1">
                {PARTY_FILTERS.map(({ key, label }) => (
                  <button
                    key={key}
                    onClick={() => setPartyFilter(key)}
                    className={cn(
                      "px-3 py-1 text-[10px] rounded-full border transition-colors",
                      partyFilter === key
                        ? "bg-primary text-primary-foreground border-primary"
                        : "border-border/50 text-muted-foreground hover:text-foreground"
                    )}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <span className="ml-auto text-[10px] text-muted-foreground">
                {visibleEmails?.length ?? 0} message
                {(visibleEmails?.length ?? 0) !== 1 ? "s" : ""}
              </span>
            </div>

            {/* Email thread */}
            <ScrollArea className="flex-1">
              <EmailThread
                emails={visibleEmails}
                isLoading={false}
                onResolved={() => {
                  refetch();
                }}
              />
            </ScrollArea>

            {/* Manual round form — only for non-terminal sessions */}
            {!isClosed(selected.status) && (
              <SubmitRoundForm
                negotiationId={selected.negotiation_id}
                onSubmit={refetch}
              />
            )}
          </>
        )}
      </div>
    </div>
  );
}
