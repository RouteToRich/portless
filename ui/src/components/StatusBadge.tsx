import { cn } from "@/lib/utils";
import type { DealStatus } from "@/api/types";

const DANGER: DealStatus[] = ["QC_FAILED", "DISPUTE", "CANCELLED", "NEGOTIATION_FAILED", "REFUNDED"];
const OK: DealStatus[] = ["PAID", "CLEARED", "QC_PASSED", "ESCROW_FUNDED", "NEGOTIATION_AGREED", "PI_ACCEPTED", "PO_ISSUED"];
const WARN: DealStatus[] = ["ESCROW_PENDING", "PI_SENT", "MATCHING", "NEGOTIATING", "SUPPLIER_DISPATCH_READY", "IN_TRANSIT"];

function statusLabel(status: DealStatus): string {
  return status.replace(/_/g, " ");
}

export function StatusBadge({ status }: { status: DealStatus }) {
  const isDanger = DANGER.includes(status);
  const isOk = OK.includes(status);
  const isWarn = WARN.includes(status);

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold border",
        isDanger && "bg-[rgba(255,93,108,0.15)] text-[#ff5d6c] border-[rgba(255,93,108,0.4)]",
        isOk && "bg-[rgba(46,194,138,0.15)] text-[#2ec28a] border-[rgba(46,194,138,0.4)]",
        isWarn && "bg-[rgba(245,166,35,0.15)] text-[#f5a623] border-[rgba(245,166,35,0.4)]",
        !isDanger && !isOk && !isWarn && "bg-[rgba(154,169,196,0.12)] text-muted-foreground border-border"
      )}
    >
      {statusLabel(status)}
    </span>
  );
}

export function HealthBadge({ value }: { value: string }) {
  const ok = value === "ok";
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold border",
        ok
          ? "bg-[rgba(46,194,138,0.15)] text-[#2ec28a] border-[rgba(46,194,138,0.4)]"
          : "bg-[rgba(255,93,108,0.15)] text-[#ff5d6c] border-[rgba(255,93,108,0.4)]"
      )}
    >
      {value}
    </span>
  );
}
