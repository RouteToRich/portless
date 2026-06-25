import { Navigate, Route, Routes } from "react-router-dom";
import { Toaster } from "sonner";
import { AppLayout } from "./components/AppLayout";
import { OpportunityRadar } from "./pages/OpportunityRadar";
import { DealBoard } from "./pages/DealBoard";
import { DealDetail } from "./pages/DealDetail";
import { ApprovalsInbox } from "./pages/ApprovalsInbox";
import { MoneyMargin } from "./pages/MoneyMargin";
import { Exceptions } from "./pages/Exceptions";
import { AutonomySettings } from "./pages/AutonomySettings";
import { SystemHealth } from "./pages/SystemHealth";
import { KnowledgeBase } from "./pages/KnowledgeBase";
import { Login } from "./pages/Login";
import { useAuth } from "./lib/auth";

function RequireAuth({ children }: { children: JSX.Element }) {
  const { isAuthenticated } = useAuth();
  return isAuthenticated ? children : <Navigate to="/login" replace />;
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
          <Route index element={<Navigate to="/radar" replace />} />
          <Route path="/radar" element={<OpportunityRadar />} />
          <Route path="/deals" element={<DealBoard />} />
          <Route path="/deals/:dealId" element={<DealDetail />} />
          <Route path="/approvals" element={<ApprovalsInbox />} />
          <Route path="/money" element={<MoneyMargin />} />
          <Route path="/exceptions" element={<Exceptions />} />
          <Route path="/autonomy" element={<AutonomySettings />} />
          <Route path="/health" element={<SystemHealth />} />
          <Route path="/knowledge" element={<KnowledgeBase />} />
          <Route path="*" element={<Navigate to="/radar" replace />} />
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
