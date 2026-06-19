import { Navigate, Route, Routes } from "react-router-dom";
import { AppLayout } from "./components/AppLayout";
import { OpportunityRadar } from "./pages/OpportunityRadar";
import { DealBoard } from "./pages/DealBoard";
import { DealDetail } from "./pages/DealDetail";
import { ApprovalsInbox } from "./pages/ApprovalsInbox";
import { MoneyMargin } from "./pages/MoneyMargin";
import { Exceptions } from "./pages/Exceptions";
import { AutonomySettings } from "./pages/AutonomySettings";
import { SystemHealth } from "./pages/SystemHealth";

export default function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route index element={<Navigate to="/radar" replace />} />
        <Route path="/radar" element={<OpportunityRadar />} />
        <Route path="/deals" element={<DealBoard />} />
        <Route path="/deals/:dealId" element={<DealDetail />} />
        <Route path="/approvals" element={<ApprovalsInbox />} />
        <Route path="/money" element={<MoneyMargin />} />
        <Route path="/exceptions" element={<Exceptions />} />
        <Route path="/autonomy" element={<AutonomySettings />} />
        <Route path="/health" element={<SystemHealth />} />
        <Route path="*" element={<Navigate to="/radar" replace />} />
      </Route>
    </Routes>
  );
}
