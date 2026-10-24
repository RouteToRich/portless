import { useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowRightCircle,
  Boxes,
  FileText,
  Handshake,
  Mail,
  MessageSquare,
  Search,
  ShieldCheck,
  Wallet,
} from "lucide-react";
import { cn, fmtDateTime } from "@/lib/utils";
import { useDealTimeline } from "@/api/hooks";
import type { TimelineEvent } from "@/api/types";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";

const KIND_META: Record<string, { label: string; color: string; Icon: React.ElementType }> = {
  deal: { label: "Deal", color: "#4f8cff", Icon: Activity },
  status: { label: "Status", color: "#4f8cff", Icon: ArrowRightCircle },
  outreach: { label: "Outreach", color: "#a78bfa", Icon: MessageSquare },
  sourcing: { label: "Sourcing", color: "#f5a623", Icon: Search },
  negotiation: { label: "Negotiation", color: "#28c2a0", Icon: Handshake },
  email: { label: "E-mail", color: "#60a5fa", Icon: Mail },
  gate: { label: "Gate", color: "#f5a623", Icon: ShieldCheck },
  document: { label: "Document", color: "#94a3b8", Icon: FileText },
  operations: { label: "Operations", color: "#2ec28a", Icon: Boxes },
  money: { label: "Money", color: "#2ec28a", Icon: Wallet },
  exception: { label: "Exception", color: "#ff5d6c", Icon: AlertTriangle },
};

function dayKey(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10);
  return d.toLocaleDateString(undefined, { weekday: "short", year: "numeric", month: "short", day: "numeric" });
}

export function TimelineTab({ dealId }: { dealId: string }) {
  const { data, isLoading, isError, refetch } = useDealTimeline(dealId);
  const [filter, setFilter] = useState<string | null>(null);

  const events = useMemo(() => {
    const all = data?.events ?? [];
    return filter ? all.filter((e) => e.kind === filter) : all;
  }, [data, filter]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const e of data?.events ?? []) c[e.kind] = (c[e.kind] ?? 0) + 1;
    return c;
  }, [data]);

  const groups = useMemo(() => {
    const out: { day: string; events: TimelineEvent[] }[] = [];
    for (const e of events) {
      const day = dayKey(e.at);
      const last = out[out.length - 1];
      if (last && last.day === day) last.events.push(e);
      else out.push({ day, events: [e] });
    }
    return out;
  }, [events]);

  if (isLoading) return <Skeleton className="h-64 w-full rounded-xl" />;
  if (isError || !data) {
    return (
      <Card>
        <CardContent className="py-8 text-sm text-muted-foreground flex items-center justify-between">
          Timeline unavailable
          <Button variant="outline" size="sm" onClick={() => refetch()}>Retry</Button>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <button
          onClick={() => setFilter(null)}
          className={cn(
            "px-2.5 py-1 rounded-md text-[11px] font-semibold border",
            filter === null ? "bg-primary/10 border-primary/40 text-primary" : "border-border text-muted-foreground hover:text-foreground"
          )}
        >
          All · {data.count}
        </button>
        {Object.entries(KIND_META)
          .filter(([k]) => counts[k])
          .map(([k, meta]) => (
            <button
              key={k}
              onClick={() => setFilter(filter === k ? null : k)}
              className={cn(
                "px-2.5 py-1 rounded-md text-[11px] font-semibold border flex items-center gap-1",
                filter === k ? "text-white" : "border-border text-muted-foreground hover:text-foreground"
              )}
              style={filter === k ? { background: meta.color, borderColor: meta.color } : undefined}
            >
              <meta.Icon className="w-3 h-3" />
              {meta.label} · {counts[k]}
            </button>
          ))}
      </div>

      {data.warnings.length > 0 && (
        <p className="text-[10px] text-muted-foreground">
          Some sources were unavailable: {data.warnings.join(" · ")}
        </p>
      )}

      {groups.length === 0 ? (
        <Card>
          <CardContent className="py-8 text-sm text-muted-foreground text-center">No events yet</CardContent>
        </Card>
      ) : (
        groups.map((g) => (
          <Card key={g.day}>
            <CardContent className="pt-4 pb-3">
              <p className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground mb-3">{g.day}</p>
              <ol className="relative border-l border-border ml-2 space-y-3">
                {g.events.map((e, i) => {
                  const meta = KIND_META[e.kind] ?? { label: e.kind, color: "#94a3b8", Icon: Activity };
                  const Icon = meta.Icon;
                  return (
                    <li key={`${e.at}-${e.ref ?? ""}-${i}`} className="ml-4">
                      <span
                        className="absolute -left-[9px] flex items-center justify-center w-[18px] h-[18px] rounded-full border bg-card"
                        style={{ borderColor: meta.color }}
                      >
                        <Icon className="w-2.5 h-2.5" style={{ color: meta.color }} />
                      </span>
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <p className="text-sm font-medium leading-tight">{e.title}</p>
                          {e.detail && <p className="text-[11px] text-muted-foreground mt-0.5 break-words">{e.detail}</p>}
                          <p className="text-[10px] text-muted-foreground mt-0.5 flex items-center gap-2 flex-wrap">
                            <span style={{ color: meta.color }} className="font-semibold">{meta.label}</span>
                            {e.source && <span>· {e.source}</span>}
                            {e.actor && <span>· by {e.actor}</span>}
                            {e.ref && <span className="font-mono">· {e.ref}</span>}
                          </p>
                        </div>
                        <span className="text-[10px] text-muted-foreground whitespace-nowrap shrink-0">
                          {fmtDateTime(e.at)}
                        </span>
                      </div>
                    </li>
                  );
                })}
              </ol>
            </CardContent>
          </Card>
        ))
      )}
    </div>
  );
}
