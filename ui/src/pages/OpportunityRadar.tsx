import { useOpportunities, useScanOpportunities, useCreateDeal } from "../api/hooks";
import { QueryState } from "../components/QueryState";

export function OpportunityRadar() {
  const { data, isLoading, isError } = useOpportunities();
  const scan = useScanOpportunities();
  const createDeal = useCreateDeal();

  return (
    <div>
      <div className="page-head">
        <h2>Opportunity Radar</h2>
        <button
          className="btn btn-primary"
          onClick={() => scan.mutate()}
          disabled={scan.isPending}
        >
          {scan.isPending ? "Scanning…" : "Run Scan"}
        </button>
      </div>

      <QueryState
        isLoading={isLoading}
        isError={isError}
        isEmpty={!data || data.length === 0}
        emptyText="No opportunities detected yet. Run a scan to query Comtrade / Volza / MacMap."
      >
        <div className="grid cols-3">
          {data?.map((op) => (
            <div className="card" key={op.opportunity_id}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <h3>HS {op.hs_code}</h3>
                <span className="badge neutral">rank {op.arbitrage_rank.toFixed(1)}</span>
              </div>
              <p className="muted">Market: {op.market}</p>
              <p className="muted">Source: {op.source ?? "—"}</p>
              <button
                className="btn btn-primary"
                onClick={() =>
                  createDeal.mutate({ operator_id: "OP-901", hs_code: op.hs_code })
                }
                disabled={createDeal.isPending}
              >
                Start Deal
              </button>
            </div>
          ))}
        </div>
      </QueryState>
    </div>
  );
}
