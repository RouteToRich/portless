import { useState } from "react";
import { toast } from "sonner";
import { FileJson, FileText, ExternalLink } from "lucide-react";
import { DealsApi } from "@/api/endpoints";
import { Button } from "@/components/ui/button";
import { downloadBlob } from "./JournalCard";

/** Download / open the evidence pack (GET /deals/{id}/evidence-pack). Both go
 *  through the authenticated client — a plain <a href> cannot send the token. */
export function EvidencePackButtons({ dealId, compact = false }: { dealId: string; compact?: boolean }) {
  const [busy, setBusy] = useState<"json" | "html" | "open" | null>(null);

  async function json() {
    setBusy("json");
    try {
      const pack = await DealsApi.evidencePackJson(dealId);
      const blob = new Blob([JSON.stringify(pack, null, 2)], { type: "application/json" });
      downloadBlob(blob, `evidence-pack-${dealId}.json`);
      const hash = String((pack as { content_hash?: string }).content_hash ?? "");
      toast.success("Evidence pack downloaded", { description: hash ? `SHA-256 ${hash.slice(0, 16)}…` : undefined });
    } catch {
      toast.error("Could not build the evidence pack");
    } finally {
      setBusy(null);
    }
  }

  async function html(mode: "html" | "open") {
    setBusy(mode);
    try {
      const blob = await DealsApi.evidencePackHtml(dealId);
      if (mode === "open") {
        const url = URL.createObjectURL(blob);
        window.open(url, "_blank", "noopener");
        setTimeout(() => URL.revokeObjectURL(url), 60_000);
      } else {
        downloadBlob(blob, `evidence-pack-${dealId}.html`);
      }
    } catch {
      toast.error("Could not render the evidence pack");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      <Button variant="outline" size="sm" className="h-8 text-xs gap-1.5" onClick={() => html("open")} disabled={busy !== null}
        title="Printable HTML pack: deal, parties, PI/PO, document hashes, orders, ledger + journal, approvals, exceptions, correspondence, timeline">
        <ExternalLink className="w-3.5 h-3.5" /> {compact ? "Evidence" : busy === "open" ? "Opening…" : "Open evidence pack"}
      </Button>
      <Button variant="ghost" size="sm" className="h-8 text-xs gap-1.5" onClick={() => html("html")} disabled={busy !== null} title="Download as HTML">
        <FileText className="w-3.5 h-3.5" /> HTML
      </Button>
      <Button variant="ghost" size="sm" className="h-8 text-xs gap-1.5" onClick={json} disabled={busy !== null} title="Download as JSON (with SHA-256 content hash)">
        <FileJson className="w-3.5 h-3.5" /> JSON
      </Button>
    </div>
  );
}
