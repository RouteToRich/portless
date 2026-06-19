import { useDeals } from "../api/hooks";
import { QueryState } from "../components/QueryState";

export function MoneyMargin() {
  const { data, isLoading, isError } = useDeals();

  const projected = (data ?? []).reduce((sum, d) => sum + (d.est_margin_usd ?? 0), 0);
  const realized = (data ?? []).reduce((sum, d) => sum + (d.realized_margin_usd ?? 0), 0);

  return (
    <div>
      <div className="page-head">
        <h2>Money &amp; Margin</h2>
      </div>

      <div className="grid cols-4" style={{ marginBottom: 16 }}>
        <div className="card">
          <h3 className="muted">Projected Margin</h3>
          <div className="metric">${projected.toLocaleString()}</div>
        </div>
        <div className="card">
          <h3 className="muted">Realized Margin</h3>
          <div className="metric">${realized.toLocaleString()}</div>
        </div>
        <div className="card">
          <h3 className="muted">Active Deals</h3>
          <div className="metric">{data?.length ?? 0}</div>
        </div>
        <div className="card">
          <h3 className="muted">FIRC / RoDTEP</h3>
          <div className="metric muted">—</div>
        </div>
      </div>

      <QueryState isLoading={isLoading} isError={isError}>
        <div className="card">
          <h3>Escrow &amp; Scheduled Payouts</h3>
          <p className="muted">
            Escrow balances and milestone payout schedule render here once the Escrow-Guard
            ledger endpoints are implemented (blueprint §2.2 Money &amp; Margin).
          </p>
        </div>
      </QueryState>
    </div>
  );
}
