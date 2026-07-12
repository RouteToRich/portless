import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { AuthApi } from "@/api/endpoints";
import { TOKEN_KEY, USER_KEY } from "@/api/client";
import type { AuthUser } from "@/api/types";

interface AuthContextValue {
  user: AuthUser | null;
  isAuthenticated: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, name: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function readStoredUser(): AuthUser | null {
  try {
    const raw = localStorage.getItem(USER_KEY);
    return raw ? (JSON.parse(raw) as AuthUser) : null;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(readStoredUser);
  const navigate = useNavigate();
  const qc = useQueryClient();

  const persist = useCallback((token: string, u: AuthUser) => {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(USER_KEY, JSON.stringify(u));
    setUser(u);
  }, []);

  const login = useCallback(
    async (email: string, password: string) => {
      const res = await AuthApi.login({ email, password });
      persist(res.access_token, res.user);
    },
    [persist]
  );

  const register = useCallback(
    async (email: string, name: string, password: string) => {
      const res = await AuthApi.register({ email, name, password });
      persist(res.access_token, res.user);
    },
    [persist]
  );

  const logout = useCallback(async () => {
    try {
      // Revoke the token server-side (shared/auth/blacklist.py) so it can't
      // be replayed. Best-effort — proceed with local cleanup even if this
      // fails (backend unreachable, token already expired, etc.).
      await AuthApi.logout();
    } catch {
      // ignore — local cleanup below still runs
    } finally {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
      setUser(null);
      qc.clear(); // drop the previous user's cached data
      navigate("/login", { replace: true });
    }
  }, [navigate, qc]);

  const value = useMemo<AuthContextValue>(
    () => ({ user, isAuthenticated: Boolean(user), login, register, logout }),
    [user, login, register, logout]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
