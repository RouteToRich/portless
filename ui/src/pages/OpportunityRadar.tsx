import { useState } from "react";
import { toast } from "sonner";
import { RefreshCw, Zap, TrendingUp, Globe, BarChart2, ArrowUpRight } from "lucide-react";
import { cn } from "@/lib/utils";
import { useOpportunities, useScanOpportunities, useCreateDeal } from "@/api/hooks";
import type { Opportunity } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Progress } from "@/components/ui/progress";

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
  const scan = useScanOpportunities();
  const createDeal = useCreateDeal();
  const [sortBy, setSortBy] = useState<"rank" | "market">("rank");

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
    try {
      const deal = await createDeal.mutateAsync({
        operator_id: "OP-901",
        hs_code: opp.hs_code,
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
        <div>
          <h1 className="text-lg font-bold">Opportunity Radar</h1>
          <p className="text-xs text-muted-foreground mt-0.5">
            Live arbitrage signals from Comtrade, US Census &amp; World Bank WITS
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
            <table className="w-full min-w-[560px]">
              <thead>
                <tr className="bg-popover border-b border-border">
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
                  <th className="px-4 py-3" />
                </tr>
              </thead>
              <tbody>
                {opportunities.map((opp, idx) => (
                  <tr
                    key={opp.opportunity_id}
                    className={cn(
                      "border-b border-border hover:bg-card transition-colors",
                      idx % 2 === 0 ? "bg-transparent" : "bg-secondary/30"
                    )}
                  >
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
                      <Badge variant="outline" className="text-[10px]">
                        {opp.source ?? "trade-min"}
                      </Badge>
                    </td>
                    <td className="px-4 py-3">
                      <ArbitrageBar rank={opp.arbitrage_rank} />
                    </td>
                    <td className="px-4 py-3">
                      <Button
                        size="sm"
                        className="h-7 text-xs gap-1"
                        onClick={() => handleStartDeal(opp)}
                        disabled={createDeal.isPending}
                      >
                        <ArrowUpRight className="w-3 h-3" /> Start Deal
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
