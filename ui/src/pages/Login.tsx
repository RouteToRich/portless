import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Package2, Loader2 } from "lucide-react";
import axios from "axios";
import { useAuth } from "@/lib/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export function Login() {
  const { login, register } = useAuth();
  const navigate = useNavigate();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (mode === "login") {
        await login(email, password);
      } else {
        await register(email, name, password);
      }
      navigate("/", { replace: true });
    } catch (err) {
      let msg = "Something went wrong. Please try again.";
      if (axios.isAxiosError(err)) {
        msg = (err.response?.data as { detail?: string })?.detail ?? err.message;
      }
      setError(msg);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-4">
      <div className="w-full max-w-sm">
        {/* Brand */}
        <div className="mb-6 flex flex-col items-center gap-2">
          <div className="flex items-center justify-center w-11 h-11 rounded-xl bg-primary/20 ring-1 ring-primary/40">
            <Package2 className="w-6 h-6 text-primary" />
          </div>
          <h1 className="text-xl font-bold tracking-tight">
            Port<span className="text-primary">Less</span>
          </h1>
          <p className="text-xs text-muted-foreground">Merchant-export Ops Console</p>
        </div>

        <div className="rounded-xl border border-border bg-card p-6 shadow-sm">
          {/* Tabs */}
          <div className="mb-5 flex rounded-lg border border-border p-0.5">
            {(["login", "register"] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => {
                  setMode(m);
                  setError(null);
                }}
                className={
                  "flex-1 rounded-md py-1.5 text-xs font-medium capitalize transition-colors " +
                  (mode === m
                    ? "bg-primary/15 text-primary"
                    : "text-muted-foreground hover:text-foreground")
                }
              >
                {m === "login" ? "Sign in" : "Create account"}
              </button>
            ))}
          </div>

          <form onSubmit={handleSubmit} className="space-y-3">
            {mode === "register" && (
              <div className="space-y-1.5">
                <Label htmlFor="name" className="text-xs">
                  Full name
                </Label>
                <Input
                  id="name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Jane Operator"
                  required
                  autoComplete="name"
                />
              </div>
            )}
            <div className="space-y-1.5">
              <Label htmlFor="email" className="text-xs">
                Email
              </Label>
              <Input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@company.com"
                required
                autoComplete="email"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="password" className="text-xs">
                Password
              </Label>
              <Input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                required
                minLength={6}
                autoComplete={mode === "login" ? "current-password" : "new-password"}
              />
            </div>

            {error && (
              <p className="rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">
                {error}
              </p>
            )}

            <Button type="submit" className="w-full gap-2" disabled={busy}>
              {busy && <Loader2 className="w-4 h-4 animate-spin" />}
              {mode === "login" ? "Sign in" : "Create account"}
            </Button>
          </form>
        </div>

        <p className="mt-4 text-center text-[10px] text-muted-foreground">
          {mode === "login" ? "New here? " : "Already have an account? "}
          <button
            type="button"
            className="text-primary hover:underline"
            onClick={() => {
              setMode(mode === "login" ? "register" : "login");
              setError(null);
            }}
          >
            {mode === "login" ? "Create an account" : "Sign in"}
          </button>
        </p>
      </div>
    </div>
  );
}
