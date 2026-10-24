import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { Settings2, Save, Info, RotateCcw, ShieldAlert, Clock, DollarSign, Users } from "lucide-react";
import { cn, fmtUSD } from "@/lib/utils";
import { useAuth } from "@/lib/auth";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useAutonomyPolicy, useSaveAutonomyPolicy } from "@/api/hooks";
import type { AutonomyActionPolicy, GateOverride, OnTimeout } from "@/api/endpoints";
import type { AutonomyLevel } from "@/api/types";

type Level = AutonomyLevel;

const LEVELS: { value: Level; label: string; hint: string; color: string }[] = [
  {
    value: "L0",
    label: "L0",
    hint: "Always Ask — waits indefinitely for human approval",
    color: "#ff5d6c",
  },
  {
    value: "L1",
    label: "L1",
    hint: "Ask with Timeout — auto-resolves with the on-timeout default after expiry",
    color: "#f5a623",
  },
  {
    value: "L2",
    label: "L2",
    hint: "Auto + Notify — proceeds, informs operator after",
    color: "#4f8cff",
  },
  {
    value: "L3",
    label: "L3",
    hint: "Fully Autonomous — acts and logs only",
    color: "#2ec28a",
  },
];

const SOURCE_LABEL: Record<string, string> = {
  override: "saved override",
  env: "environment",
  default: "built-in default",
  money_ceiling: "capped by money ceiling",
  value_threshold: "value threshold",
};

/** Pending per-action edits. `undefined` = untouched; `null` = clear the override. */
interface GateEdit {
  level?: Level | null;
  timeout_seconds?: number | null;
  default_on_timeout?: OnTimeout | null;
  auto_approve_below_usd?: number | null;
}

function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds % 86400 === 0) return `${seconds / 86400} d`;
  if (seconds % 3600 === 0) return `${seconds / 3600} h`;
  if (seconds % 60 === 0) return `${seconds / 60} min`;
  return `${seconds} s`;
}

function LevelChip({
  level,
  selected,
  onClick,
}: {
  level: (typeof LEVELS)[number];
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          onClick={onClick}
          className={cn(
            "flex-1 py-1.5 text-xs font-semibold rounded-md border transition-all",
            selected
              ? "border-current text-white"
              : "border-border text-muted-foreground hover:border-current"
          )}
          style={
            selected
              ? { background: level.color, borderColor: level.color, color: "#fff" }
              : { color: level.color, borderColor: "transparent" }
          }
        >
          {level.label}
        </button>
      </TooltipTrigger>
      <TooltipContent>{level.hint}</TooltipContent>
    </Tooltip>
  );
}

/** Timeout editor: hours input (fractions allowed) → seconds. */
function TimeoutField({
  seconds,
  defaultSeconds,
  isOverride,
  onChange,
}: {
  seconds: number;
  defaultSeconds: number;
  isOverride: boolean;
  onChange: (seconds: number | null) => void;
}) {
  const hours = Math.round((seconds / 3600) * 100) / 100;
  return (
    <div className="flex items-center gap-1.5">
      <Clock className="w-3.5 h-3.5 text-muted-foreground" />
      <Input
        type="number"
        min={0.1}
        step={0.5}
        value={hours}
        onChange={(e) => {
          const v = Number(e.target.value);
          if (!Number.isFinite(v) || v <= 0) return;
          onChange(Math.round(v * 3600));
        }}
        className="h-7 w-20 text-xs"
        title="L1 timeout in hours"
      />
      <span className="text-[10px] text-muted-foreground">h</span>
      {isOverride && (
        <button
          className="text-[10px] text-muted-foreground underline"
          onClick={() => onChange(null)}
          title={`Back to the default (${fmtDuration(defaultSeconds)})`}
        >
          reset
        </button>
      )}
    </div>
  );
}

export function AutonomySettings() {
  const { user } = useAuth();
  const { data, isLoading, isError, refetch } = useAutonomyPolicy();
  const save = useSaveAutonomyPolicy();
  const [edits, setEdits] = useState<Record<string, GateEdit>>({});
  const [moneyThreshold, setMoneyThreshold] = useState<string>("");
  const [moneyThresholdDirty, setMoneyThresholdDirty] = useState(false);

  const policy: AutonomyActionPolicy[] = data?.actions ?? [];
  const moneyCeiling = data?.money_ceiling ?? "L1";

  useEffect(() => {
    if (data && !moneyThresholdDirty) {
      setMoneyThreshold(data.money_auto_approve_below_usd != null ? String(data.money_auto_approve_below_usd) : "");
    }
  }, [data, moneyThresholdDirty]);

  function edit(action: string, patch: GateEdit) {
    setEdits((e) => ({ ...e, [action]: { ...(e[action] ?? {}), ...patch } }));
  }

  // Shown values per action: pending edit → saved override → env → default.
  const shown = useMemo(() => {
    const out: Record<string, { level: Level; timeout: number; onTimeout: OnTimeout; threshold: number | null }> = {};
    for (const a of policy) {
      const e = edits[a.action] ?? {};
      const level: Level =
        e.level !== undefined ? (e.level ?? a.env ?? a.default) : a.override ?? a.env ?? a.default;
      const timeout =
        e.timeout_seconds !== undefined
          ? (e.timeout_seconds ?? a.default_timeout_seconds)
          : a.timeout_seconds;
      const onTimeout: OnTimeout =
        e.default_on_timeout !== undefined
          ? (e.default_on_timeout ?? a.default_on_timeout)
          : a.default_on_timeout;
      const threshold =
        e.auto_approve_below_usd !== undefined ? e.auto_approve_below_usd : a.auto_approve_below_usd;
      out[a.action] = { level, timeout, onTimeout, threshold };
    }
    return out;
  }, [policy, edits]);

  const dirty = Object.keys(edits).length > 0 || moneyThresholdDirty;

  async function handleSave() {
    if (!dirty) return;
    const levels: Record<string, Level | null> = {};
    const gates: Record<string, GateOverride | null> = {};
    for (const [action, e] of Object.entries(edits)) {
      if (e.level !== undefined) levels[action] = e.level;
      const gate: GateOverride = {};
      if (e.timeout_seconds !== undefined) gate.timeout_seconds = e.timeout_seconds;
      if (e.default_on_timeout !== undefined) gate.default_on_timeout = e.default_on_timeout;
      if (e.auto_approve_below_usd !== undefined) gate.auto_approve_below_usd = e.auto_approve_below_usd;
      if (Object.keys(gate).length) gates[action] = gate;
    }
    const parsed = moneyThreshold.trim() === "" ? null : Number(moneyThreshold);
    if (moneyThresholdDirty && parsed != null && (!Number.isFinite(parsed) || parsed < 0)) {
      toast.error("Money threshold must be a positive number (or empty to disable)");
      return;
    }
    try {
      await save.mutateAsync({
        levels,
        gates,
        money_auto_approve_below_usd: moneyThresholdDirty ? parsed : null,
        set_money_threshold: moneyThresholdDirty,
        updated_by: user?.email ?? "operator",
      });
      setEdits({});
      setMoneyThresholdDirty(false);
      toast.success("Autonomy policy saved", {
        description: "New approvals are created with the updated levels, timeouts and thresholds immediately.",
      });
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      toast.error("Save failed", { description: detail ?? (e as Error).message });
    }
  }

  function handleResetDefaults() {
    // Clear every saved override (env + built-in defaults apply again).
    const cleared: Record<string, GateEdit> = {};
    for (const a of policy) {
      const e: GateEdit = {};
      if (a.override) e.level = null;
      if (a.timeout_override != null) e.timeout_seconds = null;
      if (a.on_timeout_override) e.default_on_timeout = null;
      if (a.threshold_override != null) e.auto_approve_below_usd = null;
      if (Object.keys(e).length) cleared[a.action] = e;
    }
    setEdits(cleared);
    toast.info("Overrides cleared — press Save to apply");
  }

  const moneyActions = policy.filter((a) => a.money_moving);

  return (
    <div className="space-y-4 max-w-4xl">
      {/* Header */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex items-center justify-center w-9 h-9 rounded-xl bg-[#4f8cff]/10">
            <Settings2 className="w-5 h-5 text-[#4f8cff]" />
          </div>
          <div>
            <h1 className="text-lg font-bold">Autonomy Settings</h1>
            <p className="text-xs text-muted-foreground">
              Per-action autonomy level, L1 timeout, on-timeout default and value threshold — L2 on
              every row runs a deal end-to-end without an operator; money movement stays capped at{" "}
              <span className="font-semibold">{moneyCeiling}</span>
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            className="h-8 gap-1.5 text-xs"
            onClick={handleResetDefaults}
            disabled={isLoading}
          >
            <RotateCcw className="w-3.5 h-3.5" />
            Reset
          </Button>
          <Button
            size="sm"
            className="h-8 gap-1.5 text-xs"
            onClick={handleSave}
            disabled={!dirty || save.isPending || isLoading}
          >
            <Save className="w-3.5 h-3.5" />
            {save.isPending ? "Saving…" : dirty ? "Save" : "Saved"}
          </Button>
        </div>
      </div>

      {/* Level legend */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Info className="w-4 h-4 text-muted-foreground" />
            Autonomy Level Guide
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-3">
            {LEVELS.map((l) => (
              <div
                key={l.value}
                className="flex items-start gap-3 p-3 rounded-lg border"
                style={{ borderColor: `${l.color}30`, background: `${l.color}0d` }}
              >
                <Badge
                  className="text-xs shrink-0 mt-0.5"
                  style={{
                    background: `${l.color}20`,
                    color: l.color,
                    border: `1px solid ${l.color}40`,
                  }}
                >
                  {l.label}
                </Badge>
                <p className="text-xs text-muted-foreground leading-relaxed">{l.hint}</p>
              </div>
            ))}
          </div>
          <div className="text-xs text-muted-foreground mt-3 space-y-1.5">
            <p className="flex items-start gap-1.5">
              <ShieldAlert className="w-3.5 h-3.5 mt-0.5 shrink-0" />
              <span>
                Supplier payout, escrow tranche release and buyer-acceptance relay never run above the
                money ceiling (<code>AUTONOMY_MONEY_CEILING</code>, currently {moneyCeiling}); degraded
                or placeholder email drafts and the first outbound negotiation email always stay at L0.
              </span>
            </p>
            <p className="flex items-start gap-1.5">
              <DollarSign className="w-3.5 h-3.5 mt-0.5 shrink-0" />
              <span>
                A <b>value threshold</b> auto-approves a gate below that amount <i>only when its
                preconditions hold</i> — proof references attached, ledger reconciled, verified
                supplier inside the price corridor. Anything above it, or with a precondition missing,
                waits for a human.
              </span>
            </p>
            <p className="flex items-start gap-1.5">
              <Users className="w-3.5 h-3.5 mt-0.5 shrink-0" />
              <span>
                Money gates at or above <code>FOUR_EYES_THRESHOLD_USD</code> need <b>two different
                operators</b> to approve — the first approval keeps the gate pending and the second
                person countersigns in the Approvals Inbox. No threshold or level lifts that.
              </span>
            </p>
          </div>
        </CardContent>
      </Card>

      {/* Global money threshold */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <DollarSign className="w-4 h-4 text-[#f5a623]" />
            Unattended money threshold
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <div className="flex items-center gap-3 flex-wrap">
            <Input
              type="number"
              min={0}
              step={500}
              placeholder="disabled — every money gate waits for a human"
              value={moneyThreshold}
              onChange={(e) => {
                setMoneyThreshold(e.target.value);
                setMoneyThresholdDirty(true);
              }}
              className="h-8 w-72 text-xs"
            />
            <span className="text-xs text-muted-foreground">USD</span>
            {moneyThreshold.trim() !== "" && (
              <button
                className="text-[10px] text-muted-foreground underline"
                onClick={() => {
                  setMoneyThreshold("");
                  setMoneyThresholdDirty(true);
                }}
              >
                disable
              </button>
            )}
          </div>
          <p className="text-[11px] text-muted-foreground">
            Applies to {moneyActions.map((a) => a.label).join(", ") || "money-moving actions"} unless a
            per-action threshold below overrides it. Tranche releases need their proof references and a
            reconciled ledger; supplier payouts need the ledger reconciled. The money ceiling ({moneyCeiling})
            still caps the <i>level</i> — the threshold is the only way a money gate resolves without a click.
          </p>
        </CardContent>
      </Card>

      <Separator />

      {/* Action class table */}
      <div className="space-y-2">
        <p className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
          Action Classes
        </p>
        {isError && (
          <Card>
            <CardContent className="pt-4 text-sm text-destructive">
              Could not load the autonomy policy{" "}
              <button className="underline" onClick={() => void refetch()}>
                retry
              </button>
            </CardContent>
          </Card>
        )}
        {isLoading &&
          [0, 1, 2, 3].map((i) => (
            <Card key={i}>
              <CardContent className="pt-4">
                <Skeleton className="h-10 w-full" />
              </CardContent>
            </Card>
          ))}
        {!isLoading &&
          policy.map((action) => {
            const cur = shown[action.action];
            const pending = edits[action.action] !== undefined;
            const timeoutIsOverride =
              edits[action.action]?.timeout_seconds !== undefined
                ? edits[action.action]?.timeout_seconds != null
                : action.timeout_override != null;
            const thresholdIsOverride =
              edits[action.action]?.auto_approve_below_usd !== undefined
                ? edits[action.action]?.auto_approve_below_usd != null
                : action.threshold_override != null;
            return (
              <Card key={action.action} className={cn(pending && "border-[#4f8cff]/60")}>
                <CardContent className="pt-4 space-y-3">
                  <div className="flex items-center gap-4">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <p className="text-sm font-semibold">{action.label}</p>
                        <span className="text-[10px] font-mono text-muted-foreground">{action.action}</span>
                        {action.money_moving && (
                          <Badge variant="outline" className="text-[10px]">
                            money
                          </Badge>
                        )}
                        {action.follows_deal_profile && (
                          <Badge variant="outline" className="text-[10px]">
                            per-deal profile
                          </Badge>
                        )}
                      </div>
                      <p className="text-xs text-muted-foreground mt-0.5">{action.description}</p>
                      <p className="text-[11px] text-muted-foreground mt-1">
                        effective <span className="font-semibold">{action.effective}</span> ·{" "}
                        {SOURCE_LABEL[action.source] ?? action.source}
                        {pending && " · unsaved change"}
                      </p>
                    </div>
                    <div className="flex items-center gap-1 shrink-0 w-[200px] bg-secondary rounded-lg border border-border p-1">
                      {LEVELS.map((level) => (
                        <LevelChip
                          key={level.value}
                          level={level}
                          selected={cur.level === level.value}
                          onClick={() => edit(action.action, { level: level.value })}
                        />
                      ))}
                    </div>
                  </div>

                  {/* v2 gate settings */}
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-3 rounded-lg bg-secondary/60 border border-border p-2.5">
                    <div className="space-y-1">
                      <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
                        L1 timeout{" "}
                        <span className="normal-case">(default {fmtDuration(action.default_timeout_seconds)})</span>
                      </p>
                      <TimeoutField
                        seconds={cur.timeout}
                        defaultSeconds={action.default_timeout_seconds}
                        isOverride={timeoutIsOverride}
                        onChange={(s) => edit(action.action, { timeout_seconds: s })}
                      />
                    </div>
                    <div className="space-y-1">
                      <p className="text-[10px] uppercase tracking-wide text-muted-foreground">On timeout</p>
                      <div className="flex items-center gap-1">
                        {(["APPROVE", "REJECT"] as OnTimeout[]).map((v) => (
                          <button
                            key={v}
                            onClick={() => edit(action.action, { default_on_timeout: v })}
                            className={cn(
                              "px-2 py-1 text-[11px] font-semibold rounded-md border",
                              cur.onTimeout === v
                                ? v === "APPROVE"
                                  ? "bg-[#2ec28a]/15 border-[#2ec28a]/50 text-[#2ec28a]"
                                  : "bg-[#ff5d6c]/15 border-[#ff5d6c]/50 text-[#ff5d6c]"
                                : "border-border text-muted-foreground hover:border-current"
                            )}
                          >
                            auto-{v.toLowerCase()}
                          </button>
                        ))}
                        <span className="text-[10px] text-muted-foreground ml-1">
                          when an L1 gate expires
                        </span>
                      </div>
                    </div>
                    <div className="space-y-1">
                      <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
                        Auto-approve below (USD)
                      </p>
                      <div className="flex items-center gap-1.5">
                        <Input
                          type="number"
                          min={0}
                          step={500}
                          placeholder={action.money_moving ? "global threshold" : "no threshold"}
                          value={cur.threshold ?? ""}
                          onChange={(e) => {
                            const raw = e.target.value;
                            edit(action.action, {
                              auto_approve_below_usd: raw.trim() === "" ? null : Number(raw),
                            });
                          }}
                          className="h-7 w-28 text-xs"
                        />
                        {thresholdIsOverride && (
                          <button
                            className="text-[10px] text-muted-foreground underline"
                            onClick={() => edit(action.action, { auto_approve_below_usd: null })}
                          >
                            reset
                          </button>
                        )}
                        {cur.threshold != null && (
                          <span className="text-[10px] text-muted-foreground">
                            ≤ {fmtUSD(cur.threshold)} with preconditions
                          </span>
                        )}
                      </div>
                    </div>
                  </div>
                </CardContent>
              </Card>
            );
          })}
      </div>

      {/* Summary */}
      <Card>
        <CardContent className="pt-4">
          <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-3">
            Profile Summary
          </p>
          <div className="flex flex-wrap gap-2">
            {LEVELS.map((level) => {
              const count = policy.filter((a) => shown[a.action]?.level === level.value).length;
              return (
                <div
                  key={level.value}
                  className="flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs"
                  style={{ borderColor: `${level.color}40`, background: `${level.color}10` }}
                >
                  <span className="font-bold" style={{ color: level.color }}>
                    {level.label}
                  </span>
                  <span className="text-muted-foreground">
                    {count} action{count !== 1 ? "s" : ""}
                  </span>
                </div>
              );
            })}
            {policy.some((a) => shown[a.action]?.threshold != null) && (
              <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-[#f5a623]/40 bg-[#f5a623]/10 text-xs">
                <DollarSign className="w-3.5 h-3.5 text-[#f5a623]" />
                <span className="text-muted-foreground">
                  {policy.filter((a) => shown[a.action]?.threshold != null).length} with a value threshold
                </span>
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
