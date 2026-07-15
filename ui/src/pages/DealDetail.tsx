import { useState } from "react";
import { useParams, Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ArrowLeft,
  CheckCircle2,
  XCircle,
  ExternalLink,
  FileDown,
  Ship,
  ShieldCheck,
  TruckIcon,
  CreditCard,
  FileText,
  Repeat2,
  Send,
  Users,
} from "lucide-react";
import { cn, fmtUSD, fmtPct, timeAgo, fmtDateTime } from "@/lib/utils";
import {
  useDeal,
  useNegotiationSessions,
  useNegotiationRounds,
  useNegotiationEmails,
  useRunNegotiationRound,
  useApprovals,
  useEscrowHolds,
  useQcRecords,
  useFreightRecords,
  useDocuments,
  useMatchScore,
} from "@/api/hooks";
import { StatusBadge } from "@/components/StatusBadge";
import { QueryState } from "@/components/QueryState";
import { DealPipeline } from "@/components/DealPipeline";
import { ScoreRing } from "@/components/ScoreRing";
import { EmailThread } from "@/components/EmailThread";
import { HitlApprovalCard } from "@/components/HitlApprovalCard";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Progress } from "@/components/ui/progress";
import type { QcRecord, FreightRecord } from "@/api/types";

const NEGOTIATION_TERMINAL = new Set(["AGREED", "FAILED", "ESCALATED"]);

// Manual inbound-reply submission — moved here from the (now removed) standalone
// Negotiation Inbox page so a negotiation can still be advanced by hand from the
// deal's Negotiation tab when IMAP auto-detection isn't configured.
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
      toast.error(e instanceof Error ? e.message : "Failed to submit round");
    }
  }

  return (
    <Card>
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-2 px-4 py-3 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
      >
        <Send className="w-3.5 h-3.5" />
        Submit Inbound Reply
        <span className="ml-auto text-[10px] text-muted-foreground">
          {open ? "▲ collapse" : "▼ expand"}
        </span>
      </button>

      {open && (
        <div className="px-4 pb-4 space-y-3">
          <Separator />
          <p className="text-[10px] text-muted-foreground">
            Use this to manually submit an inbound counter-offer when IMAP
            auto-detection is not configured.
          </p>

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
            <Button variant="ghost" size="sm" className="h-8 text-xs" onClick={() => setOpen(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </Card>
  );
}

// ── Sub-tabs ─────────────────────────────────────────────────────────────────

function OverviewTab({ dealId }: { dealId: string }) {
  const { data: deal } = useDeal(dealId);
  const { data: matchScore, isLoading: scoreLoading } = useMatchScore(dealId);

  if (!deal) return null;

  const buyer = deal.parties.find((p) => p.role === "buyer");
  const supplier = deal.parties.find((p) => p.role === "supplier");

  return (
    <div className="space-y-4">
      {/* Metric cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {[
          {
            label: "Est. Margin",
            value: fmtUSD(deal.est_margin_usd),
            color: "#2ec28a",
          },
          {
            label: "Realized Margin",
            value: fmtUSD(deal.realized_margin_usd),
            color: "#28c2a0",
          },
          {
            label: "Margin Floor",
            value: fmtPct(deal.margin_floor_pct),
            color: "#f5a623",
          },
          {
            label: "Autonomy",
            value: deal.autonomy_profile,
            color: "#4f8cff",
          },
        ].map((m) => (
          <Card key={m.label}>
            <CardHeader className="pb-1">
              <CardTitle className="text-xs text-muted-foreground">{m.label}</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-xl font-bold" style={{ color: m.color }}>
                {m.value}
              </p>
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Parties */}
        <div className="lg:col-span-2 space-y-3">
          {[buyer, supplier].filter(Boolean).map((party) => {
            if (!party) return null;
            return (
              <Card key={party.role}>
                <CardHeader>
                  <div className="flex items-center justify-between">
                    <CardTitle className="capitalize">{party.role}</CardTitle>
                    <Badge variant={party.role === "buyer" ? "info" : "teal"}>
                      {party.role}
                    </Badge>
                  </div>
                </CardHeader>
                <CardContent className="space-y-2">
                  <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm">
                    <div>
                      <p className="text-[10px] text-muted-foreground">Legal Name</p>
                      <p className="font-medium">{party.legal_name}</p>
                    </div>
                    <div>
                      <p className="text-[10px] text-muted-foreground">Country</p>
                      <p className="font-medium">{party.country}</p>
                    </div>
                    {party.gstin && (
                      <div>
                        <p className="text-[10px] text-muted-foreground">GSTIN</p>
                        <p className="font-mono text-xs">{party.gstin}</p>
                      </div>
                    )}
                    {party.iec && (
                      <div>
                        <p className="text-[10px] text-muted-foreground">IEC</p>
                        <p className="font-mono text-xs">{party.iec}</p>
                      </div>
                    )}
                  </div>
                  {/* Verification badges */}
                  <div className="flex items-center gap-2 flex-wrap pt-1">
                    {party.verification_status && (
                      <span
                        className={cn(
                          "inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full border font-medium",
                          party.verification_status === "verified"
                            ? "bg-[rgba(46,194,138,0.15)] text-[#2ec28a] border-[rgba(46,194,138,0.4)]"
                            : "bg-[rgba(245,166,35,0.15)] text-[#f5a623] border-[rgba(245,166,35,0.4)]"
                        )}
                      >
                        {party.verification_status === "verified" ? (
                          <CheckCircle2 className="w-2.5 h-2.5" />
                        ) : (
                          <XCircle className="w-2.5 h-2.5" />
                        )}
                        {party.verification_status}
                      </span>
                    )}
                    {party.credit_status && (
                      <span className="inline-flex items-center text-[10px] px-2 py-0.5 rounded-full border bg-[rgba(79,140,255,0.15)] text-[#4f8cff] border-[rgba(79,140,255,0.4)] font-medium">
                        Credit: {party.credit_status}
                      </span>
                    )}
                    {party.risk_score != null && (
                      <span
                        className={cn(
                          "text-[10px] px-2 py-0.5 rounded-full border font-medium",
                          party.risk_score < 30
                            ? "bg-[rgba(46,194,138,0.15)] text-[#2ec28a] border-[rgba(46,194,138,0.4)]"
                            : party.risk_score < 60
                            ? "bg-[rgba(245,166,35,0.15)] text-[#f5a623] border-[rgba(245,166,35,0.4)]"
                            : "bg-[rgba(255,93,108,0.15)] text-[#ff5d6c] border-[rgba(255,93,108,0.4)]"
                        )}
                      >
                        Risk: {party.risk_score}
                      </span>
                    )}
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>

        {/* Match scores */}
        <Card>
          <CardHeader>
            <CardTitle>Match Scores</CardTitle>
          </CardHeader>
          <CardContent>
            {scoreLoading ? (
              <div className="grid grid-cols-2 gap-4">
                {[1, 2, 3, 4].map((i) => (
                  <Skeleton key={i} className="h-24 w-full rounded-lg" />
                ))}
              </div>
            ) : matchScore ? (
              <div className="flex flex-col items-center gap-4">
                <ScoreRing
                  score={matchScore.composite_score}
                  label="Composite"
                  size="lg"
                />
                <div className="grid grid-cols-2 gap-3 w-full">
                  <ScoreRing score={matchScore.price_score} label="Price" size="sm" />
                  <ScoreRing score={matchScore.margin_score} label="Margin" size="sm" />
                  <ScoreRing score={matchScore.risk_score} label="Risk" size="sm" />
                  <ScoreRing score={matchScore.logistics_score} label="Logistics" size="sm" />
                </div>
              </div>
            ) : (
              <p className="text-xs text-muted-foreground text-center py-8">
                Match scores not available
              </p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function NegotiationTab({ dealId }: { dealId: string }) {
  const { data: sessions, isLoading: sessionsLoading } = useNegotiationSessions(dealId);
  const primarySession = sessions?.[0];

  const { data: rounds, isLoading: roundsLoading } = useNegotiationRounds(
    primarySession?.negotiation_id
  );
  const { data: emails, isLoading: emailsLoading, refetch: refetchEmails } = useNegotiationEmails(
    primarySession?.negotiation_id
  );
  const { data: approvals } = useApprovals("PENDING");
  const dealApprovals = (approvals ?? []).filter((a) => a.deal_id === dealId);

  const qc = useQueryClient();
  const handleEmailResolved = () => {
    qc.invalidateQueries({ queryKey: ["negotiation-emails", primarySession?.negotiation_id] });
    qc.invalidateQueries({ queryKey: ["approvals"] });
  };

  if (sessionsLoading) {
    return (
      <div className="flex gap-4">
        <div className="flex-[3] space-y-3">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-32 w-full rounded-lg" />
          ))}
        </div>
        <div className="flex-[2] space-y-3">
          <Skeleton className="h-48 w-full rounded-lg" />
          <Skeleton className="h-32 w-full rounded-lg" />
        </div>
      </div>
    );
  }

  if (!primarySession) {
    return (
      <div className="flex flex-col items-center py-16 text-muted-foreground">
        <Repeat2 className="w-8 h-8 mb-3" />
        <p className="text-sm">No negotiation sessions yet</p>
      </div>
    );
  }

  return (
    <div className="flex gap-4">
      {/* Left: Email thread */}
      <div className="flex-[3] min-w-0">
        <Card>
          <CardHeader>
            <CardTitle>Email Thread</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <div className="max-h-[600px] overflow-y-auto scrollbar-thin">
              <EmailThread
                emails={emails}
                isLoading={emailsLoading}
                onResolved={() => {
                  handleEmailResolved();
                  refetchEmails();
                }}
              />
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Right: Session info */}
      <div className="flex-[2] space-y-3 min-w-0">
        {/* Session summary */}
        <Card>
          <CardHeader>
            <CardTitle>Session Summary</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-2 gap-3 text-sm">
              <div>
                <p className="text-[10px] text-muted-foreground">Status</p>
                <Badge
                  variant={
                    primarySession.status === "AGREED"
                      ? "success"
                      : primarySession.status === "FAILED"
                      ? "destructive"
                      : "warning"
                  }
                  className="mt-0.5"
                >
                  {primarySession.status}
                </Badge>
              </div>
              <div>
                <p className="text-[10px] text-muted-foreground">Round</p>
                <p className="font-bold text-lg text-[#4f8cff]">
                  #{primarySession.round_no}
                </p>
              </div>
              <div>
                <p className="text-[10px] text-muted-foreground">Current Bid</p>
                <p className="font-semibold">{fmtUSD(primarySession.current_bid)}</p>
              </div>
              <div>
                <p className="text-[10px] text-muted-foreground">Current Ask</p>
                <p className="font-semibold">{fmtUSD(primarySession.current_ask)}</p>
              </div>
              {primarySession.agreed_price && (
                <div className="col-span-2">
                  <p className="text-[10px] text-muted-foreground">Agreed Price</p>
                  <p className="font-bold text-[#2ec28a] text-lg">
                    {fmtUSD(primarySession.agreed_price)}
                  </p>
                </div>
              )}
              {primarySession.margin_at_current != null && (
                <div className="col-span-2">
                  <p className="text-[10px] text-muted-foreground mb-1">
                    Margin at current: {fmtPct(primarySession.margin_at_current)}
                  </p>
                  <Progress
                    value={Math.min(100, primarySession.margin_at_current)}
                    indicatorClassName={
                      primarySession.margin_at_current >= 10
                        ? "bg-[#2ec28a]"
                        : primarySession.margin_at_current >= 5
                        ? "bg-[#f5a623]"
                        : "bg-[#ff5d6c]"
                    }
                  />
                </div>
              )}
            </div>
          </CardContent>
        </Card>

        {/* Manual round submission — only for non-terminal sessions */}
        {!NEGOTIATION_TERMINAL.has(primarySession.status) && (
          <SubmitRoundForm
            negotiationId={primarySession.negotiation_id}
            onSubmit={() => {
              qc.invalidateQueries({ queryKey: ["negotiation-sessions", dealId] });
              qc.invalidateQueries({ queryKey: ["negotiation-rounds", primarySession.negotiation_id] });
              refetchEmails();
            }}
          />
        )}

        {/* Pending HITL approvals */}
        {dealApprovals.length > 0 && (
          <div className="space-y-2">
            <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">
              Pending Approvals
            </p>
            {dealApprovals.map((appr) => (
              <HitlApprovalCard key={appr.approval_id} approval={appr} compact />
            ))}
          </div>
        )}

        {/* Round history */}
        {!roundsLoading && rounds && rounds.length > 0 && (
          <Card>
            <CardHeader>
              <CardTitle>Round History</CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              <div className="max-h-[280px] overflow-y-auto scrollbar-thin">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-card z-10">
                    <tr className="border-b border-border">
                      <th className="px-3 py-2 text-left text-muted-foreground">#</th>
                      <th className="px-3 py-2 text-left text-muted-foreground">Party</th>
                      <th className="px-3 py-2 text-right text-muted-foreground">Price</th>
                      <th className="px-3 py-2 text-right text-muted-foreground">Margin</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...rounds].reverse().map((round) => (
                      <tr
                        key={round.round_id}
                        className="border-b border-border/50 hover:bg-secondary/30"
                      >
                        <td className="px-3 py-2 font-mono text-muted-foreground">
                          {round.round_no}
                        </td>
                        <td className="px-3 py-2 capitalize">{round.party}</td>
                        <td className="px-3 py-2 text-right font-medium">
                          {fmtUSD(round.inbound_price)}
                        </td>
                        <td className="px-3 py-2 text-right">
                          <span
                            className={cn(
                              "font-medium",
                              round.margin_at_round != null && round.margin_at_round >= 8
                                ? "text-[#2ec28a]"
                                : "text-[#f5a623]"
                            )}
                          >
                            {fmtPct(round.margin_at_round)}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}

function qcStatusColor(status: QcRecord["status"]) {
  switch (status) {
    case "PASSED": return "text-[#2ec28a]";
    case "FAILED": return "text-[#ff5d6c]";
    case "IN_PROGRESS": return "text-[#f5a623]";
    default: return "text-muted-foreground";
  }
}

function freightProgress(status: FreightRecord["status"]): number {
  switch (status) {
    case "BOOKED": return 25;
    case "IN_TRANSIT": return 60;
    case "ARRIVED": return 85;
    case "CLEARED": return 100;
    default: return 0;
  }
}

function LogisticsTab({ dealId }: { dealId: string }) {
  const { data: qcRecords, isLoading: qcLoading } = useQcRecords(dealId);
  const { data: freightRecords, isLoading: freightLoading } = useFreightRecords(dealId);

  return (
    <div className="space-y-4">
      {/* QC */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <ShieldCheck className="w-4 h-4 text-[#28c2a0]" />
            <CardTitle>Quality Control</CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          {qcLoading ? (
            <Skeleton className="h-24 w-full" />
          ) : !qcRecords || qcRecords.length === 0 ? (
            <p className="text-sm text-muted-foreground">No QC records</p>
          ) : (
            <div className="space-y-3">
              {qcRecords.map((qc) => (
                <div
                  key={qc.qc_id}
                  className="flex items-center justify-between p-3 rounded-lg bg-secondary border border-border"
                >
                  <div>
                    <p className="text-sm font-medium">{qc.provider}</p>
                    <p className="text-[10px] text-muted-foreground mt-0.5">
                      Booked {timeAgo(qc.booked_at)}
                    </p>
                  </div>
                  <div className="flex items-center gap-3">
                    {qc.report_url && (
                      <a
                        href={qc.report_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="flex items-center gap-1 text-xs text-[#4f8cff] hover:underline"
                      >
                        Report <ExternalLink className="w-3 h-3" />
                      </a>
                    )}
                    <span className={cn("text-sm font-bold", qcStatusColor(qc.status))}>
                      {qc.status}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Freight */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <TruckIcon className="w-4 h-4 text-[#4f8cff]" />
            <CardTitle>Freight</CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          {freightLoading ? (
            <Skeleton className="h-24 w-full" />
          ) : !freightRecords || freightRecords.length === 0 ? (
            <p className="text-sm text-muted-foreground">No freight records</p>
          ) : (
            <div className="space-y-4">
              {freightRecords.map((fr) => (
                <div key={fr.tpo_id} className="space-y-2">
                  <div className="flex items-center justify-between">
                    <div>
                      <p className="text-sm font-medium">{fr.provider}</p>
                      {fr.bl_number && (
                        <p className="text-[10px] text-muted-foreground font-mono">
                          BL: {fr.bl_number}
                        </p>
                      )}
                    </div>
                    <div className="text-right">
                      <Badge
                        variant={
                          fr.status === "CLEARED"
                            ? "success"
                            : fr.status === "IN_TRANSIT"
                            ? "info"
                            : "secondary"
                        }
                      >
                        {fr.status}
                      </Badge>
                      {fr.eta && (
                        <p className="text-[10px] text-muted-foreground mt-1">
                          ETA: {fmtDateTime(fr.eta)}
                        </p>
                      )}
                    </div>
                  </div>
                  <Progress
                    value={freightProgress(fr.status)}
                    indicatorClassName="bg-[#4f8cff]"
                  />
                  <div className="flex justify-between text-[10px] text-muted-foreground">
                    {["Booked", "In Transit", "Arrived", "Cleared"].map((s) => (
                      <span key={s}>{s}</span>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function DocumentsTab({ dealId }: { dealId: string }) {
  const { data: documents, isLoading } = useDocuments(dealId);

  const docIcons: Record<string, React.ElementType> = {
    CI: FileText,
    PL: FileText,
    CoO: FileText,
    SDF: FileText,
    "Shipping Bill": Ship,
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <FileText className="w-4 h-4 text-[#4f8cff]" />
          <CardTitle>Generated Documents</CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="space-y-2">
            {[1, 2, 3].map((i) => <Skeleton key={i} className="h-14 w-full" />)}
          </div>
        ) : !documents || documents.length === 0 ? (
          <p className="text-sm text-muted-foreground">No documents generated yet</p>
        ) : (
          <div className="space-y-2">
            {documents.map((doc) => {
              const Icon = docIcons[doc.document_type] ?? FileText;
              return (
                <div
                  key={doc.document_id}
                  className="flex items-center justify-between p-3 rounded-lg bg-secondary border border-border"
                >
                  <div className="flex items-center gap-3">
                    <div className="flex items-center justify-center w-8 h-8 rounded-lg bg-[#4f8cff]/10">
                      <Icon className="w-4 h-4 text-[#4f8cff]" />
                    </div>
                    <div>
                      <p className="text-sm font-medium">{doc.document_type}</p>
                      <p className="text-[10px] text-muted-foreground">
                        {timeAgo(doc.created_at)}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge
                      variant={doc.status === "generated" ? "success" : "secondary"}
                      className="text-[10px]"
                    >
                      {doc.status}
                    </Badge>
                    {doc.file_url && (
                      <Button variant="outline" size="icon" className="w-7 h-7" asChild>
                        <a href={doc.file_url} download>
                          <FileDown className="w-3.5 h-3.5" />
                        </a>
                      </Button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function PaymentsTab({ dealId }: { dealId: string }) {
  const { data: holds, isLoading } = useEscrowHolds(dealId);

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <CreditCard className="w-4 h-4 text-[#28c2a0]" />
            <CardTitle>Escrow Holds</CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <Skeleton className="h-32 w-full" />
          ) : !holds || holds.length === 0 ? (
            <p className="text-sm text-muted-foreground">No escrow holds</p>
          ) : (
            <div className="space-y-3">
              {holds.map((hold) => (
                <div
                  key={hold.hold_id}
                  className="p-4 rounded-lg bg-secondary border border-border space-y-3"
                >
                  <div className="flex items-center justify-between">
                    <p className="text-2xl font-bold text-[#2ec28a]">
                      {fmtUSD(hold.amount_usd)}
                    </p>
                    <Badge
                      variant={
                        hold.status === "FUNDED"
                          ? "success"
                          : hold.status === "RELEASED"
                          ? "teal"
                          : hold.status === "REFUNDED"
                          ? "warning"
                          : "secondary"
                      }
                    >
                      {hold.status}
                    </Badge>
                  </div>
                  <div className="grid grid-cols-2 gap-3 text-xs">
                    {hold.stripe_payment_intent_id && (
                      <div>
                        <p className="text-muted-foreground text-[10px]">Stripe PI</p>
                        <p className="font-mono">{hold.stripe_payment_intent_id}</p>
                      </div>
                    )}
                    {hold.funded_at && (
                      <div>
                        <p className="text-muted-foreground text-[10px]">Funded At</p>
                        <p>{fmtDateTime(hold.funded_at)}</p>
                      </div>
                    )}
                  </div>
                  {/* Payment timeline */}
                  <div className="flex items-center gap-2 text-[10px]">
                    {["PENDING", "FUNDED", "RELEASED"].map((s, i) => {
                      const reached =
                        (hold.status === "PENDING" && i === 0) ||
                        (hold.status === "FUNDED" && i <= 1) ||
                        (hold.status === "RELEASED" && i <= 2) ||
                        hold.status === "REFUNDED";
                      return (
                        <div key={s} className="flex items-center gap-2">
                          {i > 0 && (
                            <div
                              className={cn(
                                "h-0.5 w-8",
                                reached ? "bg-[#2ec28a]" : "bg-border"
                              )}
                            />
                          )}
                          <span
                            className={cn(
                              "font-medium",
                              reached ? "text-[#2ec28a]" : "text-muted-foreground"
                            )}
                          >
                            {s}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// ── Main DealDetail page ──────────────────────────────────────────────────────

export function DealDetail() {
  const { dealId } = useParams<{ dealId: string }>();
  const { data: deal, isLoading, isError, refetch } = useDeal(dealId);

  return (
    <div className="space-y-4">
      {/* Back + header */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="sm" className="gap-1.5 -ml-2" asChild>
            <Link to="/deals">
              <ArrowLeft className="w-4 h-4" /> Back
            </Link>
          </Button>
          {isLoading ? (
            <Skeleton className="h-6 w-48" />
          ) : deal ? (
            <div className="flex items-center gap-3 flex-wrap">
              <h1 className="text-lg font-bold">
                Deal{" "}
                <span className="font-mono text-muted-foreground text-sm">
                  #{deal.deal_id.slice(0, 8)}
                </span>
              </h1>
              <span className="text-muted-foreground text-sm">·</span>
              <span className="font-mono text-sm font-semibold text-[#4f8cff]">
                HS {deal.hs_code}
              </span>
              <StatusBadge status={deal.status} />
            </div>
          ) : null}
        </div>
      </div>

      {/* Error */}
      {isError && (
        <QueryState isError onRetry={refetch}>
          <></>
        </QueryState>
      )}

      {/* Pipeline */}
      {!isLoading && deal && (
        <div className="rounded-xl border border-border bg-popover px-4 py-4">
          <p className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground mb-3">
            Deal Pipeline
          </p>
          <DealPipeline status={deal.status} />
        </div>
      )}

      {/* Main tabs */}
      {!isLoading && deal && dealId && (
        <Tabs defaultValue="overview">
          <TabsList className="w-full justify-start overflow-x-auto flex-nowrap scrollbar-thin">
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="negotiation">Negotiation</TabsTrigger>
            <TabsTrigger value="logistics">Logistics</TabsTrigger>
            <TabsTrigger value="documents">Documents</TabsTrigger>
            <TabsTrigger value="payments">Payments</TabsTrigger>
          </TabsList>

          <TabsContent value="overview">
            <OverviewTab dealId={dealId} />
          </TabsContent>
          <TabsContent value="negotiation">
            <NegotiationTab dealId={dealId} />
          </TabsContent>
          <TabsContent value="logistics">
            <LogisticsTab dealId={dealId} />
          </TabsContent>
          <TabsContent value="documents">
            <DocumentsTab dealId={dealId} />
          </TabsContent>
          <TabsContent value="payments">
            <PaymentsTab dealId={dealId} />
          </TabsContent>
        </Tabs>
      )}

      {/* Loading skeleton */}
      {isLoading && (
        <div className="space-y-4">
          <Skeleton className="h-24 w-full rounded-xl" />
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            {[1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-20" />)}
          </div>
        </div>
      )}
    </div>
  );
}
