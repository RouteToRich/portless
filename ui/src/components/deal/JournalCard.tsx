import { useState } from "react";
import { toast } from "sonner";
import { BookOpenCheck, Download, AlertTriangle } from "lucide-react";
import { cn, fmtDateTime } from "@/lib/utils";
import { useDealJournal } from "@/api/hooks";
import { EscrowApi } from "@/api/endpoints";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

function money(n: number, currency: string) {
  return `${currency} ${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function JournalCard({ dealId }: { dealId: string }) {
  const { data: journal, isLoading } = useDealJournal(dealId);
  const [busy, setBusy] = useState(false);

  async function downloadCsv() {
    setBusy(true);
    try {
      const blob = await EscrowApi.journalCsv(dealId);
      downloadBlob(blob, `journal-${dealId}.csv`);
    } catch {
      toast.error("Could not download the journal");
    } finally {
      setBusy(false);
    }
  }

  if (isLoading) return <Skeleton className="h-40 w-full rounded-xl" />;
  if (!journal) return null;

  const accounts = journal.accounts ?? {};

  return (
    <Card className={cn(!journal.balanced && "border-[#ff5d6c]/40")}>
      <CardHeader>
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div className="flex items-center gap-2">
            <BookOpenCheck className="w-4 h-4 text-[#28c2a0]" />
            <CardTitle>Double-entry journal</CardTitle>
            <Badge variant={journal.balanced ? "success" : "destructive"} className="text-[10px]">
              {journal.balanced ? "balanced" : "out of balance"}
            </Badge>
            <span className="text-[10px] text-muted-foreground">
              debits {money(journal.debits_total, journal.currency)} · credits {money(journal.credits_total, journal.currency)}
            </span>
          </div>
          <Button variant="outline" size="sm" className="h-8 text-xs gap-1.5" onClick={downloadCsv} disabled={busy}>
            <Download className="w-3.5 h-3.5" /> {busy ? "Preparing…" : "Download CSV"}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {journal.issues.length > 0 && (
          <div className="rounded-lg border border-[#ff5d6c]/40 bg-[#ff5d6c]/10 px-3 py-2 text-xs text-[#ff5d6c] space-y-0.5">
            {journal.issues.map((i) => (
              <p key={i} className="flex items-center gap-1.5"><AlertTriangle className="w-3 h-3" /> {i}</p>
            ))}
          </div>
        )}

        {/* Account balances */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          {Object.entries(journal.balances)
            .filter(([, v]) => Math.abs(v) > 0.004)
            .map(([acct, bal]) => (
              <div key={acct} className="rounded-lg bg-secondary border border-border p-2">
                <p className="text-[10px] text-muted-foreground truncate" title={accounts[acct] ?? acct}>
                  {accounts[acct] ?? acct}
                </p>
                <p className={cn("text-sm font-semibold", bal < 0 ? "text-[#f5a623]" : "text-foreground")}>
                  {bal < 0 ? "Cr " : "Dr "}
                  {money(Math.abs(bal), journal.currency)}
                </p>
              </div>
            ))}
        </div>

        {/* Entries */}
        {journal.entries.length === 0 ? (
          <p className="text-xs text-muted-foreground">No entries yet — the first one posts when the buyer funds the escrow.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  {["#", "When", "Event", "Debit", "Credit", "Amount", "Ref / memo"].map((h) => (
                    <th key={h} className={cn("py-1.5 pr-3 font-semibold uppercase tracking-wide text-[10px]", h === "Amount" ? "text-right" : "text-left")}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {journal.entries.map((e) => (
                  <tr key={e.seq} className="border-b border-border/50 align-top">
                    <td className="py-1.5 pr-3 text-muted-foreground">{e.seq}</td>
                    <td className="py-1.5 pr-3 whitespace-nowrap text-muted-foreground">{e.at ? fmtDateTime(e.at) : "—"}</td>
                    <td className="py-1.5 pr-3 font-mono">{e.event}</td>
                    <td className="py-1.5 pr-3">{accounts[e.debit] ?? e.debit}</td>
                    <td className="py-1.5 pr-3">{accounts[e.credit] ?? e.credit}</td>
                    <td className="py-1.5 pr-3 text-right font-semibold whitespace-nowrap">{money(e.amount, e.currency)}</td>
                    <td className="py-1.5 text-muted-foreground">
                      {e.ref && <span className="font-mono">{e.ref}</span>}
                      {e.ref && e.memo && " · "}
                      {e.memo}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="text-[10px] text-muted-foreground">
          Derived from the escrow hold, tranche releases, refunds and completed payouts — revenue is
          recognised as each tranche releases; provider fees are expensed per release.
        </p>
      </CardContent>
    </Card>
  );
}
