import { Navigate, Route, Routes } from "react-router-dom";
import { Toaster } from "sonner";
import { AppLayout } from "./components/AppLayout";
import { OpportunityRadar } from "./pages/OpportunityRadar";
import { DealBoard } from "./pages/DealBoard";
import { OrgDeals } from "./pages/OrgDeals";
import { DealDetail } from "./pages/DealDetail";
import { ApprovalsInbox } from "./pages/ApprovalsInbox";
import { MoneyMargin } from "./pages/MoneyMargin";
import { Exceptions } from "./pages/Exceptions";
import { AutonomySettings } from "./pages/AutonomySettings";
import { Catalogue } from "./pages/Catalogue";
import { SystemHealth } from "./pages/SystemHealth";
import { KnowledgeBase } from "./pages/KnowledgeBase";
import { OrganizationsAdmin } from "./pages/OrganizationsAdmin";
import { Login } from "./pages/Login";
import { useAuth } from "./lib/auth";

function RequireAuth({ children }: { children: JSX.Element }) {
  const { isAuthenticated } = useAuth();
  return isAuthenticated ? children : <Navigate to="/login" replace />;
}

// Portless-only pages — the entire Ops Console except the deals list. A
// non-Portless (regular organization) user only ever sees their org's deals
// (see OrgDeals) — the backend enforces the identical boundary at
// shared/auth/middleware.py, this is not just a UI convention. Bounces to
// /deals rather than showing a 403 screen.
function RequirePortless({ children }: { children: JSX.Element }) {
  const { user } = useAuth();
  return user?.is_portless ? children : <Navigate to="/deals" replace />;
}

// Portless staff land on the Opportunity Radar; everyone else lands on
// (and is confined to) their organization's deals list.
function DefaultRoute() {
  const { user } = useAuth();
  return <Navigate to={user?.is_portless ? "/radar" : "/deals"} replace />;
}

// /deals renders the full Kanban Deal Board for Portless staff, or the
// minimal read-only org-scoped list for everyone else.
function DealsRoute() {
  const { user } = useAuth();
  return user?.is_portless ? <DealBoard /> : <OrgDeals />;
}

export default function App() {
  return (
    <>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          element={
            <RequireAuth>
              <AppLayout />
            </RequireAuth>
          }
        >
          <Route index element={<DefaultRoute />} />
          <Route
            path="/radar"
            element={
              <RequirePortless>
                <OpportunityRadar />
              </RequirePortless>
            }
          />
          <Route path="/deals" element={<DealsRoute />} />
          <Route
            path="/deals/:dealId"
            element={
              <RequirePortless>
                <DealDetail />
              </RequirePortless>
            }
          />
          <Route
            path="/approvals"
            element={
              <RequirePortless>
                <ApprovalsInbox />
              </RequirePortless>
            }
          />
          <Route
            path="/money"
            element={
              <RequirePortless>
                <MoneyMargin />
              </RequirePortless>
            }
          />
          <Route
            path="/exceptions"
            element={
              <RequirePortless>
                <Exceptions />
              </RequirePortless>
            }
          />
          <Route
            path="/catalogue"
            element={
              <RequirePortless>
                <Catalogue />
              </RequirePortless>
            }
          />
          <Route
            path="/autonomy"
            element={
              <RequirePortless>
                <AutonomySettings />
              </RequirePortless>
            }
          />
          <Route
            path="/health"
            element={
              <RequirePortless>
                <SystemHealth />
              </RequirePortless>
            }
          />
          <Route
            path="/knowledge"
            element={
              <RequirePortless>
                <KnowledgeBase />
              </RequirePortless>
            }
          />
          <Route
            path="/admin/organizations"
            element={
              <RequirePortless>
                <OrganizationsAdmin />
              </RequirePortless>
            }
          />
          <Route path="*" element={<DefaultRoute />} />
        </Route>
      </Routes>

      <Toaster
        position="bottom-right"
        theme="dark"
        richColors
        toastOptions={{
          style: {
            background: "#16203a",
            border: "1px solid #243150",
            color: "#e6ecf5",
          },
        }}
      />
    </>
  );
}
