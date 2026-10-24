import { useState } from "react";
import { toast } from "sonner";
import {
  Activity, AlertTriangle, CheckCircle2, ShieldCheck, XCircle, RefreshCw, Server, Wifi,
  Database, Send, Newspaper, ExternalLink,
} from "lucide-react";
import { cn, timeAgo } from "@/lib/utils";
import {
  useDigest, useDownstreamHealth, useGatewayHealth, useReadiness, useSendDigest, useTradeSources,
} from "@/api/hooks";
import type { ReadinessCheck, TradeSource } from "@/api/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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

// ── Data sources (GET /trade-min/sources) ────────────────────────────────────
// Which demand / buyer / seller / enrichment sources the scanner can reach,
// incl. the PortLess-owned Apify actors, the daily run budget and the external
// slots (actors built outside /actor-pipeline, e.g. carrier tracking).
const ROLE_LABELS: Record<string, string> = {
  demand: "Demand", buyers: "Buyers", sellers: "Sellers", tariff: "Tariff",
  enrichment: "Enrichment", screening: "Screening", logistics: "Logistics",
};

function modeTone(mode: string) {
  switch (mode) {
    case "live": return "#2ec28a";
    case "open": return "#4f8cff";
    case "mock": return "#a78bfa";
    default: return "#f5a623";
  }
}

function DataSourcesPanel() {
  const { data, isLoading, isError, refetch } = useTradeSources();
  if (isLoading) return <Skeleton className="h-40 w-full rounded-xl" />;
  if (isError || !data) {
    return (
      <div className="rounded-xl border border-dashed border-border px-4 py-6 text-xs text-muted-foreground flex items-center justify-between">
        Data-source report unavailable
        <Button variant="outline" size="sm" onClick={() => refetch()}>Retry</Button>
      </div>
    );
  }
  const cap = data.apify.max_runs_per_day ?? 0;
  const runs = data.apify.runs_today ?? 0;
  const byRole = new Map<string, TradeSource[]>();
  for (const src of data.sources) {
    const list = byRole.get(src.role) ?? [];
    list.push(src);
    byRole.set(src.role, list);
  }
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div className="flex items-center gap-2">
            <Database className="w-4 h-4 text-[#4f8cff]" />
            <CardTitle>Data sources &amp; Apify actors</CardTitle>
            <span className="text-[10px] text-muted-foreground">
              {data.live_count} of {data.total} reachable{data.mock_mode && " · TRADE_MIN_MOCK_MODE"}
            </span>
          </div>
          <div className="flex items-center gap-2 text-[10px] flex-wrap">
            <Badge variant={data.apify.token_present ? "success" : "warning"}>
              {data.apify.token_present ? `Apify as ${data.apify.owner}` : "APIFY_TOKEN unset"}
            </Badge>
            <Badge variant="outline">{data.apify.ai_tier ? "AI tier" : "rule tier"} · {data.apify.max_items} rows/call</Badge>
            <Badge
              variant={cap > 0 && runs >= cap ? "destructive" : cap > 0 ? "info" : "warning"}
              title="APIFY_MAX_RUNS_PER_DAY — process-wide cap on paid actor runs"
            >
              runs today {runs}{cap > 0 ? ` / ${cap}` : " · no daily cap"}
            </Badge>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {[...byRole.entries()].map(([role, list]) => (
          <div key={role}>
            <p className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground mb-1.5">
              {ROLE_LABELS[role] ?? role}
            </p>
            <ul className="grid grid-cols-1 md:grid-cols-2 gap-1.5">
              {list.map((src) => {
                const color = modeTone(src.mode);
                return (
                  <li key={src.name} className="flex items-start gap-2 rounded-lg border border-border bg-secondary/50 px-2.5 py-2">
                    <span className="mt-1 w-2 h-2 rounded-full shrink-0" style={{ background: color }} />
                    <div className="min-w-0">
                      <p className="text-xs font-medium flex items-center gap-1.5 flex-wrap">
                        {src.label}
                        <span className="text-[9px] px-1.5 py-0.5 rounded-full border font-semibold uppercase"
                          style={{ color, borderColor: `${color}66`, background: `${color}1a` }}>
                          {src.mode}
                        </span>
                        {src.external && (
                          <span className="text-[9px] px-1.5 py-0.5 rounded-full border border-[#a78bfa]/60 text-[#a78bfa] bg-[#a78bfa]/10 font-semibold"
                            title="Built and published outside /actor-pipeline — not part of the pool's automatic research → implement → deploy loop">
                            external actor (not built by /actor-pipeline)
                          </span>
                        )}
                      </p>
                      <p className="text-[10px] text-muted-foreground leading-relaxed">
                        {src.detail}
                        {src.actor && <span className="font-mono"> · {src.actor}</span>}
                      </p>
                    </div>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
        <p className="text-[10px] text-muted-foreground">
          Stub = the source contributes nothing until its credential is set. The carrier-tracking slot is
          external: until <span className="font-mono">container-tracking-ai</span> exists on the account, the
          buyer's acceptance window opens by operator milestone entry or the fallback after clearance.
        </p>
      </CardContent>
    </Card>
  );
}

// ── Operator digest (GET /hitl-approval/digest) ──────────────────────────────
function DigestCard() {
  const { data, isLoading, isError, refetch } = useDigest();
  const send = useSendDigest();
  const [last, setLast] = useState<{ telegram: boolean; email: { to: string; outcome: string }[] } | null>(null);

  async function sendNow() {
    try {
      const res = await send.mutateAsync();
      setLast(res);
      const channels = [res.telegram ? "Telegram" : null, ...res.email.map((e) => `${e.to} (${e.outcome})`)].filter(Boolean);
      if (channels.length) toast.success("Digest sent", { description: channels.join(" · ") });
      else toast.info("No delivery channel configured", { description: "Set HITL_TELEGRAM_BOT_TOKEN/CHAT_ID or DIGEST_EMAIL_TO — the digest was written to the log." });
    } catch (e) {
      toast.error("Send failed", { description: (e as Error).message });
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div className="flex items-center gap-2">
            <Newspaper className="w-4 h-4 text-[#f5a623]" />
            <CardTitle>Operator digest</CardTitle>
            {data && <span className="text-[10px] text-muted-foreground">generated {timeAgo(data.generated_at)}</span>}
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" className="h-8 text-xs gap-1.5" onClick={() => refetch()}>
              <RefreshCw className="w-3.5 h-3.5" /> Refresh
            </Button>
            <Button size="sm" className="h-8 text-xs gap-1.5" onClick={sendNow} disabled={send.isPending}>
              <Send className="w-3.5 h-3.5" /> {send.isPending ? "Sending…" : "Send now"}
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {isLoading && <Skeleton className="h-32 w-full" />}
        {isError && <p className="text-xs text-destructive">Digest unavailable</p>}
        {data && (
          <>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
              {[
                { label: "Gates waiting", value: data.approvals.pending_total ?? 0, sub: `oldest ${Math.round(data.approvals.oldest_hours ?? 0)} h`, color: "#f5a623" },
                { label: "Active deals", value: data.deals.active ?? 0, sub: `${data.deals.stalled?.length ?? 0} stalled · ${data.deals.waiting_for_buyer ?? 0} waiting for a buyer`, color: "#4f8cff" },
                { label: "Open exceptions", value: data.exceptions.open_total ?? 0, sub: Object.entries(data.exceptions.by_status ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "none", color: "#ff5d6c" },
                { label: `In escrow (${data.money.currency ?? "USD"})`, value: Math.round(data.money.in_escrow ?? 0).toLocaleString(), sub: `${data.money.unreconciled?.length ?? 0} unreconciled · ${data.money.windows_open ?? 0} window(s) open`, color: "#2ec28a" },
              ].map((m) => (
                <div key={m.label} className="rounded-lg bg-secondary border border-border p-2.5">
                  <p className="text-[10px] text-muted-foreground">{m.label}</p>
                  <p className="text-lg font-bold" style={{ color: m.color }}>{m.value}</p>
                  <p className="text-[10px] text-muted-foreground truncate" title={m.sub}>{m.sub}</p>
                </div>
              ))}
            </div>
            <pre className="text-[11px] leading-relaxed whitespace-pre-wrap rounded-lg bg-secondary border border-border p-3 max-h-80 overflow-auto font-mono">
              {data.text ?? "(no text)"}
            </pre>
            {data.approvals.items && data.approvals.items.length > 0 && (
              <ul className="text-[11px] space-y-0.5">
                {data.approvals.items.slice(0, 6).map((a) => (
                  <li key={a.approval_id} className="flex items-center gap-2">
                    <span className="font-mono text-muted-foreground">{a.deal_id}</span>
                    <span>{a.action.replace(/_/g, " ").toLowerCase()}</span>
                    <span className="text-muted-foreground">{Math.round(a.age_hours ?? 0)} h</span>
                    {a.deep_link && (
                      <a href={a.deep_link} className="text-[#4f8cff] hover:underline flex items-center gap-0.5" target="_blank" rel="noopener noreferrer">
                        open <ExternalLink className="w-3 h-3" />
                      </a>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {last && (
              <p className="text-[10px] text-muted-foreground">
                Last send: Telegram {last.telegram ? "delivered" : "not configured / failed"}
                {last.email.length ? ` · e-mail ${last.email.map((e) => `${e.to} ${e.outcome}`).join(", ")}` : " · no e-mail recipient (DIGEST_EMAIL_TO)"}
              </p>
            )}
            {data.warnings.length > 0 && (
              <p className="text-[10px] text-[#f5a623]">{data.warnings.join(" · ")}</p>
            )}
            <p className="text-[10px] text-muted-foreground">
              Sent daily at DIGEST_HOUR_UTC via Telegram (HITL_TELEGRAM_BOT_TOKEN / CHAT_ID) and e-mail (DIGEST_EMAIL_TO).
            </p>
          </>
        )}
      </CardContent>
    </Card>
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

      {/* Data sources + Apify run budget, and the daily operator digest */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
        <DataSourcesPanel />
        <DigestCard />
      </div>

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
