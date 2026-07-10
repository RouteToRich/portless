import { useRef, useState } from "react";
import { toast } from "sonner";
import {
  UploadCloud,
  FileText,
  X,
  Database,
  CheckCircle2,
  XCircle,
  Loader2,
  BrainCircuit,
  RefreshCw,
  Cpu,
  HardDrive,
  Waypoints,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useRagStats, useRagUpload, useRagJob, useRagFiles } from "@/api/hooks";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";

const ACCEPTED = ".pdf,.txt,.md,.markdown,.docx,.html,.csv,.json";
const STRATEGIES = ["recursive", "semantic"] as const;
type Strategy = (typeof STRATEGIES)[number];

function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

const FILE_STORE_LABEL: Record<string, string> = {
  mongodb: "MongoDB GridFS",
};

export function KnowledgeBase() {
  const stats = useRagStats();
  const persistedFiles = useRagFiles();
  const upload = useRagUpload();
  const [files, setFiles] = useState<File[]>([]);
  const [strategy, setStrategy] = useState<Strategy>("recursive");
  const [dragging, setDragging] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const job = useRagJob(jobId);
  const running = job.data?.status === "pending" || job.data?.status === "running";

  function addFiles(list: FileList | null) {
    if (!list) return;
    const incoming = Array.from(list);
    setFiles((prev) => {
      const seen = new Set(prev.map((f) => f.name + f.size));
      return [...prev, ...incoming.filter((f) => !seen.has(f.name + f.size))];
    });
  }

  function removeFile(idx: number) {
    setFiles((prev) => prev.filter((_, i) => i !== idx));
  }

  async function handleUpload() {
    if (files.length === 0) return;
    try {
      const res = await upload.mutateAsync({ files, chunkStrategy: strategy });
      setJobId(res.job_id);
      setFiles([]);
      toast.success("Upload accepted", { description: res.message });
    } catch {
      toast.error("Upload failed", { description: "Check the RAG service is reachable" });
    }
  }

  return (
    <div className="space-y-4 max-w-3xl">
      {/* Header */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex items-center justify-center w-9 h-9 rounded-xl bg-[#a78bfa]/10">
            <BrainCircuit className="w-5 h-5 text-[#a78bfa]" />
          </div>
          <div>
            <h1 className="text-lg font-bold">Knowledge Base</h1>
            <p className="text-xs text-muted-foreground">
              Upload documents into the RAG vector store
            </p>
          </div>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="h-8 gap-1.5 text-xs"
          onClick={() => {
            stats.refetch();
            persistedFiles.refetch();
          }}
        >
          <RefreshCw className={cn("w-3.5 h-3.5", stats.isFetching && "animate-spin")} />
          Refresh
        </Button>
      </div>

      {/* Stats */}
      <Card>
        <CardContent className="flex items-center gap-3 pt-4 pb-4">
          <div className="flex items-center justify-center w-10 h-10 rounded-xl bg-[#4f8cff]/10 shrink-0">
            <Database className="w-5 h-5 text-[#4f8cff]" />
          </div>
          <div className="flex-1">
            <p className="text-[10px] text-muted-foreground">Indexed Chunks</p>
            {stats.isLoading ? (
              <Skeleton className="h-6 w-20 mt-1" />
            ) : stats.isError ? (
              <p className="text-sm text-[#ff5d6c]">RAG service unavailable</p>
            ) : (
              <p className="text-xl font-bold">{stats.data?.total_chunks ?? 0}</p>
            )}
          </div>
          {stats.data && (
            <div className="text-right">
              <p className="text-[10px] text-muted-foreground">Collection</p>
              <p className="text-xs font-mono">
                {stats.data.db}.{stats.data.collection}
              </p>
            </div>
          )}
        </CardContent>
        {stats.data && (
          <CardContent className="flex flex-wrap items-center gap-2 pt-0 pb-4">
            <Badge variant="outline" className="gap-1.5 text-[10px] font-normal">
              <Cpu className="w-3 h-3 text-[#a78bfa]" />
              {stats.data.embedding_provider === "openai"
                ? stats.data.embedding_model
                : "Local hash embeddings (offline)"}
            </Badge>
            <Badge variant="outline" className="gap-1.5 text-[10px] font-normal">
              <HardDrive className="w-3 h-3 text-[#2ec28a]" />
              {FILE_STORE_LABEL[stats.data.file_store_backend] ?? stats.data.file_store_backend}
            </Badge>
            {stats.data.graph_retrieval_enabled && (
              <Badge variant="outline" className="gap-1.5 text-[10px] font-normal">
                <Waypoints className="w-3 h-3 text-[#f5a623]" />
                Graph retrieval on
              </Badge>
            )}
          </CardContent>
        )}
      </Card>

      {/* Dropzone */}
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          addFiles(e.dataTransfer.files);
        }}
        onClick={() => inputRef.current?.click()}
        className={cn(
          "flex flex-col items-center justify-center gap-2 py-10 rounded-xl border-2 border-dashed cursor-pointer transition-colors",
          dragging
            ? "border-[#4f8cff] bg-[#4f8cff]/5"
            : "border-border hover:border-[#4f8cff]/50 hover:bg-secondary/40"
        )}
      >
        <UploadCloud className="w-8 h-8 text-muted-foreground" />
        <p className="text-sm font-medium">Drop files here or click to browse</p>
        <p className="text-[10px] text-muted-foreground">PDF, TXT, MD, DOCX, HTML, CSV, JSON</p>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept={ACCEPTED}
          className="hidden"
          onChange={(e) => {
            addFiles(e.target.files);
            e.target.value = "";
          }}
        />
      </div>

      {/* Selected files */}
      {files.length > 0 && (
        <div className="space-y-2">
          <p className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">
            {files.length} file{files.length !== 1 ? "s" : ""} selected
          </p>
          {files.map((f, i) => (
            <div
              key={f.name + f.size}
              className="flex items-center gap-3 px-3 py-2 rounded-lg border border-border bg-card"
            >
              <FileText className="w-4 h-4 text-muted-foreground shrink-0" />
              <span className="flex-1 text-sm truncate">{f.name}</span>
              <span className="text-[10px] text-muted-foreground shrink-0">{bytes(f.size)}</span>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  removeFile(i);
                }}
                className="p-1 rounded text-muted-foreground hover:text-[#ff5d6c] hover:bg-secondary"
                aria-label="Remove file"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Chunk strategy + submit */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">Chunking:</span>
          {STRATEGIES.map((s) => (
            <button
              key={s}
              onClick={() => setStrategy(s)}
              className={cn(
                "text-xs px-3 py-1 rounded-md border capitalize transition-colors",
                strategy === s
                  ? "bg-primary/15 text-primary border-primary/30"
                  : "border-border text-muted-foreground hover:bg-secondary"
              )}
            >
              {s}
            </button>
          ))}
        </div>
        <Button
          size="sm"
          className="h-8 gap-1.5 text-xs"
          disabled={files.length === 0 || upload.isPending}
          onClick={handleUpload}
        >
          {upload.isPending ? (
            <>
              <Loader2 className="w-3.5 h-3.5 animate-spin" /> Uploading…
            </>
          ) : (
            <>
              <UploadCloud className="w-3.5 h-3.5" /> Ingest {files.length || ""}
            </>
          )}
        </Button>
      </div>

      {/* Job status */}
      {jobId && job.data && (
        <Card>
          <CardContent className="flex items-center gap-3 pt-4 pb-4">
            {running ? (
              <Loader2 className="w-5 h-5 text-[#f5a623] animate-spin shrink-0" />
            ) : job.data.status === "completed" ? (
              <CheckCircle2 className="w-5 h-5 text-[#2ec28a] shrink-0" />
            ) : (
              <XCircle className="w-5 h-5 text-[#ff5d6c] shrink-0" />
            )}
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <p className="text-sm font-semibold capitalize">{job.data.status}</p>
                <Badge variant="outline" className="text-[9px] font-mono">
                  {jobId.slice(0, 8)}
                </Badge>
              </div>
              <p className="text-[10px] text-muted-foreground">
                {job.data.status === "completed"
                  ? `${job.data.inserted_count ?? 0} chunk(s) indexed`
                  : job.data.status === "failed"
                  ? job.data.error ?? "Ingestion failed"
                  : "Processing documents…"}
              </p>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Persisted documents — proves uploads survive restarts in MongoDB GridFS */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <p className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">
            Persisted Documents
          </p>
          {persistedFiles.data && (
            <span className="text-[10px] text-muted-foreground">
              {persistedFiles.data.count} file{persistedFiles.data.count !== 1 ? "s" : ""} in{" "}
              {FILE_STORE_LABEL[persistedFiles.data.backend] ?? persistedFiles.data.backend}
            </span>
          )}
        </div>

        {persistedFiles.isLoading ? (
          <Skeleton className="h-16 w-full rounded-xl" />
        ) : persistedFiles.isError ? (
          <Card>
            <CardContent className="py-4 text-sm text-[#ff5d6c]">
              Could not load persisted documents — check the RAG service is reachable.
            </CardContent>
          </Card>
        ) : !persistedFiles.data || persistedFiles.data.files.length === 0 ? (
          <Card>
            <CardContent className="py-6 text-center text-xs text-muted-foreground">
              No documents persisted yet. Uploaded files are written to MongoDB GridFS
              before ingestion, so they survive a service restart.
            </CardContent>
          </Card>
        ) : (
          <div className="space-y-1.5">
            {persistedFiles.data.files.map((f) => (
              <div
                key={f.file_id}
                className="flex items-center gap-3 px-3 py-2 rounded-lg border border-border bg-card"
              >
                <FileText className="w-4 h-4 text-muted-foreground shrink-0" />
                <span className="flex-1 text-sm truncate">{f.filename}</span>
                <span className="text-[10px] text-muted-foreground shrink-0 hidden sm:inline">
                  {formatDate(f.uploaded_at)}
                </span>
                <span className="text-[10px] text-muted-foreground shrink-0">{bytes(f.size)}</span>
                <Badge variant="outline" className="text-[9px] font-mono shrink-0">
                  {f.file_id.slice(0, 10)}
                </Badge>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
