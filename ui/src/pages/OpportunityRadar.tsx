import { Fragment, useState } from "react";
import { toast } from "sonner";
import {
  RefreshCw,
  Zap,
  TrendingUp,
  Globe,
  BarChart2,
  ArrowUpRight,
  ChevronDown,
  ChevronRight,
  Factory,
  Users,
  Percent,
} from "lucide-react";
import { cn, fmtUSD } from "@/lib/utils";
import {
  useOpportunities,
  useScanOpportunities,
  useCreateDeal,
  useTradeSources,
} from "@/api/hooks";
import type { Opportunity, SellerCandidate, TradeSource } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Progress } from "@/components/ui/progress";

function pct(v: number | null | undefined): string {
  return v == null ? "—" : `${v.toFixed(1)}%`;
}

// Source chip colours by reachability mode (GET /trade-min/sources).
function modeVariant(mode: string): "success" | "info" | "secondary" | "warning" {
  switch (mode) {
    case "live": return "success";
    case "open": return "info";
    case "mock": return "warning";
    default: return "secondary";
  }
}

const ROLE_ORDER = ["demand", "buyers", "sellers", "tariff", "enrichment", "screening"] as const;

function SourceChips({ sources }: { sources: TradeSource[] }) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1.5">
      {ROLE_ORDER.map((role) => {
        const items = sources.filter((s) => s.role === role);
        if (items.length === 0) return null;
        return (
          <div key={role} className="flex items-center gap-1 flex-wrap">
            <span className="text-[10px] uppercase tracking-wide text-muted-foreground mr-0.5">
              {role}
            </span>
            {items.map((s) => (
              <Badge
                key={s.name}
                variant={modeVariant(s.mode)}
                className="text-[9px] h-4 px-1.5 font-medium cursor-default"
                title={`${s.detail}${s.actor ? `\nActor: ${s.actor}` : ""}\nMode: ${s.mode}`}
              >
                {s.label}
                {s.provider === "apify" && <span className="ml-1 opacity-70">⚡</span>}
              </Badge>
            ))}
          </div>
        );
      })}
    </div>
  );
}

function fitChip(score: number | null | undefined) {
  if (score == null) return null;
  const color = score >= 70 ? "#2ec28a" : score >= 45 ? "#f5a623" : "#9aa9c4";
  return (
    <span
      className="text-[9px] px-1.5 py-0.5 rounded-full border font-semibold shrink-0"
      style={{ color, borderColor: `${color}66`, background: `${color}1a` }}
      title="Actor fit score (0–100)"
    >
      fit {score}
    </span>
  );
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: "good" | "warn" | "bad" }) {
  return (
    <div className="rounded-lg bg-secondary/50 border border-border px-3 py-2">
      <p className="text-[10px] text-muted-foreground uppercase tracking-wide">{label}</p>
      <p
        className={cn(
          "text-sm font-semibold mt-0.5",
          tone === "good" && "text-[#2ec28a]",
          tone === "warn" && "text-[#f5a623]",
          tone === "bad" && "text-[#ff5d6c]"
        )}
      >
        {value}
      </p>
    </div>
  );
}

// Inline detail panel revealed when an opportunity row is expanded — surfaces
// the full seller/buyer lists and the enrichment metrics that don't fit in the
// summary row.
function OpportunityDetail({ opp }: { opp: Opportunity }) {
  const sellers = opp.seller_candidates ?? [];
  const buyers = opp.buyer_signals ?? [];
  const t = opp.tariff_data;

  return (
    <div className="p-4 space-y-4 bg-secondary/20">
      {/* Enrichment metrics */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
        <Metric label="Demand Index" value={opp.demand_index != null ? opp.demand_index.toFixed(0) : "—"} />
        <Metric label="Gross Gap" value={pct(opp.price_gap_pct)} />
        <Metric
          label="Net Gap"
          value={pct(opp.net_price_gap_pct)}
          tone={opp.net_price_gap_pct != null ? (opp.net_price_gap_pct >= 15 ? "good" : opp.net_price_gap_pct >= 8 ? "warn" : "bad") : undefined}
        />
        <Metric
          label="Confidence"
          value={opp.confidence_score != null ? `${opp.confidence_score.toFixed(0)}%` : "—"}
        />
        <Metric label="Urgency Tier" value={opp.urgency_tier != null ? `T${opp.urgency_tier}` : "—"} />
        <Metric label="Active RFQs" value={opp.active_rfq_count != null ? String(opp.active_rfq_count) : "—"} />
      </div>

      {/* Real buyer-side price anchor from the export-market actor */}
      {opp.price_benchmark_usd_per_mt != null && (
        <div className="rounded-lg border border-border bg-popover px-3 py-2 text-xs flex items-center gap-2 flex-wrap">
          <span className="font-semibold">Market pays {fmtUSD(opp.price_benchmark_usd_per_mt)}/MT</span>
          <span className="text-muted-foreground">
            · UN Comtrade unit value via {opp.price_benchmark_source ?? "export-market actor"} — anchors the
            negotiation corridor instead of the 1.25× FOB heuristic
          </span>
        </div>
      )}
      {opp.source === "simulated" && (
        <div className="rounded-lg border border-[#ff5d6c]/40 bg-[#ff5d6c]/10 px-3 py-2 text-xs text-[#ff5d6c]">
          Demand index and price gap are <b>simulated</b> (no live Comtrade / Census / actor data for this
          pair). Never auto-converted; start a deal only after checking the numbers yourself.
        </div>
      )}

      {/* Tariff */}
      {t && (
        <div className="rounded-lg border border-border bg-popover px-3 py-2.5">
          <div className="flex items-center gap-1.5 mb-1.5">
            <Percent className="w-3.5 h-3.5 text-[#f5a623]" />
            <span className="text-xs font-semibold">Tariff &amp; Landed Cost</span>
            {opp.tariff_viable === false && (
              <Badge variant="destructive" className="text-[9px] ml-1">Not viable</Badge>
            )}
          </div>
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
            <span>Applied duty: <span className="text-foreground font-medium">{pct(t.applied_duty_pct)}</span></span>
            <span>GST/VAT: <span className="text-foreground font-medium">{pct(t.gst_vat_pct)}</span></span>
            <span>Total landed add: <span className="text-foreground font-medium">{pct(t.total_landed_cost_add_pct)}</span></span>
            {t.rules_of_origin && (
              <span>Rules of origin: <span className="text-foreground font-medium">{t.rules_of_origin}</span></span>
            )}
          </div>
          {t.ntb_flags && t.ntb_flags.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-1.5">
              {t.ntb_flags.map((f, i) => (
                <span key={i} className="text-[9px] px-1.5 py-0.5 rounded bg-[#f5a623]/10 text-[#f5a623] border border-[#f5a623]/20">
                  {f}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Sellers */}
        <div>
          <div className="flex items-center gap-1.5 mb-2">
            <Factory className="w-3.5 h-3.5 text-[#28c2a0]" />
            <span className="text-xs font-semibold">Seller Candidates ({sellers.length})</span>
          </div>
          {sellers.length === 0 ? (
            <p className="text-xs text-muted-foreground">No sellers found</p>
          ) : (
            <div className="space-y-1.5">
              {sellers.map((s) => (
                <div
                  key={s.candidate_id}
                  className="flex items-start justify-between gap-3 rounded-lg bg-popover border border-border px-3 py-2"
                >
                  <div className="min-w-0">
                    <p className="text-xs font-medium truncate flex items-center gap-1.5">
                      {s.company_name}
                      {s.source && (
                        <Badge variant="outline" className="text-[9px] h-4 px-1.5 font-normal shrink-0">
                          {s.source.replace("apify_", "⚡ ")}
                        </Badge>
                      )}
                      {fitChip(s.fit_score)}
                    </p>
                    <p className="text-[10px] text-muted-foreground">
                      {[s.location_city, s.location_state].filter(Boolean).join(", ") || "—"}
                      {s.annual_capacity_mt != null && ` · ${s.annual_capacity_mt.toLocaleString()} MT/yr`}
                      {s.supplier_to_port_km != null && ` · ${s.supplier_to_port_km} km to port`}
                    </p>
                    {(s.email || s.website || s.gstin) && (
                      <p className="text-[10px] text-muted-foreground truncate">
                        {s.email && <span className="font-mono">{s.email}</span>}
                        {s.website && (
                          <>
                            {s.email && " · "}
                            <a href={s.website} target="_blank" rel="noopener noreferrer" className="text-[#4f8cff] hover:underline">
                              website
                            </a>
                          </>
                        )}
                        {s.gstin && ` · GSTIN ${s.gstin}`}
                      </p>
                    )}
                  </div>
                  <span className="text-xs font-semibold shrink-0 text-[#28c2a0]" title="Estimated FOB, USD per MT">
                    {s.estimated_fob_price_usd != null ? `$${s.estimated_fob_price_usd.toFixed(2)}` : "—"}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Buyers */}
        <div>
          <div className="flex items-center gap-1.5 mb-2">
            <Users className="w-3.5 h-3.5 text-[#4f8cff]" />
            <span className="text-xs font-semibold">Buyer Signals ({buyers.length})</span>
          </div>
          {buyers.length === 0 ? (
            <p className="text-xs text-muted-foreground">No buyer signals</p>
          ) : (
            <div className="space-y-1.5">
              {buyers.map((b, i) => {
                const link = b.website ?? b.contact_url ?? null;
                return (
                  <div
                    key={i}
                    className="flex items-start justify-between gap-3 rounded-lg bg-popover border border-border px-3 py-2"
                  >
                    <div className="min-w-0">
                      <p className="text-xs font-medium truncate flex items-center gap-1.5">
                        {b.buyer_name}
                        {b.source && (
                          <Badge variant="outline" className="text-[9px] h-4 px-1.5 font-normal shrink-0">
                            {b.source.replace("apify_", "⚡ ")}
                          </Badge>
                        )}
                        {fitChip(b.fit_score)}
                      </p>
                      <p className="text-[10px] text-muted-foreground">
                        {b.buyer_country}
                        {b.signal_type && ` · ${b.signal_type}`}
                        {b.quantity != null && ` · ${b.quantity.toLocaleString()} ${b.quantity_unit ?? ""}`.trimEnd()}
                        {b.shipments_12m != null && ` · ${b.shipments_12m} shipments/12m`}
                        {b.risk_score != null && ` · risk ${b.risk_score}`}
                        {link && (
                          <>
                            {" · "}
                            <a href={link} target="_blank" rel="noopener noreferrer" className="text-[#4f8cff] hover:underline">
                              {b.website ? "website" : "contact"}
                            </a>
                          </>
                        )}
                      </p>
                      {b.pitch_angle && (
                        <p className="text-[10px] text-muted-foreground/90 italic line-clamp-2" title={b.pitch_angle}>
                          “{b.pitch_angle}”
                        </p>
                      )}
                    </div>
                    <div className="text-right shrink-0">
                      {b.unit_price_usd != null && (
                        <p className="text-xs font-semibold text-[#4f8cff]" title={b.price_unit ?? "unit price"}>
                          ${b.unit_price_usd.toFixed(2)}
                          {b.price_unit === "USD/MT" && <span className="text-[9px] font-normal">/MT</span>}
                        </p>
                      )}
                      {b.estimated_order_value_usd != null && (
                        <p className="text-[10px] text-muted-foreground">
                          ${b.estimated_order_value_usd.toLocaleString()} order
                        </p>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function ArbitrageBar({ rank }: { rank: number }) {
  // Higher rank = better (max ~10). Guard against missing/non-numeric values.
  const safeRank = Number.isFinite(rank) ? rank : 0;
  const pct = Math.min(100, (safeRank / 10) * 100);
  return (
    <div className="flex items-center gap-2">
      <Progress
        value={pct}
        className="h-1.5 flex-1"
        indicatorClassName={
          safeRank >= 7
            ? "bg-[#2ec28a]"
            : safeRank >= 4
            ? "bg-[#f5a623]"
            : "bg-[#ff5d6c]"
        }
      />
      <span
        className={cn(
          "text-xs font-bold w-8 text-right",
          safeRank >= 7 ? "text-[#2ec28a]" : safeRank >= 4 ? "text-[#f5a623]" : "text-[#ff5d6c]"
        )}
      >
        {safeRank.toFixed(1)}
      </span>
    </div>
  );
}


export function OpportunityRadar() {
  const { data, isLoading, isError, refetch } = useOpportunities();
  const { data: sourcesInfo } = useTradeSources();
  const scan = useScanOpportunities();
  const createDeal = useCreateDeal();
  const [sortBy, setSortBy] = useState<"rank" | "market">("rank");
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const opportunities = [...(data ?? [])].sort((a, b) => {
    if (sortBy === "rank") return b.arbitrage_rank - a.arbitrage_rank;
    return a.market.localeCompare(b.market);
  });

  const topHs = opportunities[0]?.hs_code ?? "—";
  const avgRank =
    opportunities.length > 0
      ? (opportunities.reduce((s, o) => s + o.arbitrage_rank, 0) / opportunities.length).toFixed(1)
      : "—";

  async function handleScan() {
    try {
      await scan.mutateAsync();
      toast.success("Scan complete", { description: "Opportunity list updated" });
    } catch {
      toast.error("Scan failed", { description: "Check API connectivity" });
    }
  }

  async function handleStartDeal(opp: Opportunity) {
    if (
      opp.source === "simulated" &&
      !window.confirm(
        `Opportunity ${opp.opportunity_id} rests on SIMULATED demand numbers (no live Comtrade / Census / actor data for HS ${opp.hs_code} → ${opp.market}). The margin corridor will be a guess. Start the deal anyway?`
      )
    ) {
      return;
    }
    try {
      const deal = await createDeal.mutateAsync({
        operator_id: "OP-901",
        hs_code: opp.hs_code,
        market: opp.market,
        opportunity_id: opp.opportunity_id,
      });
      toast.success("Deal created", { description: `Deal ${deal.deal_id.slice(0, 8)} started` });
    } catch {
      toast.error("Failed to create deal");
    }
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-lg font-bold">Opportunity Radar</h1>
          <p className="text-xs text-muted-foreground mt-0.5">
            {sourcesInfo
              ? `${sourcesInfo.live_count} of ${sourcesInfo.total} data sources reachable · ` +
                (sourcesInfo.apify.token_present
                  ? `Apify actors connected as ${sourcesInfo.apify.owner}` +
                    (sourcesInfo.apify.ai_tier ? " (AI tier)" : " (rule tier)")
                  : "APIFY_TOKEN not set — buyer/seller identification actors are skipped")
              : "Live arbitrage signals from Comtrade, US Census, World Bank WITS and the PortLess Apify actors"}
            {sourcesInfo?.mock_mode && " · TRADE_MIN_MOCK_MODE"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            className="h-8 gap-1.5 text-xs"
            onClick={() => refetch()}
          >
            <RefreshCw className="w-3.5 h-3.5" /> Refresh
          </Button>
          <Button
            size="sm"
            className="h-8 gap-1.5 text-xs"
            onClick={handleScan}
            disabled={scan.isPending}
          >
            {scan.isPending ? (
              <>
                <RefreshCw className="w-3.5 h-3.5 animate-spin" /> Scanning…
              </>
            ) : (
              <>
                <Zap className="w-3.5 h-3.5" /> Scan New
              </>
            )}
          </Button>
        </div>
      </div>

      {/* Summary stats */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {[
          {
            label: "Total Opportunities",
            value: data?.length ?? 0,
            icon: Zap,
            color: "#4f8cff",
          },
          {
            label: "Top HS Code",
            value: topHs,
            icon: TrendingUp,
            color: "#28c2a0",
          },
          {
            label: "Avg Arbitrage Rank",
            value: avgRank,
            icon: BarChart2,
            color: "#f5a623",
          },
        ].map((s) => {
          const Icon = s.icon;
          return (
            <Card key={s.label}>
              <CardContent className="flex items-center gap-3 pt-4">
                <div
                  className="flex items-center justify-center w-10 h-10 rounded-xl shrink-0"
                  style={{ background: `${s.color}1a` }}
                >
                  <Icon className="w-5 h-5" style={{ color: s.color }} />
                </div>
                <div>
                  <p className="text-[10px] text-muted-foreground">{s.label}</p>
                  <p className="text-xl font-bold" style={{ color: s.color }}>
                    {s.value}
                  </p>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      {/* Data sources — live / open / stub / mock, incl. the PortLess Apify actors (⚡) */}
      {sourcesInfo && sourcesInfo.sources.length > 0 && (
        <div className="rounded-xl border border-border bg-popover px-4 py-3">
          <SourceChips sources={sourcesInfo.sources} />
        </div>
      )}

      {/* Sort */}
      <div className="flex items-center gap-2">
        <span className="text-xs text-muted-foreground">Sort:</span>
        {(["rank", "market"] as const).map((s) => (
          <button
            key={s}
            onClick={() => setSortBy(s)}
            className={cn(
              "text-xs px-3 py-1 rounded-md border transition-colors",
              sortBy === s
                ? "bg-primary/15 text-primary border-primary/30"
                : "border-border text-muted-foreground hover:bg-secondary"
            )}
          >
            {s === "rank" ? "Arbitrage Rank" : "Market"}
          </button>
        ))}
      </div>

      {/* Opportunities grid */}
      {isLoading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {[1, 2, 3, 4, 5, 6].map((i) => (
            <Skeleton key={i} className="h-44 rounded-xl" />
          ))}
        </div>
      ) : isError ? (
        <div className="flex flex-col items-center py-16 gap-3 text-muted-foreground">
          <Zap className="w-8 h-8 opacity-30" />
          <p className="text-sm">Could not load opportunities</p>
          <Button variant="outline" size="sm" onClick={() => refetch()}>
            Retry
          </Button>
        </div>
      ) : opportunities.length === 0 ? (
        <div className="flex flex-col items-center py-16 gap-3 text-muted-foreground border border-dashed border-border rounded-xl">
          <Zap className="w-10 h-10 opacity-20" />
          <p className="text-sm">No opportunities detected yet</p>
          <p className="text-xs text-center max-w-xs">
            Run a scan to query Comtrade, US Census, and World Bank WITS for arbitrage signals
          </p>
          <Button size="sm" onClick={handleScan} disabled={scan.isPending}>
            <Zap className="w-3.5 h-3.5 mr-1.5" /> Run First Scan
          </Button>
        </div>
      ) : (
        <>
          {/* Table view */}
          <div className="rounded-xl border border-border overflow-x-auto">
            <table className="w-full min-w-[880px]">
              <thead>
                <tr className="bg-popover border-b border-border">
                  <th className="w-8 px-2 py-3" />
                  <th className="text-left px-4 py-3 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                    HS Code
                  </th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                    Market
                  </th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                    Source
                  </th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-muted-foreground uppercase tracking-wide w-48">
                    Arbitrage Score
                  </th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                    Top Seller
                  </th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                    Buyer Signals
                  </th>
                  <th className="px-4 py-3" />
                </tr>
              </thead>
              <tbody>
                {opportunities.map((opp, idx) => {
                  const sellers = opp.seller_candidates ?? [];
                  const topSeller = sellers.reduce<SellerCandidate | null>((best, s) => {
                    if (s.estimated_fob_price_usd == null) return best;
                    if (best == null || (best.estimated_fob_price_usd ?? Infinity) > s.estimated_fob_price_usd) {
                      return s;
                    }
                    return best;
                  }, null);
                  const buyers = opp.buyer_signals ?? [];
                  const isConverted = opp.status === "CONVERTED";
                  const isExpanded = expandedId === opp.opportunity_id;

                  return (
                    <Fragment key={opp.opportunity_id}>
                    <tr
                      onClick={() =>
                        setExpandedId(isExpanded ? null : opp.opportunity_id)
                      }
                      className={cn(
                        "border-b border-border hover:bg-card transition-colors cursor-pointer",
                        isExpanded ? "bg-card" : idx % 2 === 0 ? "bg-transparent" : "bg-secondary/30"
                      )}
                    >
                      <td className="px-2 py-3 text-center align-middle">
                        {isExpanded ? (
                          <ChevronDown className="w-4 h-4 text-muted-foreground inline" />
                        ) : (
                          <ChevronRight className="w-4 h-4 text-muted-foreground inline" />
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <span className="font-mono font-bold text-sm">{opp.hs_code}</span>
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-1.5 text-sm">
                          <Globe className="w-3.5 h-3.5 text-muted-foreground" />
                          {opp.market}
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        {opp.source === "simulated" ? (
                          <Badge
                            variant="destructive"
                            className="text-[10px]"
                            title="No live demand data — numbers are a deterministic simulation"
                          >
                            simulated
                          </Badge>
                        ) : (
                          <Badge variant="outline" className="text-[10px]">
                            {(opp.source ?? "trade-min").replace("apify_", "⚡ ")}
                          </Badge>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <ArbitrageBar rank={opp.arbitrage_rank} />
                      </td>
                      <td className="px-4 py-3">
                        {topSeller ? (
                          <div className="text-xs">
                            <p className="font-medium truncate max-w-[160px]">{topSeller.company_name}</p>
                            <p className="text-muted-foreground">
                              {topSeller.estimated_fob_price_usd != null
                                ? `$${topSeller.estimated_fob_price_usd.toFixed(2)} FOB`
                                : "—"}
                              {sellers.length > 1 ? ` · +${sellers.length - 1} more` : ""}
                            </p>
                          </div>
                        ) : (
                          <span className="text-xs text-muted-foreground">No sellers found</span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        {buyers.length > 0 ? (
                          <div className="text-xs">
                            <p className="font-medium">{buyers[0].buyer_name}</p>
                            <p className="text-muted-foreground">
                              {buyers[0].buyer_country}
                              {buyers.length > 1 ? ` · +${buyers.length - 1} more` : ""}
                            </p>
                          </div>
                        ) : (
                          <span className="text-xs text-muted-foreground">No buyer signals</span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        {isConverted ? (
                          <Badge variant="outline" className="text-[10px] gap-1">
                            Deal Started
                          </Badge>
                        ) : (
                          <Button
                            size="sm"
                            className="h-7 text-xs gap-1"
                            onClick={(e) => {
                              e.stopPropagation();
                              handleStartDeal(opp);
                            }}
                            disabled={createDeal.isPending}
                          >
                            <ArrowUpRight className="w-3 h-3" /> Start Deal
                          </Button>
                        )}
                      </td>
                    </tr>
                    {isExpanded && (
                      <tr className="border-b border-border">
                        <td colSpan={8} className="p-0">
                          <OpportunityDetail opp={opp} />
                        </td>
                      </tr>
                    )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
