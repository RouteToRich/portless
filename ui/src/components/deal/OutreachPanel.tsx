import { useState } from "react";
import { toast } from "sonner";
import { Mail, Send, SkipForward, UserPlus, Hourglass, Globe, Phone } from "lucide-react";
import { cn, timeAgo } from "@/lib/utils";
import { useOutreach } from "@/api/hooks";
import type { BuyerCandidate, Deal } from "@/api/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
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

const STATUS_META: Record<string, { label: string; variant: "secondary" | "info" | "warning" | "success" | "destructive" | "outline" | "teal" }> = {
  pending: { label: "pending", variant: "secondary" },
  contacted: { label: "contacted", variant: "info" },
  followed_up: { label: "followed up", variant: "warning" },
  interested: { label: "interested", variant: "success" },
  selected: { label: "selected — buyer", variant: "success" },
  declined: { label: "declined", variant: "destructive" },
  no_reply: { label: "no reply", variant: "outline" },
};

/** Mirrors shared/orchestration/outreach.py::waiting_for_buyer. */
export function waitingForBuyer(deal: Deal): boolean {
  const cands = deal.buyer_candidates ?? [];
  if (cands.length === 0) return false;
  if (cands.some((c) => c.status === "interested" || c.status === "selected")) return false;
  const operatorBuyer = deal.parties.some(
    (p) => p.role === "buyer" && Boolean(p.email) && p.contact_source === "operator"
  );
  return !operatorBuyer;
}

function AddBuyerDialog({ dealId, open, onClose }: { dealId: string; open: boolean; onClose: () => void }) {
  const { add } = useOutreach(dealId);
  const [form, setForm] = useState({ legal_name: "", country: "", email: "", website: "", phone: "", pitch_angle: "", quantity_mt: "" });

  async function submit() {
    if (!form.legal_name.trim() || !form.country.trim()) {
      toast.error("Company name and country are required");
      return;
    }
    const candidate: BuyerCandidate = {
      candidate_id: `op-${Date.now().toString(36)}`,
      legal_name: form.legal_name.trim(),
      country: form.country.trim().toUpperCase().slice(0, 2),
      email: form.email.trim() || null,
      website: form.website.trim() || null,
      phone: form.phone.trim() || null,
      pitch_angle: form.pitch_angle.trim() || null,
      quantity_mt: form.quantity_mt ? Number(form.quantity_mt) : null,
      source: "operator",
      status: "pending",
    };
    try {
      await add.mutateAsync(candidate);
      toast.success(`${candidate.legal_name} added to the funnel`);
      setForm({ legal_name: "", country: "", email: "", website: "", phone: "", pitch_angle: "", quantity_mt: "" });
      onClose();
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      toast.error(detail ?? "Could not add the buyer");
    }
  }

  const field = (k: keyof typeof form, label: string, placeholder = "") => (
    <div className="space-y-1">
      <Label className="text-[11px]">{label}</Label>
      <Input value={form[k]} onChange={(e) => setForm((f) => ({ ...f, [k]: e.target.value }))} placeholder={placeholder} className="h-8 text-xs" />
    </div>
  );

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add a buyer to the funnel</DialogTitle>
          <DialogDescription>
            An operator-sourced lead. It is contacted in order with the templated introduction once
            you press "Contact next buyers" (or automatically when a slot frees up).
          </DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-2 gap-3">
          {field("legal_name", "Company *", "Acme Imports GmbH")}
          {field("country", "Country (ISO-2) *", "DE")}
          {field("email", "E-mail", "purchasing@acme.de")}
          {field("website", "Website", "acme.de")}
          {field("phone", "Phone")}
          {field("quantity_mt", "Quantity (MT)", "20")}
        </div>
        <div className="space-y-1">
          <Label className="text-[11px]">Pitch angle (opens the e-mail)</Label>
          <Textarea
            value={form.pitch_angle}
            onChange={(e) => setForm((f) => ({ ...f, pitch_angle: e.target.value }))}
            className="min-h-[60px] text-xs"
            placeholder="They import Indian pepper every quarter and recently lost a Vietnamese supplier…"
          />
        </div>
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={onClose} disabled={add.isPending}>Cancel</Button>
          <Button size="sm" onClick={submit} disabled={add.isPending}>{add.isPending ? "Adding…" : "Add buyer"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function OutreachPanel({ deal }: { deal: Deal }) {
  const { start, skip } = useOutreach(deal.deal_id);
  const [adding, setAdding] = useState(false);
  const cands = deal.buyer_candidates ?? [];
  const waiting = waitingForBuyer(deal);
  const pending = cands.filter((c) => c.status === "pending").length;
  const active = cands.filter((c) => c.status === "contacted" || c.status === "followed_up").length;
  const selected = cands.find((c) => c.status === "selected" || c.status === "interested");

  async function contactNext() {
    try {
      const res = await start.mutateAsync();
      if (res.contacted === 0) {
        toast.info("No candidate to contact", {
          description: pending
            ? "Every pending candidate is missing an e-mail address — enrich or add one."
            : "No pending buyer left in the funnel.",
        });
      } else {
        toast.success(`${res.contacted} introduction${res.contacted === 1 ? "" : "s"} queued`, {
          description: "Each goes out through the SEND_BUYER_OUTREACH gate (L2 by default = sent immediately).",
        });
      }
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      toast.error(detail ?? "Could not start outreach");
    }
  }

  async function skipOne(c: BuyerCandidate) {
    try {
      const res = await skip.mutateAsync(c.candidate_id);
      toast.success(`${c.legal_name} skipped`, {
        description: res.exhausted
          ? "No further candidate — a deal exception was raised."
          : res.advanced
            ? `${res.advanced} next candidate contacted.`
            : undefined,
      });
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      toast.error(detail ?? "Could not skip the candidate");
    }
  }

  if (cands.length === 0 && !["MATCHING", "NEGOTIATING", "DRAFT"].includes(deal.status)) return null;

  return (
    <Card className={cn(waiting && "border-[#f5a623]/40")}>
      <CardHeader>
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div className="flex items-center gap-2">
            <Mail className="w-4 h-4 text-[#4f8cff]" />
            <CardTitle>Buyer outreach</CardTitle>
            <span className="text-[10px] text-muted-foreground">
              {cands.length} candidate{cands.length === 1 ? "" : "s"} · {active} in flight · {pending} pending
            </span>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" className="h-8 text-xs gap-1.5" onClick={() => setAdding(true)}>
              <UserPlus className="w-3.5 h-3.5" /> Add buyer
            </Button>
            <Button
              size="sm"
              className="h-8 text-xs gap-1.5"
              onClick={contactNext}
              disabled={start.isPending || pending === 0 || Boolean(selected)}
              title={selected ? "A buyer already said yes" : pending === 0 ? "No pending candidate" : "Introduce PortLess to the next pending buyers"}
            >
              <Send className="w-3.5 h-3.5" /> {start.isPending ? "Sending…" : "Contact next buyers"}
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {waiting && (
          <div className="flex items-start gap-2 rounded-lg border border-[#f5a623]/40 bg-[#f5a623]/10 px-3 py-2 text-xs text-[#f5a623]">
            <Hourglass className="w-4 h-4 shrink-0 mt-0.5" />
            <span>
              <b>Waiting for an interested buyer</b> — the negotiation starts when one replies. Replies
              to the <code>[BUY:…]</code> introduction are classified automatically; follow-ups go out after
              3 days of silence and the next candidate after 7 more.
            </span>
          </div>
        )}
        {selected && (
          <div className="flex items-start gap-2 rounded-lg border border-[#2ec28a]/40 bg-[#2ec28a]/10 px-3 py-2 text-xs text-[#2ec28a]">
            <Mail className="w-4 h-4 shrink-0 mt-0.5" />
            <span>
              <b>{selected.legal_name}</b> is interested — set as the deal's buyer{selected.replied_at ? ` ${timeAgo(selected.replied_at)}` : ""}.
            </span>
          </div>
        )}
        {cands.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            No buyer candidates yet — the scan seeds them from the Apify buyer actors; you can add leads by hand.
          </p>
        ) : (
          <ul className="space-y-2">
            {cands.map((c) => {
              const meta = STATUS_META[c.status] ?? { label: c.status, variant: "outline" as const };
              const skippable = ["pending", "contacted", "followed_up"].includes(c.status);
              return (
                <li key={c.candidate_id} className="rounded-lg border border-border bg-secondary/50 p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 space-y-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <p className="text-sm font-medium">{c.legal_name}</p>
                        <span className="text-[10px] font-mono text-muted-foreground">{c.country}</span>
                        <Badge variant={meta.variant} className="text-[10px]">{meta.label}</Badge>
                        {c.fit_score != null && (
                          <span className="text-[10px] text-muted-foreground">fit {c.fit_score}</span>
                        )}
                        {c.quantity_mt != null && (
                          <span className="text-[10px] text-muted-foreground">{c.quantity_mt} MT</span>
                        )}
                        {c.source && <span className="text-[10px] text-muted-foreground">via {c.source}</span>}
                      </div>
                      <div className="flex items-center gap-3 text-[11px] text-muted-foreground flex-wrap">
                        {c.email ? (
                          <span className="flex items-center gap-1 font-mono"><Mail className="w-3 h-3" />{c.email}</span>
                        ) : (
                          <span className="text-[#f5a623]">no e-mail — enrich or edit before contacting</span>
                        )}
                        {c.website && <span className="flex items-center gap-1"><Globe className="w-3 h-3" />{c.website}</span>}
                        {c.phone && <span className="flex items-center gap-1"><Phone className="w-3 h-3" />{c.phone}</span>}
                      </div>
                      {c.pitch_angle && (
                        <p className="text-[11px] text-muted-foreground italic">"{c.pitch_angle}"</p>
                      )}
                      <div className="flex items-center gap-3 text-[10px] text-muted-foreground flex-wrap">
                        {c.contacted_at && <span>contacted {timeAgo(c.contacted_at)}</span>}
                        {c.followed_up_at && <span>followed up {timeAgo(c.followed_up_at)}</span>}
                        {c.replied_at && <span>replied {timeAgo(c.replied_at)}</span>}
                        {c.email_id && <span className="font-mono">{c.email_id}</span>}
                      </div>
                      {c.reply_preview && (
                        <p className="text-[11px] rounded bg-background/60 border border-border px-2 py-1">
                          “{c.reply_preview}”
                        </p>
                      )}
                    </div>
                    {skippable && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 text-xs gap-1 shrink-0"
                        onClick={() => skipOne(c)}
                        disabled={skip.isPending}
                        title="Drop this buyer and move to the next candidate"
                      >
                        <SkipForward className="w-3.5 h-3.5" /> Skip
                      </Button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
      <AddBuyerDialog dealId={deal.deal_id} open={adding} onClose={() => setAdding(false)} />
    </Card>
  );
}
