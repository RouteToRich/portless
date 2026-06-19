import type { DealStatus } from "../api/types";

const DANGER: DealStatus[] = ["QC_FAILED", "DISPUTE", "CANCELLED", "NEGOTIATION_FAILED"];
const OK: DealStatus[] = ["PAID", "CLEARED", "QC_PASSED", "ESCROW_FUNDED", "NEGOTIATION_AGREED"];

export function StatusBadge({ status }: { status: DealStatus }) {
  const cls = DANGER.includes(status) ? "danger" : OK.includes(status) ? "ok" : "neutral";
  return <span className={`badge ${cls}`}>{status}</span>;
}

export function HealthBadge({ value }: { value: string }) {
  const ok = value === "ok";
  return <span className={`badge ${ok ? "ok" : "danger"}`}>{value}</span>;
}
