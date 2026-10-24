import { useState } from "react";
import { useParams, Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ArrowLeft,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  ExternalLink,
  Eye,
  FileDown,
  Globe,
  Lock,
  Mail,
  Pencil,
  Phone,
  Scale,
  Ship,
  ShieldCheck,
  Sparkles,
  TruckIcon,
  CreditCard,
  FileText,
  Repeat2,
  Send,
  Users,
  Wallet,
} from "lucide-react";
import { cn, fmtUSD, fmtPct, timeAgo, fmtDateTime, countdown } from "@/lib/utils";
import {
  useDeal,
  useNegotiationSessions,
  useNegotiationRounds,
  useNegotiationEmails,
  useRunNegotiationRound,
  useApprovals,
  useEscrowHold,
  useDealLedger,
  useEscrowAction,
  useUpdateParty,
  useEnrichDeal,
  useQcRecords,
  useFreightRecords,
  useDealmakerDocuments,
  useDocuments,
  useMatchScore,
  useOpportunity,
  type EscrowActionKind,
} from "@/api/hooks";
import { FilesApi } from "@/api/endpoints";
import { StatusBadge } from "@/components/StatusBadge";
import { QueryState } from "@/components/QueryState";
import { DealPipeline } from "@/components/DealPipeline";
import { ScoreRing } from "@/components/ScoreRing";
import { EmailThread } from "@/components/EmailThread";
import { HitlApprovalCard } from "@/components/HitlApprovalCard";
import { OutreachPanel } from "@/components/deal/OutreachPanel";
import { TimelineTab } from "@/components/deal/TimelineTab";
import { JournalCard } from "@/components/deal/JournalCard";
import { EvidencePackButtons } from "@/components/deal/EvidencePackButtons";
import { FreightMilestoneControls } from "@/components/deal/FreightMilestoneControls";
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type {
  QcRecord,
  FreightRecord,
  Party,
  EscrowRecord,
  DealLedger,
  UpdatePartyRequest,
} from "@/api/types";

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

/** The originating Trade-Min opportunity, re-fetched by id. Opportunities are
 *  marked CONVERTED (not deleted) once a deal starts, so the record — buyer
 *  signals with their initial prices, seller candidates with initial FOB
 *  quotes, price gap, route — stays viewable here after it leaves the
 *  Opportunity Radar. */
function OriginOpportunityCard({
  opportunityId,
  market,
}: {
  opportunityId: string;
  market?: string | null;
}) {
  const { data: opp, isLoading, isError } = useOpportunity(opportunityId);

  if (isLoading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Origin Opportunity</CardTitle>
        </CardHeader>
        <CardContent>
          <Skeleton className="h-24 w-full rounded-lg" />
        </CardContent>
      </Card>
    );
  }

  if (isError || !opp) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Origin Opportunity</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-muted-foreground">
            The original opportunity record{" "}
            <span className="font-mono">{opportunityId}</span> is no longer
            available (e.g. the scan store was reset). The prices captured at
            deal creation are preserved in <b>Sourcing Matches</b> below.
          </p>
        </CardContent>
      </Card>
    );
  }

  const metrics: { label: string; value: string }[] = [
    ...(opp.price_gap_pct != null
      ? [{ label: "Price Gap", value: fmtPct(opp.price_gap_pct) }]
      : []),
    ...(opp.net_price_gap_pct != null
      ? [{ label: "Net Gap (after tariff)", value: fmtPct(opp.net_price_gap_pct) }]
      : []),
    ...(opp.demand_index != null
      ? [{ label: "Demand Index", value: opp.demand_index.toFixed(0) }]
      : []),
    ...(opp.urgency_tier != null
      ? [{ label: "Urgency Tier", value: `T${opp.urgency_tier}` }]
      : []),
    ...(opp.confidence_score != null
      ? [{ label: "Confidence", value: opp.confidence_score.toFixed(0) }]
      : []),
  ];

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div className="flex items-center gap-2">
            <CardTitle>Origin Opportunity</CardTitle>
            {opp.status && (
              <Badge
                variant={opp.status === "CONVERTED" ? "success" : "secondary"}
                className="text-[9px] h-4 px-1.5"
              >
                {opp.status}
              </Badge>
            )}
          </div>
          <div className="flex items-center gap-3 text-[10px] text-muted-foreground">
            <span className="font-mono">{opp.opportunity_id}</span>
            {opp.source && <span>via {opp.source}</span>}
            {opp.scanned_at && <span>scanned {timeAgo(opp.scanned_at)}</span>}
          </div>
        </div>
        {/* Route */}
        <p className="text-xs text-muted-foreground mt-1">
          <span className="font-mono font-semibold text-foreground">{opp.hs_code}</span>
          {" · "}
          <span className="font-medium text-foreground">IN</span>
          <span className="mx-1">→</span>
          <span className="font-medium text-foreground">{opp.market ?? market ?? "—"}</span>
          {opp.currency && <span className="ml-2">({opp.currency})</span>}
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Signal metrics */}
        {metrics.length > 0 && (
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
            {metrics.map((m) => (
              <div key={m.label} className="rounded-lg bg-secondary border border-border p-2.5">
                <p className="text-[10px] text-muted-foreground">{m.label}</p>
                <p className="text-sm font-semibold">{m.value}</p>
              </div>
            ))}
          </div>
        )}

        {/* Buyer signals — the buyers' initial (pre-negotiation) prices */}
        {(opp.buyer_signals?.length ?? 0) > 0 && (
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground mb-1.5">
              Buyer Signals · initial buyer-side prices
            </p>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border text-muted-foreground">
                    <th className="text-left py-1.5 pr-3 font-semibold text-[10px] uppercase">Buyer</th>
                    <th className="text-left py-1.5 px-3 font-semibold text-[10px] uppercase">Country</th>
                    <th className="text-left py-1.5 px-3 font-semibold text-[10px] uppercase">Signal</th>
                    <th className="text-right py-1.5 px-3 font-semibold text-[10px] uppercase">Unit Price</th>
                    <th className="text-right py-1.5 px-3 font-semibold text-[10px] uppercase">Order Value</th>
                    <th className="text-right py-1.5 pl-3 font-semibold text-[10px] uppercase">Vol (MT/yr)</th>
                  </tr>
                </thead>
                <tbody>
                  {opp.buyer_signals!.map((b, i) => (
                    <tr key={i} className="border-b border-border/50">
                      <td className="py-1.5 pr-3 font-medium">{b.buyer_name}</td>
                      <td className="py-1.5 px-3 text-muted-foreground">{b.buyer_country}</td>
                      <td className="py-1.5 px-3 text-muted-foreground">{b.signal_type ?? "—"}</td>
                      <td className="py-1.5 px-3 text-right font-semibold">
                        {b.unit_price_usd != null ? fmtUSD(b.unit_price_usd) : "—"}
                      </td>
                      <td className="py-1.5 px-3 text-right">
                        {b.estimated_order_value_usd != null
                          ? fmtUSD(b.estimated_order_value_usd)
                          : "—"}
                      </td>
                      <td className="py-1.5 pl-3 text-right text-muted-foreground">
                        {b.annual_volume_mt ?? "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Seller candidates — the suppliers' initial FOB quotes */}
        {(opp.seller_candidates?.length ?? 0) > 0 && (
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground mb-1.5">
              Seller Candidates · initial supplier FOB quotes
            </p>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border text-muted-foreground">
                    <th className="text-left py-1.5 pr-3 font-semibold text-[10px] uppercase">Supplier</th>
                    <th className="text-left py-1.5 px-3 font-semibold text-[10px] uppercase">Location</th>
                    <th className="text-right py-1.5 px-3 font-semibold text-[10px] uppercase">Est. FOB</th>
                    <th className="text-right py-1.5 px-3 font-semibold text-[10px] uppercase">Capacity (MT/yr)</th>
                    <th className="text-right py-1.5 pl-3 font-semibold text-[10px] uppercase">Port Dist. (km)</th>
                  </tr>
                </thead>
                <tbody>
                  {opp.seller_candidates!.map((s, i) => (
                    <tr key={s.candidate_id ?? i} className="border-b border-border/50">
                      <td className="py-1.5 pr-3">
                        <span className="font-medium">{s.company_name}</span>
                        {s.listing_url && (
                          <a
                            href={s.listing_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="ml-1.5 inline-flex align-middle text-[#4f8cff]"
                          >
                            <ExternalLink className="w-3 h-3" />
                          </a>
                        )}
                      </td>
                      <td className="py-1.5 px-3 text-muted-foreground">
                        {[s.location_city, s.location_state].filter(Boolean).join(", ") || "—"}
                      </td>
                      <td className="py-1.5 px-3 text-right font-semibold">
                        {s.estimated_fob_price_usd != null
                          ? fmtUSD(s.estimated_fob_price_usd)
                          : "—"}
                      </td>
                      <td className="py-1.5 px-3 text-right text-muted-foreground">
                        {s.annual_capacity_mt ?? "—"}
                      </td>
                      <td className="py-1.5 pl-3 text-right text-muted-foreground">
                        {s.supplier_to_port_km ?? "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ── Party card: identity + contact + screening (Apify enrichment) ───────────

function sanctionsVariant(status: string | null | undefined) {
  switch (status) {
    case "clear": return "success" as const;
    case "review": return "warning" as const;
    case "block": return "destructive" as const;
    default: return "secondary" as const;
  }
}

function emailStatusVariant(status: string | null | undefined) {
  switch (status) {
    case "ok": return "success" as const;
    case "risky": return "warning" as const;
    case "invalid": return "destructive" as const;
    default: return "secondary" as const;
  }
}

function PartyEditForm({
  party,
  dealId,
  onDone,
}: {
  party: Party;
  dealId: string;
  onDone: () => void;
}) {
  const update = useUpdateParty(dealId);
  const [form, setForm] = useState<UpdatePartyRequest>({
    legal_name: party.legal_name,
    country: party.country,
    email: party.email ?? "",
    website: party.website ?? "",
    phone: party.phone ?? "",
    address: party.address ?? "",
    gstin: party.gstin ?? "",
    iec: party.iec ?? "",
    vat_number: party.vat_number ?? "",
  });

  const set = (k: keyof UpdatePartyRequest) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  async function save() {
    // Only send what changed / is non-empty so blanks don't wipe actor data.
    const payload: UpdatePartyRequest = {};
    (Object.keys(form) as (keyof UpdatePartyRequest)[]).forEach((k) => {
      const v = (form[k] ?? "").toString().trim();
      const prev = ((party as unknown as Record<string, unknown>)[k] ?? "") as string;
      if (v && v !== prev) payload[k] = v;
    });
    if (Object.keys(payload).length === 0) {
      onDone();
      return;
    }
    try {
      await update.mutateAsync({ role: party.role, payload });
      toast.success(`${party.role === "buyer" ? "Buyer" : "Supplier"} contact updated`);
      onDone();
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : "Could not update the party");
    }
  }

  const field = (label: string, key: keyof UpdatePartyRequest, placeholder = "") => (
    <div className="space-y-1">
      <Label className="text-[11px]">{label}</Label>
      <Input
        value={(form[key] ?? "") as string}
        onChange={set(key)}
        placeholder={placeholder}
        className="h-8 text-xs"
      />
    </div>
  );

  return (
    <div className="space-y-3 pt-2">
      <Separator />
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {field("Legal name", "legal_name")}
        {field("Country (ISO-2)", "country", "DE")}
        {field("E-mail", "email", "purchasing@company.com")}
        {field("Website", "website", "https://")}
        {field("Phone", "phone", "+49…")}
        {field("Address", "address")}
        {party.role === "supplier" ? (
          <>
            {field("GSTIN", "gstin")}
            {field("IEC", "iec")}
          </>
        ) : (
          field("VAT number", "vat_number", "DE123456789")
        )}
      </div>
      <p className="text-[10px] text-muted-foreground">
        Operator-entered contacts are marked <span className="font-mono">operator</span> and are
        never overwritten by a later actor run.
      </p>
      <div className="flex gap-2">
        <Button size="sm" className="h-8 text-xs" onClick={save} disabled={update.isPending}>
          {update.isPending ? "Saving…" : "Save"}
        </Button>
        <Button variant="ghost" size="sm" className="h-8 text-xs" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

function PartyCard({ party, dealId }: { party: Party; dealId: string }) {
  const [editing, setEditing] = useState(false);
  const sanctions = party.sanctions_status ?? "unchecked";

  return (
    <Card className={cn(sanctions === "block" && "border-[#ff5d6c]/50")}>
      <CardHeader>
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div className="flex items-center gap-2">
            <CardTitle className="capitalize">{party.role}</CardTitle>
            <Badge variant={party.role === "buyer" ? "info" : "teal"}>{party.role}</Badge>
          </div>
          <div className="flex items-center gap-2">
            <Badge
              variant={sanctionsVariant(sanctions)}
              className="text-[10px] gap-1"
              title={party.sanctions_reason ?? "No sanctions / registry screening has run yet"}
            >
              <Scale className="w-2.5 h-2.5" />
              screening: {sanctions}
            </Badge>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 text-xs gap-1"
              onClick={() => setEditing((e) => !e)}
            >
              <Pencil className="w-3 h-3" /> {editing ? "Close" : "Edit contact"}
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-sm">
          <div>
            <p className="text-[10px] text-muted-foreground">Legal Name</p>
            <p className="font-medium">{party.legal_name}</p>
          </div>
          <div>
            <p className="text-[10px] text-muted-foreground">Country</p>
            <p className="font-medium">{party.country}</p>
          </div>
          <div>
            <p className="text-[10px] text-muted-foreground">E-mail</p>
            {party.email ? (
              <p className="flex items-center gap-1.5 flex-wrap">
                <Mail className="w-3 h-3 text-muted-foreground" />
                <span className="font-mono text-xs break-all">{party.email}</span>
                <Badge variant={emailStatusVariant(party.email_status)} className="text-[9px] h-4 px-1.5">
                  {party.email_status ?? "unverified"}
                </Badge>
              </p>
            ) : (
              <p className="text-xs text-[#f5a623]">no address — outreach waits for a contact</p>
            )}
          </div>
          <div>
            <p className="text-[10px] text-muted-foreground">Website</p>
            {party.website ? (
              <a
                href={party.website}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-1 text-xs text-[#4f8cff] hover:underline break-all"
              >
                <Globe className="w-3 h-3" /> {party.website}
              </a>
            ) : (
              <p className="text-xs text-muted-foreground">—</p>
            )}
          </div>
          {party.phone && (
            <div>
              <p className="text-[10px] text-muted-foreground">Phone</p>
              <p className="flex items-center gap-1 text-xs font-mono">
                <Phone className="w-3 h-3 text-muted-foreground" /> {party.phone}
              </p>
            </div>
          )}
          {party.address && (
            <div>
              <p className="text-[10px] text-muted-foreground">Address</p>
              <p className="text-xs">{party.address}</p>
            </div>
          )}
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
          {party.vat_number && (
            <div>
              <p className="text-[10px] text-muted-foreground">VAT</p>
              <p className="font-mono text-xs flex items-center gap-1.5">
                {party.vat_number}
                {party.vat_valid != null && (
                  <Badge variant={party.vat_valid ? "success" : "destructive"} className="text-[9px] h-4 px-1.5">
                    VIES {party.vat_valid ? "valid" : "invalid"}
                  </Badge>
                )}
              </p>
            </div>
          )}
          {party.lei && (
            <div>
              <p className="text-[10px] text-muted-foreground">LEI</p>
              <p className="font-mono text-xs">{party.lei}</p>
            </div>
          )}
        </div>

        {/* Verification / provenance chips */}
        <div className="flex items-center gap-2 flex-wrap pt-1">
          {party.verification_status && (
            <span
              className={cn(
                "inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full border font-medium",
                party.verification_status === "verified" || party.verification_status === "registry_match"
                  ? "bg-[rgba(46,194,138,0.15)] text-[#2ec28a] border-[rgba(46,194,138,0.4)]"
                  : "bg-[rgba(245,166,35,0.15)] text-[#f5a623] border-[rgba(245,166,35,0.4)]"
              )}
            >
              {party.verification_status === "verified" || party.verification_status === "registry_match" ? (
                <CheckCircle2 className="w-2.5 h-2.5" />
              ) : (
                <XCircle className="w-2.5 h-2.5" />
              )}
              {party.verification_status.replace(/_/g, " ")}
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
          {party.contact_source && (
            <span
              className="text-[10px] px-2 py-0.5 rounded-full border border-border text-muted-foreground"
              title="Where the contact came from"
            >
              contact via {party.contact_source}
              {party.contact_confidence != null && ` · ${Math.round(party.contact_confidence * 100)}%`}
            </span>
          )}
          {party.sanctions_checked_at && (
            <span className="text-[10px] text-muted-foreground">
              screened {timeAgo(party.sanctions_checked_at)}
            </span>
          )}
        </div>
        {party.sanctions_reason && sanctions !== "clear" && (
          <p className="text-[11px] text-[#f5a623] flex items-start gap-1.5 pt-1">
            <AlertTriangle className="w-3 h-3 mt-0.5 shrink-0" />
            <span>{party.sanctions_reason}</span>
          </p>
        )}

        {editing && <PartyEditForm party={party} dealId={dealId} onDone={() => setEditing(false)} />}
      </CardContent>
    </Card>
  );
}

function EnrichButton({ dealId }: { dealId: string }) {
  const enrich = useEnrichDeal(dealId);

  async function run() {
    try {
      const res = await enrich.mutateAsync({ screen: true, contacts: true });
      const actors = res.actors_used.length
        ? res.actors_used.map((a) => a.split("~").pop()).join(", ")
        : "no actors (stub mode)";
      if (res.blocked) {
        toast.error("Counterparty screening returned BLOCK — a deal exception was raised", {
          description: actors,
          duration: 10000,
        });
      } else if (res.warnings.length) {
        toast.warning("Enrichment finished with warnings", {
          description: res.warnings.join(" · "),
          duration: 8000,
        });
      } else {
        toast.success("Counterparties enriched", { description: `Actors: ${actors}` });
      }
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : "Enrichment failed");
    }
  }

  return (
    <Button
      variant="outline"
      size="sm"
      className="h-8 text-xs gap-1.5"
      onClick={run}
      disabled={enrich.isPending}
      title="Company contact finder + OFAC/UN/EU/UK sanctions, LEI and VIES screening on the PortLess Apify actors"
    >
      <Sparkles className={cn("w-3.5 h-3.5", enrich.isPending && "animate-pulse")} />
      {enrich.isPending ? "Running actors…" : "Enrich via Apify actors"}
    </Button>
  );
}

function OverviewTab({ dealId }: { dealId: string }) {
  const { data: deal } = useDeal(dealId);
  const { data: matchScore, isLoading: scoreLoading } = useMatchScore(dealId);

  if (!deal) return null;

  const buyer = deal.parties.find((p) => p.role === "buyer");
  const supplier = deal.parties.find((p) => p.role === "supplier");
  const blocked = deal.parties.some((p) => p.sanctions_status === "block");

  return (
    <div className="space-y-4">
      {blocked && (
        <div className="flex items-start gap-2 rounded-lg border border-[#ff5d6c]/40 bg-[#ff5d6c]/10 px-3 py-2 text-xs text-[#ff5d6c]">
          <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
          <span>
            A counterparty on this deal is <b>sanctions-blocked</b>. No Proforma Invoice will be
            issued and the RFQ gate is forced to L0 — resolve the exception before continuing.
          </span>
        </div>
      )}

      {/* Metric cards */}
      <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-3">
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
          {
            label: "Quantity",
            value: deal.quantity_mt != null ? `${deal.quantity_mt} MT` : "—",
            color: "#28c2a0",
            hint: deal.quantity_mt != null ? "from the buyer lead / negotiation" : "pipeline default applies",
          },
          {
            label: "Buyer price",
            value: fmtUSD(deal.buyer_target_price_usd),
            color: "#4f8cff",
            hint: deal.buyer_price_source
              ? `per MT · ${deal.buyer_price_source.replace(/_/g, " ")}`
              : deal.buyer_target_price_usd != null
                ? "per MT"
                : "not known yet",
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
              {"hint" in m && m.hint && (
                <p className="text-[10px] text-muted-foreground mt-0.5">{m.hint}</p>
              )}
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Buyer outreach funnel — every identified buyer, contacted in order
          until one says yes; the negotiation waits for that reply. */}
      <OutreachPanel deal={deal} />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Parties — identity, contact and screening from the Apify actors */}
        <div className="lg:col-span-2 space-y-3">
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <p className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">
              Counterparties
            </p>
            <EnrichButton dealId={dealId} />
          </div>
          {[buyer, supplier].filter(Boolean).map((party) =>
            party ? <PartyCard key={party.role} party={party} dealId={dealId} /> : null
          )}
          {!buyer && !supplier && (
            <Card>
              <CardContent className="py-6 text-xs text-muted-foreground">
                No buyer or supplier recorded on this deal yet — run the enrichment or add them
                once sourcing identifies the parties.
              </CardContent>
            </Card>
          )}
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
                <div className="grid grid-cols-3 gap-3 w-full">
                  <ScoreRing score={matchScore.arbitrage_score} label="Arbitrage" size="sm" />
                  <ScoreRing score={matchScore.proximity_score} label="Proximity" size="sm" />
                  <ScoreRing score={matchScore.reliability_score} label="Reliability" size="sm" />
                </div>
              </div>
            ) : deal.winning_cms != null ? (
              // Score store has no live entry (e.g. in-memory store reset) —
              // fall back to the CMS snapshot stamped on the deal at creation.
              <div className="flex flex-col items-center gap-2">
                <ScoreRing score={deal.winning_cms} label="Composite" size="lg" />
                <p className="text-[10px] text-muted-foreground text-center">
                  Winning supplier's CMS captured at deal creation
                </p>
              </div>
            ) : (
              <p className="text-xs text-muted-foreground text-center py-8">
                Match scores not available
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Origin opportunity — visible here because the Opportunity Radar drops
          CONVERTED opportunities from its active list once the deal starts. */}
      {deal.opportunity_id && (
        <OriginOpportunityCard opportunityId={deal.opportunity_id} market={deal.market} />
      )}

      {/* Sourcing matches — the Match Engine scoring captured at deal creation.
          The originating opportunity leaves the Opportunity Radar once the deal
          is in progress, so this card is where the operator reviews that
          matching info afterwards. */}
      {(deal.match_results?.length ?? 0) > 0 && (
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between gap-2 flex-wrap">
              <CardTitle>Sourcing Matches</CardTitle>
              <div className="flex items-center gap-3 text-[10px] text-muted-foreground">
                {deal.opportunity_id && (
                  <span className="font-mono">
                    Opportunity: {deal.opportunity_id}
                  </span>
                )}
                {deal.buyer_target_price_usd != null && (
                  <span>
                    Buyer target:{" "}
                    <span className="text-foreground font-medium">
                      {fmtUSD(deal.buyer_target_price_usd)}
                    </span>
                  </span>
                )}
              </div>
            </div>
          </CardHeader>
          <CardContent className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  <th className="text-left py-2 pr-3 font-semibold uppercase tracking-wide text-[10px]">Supplier</th>
                  <th className="text-right py-2 px-3 font-semibold uppercase tracking-wide text-[10px]">CMS</th>
                  <th className="text-right py-2 px-3 font-semibold uppercase tracking-wide text-[10px]">Arbitrage</th>
                  <th className="text-right py-2 px-3 font-semibold uppercase tracking-wide text-[10px]">Proximity</th>
                  <th className="text-right py-2 px-3 font-semibold uppercase tracking-wide text-[10px]">Reliability</th>
                  <th className="text-right py-2 px-3 font-semibold uppercase tracking-wide text-[10px]">FOB (USD)</th>
                  <th className="text-left py-2 pl-3 font-semibold uppercase tracking-wide text-[10px]">Decision</th>
                </tr>
              </thead>
              <tbody>
                {[...(deal.match_results ?? [])]
                  .sort((a, b) => b.composite_score - a.composite_score)
                  .map((m) => {
                    const isWinner = m.supplier_id === deal.winning_supplier_id;
                    return (
                      <tr
                        key={m.match_id}
                        className={cn(
                          "border-b border-border/50",
                          isWinner && "bg-[rgba(46,194,138,0.08)]"
                        )}
                      >
                        <td className="py-2 pr-3">
                          <span className="font-medium">
                            {m.company_name || m.supplier_id}
                          </span>
                          {isWinner && (
                            <Badge variant="success" className="ml-2 text-[9px] h-4 px-1.5">
                              Winner
                            </Badge>
                          )}
                          {m.company_name && (
                            <p className="text-[10px] text-muted-foreground font-mono">
                              {m.supplier_id}
                            </p>
                          )}
                        </td>
                        <td className="py-2 px-3 text-right font-semibold">
                          {m.composite_score.toFixed(1)}
                        </td>
                        <td className="py-2 px-3 text-right text-muted-foreground">
                          {m.arbitrage_score.toFixed(1)}
                        </td>
                        <td className="py-2 px-3 text-right text-muted-foreground">
                          {m.proximity_score.toFixed(1)}
                        </td>
                        <td className="py-2 px-3 text-right text-muted-foreground">
                          {m.reliability_score.toFixed(1)}
                        </td>
                        <td className="py-2 px-3 text-right">
                          {m.fob_price_usd != null ? fmtUSD(m.fob_price_usd) : "—"}
                        </td>
                        <td className="py-2 pl-3">
                          <Badge
                            variant={m.decision === "SOURCE" ? "success" : "secondary"}
                            className="text-[9px] h-4 px-1.5"
                          >
                            {m.decision}
                          </Badge>
                        </td>
                      </tr>
                    );
                  })}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}
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
    case "BOOKED": return 20;
    case "IN_TRANSIT": return 45;
    case "ARRIVED": return 65;
    case "CLEARED": return 85;
    case "DELIVERED": return 100;
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
                          fr.status === "CLEARED" || fr.status === "DELIVERED"
                            ? "success"
                            : fr.status === "IN_TRANSIT" || fr.status === "ARRIVED"
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
                    {["Booked", "In Transit", "Arrived", "Cleared", "Delivered"].map((s) => (
                      <span key={s}>{s}</span>
                    ))}
                  </div>
                  <FreightMilestoneControls dealId={dealId} freight={fr} />
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

const DOC_TYPE_LABELS: Record<string, string> = {
  CI: "Commercial Invoice",
  PL: "Packing List",
  CoO: "Certificate of Origin",
  SDF: "SDF Declaration",
  SHIPPING_BILL: "Shipping Bill",
};

/** View / Download actions for a stored document.
 *
 *  Both buttons stream the file bytes through the backend (which holds the
 *  Google Drive credentials), so they work for any authenticated operator —
 *  the raw Drive webViewLink only opens for people with access to the
 *  platform's Google account, so it's offered as a secondary icon. Fetched as
 *  a Blob because a plain <a href> can't send the Authorization header. */
function DocumentActions({
  fetchBlob,
  filename,
  driveUrl,
}: {
  fetchBlob: () => Promise<Blob>;
  filename: string;
  driveUrl?: string | null;
}) {
  const [busy, setBusy] = useState<"view" | "download" | null>(null);

  async function run(mode: "view" | "download") {
    setBusy(mode);
    try {
      const blob = await fetchBlob();
      const url = URL.createObjectURL(blob);
      if (mode === "view") {
        window.open(url, "_blank", "noopener");
      } else {
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
      }
      // Give the new tab / download a minute before releasing the blob.
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch {
      toast.error("Could not fetch the document file");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="flex items-center gap-1.5">
      <Button
        variant="outline"
        size="sm"
        className="h-7 gap-1 text-xs"
        onClick={() => run("view")}
        disabled={busy !== null}
      >
        <Eye className="w-3 h-3" /> {busy === "view" ? "Opening…" : "View"}
      </Button>
      <Button
        variant="outline"
        size="sm"
        className="h-7 gap-1 text-xs"
        onClick={() => run("download")}
        disabled={busy !== null}
      >
        <FileDown className="w-3 h-3" /> {busy === "download" ? "Fetching…" : "Download"}
      </Button>
      {driveUrl?.startsWith("http") && (
        <Button
          variant="ghost"
          size="icon"
          className="w-7 h-7"
          asChild
          title="Open in Google Drive (needs access to the platform's Drive account)"
        >
          <a href={driveUrl} target="_blank" rel="noopener noreferrer">
            <ExternalLink className="w-3.5 h-3.5" />
          </a>
        </Button>
      )}
    </div>
  );
}

function DocumentsTab({ dealId }: { dealId: string }) {
  const { data: documents, isLoading } = useDocuments(dealId);
  const { data: contracts, isLoading: contractsLoading } = useDealmakerDocuments(dealId);

  const docIcons: Record<string, React.ElementType> = {
    CI: FileText,
    PL: FileText,
    CoO: FileText,
    SDF: FileText,
    SHIPPING_BILL: Ship,
    "Shipping Bill": Ship,
  };

  const contractRows = [
    contracts?.pi && {
      id: contracts.pi.pi_id,
      kind: "pi" as const,
      label: "Proforma Invoice (PI)",
      status: contracts.pi.status,
      uri: contracts.pi.storage_uri,
      value: contracts.pi.total_value,
      issued_at: contracts.pi.issued_at,
    },
    contracts?.po && {
      id: contracts.po.po_id,
      kind: "po" as const,
      label: "Purchase Order (PO)",
      status: contracts.po.status,
      uri: contracts.po.storage_uri,
      value: contracts.po.total_value,
      issued_at: contracts.po.issued_at,
    },
  ].filter(Boolean) as {
    id: string;
    kind: "pi" | "po";
    label: string;
    status: string;
    uri?: string | null;
    value?: number | null;
    issued_at?: string | null;
  }[];

  return (
    <div className="space-y-4">
      {/* DealMaker contracts — PI/PO stored in Google Drive */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <FileText className="w-4 h-4 text-[#28c2a0]" />
            <CardTitle>Contracts (PI / PO)</CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          {contractsLoading ? (
            <Skeleton className="h-14 w-full" />
          ) : contractRows.length === 0 ? (
            <p className="text-sm text-muted-foreground">No PI or PO issued yet</p>
          ) : (
            <div className="space-y-2">
              {contractRows.map((c) => (
                <div
                  key={c.id}
                  className="flex items-center justify-between gap-3 p-3 rounded-lg bg-secondary border border-border"
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <div className="flex items-center justify-center w-8 h-8 rounded-lg bg-[#28c2a0]/10 shrink-0">
                      <FileText className="w-4 h-4 text-[#28c2a0]" />
                    </div>
                    <div className="min-w-0">
                      <p className="text-sm font-medium">{c.label}</p>
                      <p className="text-[10px] text-muted-foreground font-mono truncate">
                        {c.id}
                        {c.issued_at && ` · issued ${timeAgo(c.issued_at)}`}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {c.value != null && (
                      <span className="text-sm font-semibold">{fmtUSD(c.value)}</span>
                    )}
                    <Badge variant="secondary" className="text-[10px]">
                      {c.status}
                    </Badge>
                    <DocumentActions
                      fetchBlob={() =>
                        c.kind === "pi" ? FilesApi.piFile(c.id) : FilesApi.poFile(c.id)
                      }
                      filename={`${c.id}.html`}
                      driveUrl={c.uri}
                    />
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* DocuGen trade documents — stored in Google Drive */}
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
                  className="flex items-center justify-between gap-3 p-3 rounded-lg bg-secondary border border-border"
                >
                  <div className="flex items-center gap-3">
                    <div className="flex items-center justify-center w-8 h-8 rounded-lg bg-[#4f8cff]/10">
                      <Icon className="w-4 h-4 text-[#4f8cff]" />
                    </div>
                    <div>
                      <p className="text-sm font-medium">
                        {DOC_TYPE_LABELS[doc.document_type] ?? doc.document_type}
                      </p>
                      <p className="text-[10px] text-muted-foreground">
                        {doc.document_type} · {timeAgo(doc.created_at)}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <Badge
                      variant={
                        doc.status === "RENDERED" || doc.status === "generated"
                          ? "success"
                          : "secondary"
                      }
                      className="text-[10px]"
                    >
                      {doc.status}
                    </Badge>
                    <DocumentActions
                      fetchBlob={() => FilesApi.documentFile(doc.document_id)}
                      filename={`${doc.deal_id}-${doc.document_type}.html`}
                      driveUrl={doc.file_url}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        )}
        </CardContent>
      </Card>
    </div>
  );
}

// ── Payments: neutral staged-release escrow + per-trade money ledger ────────

function escrowStatusVariant(status: string) {
  switch (status) {
    case "FUNDED": return "success" as const;
    case "RELEASED": return "teal" as const;
    case "PARTIALLY_FUNDED": return "warning" as const;
    case "REFUNDED": return "warning" as const;
    case "FAILED": return "destructive" as const;
    default: return "secondary" as const;
  }
}

function providerLabel(provider: string | null | undefined): string {
  switch (provider) {
    case "tazapay": return "Tazapay";
    case "escrow_com": return "Escrow.com";
    case "stripe":
    case "stripe_treasury": return "Stripe (legacy hold)";
    default: return provider ?? "—";
  }
}

const TRIGGER_LABELS: Record<string, string> = {
  PO_ISSUED: "PO issued",
  QC_PASSED: "QC passed",
  CLEARED: "Customs cleared",
  ACCEPTED: "Buyer acceptance",
};

interface EscrowDialogSpec {
  kind: EscrowActionKind;
  title: string;
  description: string;
  confirmLabel: string;
  needsReason: boolean;
  variant: "success" | "destructive" | "default";
}

function EscrowActionDialog({
  spec,
  escrow,
  dealId,
  onClose,
}: {
  spec: EscrowDialogSpec | null;
  escrow: EscrowRecord;
  dealId: string;
  onClose: () => void;
}) {
  const action = useEscrowAction(dealId);
  const [reason, setReason] = useState("");

  async function confirm() {
    if (!spec) return;
    if (spec.needsReason && !reason.trim()) {
      toast.error("A reason is required");
      return;
    }
    try {
      const res = await action.mutateAsync({
        kind: spec.kind,
        escrowId: escrow.escrow_id,
        reason: reason.trim() || undefined,
        actor: "operator",
      });
      toast.success(`${spec.title} — escrow now ${res.status}`, {
        description: `Released ${fmtUSD(res.released_total)} · refunded ${fmtUSD(res.refunded_total)}`,
      });
      setReason("");
      onClose();
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      toast.error(detail ?? (e instanceof Error ? e.message : "Escrow action failed"));
    }
  }

  return (
    <Dialog open={spec !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        {spec && (
          <>
            <DialogHeader>
              <DialogTitle>{spec.title}</DialogTitle>
              <DialogDescription>{spec.description}</DialogDescription>
            </DialogHeader>
            <div className="space-y-2">
              <Label className="text-[11px]">
                {spec.needsReason ? "Reason *" : "Note (optional)"}
              </Label>
              <Textarea
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                className="min-h-[70px] text-xs"
                placeholder={spec.needsReason ? "e.g. goods damaged in transit" : "e.g. buyer confirmed by e-mail on …"}
              />
              <p className="text-[10px] text-muted-foreground">
                This moves money on escrow {escrow.escrow_id} ({providerLabel(escrow.provider)}) and is
                recorded on the ledger.
              </p>
            </div>
            <DialogFooter>
              <Button variant="ghost" size="sm" onClick={onClose} disabled={action.isPending}>
                Cancel
              </Button>
              <Button
                variant={spec.variant}
                size="sm"
                onClick={confirm}
                disabled={action.isPending}
              >
                {action.isPending ? "Working…" : spec.confirmLabel}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function EscrowCard({ escrow, dealId }: { escrow: EscrowRecord; dealId: string }) {
  const [dialog, setDialog] = useState<EscrowDialogSpec | null>(null);

  const status = escrow.status;
  const funded = status === "FUNDED" || status === "RELEASED";
  const pendingFinal = escrow.tranches.some((t) => t.trigger === "ACCEPTED" && t.status === "PENDING");
  const received = escrow.funded_amount ?? escrow.amount;
  const undisbursed = Math.max(0, Number((received - escrow.released_total).toFixed(2)));

  const canAccept = status === "FUNDED" && pendingFinal && !escrow.disputed;
  const canDispute = funded && !escrow.disputed;
  const canOpenWindow = funded && pendingFinal && !escrow.inspection_deadline && !escrow.disputed;
  const canClearDispute = escrow.disputed;
  const canRefund =
    (status === "FUNDED" || status === "PARTIALLY_FUNDED" || status === "RELEASED") &&
    undisbursed > 0.01;

  const windowOpen =
    escrow.inspection_deadline && !escrow.buyer_accepted_at && pendingFinal;

  return (
    <Card className={cn(escrow.disputed && "border-[#ff5d6c]/40")}>
      <CardHeader>
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div className="flex items-center gap-2">
            <Lock className="w-4 h-4 text-[#28c2a0]" />
            <CardTitle>Escrow</CardTitle>
            <span className="text-[10px] font-mono text-muted-foreground">{escrow.escrow_id}</span>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            {escrow.disputed && (
              <Badge variant="destructive" className="text-[10px]">disputed</Badge>
            )}
            {escrow.settled_at && (
              <Badge variant="teal" className="text-[10px]">settled {timeAgo(escrow.settled_at)}</Badge>
            )}
            <Badge variant={escrowStatusVariant(status)}>
              {status.replace(/_/g, " ")}
              {status === "PARTIALLY_FUNDED" && escrow.shortfall != null && (
                <span className="ml-1 font-normal">· short {fmtUSD(escrow.shortfall)}</span>
              )}
            </Badge>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <div className="rounded-lg bg-secondary border border-border p-3">
            <p className="text-[10px] text-muted-foreground">Principal (PI total)</p>
            <p className="text-xl font-bold text-[#2ec28a]">{fmtUSD(escrow.amount)}</p>
            <p className="text-[10px] text-muted-foreground">{escrow.currency}</p>
          </div>
          <div className="rounded-lg bg-secondary border border-border p-3">
            <p className="text-[10px] text-muted-foreground">Funded by buyer</p>
            <p
              className={cn(
                "text-xl font-bold",
                escrow.funded_amount == null
                  ? "text-muted-foreground"
                  : escrow.funded_amount + 0.01 < escrow.amount
                  ? "text-[#f5a623]"
                  : "text-[#2ec28a]"
              )}
            >
              {escrow.funded_amount != null ? fmtUSD(escrow.funded_amount) : "—"}
            </p>
            <p className="text-[10px] text-muted-foreground">
              {escrow.funded_at ? `at ${fmtDateTime(escrow.funded_at)}` : "awaiting provider webhook"}
            </p>
          </div>
          <div className="rounded-lg bg-secondary border border-border p-3">
            <p className="text-[10px] text-muted-foreground">Provider</p>
            <p className="text-sm font-semibold mt-1">{providerLabel(escrow.provider)}</p>
            <p className="text-[10px] text-muted-foreground">
              fee {escrow.fee_pct != null ? `${escrow.fee_pct}%` : "n/a"}
              {escrow.payment_method && ` · ${escrow.payment_method}`}
            </p>
          </div>
          <div className="rounded-lg bg-secondary border border-border p-3">
            <p className="text-[10px] text-muted-foreground">Acceptance window</p>
            {windowOpen ? (
              <>
                <p className="text-sm font-semibold mt-1 text-[#f5a623]">
                  {countdown(escrow.inspection_deadline)}
                </p>
                <p className="text-[10px] text-muted-foreground">
                  auto-release {fmtDateTime(escrow.inspection_deadline)}
                  {escrow.inspection_trigger && ` · opened by ${escrow.inspection_trigger}`}
                </p>
              </>
            ) : escrow.buyer_accepted_at ? (
              <>
                <p className="text-sm font-semibold mt-1 text-[#2ec28a]">accepted</p>
                <p className="text-[10px] text-muted-foreground">{timeAgo(escrow.buyer_accepted_at)}</p>
              </>
            ) : escrow.delivery_confirmed_at ? (
              <>
                <p className="text-sm font-semibold mt-1 text-[#4f8cff]">delivered</p>
                <p className="text-[10px] text-muted-foreground">
                  {timeAgo(escrow.delivery_confirmed_at)} · opens once proof tranches release
                </p>
              </>
            ) : (
              <>
                <p className="text-sm font-semibold mt-1 text-muted-foreground">not open</p>
                <p className="text-[10px] text-muted-foreground">waits for the DELIVERED milestone</p>
              </>
            )}
          </div>
        </div>

        <div className="flex items-center gap-3 flex-wrap text-xs">
          {escrow.checkout_url && (
            <a
              href={escrow.checkout_url}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1 text-[#4f8cff] hover:underline"
            >
              Buyer payment link <ExternalLink className="w-3 h-3" />
            </a>
          )}
          {escrow.ext_ref && (
            <span className="text-muted-foreground">
              provider ref <span className="font-mono">{escrow.ext_ref}</span>
            </span>
          )}
          <span className="text-muted-foreground">opened {timeAgo(escrow.created_at)}</span>
        </div>

        {/* Tranche schedule */}
        {escrow.tranches.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  <th className="text-left py-2 pr-3 font-semibold text-[10px] uppercase">Tranche</th>
                  <th className="text-left py-2 px-3 font-semibold text-[10px] uppercase">Trigger</th>
                  <th className="text-right py-2 px-3 font-semibold text-[10px] uppercase">Share</th>
                  <th className="text-right py-2 px-3 font-semibold text-[10px] uppercase">Amount</th>
                  <th className="text-left py-2 px-3 font-semibold text-[10px] uppercase">Status</th>
                  <th className="text-left py-2 px-3 font-semibold text-[10px] uppercase">Proof</th>
                  <th className="text-right py-2 pl-3 font-semibold text-[10px] uppercase">Released</th>
                </tr>
              </thead>
              <tbody>
                {escrow.tranches.map((t) => (
                  <tr key={t.tranche_id} className="border-b border-border/50">
                    <td className="py-2 pr-3 font-mono font-semibold">{t.tranche_id}</td>
                    <td className="py-2 px-3">{TRIGGER_LABELS[t.trigger] ?? t.trigger}</td>
                    <td className="py-2 px-3 text-right">{Math.round(t.pct * 100)}%</td>
                    <td className="py-2 px-3 text-right font-medium">
                      {fmtUSD(t.amount ?? escrow.amount * t.pct)}
                    </td>
                    <td className="py-2 px-3">
                      <Badge
                        variant={t.status === "RELEASED" ? "success" : "secondary"}
                        className="text-[9px] h-4 px-1.5"
                      >
                        {t.status}
                      </Badge>
                    </td>
                    <td className="py-2 px-3 font-mono text-[10px] text-muted-foreground max-w-[220px] truncate">
                      {t.proof_refs.length ? t.proof_refs.join(", ") : "—"}
                    </td>
                    <td className="py-2 pl-3 text-right text-muted-foreground">
                      {t.released_at ? timeAgo(t.released_at) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">
            Legacy single hold — no staged tranches on this escrow.
          </p>
        )}

        {/* Operator actions — every one moves money, all confirm first */}
        <div className="flex items-center gap-2 flex-wrap pt-1">
          <Button
            variant="success"
            size="sm"
            className="h-8 text-xs gap-1.5"
            disabled={!canAccept}
            title={
              canAccept
                ? "Relay the buyer's acceptance of the goods — releases the final tranche"
                : "Enabled once the escrow is funded, the final tranche is pending and no dispute is open"
            }
            onClick={() =>
              setDialog({
                kind: "accept",
                title: "Record buyer acceptance",
                description:
                  "Releases the final ACCEPTED-trigger tranche to PortLess on the buyer's behalf. Use only when the buyer confirmed acceptance of the goods.",
                confirmLabel: "Release final tranche",
                needsReason: false,
                variant: "success",
              })
            }
          >
            <CheckCircle2 className="w-3.5 h-3.5" /> Record buyer acceptance
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="h-8 text-xs gap-1.5"
            disabled={!canDispute}
            onClick={() =>
              setDialog({
                kind: "dispute",
                title: "Raise a dispute",
                description:
                  "Blocks every further release (including the inspection-window auto-release) until the dispute is resolved by refund or acceptance.",
                confirmLabel: "Raise dispute",
                needsReason: true,
                variant: "destructive",
              })
            }
          >
            <AlertTriangle className="w-3.5 h-3.5" /> Raise dispute
          </Button>
          <Button
            variant="destructive"
            size="sm"
            className="h-8 text-xs gap-1.5"
            disabled={!canRefund}
            title={canRefund ? `Refund ${fmtUSD(undisbursed)} undisbursed` : "Nothing left to refund"}
            onClick={() =>
              setDialog({
                kind: "refund",
                title: `Refund undisbursed balance (${fmtUSD(undisbursed)})`,
                description:
                  "Returns everything not yet released to the buyer. Already-released tranches cannot be clawed back. A deal in DISPUTE moves to REFUNDED.",
                confirmLabel: `Refund ${fmtUSD(undisbursed)}`,
                needsReason: true,
                variant: "destructive",
              })
            }
          >
            <XCircle className="w-3.5 h-3.5" /> Refund undisbursed
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="h-8 text-xs gap-1.5"
            disabled={!canOpenWindow}
            title={
              canOpenWindow
                ? "Goods reached the buyer — start the acceptance window for the final tranche"
                : escrow.inspection_deadline
                  ? "The window is already open"
                  : "Enabled once the escrow is funded and the final tranche is pending"
            }
            onClick={() =>
              setDialog({
                kind: "open_window",
                title: "Confirm delivery — open the acceptance window",
                description:
                  "Starts the buyer's acceptance window for the final ACCEPTED-trigger tranche (auto-release on lapse, silence = consent). If a proof tranche is still pending the delivery is recorded and the window opens when it releases. Normally the carrier's DELIVERED milestone does this.",
                confirmLabel: "Confirm delivery",
                needsReason: false,
                variant: "default",
              })
            }
          >
            <CheckCircle2 className="w-3.5 h-3.5" /> Confirm delivery
          </Button>
          {canClearDispute && (
            <Button
              variant="outline"
              size="sm"
              className="h-8 text-xs gap-1.5"
              onClick={() =>
                setDialog({
                  kind: "clear_dispute",
                  title: "Clear the dispute",
                  description:
                    "Lifts the buyer's dispute (re-inspection agreed / resolved in PortLess's favour). Releases and the acceptance window resume.",
                  confirmLabel: "Clear dispute",
                  needsReason: true,
                  variant: "default",
                })
              }
            >
              <ShieldCheck className="w-3.5 h-3.5" /> Clear dispute
            </Button>
          )}
        </div>
      </CardContent>
      <EscrowActionDialog spec={dialog} escrow={escrow} dealId={dealId} onClose={() => setDialog(null)} />
    </Card>
  );
}

function LedgerRow({ label, value, tone }: { label: string; value: string; tone?: "good" | "warn" | "bad" | "muted" }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1.5 border-b border-border/50 last:border-0 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <span
        className={cn(
          "font-semibold",
          tone === "good" && "text-[#2ec28a]",
          tone === "warn" && "text-[#f5a623]",
          tone === "bad" && "text-[#ff5d6c]",
          tone === "muted" && "text-muted-foreground"
        )}
      >
        {value}
      </span>
    </div>
  );
}

function LedgerCard({ ledger }: { ledger: DealLedger }) {
  const hasEscrow = Boolean(ledger.escrow_id);
  return (
    <Card className={cn(!ledger.reconciled && "border-[#ff5d6c]/50")}>
      <CardHeader>
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div className="flex items-center gap-2">
            <Scale className="w-4 h-4 text-[#4f8cff]" />
            <CardTitle>Money Ledger</CardTitle>
          </div>
          <Badge variant={ledger.reconciled ? "success" : "destructive"} className="text-[10px]">
            {ledger.reconciled ? "reconciled" : `${ledger.issues.length} issue(s)`}
          </Badge>
        </div>
      </CardHeader>
      <CardContent>
        {!hasEscrow ? (
          <p className="text-xs text-muted-foreground">No escrow opened for this deal yet.</p>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8">
            <div>
              <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground mb-1">
                Buyer side (escrow)
              </p>
              <LedgerRow label="Principal held" value={fmtUSD(ledger.principal)} />
              <LedgerRow
                label="Funded"
                value={ledger.funded_amount != null ? fmtUSD(ledger.funded_amount) : "—"}
                tone={ledger.shortfall ? "warn" : undefined}
              />
              {ledger.shortfall ? (
                <LedgerRow label="Shortfall" value={fmtUSD(ledger.shortfall)} tone="warn" />
              ) : null}
              <LedgerRow label="Released to PortLess" value={fmtUSD(ledger.released_total)} tone="good" />
              <LedgerRow label="Provider fees (est.)" value={fmtUSD(ledger.fees_total)} tone="muted" />
              <LedgerRow label="Net received" value={fmtUSD(ledger.net_to_portless)} tone="good" />
              <LedgerRow label="Refunded to buyer" value={fmtUSD(ledger.refunded_total)} tone={ledger.refunded_total ? "warn" : "muted"} />
              <LedgerRow label="Still in escrow" value={fmtUSD(ledger.outstanding)} />
            </div>
            <div>
              <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground mb-1">
                Supplier side (payouts)
              </p>
              <LedgerRow
                label="Supplier owed (PO value)"
                value={ledger.supplier_owed != null ? fmtUSD(ledger.supplier_owed) : "—"}
              />
              <LedgerRow label="Paid out" value={fmtUSD(ledger.payouts_total)} tone="good" />
              <LedgerRow label="Pending payouts" value={fmtUSD(ledger.payouts_pending)} tone={ledger.payouts_pending ? "warn" : "muted"} />
              <LedgerRow
                label="Balance due to supplier"
                value={ledger.supplier_balance_due != null ? fmtUSD(ledger.supplier_balance_due) : "—"}
                tone={ledger.supplier_balance_due ? "warn" : undefined}
              />
              <LedgerRow label="Available for payout now" value={fmtUSD(ledger.available_for_payout)} tone="good" />
              <LedgerRow
                label="Realized margin so far"
                value={ledger.realized_margin != null ? fmtUSD(ledger.realized_margin) : "—"}
                tone={ledger.realized_margin != null ? (ledger.realized_margin >= 0 ? "good" : "bad") : undefined}
              />
            </div>
          </div>
        )}
        {ledger.issues.length > 0 && (
          <ul className="mt-3 space-y-1">
            {ledger.issues.map((issue, i) => (
              <li key={i} className="flex items-start gap-1.5 text-xs text-[#ff5d6c]">
                <AlertTriangle className="w-3 h-3 mt-0.5 shrink-0" />
                {issue}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function PayoutsCard({ ledger }: { ledger: DealLedger }) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Wallet className="w-4 h-4 text-[#f5a623]" />
          <CardTitle>Supplier Payouts</CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        {ledger.payouts.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            No payouts yet — the RELEASE_SUPPLIER_PAYOUT gate opens once the cargo clears and
            escrow funds have been released.
          </p>
        ) : (
          <div className="space-y-2">
            {ledger.payouts.map((p) => (
              <div
                key={p.payout_id}
                className="flex items-center justify-between gap-3 p-3 rounded-lg bg-secondary border border-border"
              >
                <div className="min-w-0">
                  <p className="text-sm font-medium">
                    {fmtUSD(p.amount)}{" "}
                    <span className="text-[10px] text-muted-foreground font-normal">
                      {p.kind === "supplier_balance" ? "balance" : "main payout"} · {p.payee_id}
                    </span>
                  </p>
                  <p className="text-[10px] text-muted-foreground font-mono truncate">
                    {p.payout_id}
                    {p.ext_ref && ` · ${p.ext_ref}`}
                    {p.total_owed != null && ` · of ${fmtUSD(p.total_owed)} owed`}
                  </p>
                </div>
                <div className="text-right shrink-0">
                  <Badge
                    variant={
                      p.status === "COMPLETED"
                        ? "success"
                        : p.status === "FAILED"
                        ? "destructive"
                        : "warning"
                    }
                    className="text-[10px]"
                  >
                    {p.status}
                  </Badge>
                  <p className="text-[10px] text-muted-foreground mt-1">
                    {p.completed_at ? `completed ${timeAgo(p.completed_at)}` : `requested ${timeAgo(p.created_at)}`}
                  </p>
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function PaymentsTab({ dealId }: { dealId: string }) {
  const { data: escrow, isLoading } = useEscrowHold(dealId);
  const { data: ledger, isLoading: ledgerLoading } = useDealLedger(dealId);

  return (
    <div className="space-y-4">
      {isLoading ? (
        <Skeleton className="h-48 w-full rounded-xl" />
      ) : !escrow ? (
        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <CreditCard className="w-4 h-4 text-[#28c2a0]" />
              <CardTitle>Escrow</CardTitle>
            </div>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              No escrow yet — one opens automatically when the buyer accepts the Proforma Invoice.
            </p>
          </CardContent>
        </Card>
      ) : (
        <EscrowCard escrow={escrow} dealId={dealId} />
      )}

      {ledgerLoading ? (
        <Skeleton className="h-40 w-full rounded-xl" />
      ) : ledger ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <LedgerCard ledger={ledger} />
          <PayoutsCard ledger={ledger} />
        </div>
      ) : null}

      {/* Double-entry journal (null until an escrow exists) */}
      <JournalCard dealId={dealId} />
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
        {deal && dealId && <EvidencePackButtons dealId={dealId} />}
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
            <TabsTrigger value="timeline">Timeline</TabsTrigger>
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
          <TabsContent value="timeline">
            <TimelineTab dealId={dealId} />
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
