import { useState } from "react";
import { toast } from "sonner";
import { Building2, Loader2, Plus, ShieldCheck, Sparkles, Users } from "lucide-react";
import {
  useAdminUsers,
  useAssignUserOrg,
  useCreateOrganization,
  useOrganizations,
} from "@/api/hooks";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";

function formatDate(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

export function OrganizationsAdmin() {
  const orgs = useOrganizations();
  const users = useAdminUsers();
  const createOrg = useCreateOrganization();
  const assignOrg = useAssignUserOrg();

  const [name, setName] = useState("");
  const [selected, setSelected] = useState<Record<string, string>>({});

  const orgById = new Map((orgs.data ?? []).map((o) => [o.org_id, o]));

  // An org that already lists this email as a known communication email —
  // e.g. registered automatically when a signed deal named this address as
  // the buyer contact (see services/dealmaker accept_pi -> auth
  // find_or_create_organization_for_deal). Lets Portless staff match a new
  // signup to the right org at a glance instead of guessing by name.
  function suggestedOrgFor(email: string) {
    const key = email.trim().toLowerCase();
    return (orgs.data ?? []).find((o) => o.known_emails.includes(key));
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    try {
      const org = await createOrg.mutateAsync(name.trim());
      toast.success("Organization created", { description: org.name });
      setName("");
    } catch {
      toast.error("Could not create organization");
    }
  }

  async function handleAssign(userId: string) {
    const orgId = selected[userId];
    if (!orgId) return;
    try {
      await assignOrg.mutateAsync({ userId, orgId });
      toast.success("User assigned", {
        description: `Assigned to ${orgById.get(orgId)?.name ?? orgId}`,
      });
    } catch {
      toast.error("Could not assign user to organization");
    }
  }

  return (
    <div className="space-y-4 max-w-4xl">
      {/* Header */}
      <div className="flex items-center gap-3">
        <div className="flex items-center justify-center w-9 h-9 rounded-xl bg-[#4f8cff]/10">
          <Building2 className="w-5 h-5 text-[#4f8cff]" />
        </div>
        <div>
          <h1 className="text-lg font-bold">Organizations</h1>
          <p className="text-xs text-muted-foreground">
            Create organizations and assign users to them — Portless staff only
          </p>
        </div>
      </div>

      {/* Create organization */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Plus className="w-4 h-4 text-muted-foreground" />
            Create Organization
          </CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleCreate} className="flex items-end gap-2">
            <div className="flex-1 space-y-1.5">
              <Label htmlFor="org-name" className="text-xs">
                Organization name
              </Label>
              <Input
                id="org-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Acme Trading Co."
                required
              />
            </div>
            <Button type="submit" className="h-9 gap-1.5 text-xs" disabled={createOrg.isPending}>
              {createOrg.isPending ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <Plus className="w-3.5 h-3.5" />
              )}
              Create
            </Button>
          </form>
        </CardContent>
      </Card>

      {/* Organizations list */}
      <div className="space-y-2">
        <p className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">
          {orgs.data ? `${orgs.data.length} organization${orgs.data.length !== 1 ? "s" : ""}` : "Organizations"}
        </p>
        {orgs.isLoading ? (
          <Skeleton className="h-16 w-full rounded-xl" />
        ) : orgs.isError ? (
          <Card>
            <CardContent className="py-4 text-sm text-[#ff5d6c]">
              Could not load organizations.
            </CardContent>
          </Card>
        ) : !orgs.data || orgs.data.length === 0 ? (
          <Card>
            <CardContent className="py-6 text-center text-xs text-muted-foreground">
              No organizations yet. Create one above.
            </CardContent>
          </Card>
        ) : (
          <div className="space-y-1.5">
            {orgs.data.map((o) => (
              <div
                key={o.org_id}
                className="flex items-center gap-3 px-3 py-2 rounded-lg border border-border bg-card flex-wrap"
              >
                <Building2 className="w-4 h-4 text-muted-foreground shrink-0" />
                <span className="text-sm font-medium truncate shrink-0">{o.name}</span>
                {o.known_emails.length > 0 && (
                  <div className="flex items-center gap-1 flex-wrap min-w-0">
                    {o.known_emails.map((email) => (
                      <Badge key={email} variant="secondary" className="text-[9px] font-normal">
                        {email}
                      </Badge>
                    ))}
                  </div>
                )}
                <span className="flex-1" />
                <span className="text-[10px] text-muted-foreground shrink-0 hidden sm:inline">
                  {formatDate(o.created_at)}
                </span>
                <Badge variant="outline" className="text-[9px] font-mono shrink-0">
                  {o.org_id}
                </Badge>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Users */}
      <div className="space-y-2">
        <p className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground flex items-center gap-1.5">
          <Users className="w-3 h-3" />
          {users.data ? `${users.data.length} user${users.data.length !== 1 ? "s" : ""}` : "Users"}
        </p>
        {users.isLoading ? (
          <Skeleton className="h-16 w-full rounded-xl" />
        ) : users.isError ? (
          <Card>
            <CardContent className="py-4 text-sm text-[#ff5d6c]">Could not load users.</CardContent>
          </Card>
        ) : !users.data || users.data.length === 0 ? (
          <Card>
            <CardContent className="py-6 text-center text-xs text-muted-foreground">
              No registered users yet.
            </CardContent>
          </Card>
        ) : (
          <div className="space-y-1.5">
            {users.data.map((u) => {
              const currentOrg = u.org_id ? orgById.get(u.org_id) : undefined;
              const suggested = !u.org_id && !u.is_portless ? suggestedOrgFor(u.email) : undefined;
              const draft = selected[u.user_id] ?? u.org_id ?? suggested?.org_id ?? "";
              const dirty = draft !== (u.org_id ?? "");
              return (
                <Card key={u.user_id}>
                  <CardContent className="flex items-center gap-3 py-3 flex-wrap">
                    <div className="flex-1 min-w-[160px]">
                      <div className="flex items-center gap-1.5">
                        <p className="text-sm font-medium truncate">{u.name}</p>
                        {u.is_portless && (
                          <Badge variant="info" className="gap-1 text-[9px]">
                            <ShieldCheck className="w-2.5 h-2.5" />
                            Portless
                          </Badge>
                        )}
                      </div>
                      <p className="text-[10px] text-muted-foreground truncate">{u.email}</p>
                    </div>

                    <div className="flex flex-col items-end gap-1 shrink-0">
                      {currentOrg ? (
                        <Badge variant="success" className="text-[10px]">
                          {currentOrg.name}
                        </Badge>
                      ) : u.is_portless ? (
                        <Badge variant="outline" className="text-[10px]">
                          n/a
                        </Badge>
                      ) : (
                        <Badge variant="warning" className="text-[10px]">
                          Unassigned
                        </Badge>
                      )}
                      {suggested && (
                        <Badge variant="info" className="gap-1 text-[9px]">
                          <Sparkles className="w-2.5 h-2.5" />
                          Suggested: {suggested.name}
                        </Badge>
                      )}
                    </div>

                    {!u.is_portless && (
                      <div className="flex items-center gap-1.5 shrink-0">
                        <select
                          className="h-8 rounded-md border border-border bg-background px-2 text-xs text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                          value={draft}
                          onChange={(e) =>
                            setSelected((s) => ({ ...s, [u.user_id]: e.target.value }))
                          }
                        >
                          <option value="">Select organization…</option>
                          {(orgs.data ?? []).map((o) => (
                            <option key={o.org_id} value={o.org_id}>
                              {o.name}
                            </option>
                          ))}
                        </select>
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-8 text-xs"
                          disabled={!dirty || !draft || assignOrg.isPending}
                          onClick={() => handleAssign(u.user_id)}
                        >
                          {assignOrg.isPending ? (
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                          ) : (
                            "Assign"
                          )}
                        </Button>
                      </div>
                    )}
                  </CardContent>
                </Card>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
