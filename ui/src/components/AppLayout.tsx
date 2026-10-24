import { useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  Bell,
  BookMarked,
  BrainCircuit,
  Building2,
  LayoutGrid,
  LogOut,
  Menu,
  Moon,
  Package2,
  Search,
  Settings2,
  Sun,
  TrendingUp,
  Wifi,
  WifiOff,
  X,
  Zap,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useTheme } from "@/lib/theme";
import { useAuth } from "@/lib/auth";
import { useApprovals, useGatewayHealth } from "@/api/hooks";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

const NAV = [
  { to: "/radar",        label: "Opportunity Radar",  icon: Zap },
  { to: "/deals",        label: "Deal Board",          icon: LayoutGrid },
  { to: "/approvals",    label: "Approvals Inbox",     icon: Bell },
  { to: "/money",        label: "Money & Margin",      icon: TrendingUp },
  { to: "/exceptions",   label: "Exceptions",          icon: AlertTriangle },
  { to: "/catalogue",    label: "Catalogue",           icon: BookMarked },
  { to: "/autonomy",     label: "Autonomy Settings",   icon: Settings2 },
  { to: "/health",       label: "System Health",       icon: Activity },
];

// Portless platform staff only — everyone else never sees these links.
const PORTLESS_NAV = [
  { to: "/knowledge",           label: "Knowledge Base", icon: BrainCircuit },
  { to: "/admin/organizations", label: "Organizations",  icon: Building2 },
];

// Regular (non-Portless) organization users see nothing but their own
// org's deals — matches the backend's non-Portless allowlist (see
// shared/auth/middleware.py), not just a UI convention.
const NON_PORTLESS_NAV = [
  { to: "/deals", label: "Deals", icon: LayoutGrid },
];

const PAGE_TITLES: Record<string, string> = {
  "/radar":              "Opportunity Radar",
  "/deals":              "Deal Board",
  "/approvals":          "Approvals Inbox",
  "/money":              "Money & Margin",
  "/exceptions":         "Exceptions",
  "/catalogue":          "Catalogue",
  "/autonomy":           "Autonomy Settings",
  "/health":             "System Health",
  "/knowledge":          "Knowledge Base",
  "/admin/organizations": "Organizations",
};

export function AppLayout() {
  const location = useLocation();
  const { theme, toggleTheme } = useTheme();
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const { user, logout } = useAuth();
  // Non-Portless users can't reach /hitl-approval/* at all (see
  // shared/auth/middleware.py) — skip the query entirely rather than firing
  // a request that will always 403.
  const { data: approvals } = useApprovals("PENDING", { enabled: Boolean(user?.is_portless) });
  const { data: health, isError: healthError } = useGatewayHealth();

  const initials = (user?.name || user?.email || "U")
    .split(/\s+/)
    .map((s) => s[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  const pendingCount = approvals?.length ?? 0;
  const isOnline = !healthError && health?.status === "ok";
  const navItems = user?.is_portless ? [...NAV, ...PORTLESS_NAV] : NON_PORTLESS_NAV;

  const pageTitle =
    location.pathname === "/deals"
      ? user?.is_portless
        ? "Deal Board"
        : "Deals"
      : PAGE_TITLES[location.pathname] ??
        (location.pathname.startsWith("/deals/") ? "Deal Detail" : "PortLess");

  const closeSidebar = () => setSidebarOpen(false);

  return (
    <div className="flex h-screen bg-background overflow-hidden">
      {/* Mobile overlay */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/50 backdrop-blur-sm lg:hidden"
          onClick={closeSidebar}
        />
      )}

      {/* ── Sidebar ── */}
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-50 flex flex-col w-[232px] shrink-0",
          "bg-white dark:bg-[#121a30] border-r border-border",
          "transition-transform duration-300 ease-in-out",
          "lg:static lg:translate-x-0",
          sidebarOpen ? "translate-x-0" : "-translate-x-full"
        )}
      >
        {/* Logo + close button */}
        <div className="flex items-center gap-2.5 px-5 py-5 border-b border-border">
          <div className="flex items-center justify-center w-8 h-8 rounded-lg bg-primary/20 ring-1 ring-primary/40">
            <Package2 className="w-4 h-4 text-primary" />
          </div>
          <span className="text-base font-bold tracking-tight">
            Port<span className="text-primary">Less</span>
          </span>
          <button
            onClick={closeSidebar}
            className="ml-auto p-1 rounded-md text-muted-foreground hover:bg-secondary lg:hidden"
            aria-label="Close menu"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Nav */}
        <nav className="flex flex-col gap-1 px-3 py-4 flex-1">
          <p className="px-2 mb-1 text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">
            Platform
          </p>
          {navItems.map((item) => {
            const Icon = item.icon;
            const isPending = item.to === "/approvals" && pendingCount > 0;
            return (
              <NavLink
                key={item.to}
                to={item.to}
                onClick={closeSidebar}
                className={({ isActive }) =>
                  cn(
                    "group flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-all duration-150",
                    isActive
                      ? "bg-primary/10 text-primary shadow-[inset_0_0_0_1px_rgba(79,140,255,0.25)]"
                      : "text-muted-foreground hover:bg-secondary hover:text-foreground"
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    <Icon
                      className={cn(
                        "w-4 h-4 shrink-0 transition-colors",
                        isActive ? "text-primary" : "text-muted-foreground group-hover:text-foreground"
                      )}
                    />
                    <span className="flex-1 truncate">{item.label}</span>
                    {isPending && (
                      <span className="flex items-center justify-center w-5 h-5 text-[10px] font-bold rounded-full bg-destructive text-destructive-foreground">
                        {pendingCount > 99 ? "99+" : pendingCount}
                      </span>
                    )}
                  </>
                )}
              </NavLink>
            );
          })}
        </nav>

        {/* Bottom section */}
        <div className="px-3 py-4 border-t border-border space-y-3">
          {/* Connection status */}
          <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-secondary">
            {isOnline ? (
              <Wifi className="w-3.5 h-3.5 text-green-500 dark:text-[#2ec28a]" />
            ) : (
              <WifiOff className="w-3.5 h-3.5 text-destructive" />
            )}
            <span className="text-xs text-muted-foreground">
              {isOnline ? "Backend connected" : "Backend offline"}
            </span>
            <span
              className={cn(
                "ml-auto w-2 h-2 rounded-full",
                isOnline ? "bg-green-500 dark:bg-[#2ec28a] animate-pulse" : "bg-destructive"
              )}
            />
          </div>

          {/* User avatar + logout */}
          <div className="flex items-center gap-2.5 px-2">
            <Avatar className="w-7 h-7">
              <AvatarFallback className="text-[10px]">{initials}</AvatarFallback>
            </Avatar>
            <div className="flex-1 min-w-0">
              <p className="text-xs font-medium truncate">{user?.name ?? "Operator"}</p>
              <p className="text-[10px] text-muted-foreground truncate">
                {user?.email ?? ""}
              </p>
            </div>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  onClick={logout}
                  className="p-1.5 rounded-md text-muted-foreground hover:bg-secondary hover:text-destructive transition-colors"
                  aria-label="Log out"
                >
                  <LogOut className="w-4 h-4" />
                </button>
              </TooltipTrigger>
              <TooltipContent>Log out</TooltipContent>
            </Tooltip>
          </div>
        </div>
      </aside>

      {/* ── Main content ── */}
      <div className="flex flex-col flex-1 min-w-0">
        {/* Top bar */}
        <header className="flex items-center gap-3 h-14 px-4 lg:px-6 bg-white dark:bg-[#121a30] border-b border-border shrink-0">
          {/* Hamburger (mobile only) */}
          <button
            onClick={() => setSidebarOpen(true)}
            className="p-1.5 rounded-md text-muted-foreground hover:bg-secondary transition-colors lg:hidden"
            aria-label="Open menu"
          >
            <Menu className="w-5 h-5" />
          </button>

          {/* Logo on mobile */}
          <span className="font-bold text-sm lg:hidden">
            Port<span className="text-primary">Less</span>
          </span>

          {/* Breadcrumb (desktop) */}
          <div className="hidden lg:flex items-center gap-2 text-sm">
            <span className="text-muted-foreground text-xs font-medium">PortLess</span>
            <span className="text-border">/</span>
            <span className="font-medium text-foreground">{pageTitle}</span>
          </div>

          <div className="flex-1" />

          {/* Search (hidden on mobile) */}
          <div className="relative hidden md:block w-48 lg:w-56">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
            <Input
              placeholder="Search deals, approvals..."
              className="pl-8 h-8 text-xs bg-secondary border-border"
            />
          </div>

          {/* Notification bell — Approvals Inbox isn't reachable for non-Portless users */}
          {user?.is_portless && (
            <Tooltip>
              <TooltipTrigger asChild>
                <NavLink
                  to="/approvals"
                  className="relative flex items-center justify-center w-8 h-8 rounded-lg hover:bg-secondary transition-colors"
                >
                  <Bell className="w-4 h-4 text-muted-foreground" />
                  {pendingCount > 0 && (
                    <span className="absolute -top-0.5 -right-0.5 flex items-center justify-center w-4 h-4 text-[9px] font-bold rounded-full bg-destructive text-destructive-foreground">
                      {pendingCount > 9 ? "9+" : pendingCount}
                    </span>
                  )}
                </NavLink>
              </TooltipTrigger>
              <TooltipContent>
                {pendingCount > 0
                  ? `${pendingCount} pending approval${pendingCount !== 1 ? "s" : ""}`
                  : "No pending approvals"}
              </TooltipContent>
            </Tooltip>
          )}

          {/* Theme toggle */}
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                onClick={toggleTheme}
                className="flex items-center justify-center w-8 h-8 rounded-lg hover:bg-secondary transition-colors text-muted-foreground hover:text-foreground"
                aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
              >
                {theme === "dark" ? (
                  <Sun className="w-4 h-4" />
                ) : (
                  <Moon className="w-4 h-4" />
                )}
              </button>
            </TooltipTrigger>
            <TooltipContent>
              {theme === "dark" ? "Light mode" : "Dark mode"}
            </TooltipContent>
          </Tooltip>

          {/* Status badge */}
          <Badge variant={isOnline ? "success" : "destructive"} className="text-[10px] hidden sm:flex">
            {isOnline ? "Live" : "Offline"}
          </Badge>
        </header>

        {/* Page content */}
        <main className="flex-1 overflow-auto scrollbar-thin">
          <div className="p-4 lg:p-6">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
