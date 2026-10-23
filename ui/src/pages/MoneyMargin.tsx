import { useState } from "react";
import { Link } from "react-router-dom";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from "recharts";
import { TrendingUp, DollarSign, Layers, Filter, Lock, Wallet, Scale, AlertTriangle } from "lucide-react";
import { cn, fmtUSD, fmtPct } from "@/lib/utils";
import type { DealStatus, DealLedger } from "@/api/types";
import { useDeals, useEscrowLedgers } from "@/api/hooks";
import { StatusBadge } from "@/components/StatusBadge";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";

function ledgerStatusVariant(status: string | null | undefined) {
  switch (status) {
    case "FUNDED": return "success" as const;
    case "RELEASED": return "teal" as const;
    case "PARTIALLY_FUNDED":
    case "REFUNDED": return "warning" as const;
    case "FAILED": return "destructive" as const;
    default: return "secondary" as const;
  }
}

// Escrow ledgers across every deal — the cash view the deal-margin estimates
// above cannot give: what is held, what PortLess actually received, what went
// to suppliers, and whether every ledger reconciles.
function EscrowLedgers() {
  const { data: ledgers, isLoading, isError } = useEscrowLedgers();
  const rows: DealLedger[] = ledgers ?? [];

  const sum = (pick: (l: DealLedger) => number | null | undefined) =>
    rows.reduce((s, l) => s + (pick(l) ?? 0), 0);
  const held = sum((l) => l.principal);
  const net = sum((l) => l.net_to_portless);
  const payouts = sum((l) => l.payouts_total);
  const realized = sum((l) => l.realized_margin);
  const unreconciled = rows.filter((l) => !l.reconciled).length;

  const tiles = [
    { label: "Held principal", value: fmtUSD(held), icon: Lock, color: "#4f8cff", sub: `${rows.length} escrow(s)` },
    { label: "Released to PortLess (net)", value: fmtUSD(net), icon: TrendingUp, color: "#2ec28a", sub: "after provider fees" },
    { label: "Supplier payouts", value: fmtUSD(payouts), icon: Wallet, color: "#f5a623", sub: "completed" },
    {
      label: "Realized margin so far",
      value: fmtUSD(realized),
      icon: Scale,
      color: realized >= 0 ? "#28c2a0" : "#ff5d6c",
      sub: unreconciled ? `${unreconciled} ledger(s) unreconciled` : "all ledgers reconciled",
    },
  ];

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Lock className="w-4 h-4 text-[#28c2a0]" />
        <h2 className="text-sm font-bold">Escrow Ledgers</h2>
        <span className="text-[10px] text-muted-foreground">
          neutral staged-release escrow · cash actually moved, not estimates
        </span>
      </div>

      {isLoading ? (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          {[1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-24 rounded-xl" />)}
        </div>
      ) : isError ? (
        <p className="text-xs text-muted-foreground">Escrow ledgers unavailable</p>
      ) : (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            {tiles.map((t) => {
              const Icon = t.icon;
              return (
                <Card key={t.label}>
                  <CardContent className="flex items-center gap-3 pt-4">
                    <div
                      className="flex items-center justify-center w-10 h-10 rounded-xl shrink-0"
                      style={{ background: `${t.color}1a` }}
                    >
                      <Icon className="w-5 h-5" style={{ color: t.color }} />
                    </div>
                    <div className="min-w-0">
                      <p className="text-[10px] text-muted-foreground truncate">{t.label}</p>
                      <p className="text-xl font-bold" style={{ color: t.color }}>{t.value}</p>
                      <p className="text-[10px] text-muted-foreground">{t.sub}</p>
                    </div>
                  </CardContent>
                </Card>
              );
            })}
          </div>

          <div className="rounded-xl border border-border overflow-x-auto">
            <table className="w-full min-w-[960px]">
              <thead>
                <tr className="bg-popover border-b border-border">
                  {[
                    ["Deal", "left"], ["Provider", "left"], ["Status", "left"], ["Principal", "right"],
                    ["Released", "right"], ["Fees", "right"], ["Payouts", "right"], ["Available", "right"],
                    ["Balance due", "right"], ["Ledger", "left"],
                  ].map(([h, align]) => (
                    <th
                      key={h}
                      className={cn(
                        "px-3 py-2.5 text-[10px] font-semibold text-muted-foreground uppercase tracking-wide",
                        align === "right" ? "text-right" : "text-left"
                      )}
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.length === 0 ? (
                  <tr>
                    <td colSpan={10} className="text-center py-8 text-xs text-muted-foreground">
                      No escrow opened yet
                    </td>
                  </tr>
                ) : (
                  rows.map((l, idx) => (
                    <tr
                      key={l.escrow_id ?? l.deal_id}
                      className={cn(
                        "border-b border-border hover:bg-card transition-colors text-xs",
                        idx % 2 === 0 ? "bg-transparent" : "bg-secondary/30",
                        !l.reconciled && "bg-[#ff5d6c]/5"
                      )}
                    >
                      <td className="px-3 py-2.5">
                        <Link to={`/deals/${l.deal_id}`} className="font-mono font-semibold text-[#4f8cff] hover:underline">
                          {l.deal_id.slice(0, 14)}
                        </Link>
                        {l.disputed && <Badge variant="destructive" className="ml-1.5 text-[9px] h-4 px-1.5">disputed</Badge>}
                        {l.settled && <Badge variant="teal" className="ml-1.5 text-[9px] h-4 px-1.5">settled</Badge>}
                      </td>
                      <td className="px-3 py-2.5 capitalize">{(l.provider ?? "—").replace("_", ".")}</td>
                      <td className="px-3 py-2.5">
                        <Badge variant={ledgerStatusVariant(l.status)} className="text-[9px] h-4 px-1.5">
                          {(l.status ?? "—").replace(/_/g, " ")}
                        </Badge>
                      </td>
                      <td className="px-3 py-2.5 text-right font-medium">{fmtUSD(l.principal)}</td>
                      <td className="px-3 py-2.5 text-right text-[#2ec28a] font-medium">{fmtUSD(l.released_total)}</td>
                      <td className="px-3 py-2.5 text-right text-muted-foreground">{fmtUSD(l.fees_total)}</td>
                      <td className="px-3 py-2.5 text-right">{fmtUSD(l.payouts_total)}</td>
                      <td className="px-3 py-2.5 text-right">{fmtUSD(l.available_for_payout)}</td>
                      <td className={cn("px-3 py-2.5 text-right", l.supplier_balance_due ? "text-[#f5a623] font-medium" : "text-muted-foreground")}>
                        {l.supplier_balance_due != null ? fmtUSD(l.supplier_balance_due) : "—"}
                      </td>
                      <td className="px-3 py-2.5">
                        {l.reconciled ? (
                          <Badge variant="success" className="text-[9px] h-4 px-1.5">reconciled</Badge>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-[#ff5d6c] font-medium" title={l.issues.join("\n")}>
                            <AlertTriangle className="w-3 h-3" /> {l.issues.length} issue(s)
                          </span>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

const ACTIVE_STATUSES: DealStatus[] = [
  "DRAFT", "MATCHING", "NEGOTIATING", "NEGOTIATION_AGREED",
  "PI_SENT", "PI_ACCEPTED", "ESCROW_PENDING", "ESCROW_FUNDED",
  "PO_ISSUED", "SUPPLIER_DISPATCH_READY", "QC_PASSED", "IN_TRANSIT",
];
const COMPLETE_STATUSES: DealStatus[] = ["CLEARED", "PAID"];

interface CustomTooltipProps {
  active?: boolean;
  payload?: Array<{ value: number; name: string }>;
  label?: string;
}

function CustomTooltip({ active, payload, label }: CustomTooltipProps) {
  if (!active || !payload?.length) return null;
  return (
    <div className="bg-card border border-border rounded-lg p-3 text-xs shadow-lg">
      <p className="font-mono font-bold text-[#4f8cff] mb-1">HS {label}</p>
      {payload.map((p) => (
        <p key={p.name} className="text-foreground">
          {p.name}: <span className="font-semibold">{fmtUSD(p.value)}</span>
        </p>
      ))}
    </div>
  );
}

export function MoneyMargin() {
  const { data: deals, isLoading, isError } = useDeals();
  const [statusFilter, setStatusFilter] = useState<"ALL" | "ACTIVE" | "COMPLETE">("ALL");

  const allDeals = deals ?? [];

  const filtered = allDeals.filter((d) => {
    if (statusFilter === "ALL") return true;
    if (statusFilter === "ACTIVE") return ACTIVE_STATUSES.includes(d.status);
    return COMPLETE_STATUSES.includes(d.status);
  });

  const totalPipeline = filtered.reduce((s, d) => s + (d.est_margin_usd ?? 0), 0);
  const totalRealized = filtered.reduce((s, d) => s + (d.realized_margin_usd ?? 0), 0);
  const avgMarginPct =
    filtered.length > 0
      ? filtered.reduce((s, d) => s + (d.margin_floor_pct ?? 0), 0) / filtered.length
      : null;

  // Chart data
  const chartData = filtered
    .filter((d) => d.est_margin_usd != null || d.realized_margin_usd != null)
    .map((d) => ({
      hs: d.hs_code,
      deal_id: d.deal_id,
      est: d.est_margin_usd ?? 0,
      realized: d.realized_margin_usd ?? 0,
      status: d.status,
    }))
    .slice(0, 20); // max 20 bars

  const metrics = [
    {
      label: "Pipeline Value",
      value: fmtUSD(totalPipeline),
      icon: DollarSign,
      color: "#4f8cff",
      sub: `${filtered.length} deals`,
    },
    {
      label: "Realized Margin",
      value: fmtUSD(totalRealized),
      icon: TrendingUp,
      color: "#2ec28a",
      sub: totalPipeline > 0 ? `${((totalRealized / totalPipeline) * 100).toFixed(1)}% realized` : "—",
    },
    {
      label: "Avg Margin Floor",
      value: fmtPct(avgMarginPct),
      icon: Layers,
      color: "#f5a623",
      sub: "across active deals",
    },
    {
      label: "Active Deals",
      value: allDeals.filter((d) => ACTIVE_STATUSES.includes(d.status)).length,
      icon: Filter,
      color: "#28c2a0",
      sub: "in pipeline",
    },
  ];

  return (
    <div className="space-y-4">
      {/* Header */}
      <div>
        <h1 className="text-lg font-bold">Money & Margin</h1>
        <p className="text-xs text-muted-foreground mt-0.5">
          Pipeline value, realized margins, and escrow tracking
        </p>
      </div>

      {/* Metric cards */}
      {isLoading ? (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          {[1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-24 rounded-xl" />)}
        </div>
      ) : (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          {metrics.map((m) => {
            const Icon = m.icon;
            return (
              <Card key={m.label}>
                <CardContent className="flex items-center gap-3 pt-4">
                  <div
                    className="flex items-center justify-center w-10 h-10 rounded-xl shrink-0"
                    style={{ background: `${m.color}1a` }}
                  >
                    <Icon className="w-5 h-5" style={{ color: m.color }} />
                  </div>
                  <div className="min-w-0">
                    <p className="text-[10px] text-muted-foreground truncate">{m.label}</p>
                    <p className="text-xl font-bold" style={{ color: m.color }}>
                      {m.value}
                    </p>
                    <p className="text-[10px] text-muted-foreground">{m.sub}</p>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      {/* Filter */}
      <div className="flex items-center gap-2">
        <span className="text-xs text-muted-foreground">Show:</span>
        {(["ALL", "ACTIVE", "COMPLETE"] as const).map((f) => (
          <button
            key={f}
            onClick={() => setStatusFilter(f)}
            className={cn(
              "text-xs px-3 py-1 rounded-md border transition-colors",
              statusFilter === f
                ? "bg-primary/15 text-primary border-primary/30"
                : "border-border text-muted-foreground hover:bg-secondary"
            )}
          >
            {f}
          </button>
        ))}
      </div>

      {/* Chart */}
      {!isLoading && !isError && chartData.length > 0 && (
        <Card>
          <CardContent className="pt-4">
            <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-4">
              Margin by Deal (HS Code)
            </p>
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={chartData} barGap={4}>
                <XAxis
                  dataKey="hs"
                  tick={{ fill: "#9aa9c4", fontSize: 10 }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  tick={{ fill: "#9aa9c4", fontSize: 10 }}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={(v) => `$${(v / 1000).toFixed(0)}k`}
                />
                <Tooltip content={<CustomTooltip />} cursor={{ fill: "rgba(79,140,255,0.05)" }} />
                <Bar dataKey="est" name="Est. Margin" radius={[3, 3, 0, 0]}>
                  {chartData.map((_entry, idx) => (
                    <Cell key={idx} fill="#4f8cff" fillOpacity={0.7} />
                  ))}
                </Bar>
                <Bar dataKey="realized" name="Realized" radius={[3, 3, 0, 0]}>
                  {chartData.map((_entry, idx) => (
                    <Cell key={idx} fill="#2ec28a" />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
            <div className="flex items-center gap-4 mt-2 justify-center">
              <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <div className="w-3 h-3 rounded-sm bg-[#4f8cff] opacity-70" />
                Est. Margin
              </div>
              <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <div className="w-3 h-3 rounded-sm bg-[#2ec28a]" />
                Realized
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Escrow ledgers — cash actually held / released / paid out */}
      <EscrowLedgers />

      {/* Table */}
      {isLoading ? (
        <Skeleton className="h-64 w-full rounded-xl" />
      ) : isError ? (
        <div className="flex flex-col items-center py-12 gap-2 text-muted-foreground">
          <TrendingUp className="w-8 h-8 opacity-30" />
          <p className="text-sm">Backend unavailable</p>
        </div>
      ) : (
        <div className="rounded-xl border border-border overflow-hidden">
          <table className="w-full">
            <thead>
              <tr className="bg-popover border-b border-border">
                <th className="text-left px-4 py-3 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                  HS Code
                </th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                  Status
                </th>
                <th className="text-right px-4 py-3 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                  Est. Margin
                </th>
                <th className="text-right px-4 py-3 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                  Realized
                </th>
                <th className="text-right px-4 py-3 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                  Margin Floor
                </th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                  Autonomy
                </th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 ? (
                <tr>
                  <td colSpan={6} className="text-center py-12 text-sm text-muted-foreground">
                    No deals
                  </td>
                </tr>
              ) : (
                filtered.map((deal, idx) => (
                  <tr
                    key={deal.deal_id}
                    className={cn(
                      "border-b border-border hover:bg-card transition-colors",
                      idx % 2 === 0 ? "bg-transparent" : "bg-secondary/30"
                    )}
                  >
                    <td className="px-4 py-3 font-mono font-bold text-sm">{deal.hs_code}</td>
                    <td className="px-4 py-3">
                      <StatusBadge status={deal.status} />
                    </td>
                    <td className="px-4 py-3 text-right font-medium">
                      {fmtUSD(deal.est_margin_usd)}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <span
                        className={cn(
                          "font-medium",
                          deal.realized_margin_usd
                            ? "text-[#2ec28a]"
                            : "text-muted-foreground"
                        )}
                      >
                        {fmtUSD(deal.realized_margin_usd)}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right text-[#f5a623] font-medium">
                      {fmtPct(deal.margin_floor_pct)}
                    </td>
                    <td className="px-4 py-3">
                      <Badge
                        variant={
                          deal.autonomy_profile === "L0"
                            ? "destructive"
                            : deal.autonomy_profile === "L1"
                            ? "warning"
                            : deal.autonomy_profile === "L2"
                            ? "info"
                            : "success"
                        }
                        className="text-[10px]"
                      >
                        {deal.autonomy_profile}
                      </Badge>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
