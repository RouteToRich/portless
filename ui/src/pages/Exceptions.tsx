import { useState } from "react";
import { ExceptionsApi } from "../api/endpoints";
import { useQuery } from "@tanstack/react-query";
import { QueryState } from "../components/QueryState";

export function Exceptions() {
  const [exceptionId, setExceptionId] = useState("");
  const [lookup, setLookup] = useState<string | null>(null);

  const { data, isLoading, isError, isFetched } = useQuery({
    queryKey: ["exception", lookup],
    queryFn: () => ExceptionsApi.get(lookup as string),
    enabled: Boolean(lookup),
  });

  return (
    <div>
      <div className="page-head">
        <h2>Exceptions</h2>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="row">
          <input
            placeholder="Exception ID (e.g. EXC-2026-0001)"
            value={exceptionId}
            onChange={(e) => setExceptionId(e.target.value)}
            style={{ width: 320 }}
          />
          <button className="btn btn-primary" onClick={() => setLookup(exceptionId.trim())}>
            Look up
          </button>
        </div>
        <p className="muted" style={{ marginTop: 10 }}>
          Failed QC, stuck shipments and disputes surface here with suggested agent remediation
          (blueprint §2.2 Exceptions).
        </p>
      </div>

      {lookup && (
        <QueryState isLoading={isLoading} isError={isError} isEmpty={isFetched && !data}>
          {data && (
            <div className="card">
              <h3>{data.exception_id}</h3>
              <p className="row" style={{ justifyContent: "space-between" }}>
                <span className="muted">Deal</span>
                <span>{data.deal_id}</span>
              </p>
              <p className="row" style={{ justifyContent: "space-between" }}>
                <span className="muted">Category</span>
                <span>{data.category ?? "—"}</span>
              </p>
              <p className="row" style={{ justifyContent: "space-between" }}>
                <span className="muted">Status</span>
                <span className="badge warn">{data.status}</span>
              </p>
              <h3 style={{ marginTop: 12 }}>Remediation plan</h3>
              {data.remediation_plan.length ? (
                <ul>
                  {data.remediation_plan.map((step, i) => (
                    <li key={i}>{step}</li>
                  ))}
                </ul>
              ) : (
                <p className="muted">No plan yet.</p>
              )}
            </div>
          )}
        </QueryState>
      )}
    </div>
  );
}
