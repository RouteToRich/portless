import type { ReactNode } from "react";

interface QueryStateProps {
  isLoading: boolean;
  isError: boolean;
  isEmpty?: boolean;
  emptyText?: string;
  children: ReactNode;
}

/** Uniform loading / error / empty wrapper for data-driven views. */
export function QueryState({
  isLoading,
  isError,
  isEmpty,
  emptyText = "No records yet.",
  children,
}: QueryStateProps) {
  if (isLoading) return <div className="state">Loading…</div>;
  if (isError)
    return (
      <div className="state error">
        Service unavailable or not yet implemented. Ensure the API gateway and downstream
        services are running.
      </div>
    );
  if (isEmpty) return <div className="state">{emptyText}</div>;
  return <>{children}</>;
}
