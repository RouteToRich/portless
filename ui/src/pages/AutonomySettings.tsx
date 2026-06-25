import { useState } from "react";
import { toast } from "sonner";
import { Settings2, Save, Info } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

type Level = "L0" | "L1" | "L2" | "L3";

const ACTION_CLASSES = [
  {
    key: "opportunity_scan",
    label: "Opportunity Scanning",
    description: "Auto-scan Comtrade/Volza/MacMap for arbitrage signals",
    def: "L3" as Level,
  },
  {
    key: "qc_booking",
    label: "QC Booking",
    description: "Book SGS/Intertek inspection for outbound shipments",
    def: "L3" as Level,
  },
  {
    key: "doc_generation",
    label: "Document Generation",
    description: "Generate PI, CI, PL, CoO, SDF, Shipping Bill documents",
    def: "L2" as Level,
  },
  {
    key: "rfq_dispatch",
    label: "RFQ Dispatch",
    description: "Send RFQ emails to verified suppliers via negotiation agent",
    def: "L2" as Level,
  },
  {
    key: "freight_booking",
    label: "Freight Booking",
    description: "Book Cogoport/Freightos freight for approved shipments",
    def: "L1" as Level,
  },
  {
    key: "negotiation_email",
    label: "Negotiation Emails",
    description: "Approve outbound negotiation emails before sending",
    def: "L1" as Level,
  },
  {
    key: "supplier_payout",
    label: "Supplier Payout",
    description: "Release Payoneer payout to supplier after QC pass",
    def: "L0" as Level,
  },
  {
    key: "po_issuance",
    label: "PO Issuance",
    description: "Issue Purchase Order to supplier after PI acceptance",
    def: "L0" as Level,
  },
];

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
  const [config, setConfig] = useState<Record<string, Level>>(
    Object.fromEntries(ACTION_CLASSES.map((a) => [a.key, a.def]))
  );
  const [saved, setSaved] = useState(false);

  function handleSave() {
    // Backend endpoint not yet implemented — local only
    setSaved(true);
    toast.success("Autonomy profile saved", {
      description: "Changes are local until backend support is added",
    });
    setTimeout(() => setSaved(false), 2000);
  }

  function handleReset() {
    setConfig(Object.fromEntries(ACTION_CLASSES.map((a) => [a.key, a.def])));
    toast.info("Reset to defaults");
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
              Configure per-action autonomy levels for the AI agent
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" className="h-8 text-xs" onClick={handleReset}>
            Reset
          </Button>
          <Button size="sm" className="h-8 gap-1.5 text-xs" onClick={handleSave}>
            <Save className="w-3.5 h-3.5" />
            {saved ? "Saved!" : "Save"}
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
        </CardContent>
      </Card>

      <Separator />

      {/* Action class table */}
      <div className="space-y-2">
        <p className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
          Action Classes
        </p>
        {ACTION_CLASSES.map((action) => {
          const current = config[action.key] as Level;
          return (
            <Card key={action.key}>
              <CardContent className="flex items-center gap-4 pt-4">
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-semibold">{action.label}</p>
                  <p className="text-xs text-muted-foreground mt-0.5">{action.description}</p>
                </div>
                <div className="flex items-center gap-1 shrink-0 w-[200px] bg-secondary rounded-lg border border-border p-1">
                  {LEVELS.map((level) => (
                    <LevelChip
                      key={level.value}
                      level={level}
                      selected={current === level.value}
                      onClick={() =>
                        setConfig((c) => ({ ...c, [action.key]: level.value }))
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
              const count = ACTION_CLASSES.filter((a) => config[a.key] === level.value).length;
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
