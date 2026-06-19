import { useApprovals, useResolveApproval } from "../api/hooks";
import { QueryState } from "../components/QueryState";

export function ApprovalsInbox() {
  const { data, isLoading, isError } = useApprovals();
  const resolve = useResolveApproval();

  return (
    <div>
      <div className="page-head">
        <h2>Approvals Inbox</h2>
        <span className="muted">{data?.length ?? 0} pending</span>
      </div>

      <QueryState
        isLoading={isLoading}
        isError={isError}
        isEmpty={!data || data.length === 0}
        emptyText="No pending approvals. Most decisions auto-clear at L1+."
      >
        <div className="card">
          <table>
            <thead>
              <tr>
                <th>Approval</th>
                <th>Deal</th>
                <th>Action</th>
                <th>Impact (USD)</th>
                <th>Risk</th>
                <th>Expires</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data?.map((a) => (
                <tr key={a.approval_id}>
                  <td>{a.approval_id}</td>
                  <td>{a.deal_id}</td>
                  <td>{a.action}</td>
                  <td>{a.financial_impact_usd != null ? `$${a.financial_impact_usd}` : "—"}</td>
                  <td>
                    {a.risk_flags.length ? (
                      <span className="badge warn">{a.risk_flags.join(", ")}</span>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td className="muted">{a.expires_at ?? "—"}</td>
                  <td>
                    <div className="row">
                      <button
                        className="btn btn-ok"
                        onClick={() =>
                          resolve.mutate({
                            approvalId: a.approval_id,
                            decision: "APPROVE",
                            resolvedBy: "OP-901",
                          })
                        }
                      >
                        Approve
                      </button>
                      <button
                        className="btn btn-danger"
                        onClick={() =>
                          resolve.mutate({
                            approvalId: a.approval_id,
                            decision: "REJECT",
                            resolvedBy: "OP-901",
                          })
                        }
                      >
                        Reject
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </QueryState>
    </div>
  );
}
