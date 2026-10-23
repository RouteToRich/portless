import { Activity, AlertTriangle, CheckCircle2, ShieldCheck, XCircle, RefreshCw, Server, Wifi } from "lucide-react";
import { cn, timeAgo } from "@/lib/utils";
import { useDownstreamHealth, useGatewayHealth, useReadiness } from "@/api/hooks";
import type { ReadinessCheck } from "@/api/types";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

const CATEGORY_LABELS: Record<string, string> = {
  persistence: "Persistence",
  bus: "Event bus",
  money: "Money",
  data: "Data & identification",
  automation: "Automation",
  email: "E-mail",
  documents: "Documents & identity",
  operations: "Operations partners",
  llm: "LLM",
};

const CATEGORY_ORDER = ["money", "data", "email", "documents", "persistence", "bus", "operations", "automation", "llm"];

function statusTone(status: ReadinessCheck["status"]) {
  switch (status) {
    case "ok": return { color: "#2ec28a", Icon: CheckCircle2, label: "ok" };
    case "warn": return { color: "#f5a623", Icon: AlertTriangle, label: "warn" };
    default: return { color: "#ff5d6c", Icon: XCircle, label: "fail" };
  }
}

// Production readiness gate (GET /health/readiness): every stub / fallback the
// pipeline is currently running on. "fail" = the system would pretend a money
// or legal step happened; "warn" = it still does the right thing, just by hand.
function ReadinessPanel() {
  const { data, isLoading, isError, refetch } = useReadiness();

  if (isLoading) return <Skeleton className="h-40 w-full rounded-xl" />;
  if (isError || !data) {
    return (
      <div className="rounded-xl border border-dashed border-border px-4 py-6 text-xs text-muted-foreground flex items-center justify-between">
        Readiness report unavailable
        <Button variant="outline" size="sm" onClick={() => refetch()}>Retry</Button>
      </div>
    );
  }

  const groups = CATEGORY_ORDER
    .map((cat) => ({ cat, checks: data.checks.filter((c) => c.category === cat) }))
    .filter((g) => g.checks.length > 0);
  // Anything in a category we did not list still shows up.
  const known = new Set(CATEGORY_ORDER);
  const rest = data.checks.filter((c) => !known.has(c.category));
  if (rest.length) groups.push({ cat: "other", checks: rest });

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3 p-4 rounded-xl border border-border bg-popover flex-wrap">
        <div className="flex items-center gap-2.5">
          <ShieldCheck className={cn("w-5 h-5", data.ready ? "text-[#2ec28a]" : "text-[#ff5d6c]")} />
          <div>
            <p className="text-sm font-bold flex items-center gap-2">
              Production readiness
              <Badge variant={data.ready ? "success" : "destructive"} className="text-[10px]">
                {data.ready ? "Ready" : "Not ready"}
              </Badge>
            </p>
            <p className="text-[10px] text-muted-foreground">
              profile <span className="font-mono">{data.profile}</span>
              {" · "}
              {data.enforced ? "enforced at boot" : "informational (not enforced)"}
              {" · "}checked {timeAgo(data.generated_at)}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3 text-xs">
          <span className="text-[#ff5d6c] font-semibold">{data.fail_count} fail</span>
          <span className="text-[#f5a623] font-semibold">{data.warn_count} warn</span>
          <span className="text-[#2ec28a] font-semibold">
            {data.checks.length - data.fail_count - data.warn_count} ok
          </span>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {groups.map((g) => (
          <Card key={g.cat}>
            <CardContent className="pt-4 pb-3">
              <p className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground mb-2">
                {CATEGORY_LABELS[g.cat] ?? g.cat}
              </p>
              <ul className="space-y-2">
                {g.checks.map((c) => {
                  const tone = statusTone(c.status);
                  const Icon = tone.Icon;
                  return (
                    <li key={c.id} className="flex items-start gap-2">
                      <Icon className="w-3.5 h-3.5 mt-0.5 shrink-0" style={{ color: tone.color }} />
                      <div className="min-w-0">
                        <p className="text-xs font-medium flex items-center gap-1.5">
                          {c.label}
                          <span
                            className="text-[9px] px-1.5 py-0.5 rounded-full border font-semibold uppercase"
                            style={{ color: tone.color, borderColor: `${tone.color}66`, background: `${tone.color}1a` }}
                          >
                            {tone.label}
                          </span>
                        </p>
                        <p className="text-[10px] text-muted-foreground leading-relaxed">{c.detail}</p>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}

const SERVICE_LABELS: Record<string, string> = {
  deals: "Deals",
  trade_min: "Trade Min",
  match_engine: "Match Engine",
  sourcing: "Sourcing",
  negotiation_agent: "Negotiation Agent",
  dealmaker: "Dealmaker",
  docugen: "DocuGen",
  shield_link: "Shield Link",
  escrow_guard: "Escrow Guard",
  hitl_approval: "HITL Approval",
  exception_resolver: "Exception Resolver",
};

const SERVICE_ICONS: Record<string, string> = {
  deals: "📋",
  trade_min: "📡",
  match_engine: "⚙️",
  sourcing: "🔍",
  negotiation_agent: "🤝",
  dealmaker: "📄",
  docugen: "📑",
  shield_link: "🛡️",
  escrow_guard: "🔒",
  hitl_approval: "✅",
  exception_resolver: "🚨",
};

interface ServiceCardProps {
  name: string;
  status: string;
}

function ServiceCard({ name, status }: ServiceCardProps) {
  const isOk = status === "ok";
  // Gateway reports hyphenated keys (e.g. "trade-min"); our maps use underscores.
  const key = name.replace(/-/g, "_");
  const label = SERVICE_LABELS[key] ?? name.replace(/[_-]/g, " ");
  const emoji = SERVICE_ICONS[key] ?? "🔧";

  return (
    <Card
      className={cn(
        "transition-all duration-200 hover:border-opacity-80",
        isOk
          ? "border-[#2ec28a]/20 hover:border-[#2ec28a]/40"
          : "border-[#ff5d6c]/30 hover:border-[#ff5d6c]/50 bg-[#ff5d6c]/5"
      )}
    >
      <CardContent className="flex items-center gap-3 pt-4 pb-4">
        {/* Status indicator */}
        <div className="relative shrink-0">
          <div
            className={cn(
              "w-10 h-10 rounded-xl flex items-center justify-center text-lg",
              isOk ? "bg-[#2ec28a]/10" : "bg-[#ff5d6c]/10"
            )}
          >
            {emoji}
          </div>
          <span
            className={cn(
              "absolute -bottom-0.5 -right-0.5 w-3.5 h-3.5 rounded-full border-2 border-card",
              isOk ? "bg-[#2ec28a]" : "bg-[#ff5d6c]"
            )}
          />
        </div>

        <div className="flex-1 min-w-0">
          <p className="text-sm font-semibold truncate">{label}</p>
          <p className="text-[10px] text-muted-foreground font-mono">{name}</p>
        </div>

        <div className="shrink-0">
          {isOk ? (
            <CheckCircle2 className="w-4 h-4 text-[#2ec28a]" />
          ) : (
            <XCircle className="w-4 h-4 text-[#ff5d6c]" />
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export function SystemHealth() {
  const gateway = useGatewayHealth();
  const downstream = useDownstreamHealth();

  const entries = Object.entries(downstream.data ?? {});
  const okCount = entries.filter(([, s]) => s === "ok").length;
  const totalCount = entries.length;
  const allOk = totalCount > 0 && okCount === totalCount;
  const gatewayOk = gateway.isSuccess && !gateway.isError;

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex items-center justify-center w-9 h-9 rounded-xl bg-[#4f8cff]/10">
            <Activity className="w-5 h-5 text-[#4f8cff]" />
          </div>
          <div>
            <h1 className="text-lg font-bold">System Health</h1>
            <p className="text-xs text-muted-foreground">
              Auto-refreshes every 10 seconds
            </p>
          </div>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="h-8 gap-1.5 text-xs"
          onClick={() => downstream.refetch()}
        >
          <RefreshCw className={cn("w-3.5 h-3.5", downstream.isLoading && "animate-spin")} />
          Refresh
        </Button>
      </div>

      {/* Summary bar */}
      <div className="flex items-center gap-3 p-4 rounded-xl border border-border bg-popover">
        {/* Gateway */}
        <div className="flex items-center gap-2.5 pr-4 border-r border-border">
          <Wifi className={cn("w-4 h-4", gatewayOk ? "text-[#2ec28a]" : "text-[#ff5d6c]")} />
          <div>
            <p className="text-[10px] text-muted-foreground">API Gateway</p>
            <Badge variant={gatewayOk ? "success" : "destructive"} className="text-[10px] mt-0.5">
              {gatewayOk ? "Online" : "Offline"}
            </Badge>
          </div>
        </div>

        {/* Services */}
        <div className="flex items-center gap-2.5 flex-1">
          <Server className={cn("w-4 h-4", allOk ? "text-[#2ec28a]" : "text-[#f5a623]")} />
          <div>
            <p className="text-[10px] text-muted-foreground">Downstream Services</p>
            <p className="text-sm font-bold">
              <span className={allOk ? "text-[#2ec28a]" : "text-[#f5a623]"}>{okCount}</span>
              <span className="text-muted-foreground font-normal"> / {totalCount} healthy</span>
            </p>
          </div>
        </div>

        {/* Overall status */}
        <div
          className={cn(
            "flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold",
            allOk && gatewayOk
              ? "bg-[#2ec28a]/10 text-[#2ec28a] border border-[#2ec28a]/30"
              : "bg-[#f5a623]/10 text-[#f5a623] border border-[#f5a623]/30"
          )}
        >
          <span
            className={cn(
              "w-2 h-2 rounded-full",
              allOk && gatewayOk ? "bg-[#2ec28a] animate-pulse" : "bg-[#f5a623]"
            )}
          />
          {allOk && gatewayOk ? "All Systems Operational" : "Partial Degradation"}
        </div>
      </div>

      {/* Production readiness — which stubs / fallbacks the pipeline runs on */}
      <ReadinessPanel />

      {/* Service cards */}
      {downstream.isLoading ? (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-20 rounded-xl" />
          ))}
        </div>
      ) : downstream.isError ? (
        <div className="flex flex-col items-center py-12 gap-3 text-muted-foreground border border-dashed border-border rounded-xl">
          <XCircle className="w-8 h-8 text-[#ff5d6c]" />
          <p className="text-sm">Could not reach backend</p>
          <Button variant="outline" size="sm" onClick={() => downstream.refetch()}>
            Retry
          </Button>
        </div>
      ) : entries.length === 0 ? (
        <div className="flex flex-col items-center py-12 gap-2 text-muted-foreground border border-dashed border-border rounded-xl">
          <Activity className="w-8 h-8 opacity-30" />
          <p className="text-sm">No downstream services reported</p>
        </div>
      ) : (
        <>
          {/* Failed services first */}
          {entries.some(([, s]) => s !== "ok") && (
            <div className="space-y-2">
              <p className="text-[10px] font-semibold uppercase tracking-widest text-[#ff5d6c]">
                Unhealthy
              </p>
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
                {entries
                  .filter(([, s]) => s !== "ok")
                  .map(([name, status]) => (
                    <ServiceCard key={name} name={name} status={status} />
                  ))}
              </div>
            </div>
          )}

          <div className="space-y-2">
            <p className="text-[10px] font-semibold uppercase tracking-widest text-[#2ec28a]">
              Healthy ({okCount})
            </p>
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
              {entries
                .filter(([, s]) => s === "ok")
                .map(([name, status]) => (
                  <ServiceCard key={name} name={name} status={status} />
                ))}
            </div>
          </div>
        </>
      )}

      {/* Last checked */}
      {!downstream.isLoading && (
        <p className="text-[10px] text-muted-foreground text-center">
          Last checked: {timeAgo(new Date().toISOString())} · auto-refresh in ~10s
        </p>
      )}
    </div>
  );
}
