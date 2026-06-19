import { useParams, Link } from "react-router-dom";
import { useDeal } from "../api/hooks";
import { QueryState } from "../components/QueryState";
import { StatusBadge } from "../components/StatusBadge";

export function DealDetail() {
  const { dealId } = useParams<{ dealId: string }>();
  const { data: deal, isLoading, isError } = useDeal(dealId);

  return (
    <div>
      <div className="page-head">
        <h2>
          <Link to="/deals" className="muted">
            Deals
          </Link>{" "}
          / {dealId}
        </h2>
      </div>

      <QueryState isLoading={isLoading} isError={isError}>
        {deal && (
          <div className="grid cols-3">
            <div className="card">
              <h3>Overview</h3>
              <p className="row" style={{ justifyContent: "space-between" }}>
                <span className="muted">Status</span>
                <StatusBadge status={deal.status} />
              </p>
              <p className="row" style={{ justifyContent: "space-between" }}>
                <span className="muted">HS Code</span>
                <span>{deal.hs_code}</span>
              </p>
              <p className="row" style={{ justifyContent: "space-between" }}>
                <span className="muted">Autonomy</span>
                <span className="badge neutral">{deal.autonomy_profile}</span>
              </p>
              <p className="row" style={{ justifyContent: "space-between" }}>
                <span className="muted">Operator</span>
                <span>{deal.operator_id}</span>
              </p>
            </div>

            <div className="card">
              <h3>Margin</h3>
              <p className="row" style={{ justifyContent: "space-between" }}>
                <span className="muted">Est. margin</span>
                <span>{deal.est_margin_usd != null ? `$${deal.est_margin_usd}` : "—"}</span>
              </p>
              <p className="row" style={{ justifyContent: "space-between" }}>
                <span className="muted">Realized</span>
                <span>
                  {deal.realized_margin_usd != null ? `$${deal.realized_margin_usd}` : "—"}
                </span>
              </p>
              <p className="row" style={{ justifyContent: "space-between" }}>
                <span className="muted">Margin floor</span>
                <span>{deal.margin_floor_pct != null ? `${deal.margin_floor_pct}%` : "—"}</span>
              </p>
            </div>

            <div className="card">
              <h3>Parties</h3>
              {deal.parties.length === 0 && <p className="muted">No parties linked.</p>}
              {deal.parties.map((p, i) => (
                <p key={i} className="row" style={{ justifyContent: "space-between" }}>
                  <span className="badge neutral">{p.role}</span>
                  <span>
                    {p.legal_name} · {p.country}
                  </span>
                </p>
              ))}
            </div>

            <div className="card" style={{ gridColumn: "1 / -1" }}>
              <h3>Agent Activity & Approvals</h3>
              <p className="muted">
                Live event timeline and the Approve / Reject / Override action bar render here
                once the event stream is wired (blueprint §2.2 Deal Detail).
              </p>
            </div>
          </div>
        )}
      </QueryState>
    </div>
  );
}
