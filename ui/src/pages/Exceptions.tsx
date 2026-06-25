import { useState } from "react";
import { AlertTriangle, Search, ExternalLink, RefreshCw, Layers } from "lucide-react";
import { cn } from "@/lib/utils";
import { useExceptions, useException } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Link } from "react-router-dom";

const CATEGORY_COLORS: Record<string, string> = {
  QC_FAILED: "#ff5d6c",
  FREIGHT_DELAY: "#f5a623",
  PAYMENT_FAILED: "#ff5d6c",
  NEGOTIATION_FAILED: "#ff5d6c",
  CUSTOMS_REJECTED: "#f5a623",
  DOCUMENT_ERROR: "#f5a623",
  CONNECTOR_ERROR: "#9aa9c4",
  MARGIN_BREACH: "#f5a623",
  APPROVAL_REJECTED: "#ff5d6c",
  CALLBACK_FAILED: "#9aa9c4",
  UNKNOWN: "#9aa9c4",
};

const STATUS_VARIANT: Record<
  string,
  "destructive" | "warning" | "success" | "secondary" | "outline"
> = {
  OPEN: "destructive",
  ANALYZING: "warning",
  REMEDIATING: "warning",
  RESOLVED: "success",
  ESCALATED: "warning",
  UNRESOLVABLE: "destructive",
};

function ExceptionDetail({ exceptionId }: { exceptionId: string }) {
  const { data, isLoading, isError } = useException(exceptionId);

  if (isLoading) {
    return (
      <div className="space-y-3 p-4">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div className="flex flex-col items-center py-12 gap-2 text-muted-foreground">
        <AlertTriangle className="w-6 h-6" />
        <p className="text-sm">Exception not found</p>
      </div>
    );
  }

  const catColor = CATEGORY_COLORS[data.category ?? "UNKNOWN"] ?? "#9aa9c4";

  return (
    <div className="space-y-4 p-4">
      {/* Header */}
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-xs text-muted-foreground font-mono">{data.exception_id}</p>
          <h2 className="text-base font-bold mt-0.5">
            {(data.category ?? "UNKNOWN").replace(/_/g, " ")}
          </h2>
        </div>
        <Badge variant={STATUS_VARIANT[data.status] ?? "outline"}>{data.status}</Badge>
      </div>

      {/* Deal link */}
      <Link
        to={`/deals/${data.deal_id}`}
        className="flex items-center gap-1.5 text-xs text-[#4f8cff] hover:underline"
      >
        Deal: {data.deal_id.slice(0, 12)}… <ExternalLink className="w-3 h-3" />
      </Link>

      {/* Category chip */}
      {data.category && (
        <div
          className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs font-semibold"
          style={{
            color: catColor,
            borderColor: `${catColor}40`,
            background: `${catColor}15`,
          }}
        >
          <AlertTriangle className="w-3.5 h-3.5" />
          {data.category}
        </div>
      )}

      {/* Remediation plan */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Layers className="w-4 h-4 text-[#4f8cff]" />
            Remediation Plan
          </CardTitle>
        </CardHeader>
        <CardContent>
          {data.remediation_plan.length === 0 ? (
            <p className="text-sm text-muted-foreground">No plan generated yet</p>
          ) : (
            <ol className="space-y-2">
              {data.remediation_plan.map((step, i) => (
                <li key={i} className="flex gap-3 text-sm">
                  <span className="flex items-center justify-center w-5 h-5 rounded-full bg-[#4f8cff]/15 text-[#4f8cff] text-[10px] font-bold shrink-0 mt-0.5">
                    {i + 1}
                  </span>
                  <span className="text-foreground leading-relaxed">{step}</span>
                </li>
              ))}
            </ol>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

export function Exceptions() {
  const { data: exceptions, isLoading, isError, refetch } = useExceptions();
  const [manualId, setManualId] = useState("");
  const [lookupId, setLookupId] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const activeExceptionId = lookupId ?? selected;

  const exList = exceptions ?? [];
  const openCount = exList.filter((e) => e.status === "OPEN" || e.status === "ANALYZING").length;

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <h1 className="text-lg font-bold">Exceptions</h1>
          {openCount > 0 && (
            <span className="flex items-center justify-center px-2 h-5 text-[10px] font-bold rounded-full bg-[#ff5d6c] text-white">
              {openCount} open
            </span>
          )}
        </div>
        <Button variant="outline" size="sm" className="h-8 gap-1.5 text-xs" onClick={() => refetch()}>
          <RefreshCw className="w-3.5 h-3.5" /> Refresh
        </Button>
      </div>

      {/* Manual lookup */}
      <div className="flex items-center gap-2">
        <div className="relative flex-1 max-w-sm">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
          <Input
            placeholder="Exception ID lookup (e.g. EXC-2026-0001)"
            value={manualId}
            onChange={(e) => setManualId(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && manualId.trim()) {
                setLookupId(manualId.trim());
                setSelected(null);
              }
            }}
            className="pl-8 h-8 text-xs"
          />
        </div>
        <Button
          size="sm"
          className="h-8 text-xs"
          onClick={() => {
            if (manualId.trim()) {
              setLookupId(manualId.trim());
              setSelected(null);
            }
          }}
          disabled={!manualId.trim()}
        >
          Look up
        </Button>
        {lookupId && (
          <Button
            variant="ghost"
            size="sm"
            className="h-8 text-xs"
            onClick={() => {
              setLookupId(null);
              setManualId("");
            }}
          >
            Clear
          </Button>
        )}
      </div>

      <div className="flex flex-col md:flex-row gap-3">
        {/* Left: exception list */}
        <div className="w-full md:w-[320px] md:shrink-0 flex flex-col bg-popover border border-border rounded-xl overflow-hidden">
          <div className="px-4 py-3 border-b border-border">
            <p className="text-xs font-semibold text-muted-foreground">
              {exList.length} exception{exList.length !== 1 ? "s" : ""}
            </p>
          </div>
          <ScrollArea className="flex-1 h-64 md:h-[calc(100vh-300px)]">
            {isLoading ? (
              <div className="p-3 space-y-2">
                {[1, 2, 3].map((i) => <Skeleton key={i} className="h-16 w-full rounded-lg" />)}
              </div>
            ) : isError ? (
              <div className="flex flex-col items-center py-8 gap-2 text-muted-foreground text-xs">
                <AlertTriangle className="w-5 h-5" />
                Backend unavailable
              </div>
            ) : exList.length === 0 ? (
              <div className="flex flex-col items-center py-8 gap-2 text-muted-foreground text-xs">
                <AlertTriangle className="w-5 h-5 opacity-30" />
                No exceptions
              </div>
            ) : (
              exList.map((ex) => {
                const catColor = CATEGORY_COLORS[ex.category ?? "UNKNOWN"] ?? "#9aa9c4";
                return (
                  <button
                    key={ex.exception_id}
                    onClick={() => {
                      setSelected(ex.exception_id);
                      setLookupId(null);
                    }}
                    className={cn(
                      "w-full text-left px-4 py-3 border-b border-border/50 hover:bg-card transition-colors border-l-2",
                      selected === ex.exception_id && !lookupId
                        ? "bg-card border-l-primary"
                        : "border-l-transparent"
                    )}
                  >
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-xs font-semibold font-mono">
                        {ex.exception_id.slice(0, 14)}…
                      </span>
                      <Badge
                        variant={STATUS_VARIANT[ex.status] ?? "outline"}
                        className="text-[9px]"
                      >
                        {ex.status}
                      </Badge>
                    </div>
                    <div
                      className="text-[10px] font-medium"
                      style={{ color: catColor }}
                    >
                      {(ex.category ?? "UNKNOWN").replace(/_/g, " ")}
                    </div>
                    <p className="text-[10px] text-muted-foreground mt-0.5 font-mono truncate">
                      Deal: {ex.deal_id.slice(0, 10)}…
                    </p>
                  </button>
                );
              })
            )}
          </ScrollArea>
        </div>

        {/* Right: detail */}
        <div className="flex-1 min-w-0 bg-popover border border-border rounded-xl overflow-hidden">
          {!activeExceptionId ? (
            <div className="flex flex-col items-center justify-center h-full py-16 gap-3 text-muted-foreground">
              <AlertTriangle className="w-10 h-10 opacity-20" />
              <p className="text-sm">Select an exception to view details</p>
            </div>
          ) : (
            <ExceptionDetail exceptionId={activeExceptionId} />
          )}
        </div>
      </div>
    </div>
  );
}
