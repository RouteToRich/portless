import { cn } from "@/lib/utils";
import type { DealStatus } from "@/api/types";
import {
  Check,
  X,
  FileText,
  Handshake,
  Lock,
  PackageCheck,
  Ship,
  CheckCircle2,
  AlertCircle,
  Clock,
  Banknote,
  ShieldCheck,
  TruckIcon,
  FileCheck,
  Zap,
} from "lucide-react";

interface Stage {
  status: DealStatus;
  label: string;
  icon: React.ElementType;
}

const STAGES: Stage[] = [
  { status: "DRAFT",                   label: "Draft",          icon: FileText },
  { status: "MATCHING",                label: "Matching",       icon: Zap },
  { status: "NEGOTIATING",             label: "Negotiating",    icon: Handshake },
  { status: "NEGOTIATION_AGREED",      label: "Agreed",         icon: CheckCircle2 },
  { status: "PI_SENT",                 label: "PI Sent",        icon: FileText },
  { status: "PI_ACCEPTED",             label: "PI Accepted",    icon: FileCheck },
  { status: "ESCROW_PENDING",          label: "Escrow Pending", icon: Clock },
  { status: "ESCROW_FUNDED",           label: "Funded",         icon: Lock },
  { status: "PO_ISSUED",              label: "PO Issued",      icon: FileText },
  { status: "SUPPLIER_DISPATCH_READY", label: "Dispatch Ready", icon: PackageCheck },
  { status: "QC_PASSED",              label: "QC Passed",      icon: ShieldCheck },
  { status: "IN_TRANSIT",             label: "In Transit",     icon: TruckIcon },
  { status: "CLEARED",               label: "Cleared",        icon: Ship },
  { status: "PAID",                  label: "Paid",           icon: Banknote },
];

const TERMINAL_STATUSES: DealStatus[] = [
  "NEGOTIATION_FAILED", "QC_FAILED", "DISPUTE", "CANCELLED", "REFUNDED",
];

const STATUS_ORDER = STAGES.map((s) => s.status);

function getStageIndex(status: DealStatus): number {
  return STATUS_ORDER.indexOf(status);
}

interface DealPipelineProps {
  status: DealStatus;
  compact?: boolean;
}

export function DealPipeline({ status, compact = false }: DealPipelineProps) {
  const isTerminal = TERMINAL_STATUSES.includes(status);
  const currentIdx = getStageIndex(status);

  if (compact) {
    const total = STAGES.length;
    const done = Math.max(0, currentIdx);
    const pct = Math.round((done / (total - 1)) * 100);

    return (
      <div className="flex items-center gap-2">
        <div
          className="flex-1 h-1.5 rounded-full overflow-hidden"
          style={{ background: "var(--progress-track)" }}
        >
          <div
            className="h-full rounded-full transition-all duration-500"
            style={{
              width: `${pct}%`,
              background: isTerminal
                ? "var(--step-fail)"
                : status === "PAID"
                ? "var(--step-done)"
                : "var(--step-current)",
            }}
          />
        </div>
        <span className="text-[10px] text-muted-foreground font-mono w-8 text-right">{pct}%</span>
      </div>
    );
  }

  return (
    <div className="relative overflow-x-auto scrollbar-thin pb-3">
      <div className="flex items-start gap-0 min-w-max px-2">
        {STAGES.map((stage, idx) => {
          const isDone = !isTerminal && currentIdx > idx;
          const isCurrent = !isTerminal && currentIdx === idx;
          const isFuture = !isTerminal && currentIdx < idx;
          const isFailPoint = isTerminal && currentIdx === idx;
          const Icon = stage.icon;

          const nodeStyle = isDone
            ? { background: "var(--step-done-bg)", borderColor: "var(--step-done)", color: "var(--step-done)" }
            : isCurrent
            ? { background: "var(--step-current-bg)", borderColor: "var(--step-current)", color: "var(--step-current)" }
            : isFuture
            ? { background: "var(--step-future-bg)", borderColor: "var(--step-future)", color: "var(--step-future)" }
            : isFailPoint
            ? { background: "var(--step-fail-bg)", borderColor: "var(--step-fail)", color: "var(--step-fail)" }
            : {};

          const labelColor = isDone
            ? "var(--step-done)"
            : isCurrent
            ? "var(--step-current)"
            : isFuture
            ? "var(--step-future)"
            : isFailPoint
            ? "var(--step-fail)"
            : "inherit";

          return (
            <div key={stage.status} className="flex items-center">
              {/* Node */}
              <div className="flex flex-col items-center gap-1.5">
                <div className="relative">
                  <div
                    className={cn(
                      "w-8 h-8 rounded-full flex items-center justify-center border-2 transition-all duration-300",
                      isCurrent && "animate-pulse-ring"
                    )}
                    style={nodeStyle}
                  >
                    {isDone ? (
                      <Check className="w-3.5 h-3.5" />
                    ) : isFailPoint ? (
                      <X className="w-3.5 h-3.5" />
                    ) : (
                      <Icon className="w-3.5 h-3.5" />
                    )}
                  </div>
                  {isCurrent && (
                    <span
                      className="absolute inset-0 rounded-full border-2 animate-ping opacity-40"
                      style={{ borderColor: "var(--step-current)" }}
                    />
                  )}
                </div>

                <span
                  className="text-[10px] text-center max-w-[64px] leading-tight font-medium whitespace-nowrap"
                  style={{ color: labelColor, fontWeight: isCurrent ? 600 : undefined }}
                >
                  {stage.label}
                </span>
              </div>

              {/* Connector line */}
              {idx < STAGES.length - 1 && (
                <div
                  className="w-10 h-0.5 mx-0.5 mt-[-18px] transition-all duration-300"
                  style={{
                    background:
                      idx < currentIdx && !isTerminal
                        ? "var(--connector-done)"
                        : "var(--connector-future)",
                  }}
                />
              )}
            </div>
          );
        })}

        {/* Terminal indicator */}
        {isTerminal && (
          <div className="flex items-center ml-1">
            <div
              className="w-10 h-0.5 mx-0.5 mt-[-18px]"
              style={{ background: "var(--step-fail)" }}
            />
            <div className="flex flex-col items-center gap-1.5">
              <div
                className="w-8 h-8 rounded-full flex items-center justify-center border-2"
                style={{
                  background: "var(--step-fail-bg)",
                  borderColor: "var(--step-fail)",
                  color: "var(--step-fail)",
                }}
              >
                <AlertCircle className="w-3.5 h-3.5" />
              </div>
              <span
                className="text-[10px] font-semibold whitespace-nowrap"
                style={{ color: "var(--step-fail)" }}
              >
                {status.replace(/_/g, " ")}
              </span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
