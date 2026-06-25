import type { ReactNode } from "react";
import { AlertCircle, ServerCrash } from "lucide-react";
import { Button } from "@/components/ui/button";

interface QueryStateProps {
  isLoading?: boolean;
  isError?: boolean;
  isEmpty?: boolean;
  emptyText?: string;
  emptyIcon?: ReactNode;
  onRetry?: () => void;
  children: ReactNode;
}

/** Uniform loading / error / empty wrapper for data-driven views. */
export function QueryState({
  isLoading,
  isError,
  isEmpty,
  emptyText = "No records yet.",
  emptyIcon,
  onRetry,
  children,
}: QueryStateProps) {
  if (isLoading) {
    return (
      <div className="flex flex-col items-center justify-center py-16 gap-3 text-muted-foreground">
        <div className="w-6 h-6 border-2 border-[#4f8cff] border-t-transparent rounded-full animate-spin" />
        <p className="text-sm">Loading…</p>
      </div>
    );
  }

  if (isError) {
    return (
      <div className="flex flex-col items-center justify-center py-16 gap-3">
        <ServerCrash className="w-8 h-8 text-[#ff5d6c]" />
        <p className="text-sm font-medium text-[#ff5d6c]">Service unavailable</p>
        <p className="text-xs text-muted-foreground text-center max-w-sm">
          Ensure the backend gateway and downstream services are running at localhost:8081
        </p>
        {onRetry && (
          <Button variant="outline" size="sm" onClick={onRetry}>
            Retry
          </Button>
        )}
      </div>
    );
  }

  if (isEmpty) {
    return (
      <div className="flex flex-col items-center justify-center py-16 gap-3 text-muted-foreground">
        {emptyIcon ?? <AlertCircle className="w-8 h-8" />}
        <p className="text-sm">{emptyText}</p>
      </div>
    );
  }

  return <>{children}</>;
}
