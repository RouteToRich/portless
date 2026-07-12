import axios from "axios";
import { Building2, LayoutGrid, RefreshCw } from "lucide-react";
import { cn, timeAgo } from "@/lib/utils";
import { useAuth } from "@/lib/auth";
import { useDeals } from "@/api/hooks";
import { StatusBadge } from "@/components/StatusBadge";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * Read-only deals view for non-Portless (regular organization) users.
 *
 * Deliberately minimal: deal_id, hs_code, market, and status only — no
 * kanban board, no click-through to deal detail, no negotiation/escrow/QC/
 * freight/document sub-resources. The backend enforces the same boundary
 * (shared/auth/middleware.py's non-Portless allowlist only permits
 * GET /deals + GET /auth/me), so this page isn't just a UI convention.
 */
export function OrgDeals() {
  const { user } = useAuth();
  const { data: deals, isLoading, isError, error, refetch, isFetching } = useDeals();

  const unassigned =
    axios.isAxiosError(error) && error.response?.status === 403 && !user?.org_id;

  return (
    <div className="space-y-4 max-w-4xl">
      {/* Header */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex items-center justify-center w-9 h-9 rounded-xl bg-[#4f8cff]/10">
            <LayoutGrid className="w-5 h-5 text-[#4f8cff]" />
          </div>
          <div>
            <h1 className="text-lg font-bold">Deals</h1>
            <p className="text-xs text-muted-foreground">
              {deals ? `${deals.length} deal${deals.length !== 1 ? "s" : ""}` : "Your organization's deals"}
            </p>
          </div>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="h-8 gap-1.5 text-xs"
          onClick={() => refetch()}
        >
          <RefreshCw className={cn("w-3.5 h-3.5", isFetching && "animate-spin")} />
          Refresh
        </Button>
      </div>

      {isLoading ? (
        <div className="space-y-2">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-14 w-full rounded-lg" />
          ))}
        </div>
      ) : unassigned ? (
        <Card>
          <CardContent className="flex flex-col items-center gap-2 py-10 text-center">
            <Building2 className="w-8 h-8 text-muted-foreground" />
            <p className="text-sm font-medium">Not yet assigned to an organization</p>
            <p className="text-xs text-muted-foreground max-w-sm">
              Your account isn't linked to an organization yet. Contact a Portless
              administrator to get access to your organization's deals.
            </p>
          </CardContent>
        </Card>
      ) : isError ? (
        <Card>
          <CardContent className="py-6 text-center text-sm text-[#ff5d6c]">
            Could not load deals. Please try again.
          </CardContent>
        </Card>
      ) : !deals || deals.length === 0 ? (
        <Card>
          <CardContent className="py-10 text-center text-sm text-muted-foreground">
            No deals yet for your organization.
          </CardContent>
        </Card>
      ) : (
        <div className="rounded-lg border border-border overflow-hidden">
          <table className="w-full">
            <thead>
              <tr className="border-b border-border bg-popover">
                <th className="text-left px-4 py-3 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                  Deal
                </th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                  HS Code
                </th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                  Status
                </th>
                <th className="text-right px-4 py-3 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                  Created
                </th>
              </tr>
            </thead>
            <tbody>
              {deals.map((deal, idx) => (
                <tr
                  key={deal.deal_id}
                  className={cn(
                    "border-b border-border last:border-b-0",
                    idx % 2 === 0 ? "bg-transparent" : "bg-secondary/30"
                  )}
                >
                  <td className="px-4 py-3">
                    <span className="font-mono text-xs">{deal.deal_id}</span>
                  </td>
                  <td className="px-4 py-3">
                    <span className="font-mono text-sm font-bold">{deal.hs_code}</span>
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge status={deal.status} />
                  </td>
                  <td className="px-4 py-3 text-right text-xs text-muted-foreground">
                    {timeAgo(deal.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
