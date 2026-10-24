import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { BookMarked, Plus, Trash2, Save, RotateCcw, Radar, Info } from "lucide-react";
import { cn, timeAgo } from "@/lib/utils";
import { useAuth } from "@/lib/auth";
import { useCatalogue, useSaveCatalogue, useScanOpportunities } from "@/api/hooks";
import type { ProductLine } from "@/api/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Skeleton } from "@/components/ui/skeleton";

/** Editable row: lists are kept as comma-separated text while typing. */
interface Row {
  key: string;
  product_id: string;
  hs_code: string;
  name: string;
  products: string;
  markets: string;
  seller_profile: string;
  default_quantity_mt: string;
  min_order_mt: string;
  active: boolean;
  notes: string;
}

function toRow(p: ProductLine, i: number): Row {
  return {
    key: `${p.product_id}-${i}`,
    product_id: p.product_id,
    hs_code: p.hs_code,
    name: p.name,
    products: p.products.join(", "),
    markets: p.markets.join(", "),
    seller_profile: p.seller_profile ?? "",
    default_quantity_mt: p.default_quantity_mt != null ? String(p.default_quantity_mt) : "",
    min_order_mt: p.min_order_mt != null ? String(p.min_order_mt) : "",
    active: p.active,
    notes: p.notes ?? "",
  };
}

function splitList(s: string): string[] {
  return s.split(/[,\n;]/).map((x) => x.trim()).filter(Boolean);
}

function fromRow(r: Row): ProductLine {
  return {
    product_id: r.product_id.trim() || r.name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-"),
    hs_code: r.hs_code.replace(/\D/g, ""),
    name: r.name.trim(),
    products: splitList(r.products),
    markets: splitList(r.markets).map((m) => m.toUpperCase().slice(0, 2)),
    seller_profile: r.seller_profile.trim() || null,
    default_quantity_mt: r.default_quantity_mt ? Number(r.default_quantity_mt) : null,
    min_order_mt: r.min_order_mt ? Number(r.min_order_mt) : null,
    active: r.active,
    notes: r.notes.trim() || null,
  };
}

function emptyRow(): Row {
  return {
    key: `new-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`,
    product_id: "",
    hs_code: "",
    name: "",
    products: "",
    markets: "",
    seller_profile: "",
    default_quantity_mt: "",
    min_order_mt: "",
    active: true,
    notes: "",
  };
}

export function Catalogue() {
  const { user } = useAuth();
  const { data, isLoading, isError, refetch } = useCatalogue();
  const save = useSaveCatalogue();
  const scan = useScanOpportunities();
  const [rows, setRows] = useState<Row[]>([]);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (data && !dirty) setRows(data.products.map(toRow));
  }, [data, dirty]);

  function update(key: string, patch: Partial<Row>) {
    setRows((rs) => rs.map((r) => (r.key === key ? { ...r, ...patch } : r)));
    setDirty(true);
  }

  // Corridors preview from the unsaved rows (active lines × their markets).
  const previewCorridors = useMemo(() => {
    const out: { hs_code: string; name: string; market: string }[] = [];
    for (const r of rows) {
      if (!r.active) continue;
      const hs = r.hs_code.replace(/\D/g, "");
      const markets = splitList(r.markets).map((m) => m.toUpperCase().slice(0, 2));
      for (const m of markets.length ? markets : data?.defaults.markets ?? []) {
        out.push({ hs_code: hs, name: r.name, market: m });
      }
    }
    return out;
  }, [rows, data]);

  async function handleSave() {
    const lines = rows.map(fromRow);
    const bad = lines.find((l) => l.hs_code.length < 4 || !l.name);
    if (bad) {
      toast.error("Every line needs a name and an HS code of at least 4 digits");
      return;
    }
    const ids = new Set<string>();
    for (const l of lines) {
      if (ids.has(l.product_id)) {
        toast.error(`Duplicate product id "${l.product_id}"`);
        return;
      }
      ids.add(l.product_id);
    }
    try {
      const res = await save.mutateAsync({ products: lines, updatedBy: user?.email ?? "operator" });
      setDirty(false);
      setRows(res.products.map(toRow));
      toast.success("Catalogue saved", {
        description: `${res.corridors.length} corridor${res.corridors.length === 1 ? "" : "s"} now drive the scanner, the actor inputs and the outreach e-mails.`,
      });
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      toast.error("Save failed", { description: detail ?? (e as Error).message });
    }
  }

  async function scanNow() {
    try {
      const res = await scan.mutateAsync();
      toast.success(`Scan finished — ${res.length} opportunit${res.length === 1 ? "y" : "ies"}`, {
        description: data?.active
          ? "Scanned every active catalogue corridor."
          : "Catalogue empty — the built-in default HS codes × markets were scanned.",
      });
    } catch (e) {
      toast.error("Scan failed", { description: (e as Error).message });
    }
  }

  return (
    <div className="space-y-4 max-w-6xl">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3">
          <div className="flex items-center justify-center w-9 h-9 rounded-xl bg-[#f5a623]/10">
            <BookMarked className="w-5 h-5 text-[#f5a623]" />
          </div>
          <div>
            <h1 className="text-lg font-bold">Catalogue</h1>
            <p className="text-xs text-muted-foreground">
              What PortLess sells and where — product lines, search words, markets and the seller
              profile the buyer actors pitch with.
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" className="h-8 gap-1.5 text-xs" onClick={scanNow} disabled={scan.isPending}>
            <Radar className={cn("w-3.5 h-3.5", scan.isPending && "animate-spin")} />
            {scan.isPending ? "Scanning…" : "Scan catalogue now"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="h-8 gap-1.5 text-xs"
            onClick={() => {
              setRows((data?.products ?? []).map(toRow));
              setDirty(false);
            }}
            disabled={!dirty}
          >
            <RotateCcw className="w-3.5 h-3.5" /> Discard
          </Button>
          <Button size="sm" className="h-8 gap-1.5 text-xs" onClick={handleSave} disabled={!dirty || save.isPending}>
            <Save className="w-3.5 h-3.5" /> {save.isPending ? "Saving…" : dirty ? "Save" : "Saved"}
          </Button>
        </div>
      </div>

      <Card>
        <CardContent className="pt-4 text-xs text-muted-foreground space-y-1.5">
          <p className="flex items-start gap-1.5">
            <Info className="w-3.5 h-3.5 mt-0.5 shrink-0" />
            <span>
              With at least one active line, <code>POST /trade-min/scan</code> without explicit codes
              scans <b>each line × its own markets</b>; the Alibaba RFQ, US importer and India supplier
              actors receive the line's <b>products</b> words and <b>seller profile</b>; outreach e-mails
              name the product by its <b>name</b>. Empty catalogue → the built-in defaults below apply.
            </span>
          </p>
          {data && (
            <p>
              Built-in defaults: HS {data.defaults.hs_codes.join(", ")} × markets {data.defaults.markets.join(", ")}
              {data.updated_at && (
                <> · last saved {timeAgo(data.updated_at)}{data.updated_by ? ` by ${data.updated_by}` : ""}</>
              )}
              {" · "}
              <Badge variant={data.active ? "success" : "secondary"} className="text-[10px]">
                {data.active ? "catalogue drives the scanner" : "defaults in use"}
              </Badge>
            </p>
          )}
        </CardContent>
      </Card>

      {isLoading && <Skeleton className="h-48 w-full rounded-xl" />}
      {isError && (
        <Card>
          <CardContent className="pt-4 text-sm text-destructive">
            Could not load the catalogue{" "}
            <button className="underline" onClick={() => void refetch()}>retry</button>
          </CardContent>
        </Card>
      )}

      {!isLoading && !isError && (
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between gap-2">
              <CardTitle>Product lines ({rows.length})</CardTitle>
              <Button
                variant="outline"
                size="sm"
                className="h-8 gap-1.5 text-xs"
                onClick={() => {
                  setRows((rs) => [...rs, emptyRow()]);
                  setDirty(true);
                }}
              >
                <Plus className="w-3.5 h-3.5" /> Add line
              </Button>
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            {rows.length === 0 && (
              <p className="text-xs text-muted-foreground">
                No product line yet — add the goods you actually trade (e.g. HS 090411 black pepper → DE, US, AE).
              </p>
            )}
            {rows.map((r) => (
              <div
                key={r.key}
                className={cn("rounded-lg border border-border p-3 space-y-2", !r.active && "opacity-60")}
              >
                <div className="grid grid-cols-2 md:grid-cols-6 gap-2">
                  <div className="space-y-1">
                    <p className="text-[10px] uppercase tracking-wide text-muted-foreground">Product id</p>
                    <Input value={r.product_id} onChange={(e) => update(r.key, { product_id: e.target.value })} placeholder="pepper" className="h-8 text-xs font-mono" />
                  </div>
                  <div className="space-y-1">
                    <p className="text-[10px] uppercase tracking-wide text-muted-foreground">HS code *</p>
                    <Input value={r.hs_code} onChange={(e) => update(r.key, { hs_code: e.target.value })} placeholder="090411" className="h-8 text-xs font-mono" />
                  </div>
                  <div className="space-y-1 md:col-span-2">
                    <p className="text-[10px] uppercase tracking-wide text-muted-foreground">Name *</p>
                    <Input value={r.name} onChange={(e) => update(r.key, { name: e.target.value })} placeholder="black pepper" className="h-8 text-xs" />
                  </div>
                  <div className="space-y-1">
                    <p className="text-[10px] uppercase tracking-wide text-muted-foreground">Default MT</p>
                    <Input type="number" value={r.default_quantity_mt} onChange={(e) => update(r.key, { default_quantity_mt: e.target.value })} placeholder="20" className="h-8 text-xs" />
                  </div>
                  <div className="space-y-1">
                    <p className="text-[10px] uppercase tracking-wide text-muted-foreground">Min order MT</p>
                    <Input type="number" value={r.min_order_mt} onChange={(e) => update(r.key, { min_order_mt: e.target.value })} placeholder="10" className="h-8 text-xs" />
                  </div>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                  <div className="space-y-1">
                    <p className="text-[10px] uppercase tracking-wide text-muted-foreground">Products (bill-of-lading / RFQ words, comma-separated)</p>
                    <Input value={r.products} onChange={(e) => update(r.key, { products: e.target.value })} placeholder="black pepper, piper nigrum, pepper whole" className="h-8 text-xs" />
                  </div>
                  <div className="space-y-1">
                    <p className="text-[10px] uppercase tracking-wide text-muted-foreground">Markets (ISO-2, comma-separated; empty = defaults)</p>
                    <Input value={r.markets} onChange={(e) => update(r.key, { markets: e.target.value })} placeholder="DE, US, AE" className="h-8 text-xs font-mono" />
                  </div>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                  <div className="space-y-1">
                    <p className="text-[10px] uppercase tracking-wide text-muted-foreground">Seller profile (what the Alibaba RFQ actor tells buyers)</p>
                    <Textarea value={r.seller_profile} onChange={(e) => update(r.key, { seller_profile: e.target.value })} className="min-h-[52px] text-xs" placeholder="Indian merchant exporter: verified Kerala / Karnataka growers, SGS/QIMA pre-shipment inspection, neutral escrow, FOB/CIF, 20–40 MT lots." />
                  </div>
                  <div className="space-y-1">
                    <p className="text-[10px] uppercase tracking-wide text-muted-foreground">Notes</p>
                    <Textarea value={r.notes} onChange={(e) => update(r.key, { notes: e.target.value })} className="min-h-[52px] text-xs" />
                  </div>
                </div>
                <div className="flex items-center justify-between">
                  <label className="flex items-center gap-2 text-xs">
                    <input type="checkbox" checked={r.active} onChange={(e) => update(r.key, { active: e.target.checked })} />
                    Active (scanned)
                  </label>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-7 text-xs gap-1 text-destructive"
                    onClick={() => {
                      setRows((rs) => rs.filter((x) => x.key !== r.key));
                      setDirty(true);
                    }}
                  >
                    <Trash2 className="w-3.5 h-3.5" /> Remove
                  </Button>
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      {!isLoading && !isError && (
        <Card>
          <CardHeader>
            <CardTitle>Corridors preview ({previewCorridors.length})</CardTitle>
          </CardHeader>
          <CardContent>
            {previewCorridors.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                No active corridor — the scanner falls back to the built-in defaults.
              </p>
            ) : (
              <div className="flex flex-wrap gap-1.5">
                {previewCorridors.map((c, i) => (
                  <Badge key={`${c.hs_code}-${c.market}-${i}`} variant="outline" className="text-[10px] font-mono">
                    {c.hs_code || "????"} {c.name || "—"} → {c.market}
                  </Badge>
                ))}
              </div>
            )}
            {dirty && <p className="text-[10px] text-muted-foreground mt-2">Preview of unsaved changes.</p>}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
