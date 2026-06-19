import { useDownstreamHealth, useGatewayHealth } from "../api/hooks";
import { QueryState } from "../components/QueryState";
import { HealthBadge } from "../components/StatusBadge";

export function SystemHealth() {
  const gateway = useGatewayHealth();
  const downstream = useDownstreamHealth();

  const entries = Object.entries(downstream.data ?? {});

  return (
    <div>
      <div className="page-head">
        <h2>System Health</h2>
        <span className="row">
          <span className={`dot ${gateway.isSuccess ? "ok" : "bad"}`} />
          <span className="muted">api-gateway {gateway.isSuccess ? "up" : "down"}</span>
        </span>
      </div>

      <QueryState
        isLoading={downstream.isLoading}
        isError={downstream.isError}
        isEmpty={entries.length === 0}
      >
        <div className="grid cols-4">
          {entries.map(([name, status]) => (
            <div className="card" key={name}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <h3>{name}</h3>
                <HealthBadge value={status} />
              </div>
            </div>
          ))}
        </div>
      </QueryState>
    </div>
  );
}
