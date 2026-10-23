import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { Settings2, Save, Info, RotateCcw, ShieldAlert } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { AutonomyApi, type AutonomyActionPolicy } from "@/api/endpoints";
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
    hint: "Ask with Timeout — auto-resolves after expiry",
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

const SOURCE_LABEL: Record<AutonomyActionPolicy["source"], string> = {
  override: "saved override",
  env: "environment",
  default: "built-in default",
  money_ceiling: "capped by money ceiling",
};

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

export function AutonomySettings() {
  const [policy, setPolicy] = useState<AutonomyActionPolicy[]>([]);
  const [moneyCeiling, setMoneyCeiling] = useState<Level>("L1");
  // Pending edits: action → level (or null = clear the override)
  const [edits, setEdits] = useState<Record<string, Level | null>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const res = await AutonomyApi.get();
      setPolicy(res.actions);
      setMoneyCeiling(res.money_ceiling);
      setEdits({});
    } catch (e) {
      setError((e as Error).message ?? "Could not load the autonomy policy");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  // Level shown per action: pending edit → saved override → env → default.
  const shown = useMemo(() => {
    const out: Record<string, Level> = {};
    for (const a of policy) {
      const pending = edits[a.action];
      out[a.action] =
        pending !== undefined
          ? (pending ?? a.env ?? a.default)
          : a.override ?? a.env ?? a.default;
    }
    return out;
  }, [policy, edits]);

  const dirty = Object.keys(edits).length > 0;

  async function handleSave() {
    if (!dirty) return;
    setSaving(true);
    try {
      const res = await AutonomyApi.save(edits);
      setPolicy(res.actions);
      setMoneyCeiling(res.money_ceiling);
      setEdits({});
      toast.success("Autonomy policy saved", {
        description: "New approvals are created at the updated levels immediately.",
      });
    } catch (e) {
      toast.error("Save failed", { description: (e as Error).message });
    } finally {
      setSaving(false);
    }
  }

  function handleResetDefaults() {
    // Clear every saved override (env + built-in defaults apply again).
    const cleared: Record<string, Level | null> = {};
    for (const a of policy) if (a.override) cleared[a.action] = null;
    setEdits(cleared);
    toast.info("Overrides cleared — press Save to apply");
  }

  return (
    <div className="space-y-4 max-w-3xl">
      {/* Header */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex items-center justify-center w-9 h-9 rounded-xl bg-[#4f8cff]/10">
            <Settings2 className="w-5 h-5 text-[#4f8cff]" />
          </div>
          <div>
            <h1 className="text-lg font-bold">Autonomy Settings</h1>
            <p className="text-xs text-muted-foreground">
              Per-action autonomy levels for the pipeline — L2 on every row runs a deal
              end-to-end without an operator; money movement stays capped at{" "}
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
            disabled={loading}
          >
            <RotateCcw className="w-3.5 h-3.5" />
            Reset
          </Button>
          <Button
            size="sm"
            className="h-8 gap-1.5 text-xs"
            onClick={handleSave}
            disabled={!dirty || saving || loading}
          >
            <Save className="w-3.5 h-3.5" />
            {saving ? "Saving…" : dirty ? "Save" : "Saved"}
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
          <p className="text-xs text-muted-foreground mt-3 flex items-center gap-1.5">
            <ShieldAlert className="w-3.5 h-3.5" />
            Supplier payout and escrow tranche release never run above the money ceiling
            (<code>AUTONOMY_MONEY_CEILING</code>, currently {moneyCeiling}); degraded or
            placeholder email drafts and the first outbound negotiation email always stay
            at L0.
          </p>
        </CardContent>
      </Card>

      <Separator />

      {/* Action class table */}
      <div className="space-y-2">
        <p className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
          Action Classes
        </p>
        {error && (
          <Card>
            <CardContent className="pt-4 text-sm text-destructive">
              {error}{" "}
              <button className="underline" onClick={() => void load()}>
                retry
              </button>
            </CardContent>
          </Card>
        )}
        {loading &&
          [0, 1, 2, 3].map((i) => (
            <Card key={i}>
              <CardContent className="pt-4">
                <Skeleton className="h-10 w-full" />
              </CardContent>
            </Card>
          ))}
        {!loading &&
          policy.map((action) => {
            const current = shown[action.action];
            const pending = edits[action.action] !== undefined;
            return (
              <Card key={action.action} className={cn(pending && "border-[#4f8cff]/60")}>
                <CardContent className="flex items-center gap-4 pt-4">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <p className="text-sm font-semibold">{action.label}</p>
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
                      {SOURCE_LABEL[action.source]}
                      {pending && " · unsaved change"}
                    </p>
                  </div>
                  <div className="flex items-center gap-1 shrink-0 w-[200px] bg-secondary rounded-lg border border-border p-1">
                    {LEVELS.map((level) => (
                      <LevelChip
                        key={level.value}
                        level={level}
                        selected={current === level.value}
                        onClick={() =>
                          setEdits((e) => ({ ...e, [action.action]: level.value }))
                        }
                      />
                    ))}
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
              const count = policy.filter((a) => shown[a.action] === level.value).length;
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
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
