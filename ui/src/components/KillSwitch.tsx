import { useState } from "react";

/**
 * Emergency global pause (HLD 2.3 / blueprint §2.2 Kill Switch).
 * Wired to a confirmation only — the backend pause command is a TODO.
 */
export function KillSwitch() {
  const [armed, setArmed] = useState(false);

  return (
    <button
      className={armed ? "btn btn-danger" : "btn"}
      onClick={() => {
        if (!armed) {
          setArmed(true);
          return;
        }
        // TODO: POST a global-pause command to the gateway once implemented.
        window.alert("Global pause requested (backend command not yet implemented).");
        setArmed(false);
      }}
      title="Pause all deals, lock escrow, halt filings"
    >
      {armed ? "Confirm Pause All" : "Kill Switch"}
    </button>
  );
}
