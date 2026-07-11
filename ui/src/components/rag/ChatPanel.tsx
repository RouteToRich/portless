import { useEffect, useRef, useState } from "react";
import {
  BrainCircuit,
  ChevronDown,
  ChevronRight,
  FileSearch,
  Loader2,
  MessageSquareText,
  RotateCcw,
  Send,
  User,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useRagQuery } from "@/api/hooks";
import type { RagEvaluationMetrics, RagSourceChunk } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources?: RagSourceChunk[];
  metrics?: RagEvaluationMetrics;
  isError?: boolean;
}

const SUGGESTIONS = [
  "What documents are required for this shipment?",
  "What are the standard payment terms?",
  "Summarize the QC inspection process.",
];

export function ChatPanel() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const ragQuery = useRagQuery();
  const scrollEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, ragQuery.isPending]);

  async function send(question: string) {
    const q = question.trim();
    if (!q || ragQuery.isPending) return;

    setMessages((prev) => [...prev, { id: crypto.randomUUID(), role: "user", content: q }]);
    setInput("");

    try {
      const res = await ragQuery.mutateAsync(q);
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: res.answer,
          sources: res.sources,
          metrics: res.evaluation_metrics,
        },
      ]);
    } catch {
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: "Query failed — check the RAG service is reachable and try again.",
          isError: true,
        },
      ]);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send(input);
    }
  }

  return (
    <div className="flex flex-col h-full min-h-[520px] rounded-xl border border-border bg-card overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between gap-2 px-4 py-3 border-b border-border shrink-0">
        <div className="flex items-center gap-2.5">
          <div className="flex items-center justify-center w-8 h-8 rounded-lg bg-[#2ec28a]/10">
            <MessageSquareText className="w-4 h-4 text-[#2ec28a]" />
          </div>
          <div>
            <p className="text-sm font-semibold leading-tight">Test Chat</p>
            <p className="text-[10px] text-muted-foreground">
              Query the RAG pipeline end-to-end
            </p>
          </div>
        </div>
        {messages.length > 0 && (
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1 text-[10px] text-muted-foreground"
            onClick={() => setMessages([])}
          >
            <RotateCcw className="w-3 h-3" />
            Clear
          </Button>
        )}
      </div>

      {/* Messages */}
      <ScrollArea className="flex-1 min-h-0">
        <div className="px-4 py-4 space-y-4">
          {messages.length === 0 ? (
            <EmptyState onPick={(q) => send(q)} />
          ) : (
            messages.map((m) => <MessageBubble key={m.id} message={m} />)
          )}

          {ragQuery.isPending && (
            <div className="flex items-start gap-2.5">
              <AssistantAvatar />
              <div className="flex items-center gap-2 rounded-2xl rounded-tl-sm bg-secondary/60 px-3.5 py-2.5 text-xs text-muted-foreground">
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                Retrieving chunks and generating an answer…
              </div>
            </div>
          )}
          <div ref={scrollEndRef} />
        </div>
      </ScrollArea>

      {/* Composer */}
      <div className="border-t border-border p-3 shrink-0">
        <div className="flex items-end gap-2">
          <Textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask a question about the ingested documents…"
            className="min-h-[44px] max-h-32 text-xs resize-none"
            rows={1}
          />
          <Button
            size="icon"
            className="shrink-0"
            disabled={!input.trim() || ragQuery.isPending}
            onClick={() => send(input)}
            aria-label="Send"
          >
            {ragQuery.isPending ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Send className="w-4 h-4" />
            )}
          </Button>
        </div>
        <p className="text-[9px] text-muted-foreground mt-1.5">
          Enter to send · Shift+Enter for a new line
        </p>
      </div>
    </div>
  );
}

function EmptyState({ onPick }: { onPick: (q: string) => void }) {
  return (
    <div className="flex flex-col items-center text-center gap-3 py-8">
      <div className="flex items-center justify-center w-12 h-12 rounded-2xl bg-[#2ec28a]/10">
        <BrainCircuit className="w-6 h-6 text-[#2ec28a]" />
      </div>
      <div>
        <p className="text-sm font-medium">Test retrieval &amp; generation</p>
        <p className="text-[11px] text-muted-foreground mt-1 max-w-[260px]">
          Ask a question and see exactly which chunks were retrieved and how the
          LLM used them to answer.
        </p>
      </div>
      <div className="flex flex-col gap-1.5 w-full max-w-[280px] mt-1">
        {SUGGESTIONS.map((q) => (
          <button
            key={q}
            onClick={() => onPick(q)}
            className="text-left text-[11px] px-3 py-2 rounded-lg border border-border hover:border-[#2ec28a]/50 hover:bg-secondary/40 transition-colors text-muted-foreground hover:text-foreground"
          >
            {q}
          </button>
        ))}
      </div>
    </div>
  );
}

function AssistantAvatar() {
  return (
    <div className="flex items-center justify-center w-7 h-7 rounded-full bg-[#2ec28a]/10 shrink-0 mt-0.5">
      <BrainCircuit className="w-3.5 h-3.5 text-[#2ec28a]" />
    </div>
  );
}

function MessageBubble({ message }: { message: ChatMessage }) {
  if (message.role === "user") {
    return (
      <div className="flex items-start gap-2.5 justify-end">
        <div className="rounded-2xl rounded-tr-sm bg-primary/15 border border-primary/20 px-3.5 py-2.5 text-xs max-w-[85%] whitespace-pre-wrap">
          {message.content}
        </div>
        <div className="flex items-center justify-center w-7 h-7 rounded-full bg-secondary shrink-0 mt-0.5">
          <User className="w-3.5 h-3.5 text-muted-foreground" />
        </div>
      </div>
    );
  }

  return (
    <div className="flex items-start gap-2.5">
      <AssistantAvatar />
      <div className="space-y-2 max-w-[88%]">
        <div
          className={cn(
            "rounded-2xl rounded-tl-sm px-3.5 py-2.5 text-xs whitespace-pre-wrap leading-relaxed",
            message.isError
              ? "bg-[#ff5d6c]/10 border border-[#ff5d6c]/30 text-[#ff5d6c]"
              : "bg-secondary/60"
          )}
        >
          {message.content}
        </div>

        {message.metrics && (
          <div className="flex flex-wrap gap-1.5">
            <MetricBadge label="Faithfulness" value={message.metrics.faithfulness} />
            <MetricBadge label="Context precision" value={message.metrics.context_precision} />
            <MetricBadge label="Relevance" value={message.metrics.answer_relevance} />
          </div>
        )}

        {message.sources && message.sources.length > 0 && (
          <RetrievedChunks sources={message.sources} />
        )}
      </div>
    </div>
  );
}

function MetricBadge({ label, value }: { label: string; value: number }) {
  const color =
    value >= 0.7 ? "text-[#2ec28a] border-[#2ec28a]/30" :
    value >= 0.4 ? "text-[#f5a623] border-[#f5a623]/30" :
    "text-[#ff5d6c] border-[#ff5d6c]/30";
  return (
    <Badge variant="outline" className={cn("text-[9px] font-normal gap-1", color)}>
      {label} {value.toFixed(2)}
    </Badge>
  );
}

function RetrievedChunks({ sources }: { sources: RagSourceChunk[] }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="rounded-lg border border-border bg-background/60">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-1.5 px-3 py-2 text-[10px] font-medium text-muted-foreground hover:text-foreground transition-colors"
      >
        <FileSearch className="w-3 h-3" />
        Retrieved chunks ({sources.length})
        {open ? (
          <ChevronDown className="w-3 h-3 ml-auto" />
        ) : (
          <ChevronRight className="w-3 h-3 ml-auto" />
        )}
      </button>

      {open && (
        <div className="px-3 pb-3 space-y-2">
          <Separator />
          {sources.map((s, i) => (
            <SourceChunkCard key={s.chunk_id ?? i} index={i + 1} chunk={s} />
          ))}
        </div>
      )}
    </div>
  );
}

function SourceChunkCard({ index, chunk }: { index: number; chunk: RagSourceChunk }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="rounded-md border border-border/70 px-2.5 py-2 space-y-1">
      <div className="flex items-center gap-1.5 flex-wrap">
        <Badge variant="outline" className="text-[9px] font-mono px-1.5">
          [{index}]
        </Badge>
        <span className="text-[11px] font-medium truncate">
          {chunk.title || chunk.source || "Untitled source"}
        </span>
        {chunk.page_number != null && (
          <span className="text-[9px] text-muted-foreground">p. {chunk.page_number}</span>
        )}
        {chunk.section && (
          <span className="text-[9px] text-muted-foreground">· {chunk.section}</span>
        )}
      </div>
      {chunk.content && (
        <button
          onClick={() => setExpanded((e) => !e)}
          className={cn(
            "text-left text-[10px] text-muted-foreground leading-relaxed w-full",
            !expanded && "line-clamp-2"
          )}
        >
          {chunk.content}
        </button>
      )}
    </div>
  );
}
