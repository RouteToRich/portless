import { NavLink, Outlet } from "react-router-dom";
import { KillSwitch } from "./KillSwitch";

const NAV = [
  { to: "/radar", label: "Opportunity Radar" },
  { to: "/deals", label: "Deal Board" },
  { to: "/approvals", label: "Approvals Inbox" },
  { to: "/money", label: "Money & Margin" },
  { to: "/exceptions", label: "Exceptions" },
  { to: "/autonomy", label: "Autonomy Settings" },
  { to: "/health", label: "System Health" },
];

export function AppLayout() {
  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          Port<span>Less</span>
        </div>
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}
          >
            {item.label}
          </NavLink>
        ))}
        <div className="nav-spacer" />
        <KillSwitch />
      </aside>
      <main className="main">
        <header className="topbar">
          <h1>Operator Ops Console</h1>
          <span className="muted">Zero-Inventory Autonomous Merchant Exporter</span>
        </header>
        <div className="content">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
