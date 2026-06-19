// Autonomy Levels per action class (LLD 4.6 / blueprint §7.3).
// Local-only editor in this skeleton; persisting to the backend is a TODO.
import { useState } from "react";

type Level = "L0" | "L1" | "L2" | "L3";

const ACTION_CLASSES = [
  { key: "opportunity_scan", label: "Opportunity Scanning", def: "L3" as Level },
  { key: "qc_booking", label: "QC Booking", def: "L3" as Level },
  { key: "doc_generation", label: "Document Generation", def: "L2" as Level },
  { key: "rfq_dispatch", label: "RFQ Dispatch", def: "L2" as Level },
  { key: "freight_booking", label: "Freight Booking", def: "L1" as Level },
  { key: "supplier_payout", label: "Supplier Payout", def: "L0" as Level },
];

const LEVELS: { value: Level; hint: string }[] = [
  { value: "L0", hint: "Always ask" },
  { value: "L1", hint: "Ask, auto-approve on timeout" },
  { value: "L2", hint: "Auto, notify after" },
  { value: "L3", hint: "Fully autonomous, log only" },
];

export function AutonomySettings() {
  const [config, setConfig] = useState<Record<string, Level>>(
    Object.fromEntries(ACTION_CLASSES.map((a) => [a.key, a.def]))
  );

  return (
    <div>
      <div className="page-head">
        <h2>Autonomy Settings</h2>
        <button
          className="btn btn-primary"
          onClick={() =>
            window.alert("Saving autonomy profile (backend endpoint not yet implemented).")
          }
        >
          Save
        </button>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="row" style={{ flexWrap: "wrap", gap: 16 }}>
          {LEVELS.map((l) => (
            <span key={l.value}>
              <span className="badge neutral">{l.value}</span> {l.hint}
            </span>
          ))}
        </div>
      </div>

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>Action Class</th>
              <th>Autonomy Level</th>
            </tr>
          </thead>
          <tbody>
            {ACTION_CLASSES.map((a) => (
              <tr key={a.key}>
                <td>{a.label}</td>
                <td>
                  <select
                    value={config[a.key]}
                    onChange={(e) =>
                      setConfig((c) => ({ ...c, [a.key]: e.target.value as Level }))
                    }
                  >
                    {LEVELS.map((l) => (
                      <option key={l.value} value={l.value}>
                        {l.value} — {l.hint}
                      </option>
                    ))}
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
