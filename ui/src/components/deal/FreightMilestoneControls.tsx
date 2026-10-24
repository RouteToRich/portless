import { useState } from "react";
import { toast } from "sonner";
import { MapPin, RadioTower, PackageCheck } from "lucide-react";
import { cn, fmtDateTime } from "@/lib/utils";
import { useFreightMilestones } from "@/api/hooks";
import type { FreightMilestone, FreightRecord } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

const MILESTONES: { value: FreightMilestone; label: string; hint: string }[] = [
  { value: "DEPARTED", label: "Departed", hint: "Loaded / sailed from the port of loading" },
  { value: "IN_TRANSIT", label: "In transit", hint: "At sea / transhipment" },
  { value: "ARRIVED", label: "Arrived", hint: "Discharged at destination — deal → CLEARED" },
  { value: "CLEARED", label: "Customs cleared", hint: "Released by import customs — deal → CLEARED" },
  { value: "DELIVERED", label: "Delivered", hint: "At the buyer — opens the acceptance window for the final tranche" },
];

const TRACK_STATUS_TEXT: Record<string, string> = {
  ok: "Milestone recorded from the carrier",
  unchanged: "No new milestone — still where it was",
  stub: "Carrier-tracking actor not configured (APIFY_TOKEN / container-tracking-ai) — record the milestone by hand or wait for the fallback window",
  no_booking: "No freight booking on this deal",
  no_reference: "Booking has no container number or carrier reference to track",
  no_event: "The carrier returned no event for this reference yet",
  error: "Tracking call failed",
  ignored: "Carrier reported an older milestone than the one already recorded",
};

export function FreightMilestoneControls({ dealId, freight }: { dealId: string; freight: FreightRecord }) {
  const { record, poll } = useFreightMilestones(dealId);
  const [open, setOpen] = useState(false);
  const [milestone, setMilestone] = useState<FreightMilestone>("DELIVERED");
  const [location, setLocation] = useState("");
  const [notes, setNotes] = useState("");
  const [lastPoll, setLastPoll] = useState<string | null>(null);

  async function submit() {
    try {
      const res = await record.mutateAsync({
        tpoId: freight.tpo_id,
        payload: { milestone, location: location.trim() || null, notes: notes.trim() || null, source: "operator" },
      });
      if (res.status === "ignored") {
        toast.info(`Ignored — already at ${res.last_milestone ?? "a later milestone"}`);
      } else {
        toast.success(`${milestone} recorded`, {
          description:
            milestone === "DELIVERED"
              ? "The buyer's acceptance window opens on the escrow (once the proof tranches are released)."
              : milestone === "ARRIVED" || milestone === "CLEARED"
                ? "Deal moves to CLEARED; the T3 tranche gate follows."
                : undefined,
        });
      }
      setOpen(false);
      setLocation("");
      setNotes("");
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      toast.error(detail ?? "Could not record the milestone");
    }
  }

  async function track() {
    try {
      const res = await poll.mutateAsync();
      const text = TRACK_STATUS_TEXT[res.status] ?? res.status;
      setLastPoll(`${res.status}${res.milestone ? ` → ${res.milestone}` : ""}`);
      if (res.status === "ok") toast.success(text, { description: res.milestone });
      else if (res.status === "error") toast.error(text, { description: res.error });
      else toast.info(text);
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      toast.error(detail ?? "Tracking poll failed");
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-3 text-[11px] text-muted-foreground flex-wrap">
        {freight.container_number && (
          <span className="font-mono">Container {freight.container_number}</span>
        )}
        {freight.last_milestone && (
          <span>
            last milestone <b className="text-foreground">{freight.last_milestone}</b>
            {freight.last_milestone_at && ` · ${fmtDateTime(freight.last_milestone_at)}`}
          </span>
        )}
        {freight.delivered_at && (
          <span className="flex items-center gap-1 text-[#2ec28a]">
            <PackageCheck className="w-3 h-3" /> delivered {fmtDateTime(freight.delivered_at)}
          </span>
        )}
        {lastPoll && <span>tracking: {lastPoll}</span>}
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <Button variant="outline" size="sm" className="h-7 text-xs gap-1.5" onClick={() => setOpen(true)}>
          <MapPin className="w-3.5 h-3.5" /> Record milestone
        </Button>
        <Button variant="ghost" size="sm" className="h-7 text-xs gap-1.5" onClick={track} disabled={poll.isPending}
          title="Ask the carrier-tracking actor where the container is">
          <RadioTower className={cn("w-3.5 h-3.5", poll.isPending && "animate-pulse")} />
          {poll.isPending ? "Polling…" : "Poll carrier tracking"}
        </Button>
        <span className="text-[10px] text-muted-foreground">
          DELIVERED opens the buyer's acceptance window; the watcher polls every 12 h and falls back after 21 days.
        </span>
      </div>

      <Dialog open={open} onOpenChange={(o) => !o && setOpen(false)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Record a carrier milestone</DialogTitle>
            <DialogDescription>
              For {freight.provider} booking {freight.bl_number ?? freight.tpo_id}. Milestones only move
              forward — an earlier one than the current {freight.last_milestone ?? "state"} is ignored.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="grid grid-cols-1 gap-1.5">
              {MILESTONES.map((m) => (
                <button
                  key={m.value}
                  onClick={() => setMilestone(m.value)}
                  className={cn(
                    "text-left rounded-lg border px-3 py-2",
                    milestone === m.value ? "border-primary bg-primary/10" : "border-border hover:border-primary/40"
                  )}
                >
                  <p className="text-xs font-semibold">{m.label}</p>
                  <p className="text-[10px] text-muted-foreground">{m.hint}</p>
                </button>
              ))}
            </div>
            <div className="space-y-1">
              <Label className="text-[11px]">Location</Label>
              <Input value={location} onChange={(e) => setLocation(e.target.value)} placeholder="Hamburg CTA / buyer warehouse" className="h-8 text-xs" />
            </div>
            <div className="space-y-1">
              <Label className="text-[11px]">Notes</Label>
              <Textarea value={notes} onChange={(e) => setNotes(e.target.value)} className="min-h-[50px] text-xs" placeholder="POD signed by …" />
            </div>
          </div>
          <DialogFooter>
            <Button variant="ghost" size="sm" onClick={() => setOpen(false)} disabled={record.isPending}>Cancel</Button>
            <Button size="sm" onClick={submit} disabled={record.isPending}>
              {record.isPending ? "Recording…" : `Record ${milestone}`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
