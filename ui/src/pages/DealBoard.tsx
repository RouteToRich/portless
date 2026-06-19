import { Link } from "react-router-dom";
import { useDeals } from "../api/hooks";
import { QueryState } from "../components/QueryState";
import type { Deal, DealStatus } from "../api/types";

// Kanban columns map to the key stages of the deal state machine (LLD 4.2).
const COLUMNS: DealStatus[] = [
  "MATCHING",
  "NEGOTIATING",
  "PI_SENT",
  "ESCROW_FUNDED",
  "PO_ISSUED",
  "QC_PASSED",
  "IN_TRANSIT",
  "CLEARED",
  "PAID",
];

export function DealBoard() {
  const { data, isLoading, isError } = useDeals();

  const byColumn = (status: DealStatus): Deal[] =>
    (data ?? []).filter((d) => d.status === status);

  return (
    <div>
      <div className="page-head">
        <h2>Deal Board</h2>
        <span className="muted">{data?.length ?? 0} active deals</span>
      </div>

      <QueryState isLoading={isLoading} isError={isError}>
        <div className="kanban">
          {COLUMNS.map((col) => {
            const deals = byColumn(col);
            return (
              <div className="kanban-col" key={col}>
                <h4>
                  <span>{col}</span>
                  <span>{deals.length}</span>
                </h4>
                {deals.map((d) => (
                  <Link className="deal-card" to={`/deals/${d.deal_id}`} key={d.deal_id}>
                    <div className="hs">HS {d.hs_code}</div>
                    <div className="muted">{d.deal_id}</div>
                    <div className="muted">
                      margin: {d.est_margin_usd != null ? `$${d.est_margin_usd}` : "—"}
                    </div>
                  </Link>
                ))}
              </div>
            );
          })}
        </div>
      </QueryState>
    </div>
  );
}
