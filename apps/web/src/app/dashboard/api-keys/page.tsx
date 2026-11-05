"use client";

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Copy, KeyRound, Plus, RefreshCw, Trash2 } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { EmptyState, ErrorState, RelativeTime, TableSkeleton } from "@/components/console/bits";
import { useToast } from "@/components/console/toast";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api, ApiError, type ApiKey, type ApiKeyCreated, type Project } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

export default function ApiKeysPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;
  const queryClient = useQueryClient();
  const toast = useToast();

  const [selectedProjectId, setSelectedProjectId] = useState<string>("");
  const [createOpen, setCreateOpen] = useState(false);
  const [keyName, setKeyName] = useState("");
  const [newKeyResult, setNewKeyResult] = useState<ApiKeyCreated | null>(null);
  const [copied, setCopied] = useState(false);
  const [revokeTarget, setRevokeTarget] = useState<ApiKey | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // 1. Fetch user projects
  const projectsQuery = useQuery<Project[]>({
    queryKey: ["projects"],
    enabled: Boolean(accessToken),
    queryFn: () => api.getProjects(token),
  });

  const projects = projectsQuery.data ?? [];
  const activeProjectId = selectedProjectId || (projects[0]?.id ?? "");

  // 2. Fetch API keys for selected project
  const keysQuery = useQuery<ApiKey[]>({
    queryKey: ["api-keys", activeProjectId],
    enabled: Boolean(accessToken && activeProjectId),
    queryFn: () => api.listApiKeys(token, activeProjectId),
  });

  const keys = keysQuery.data ?? [];

  // 3. Mutations
  const createMutation = useMutation({
    mutationFn: () => api.createApiKey(token, activeProjectId, keyName.trim()),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["api-keys", activeProjectId] });
      toast.success("API key created. Copy it now. You will not see it again.");
      setNewKeyResult(data);
      setKeyName("");
      setErrorMessage(null);
    },
    onError: (err) => {
      setErrorMessage(err instanceof ApiError ? err.message : "We could not create the API key. Check the name and try again.");
    },
  });

  const revokeMutation = useMutation({
    mutationFn: (keyId: string) => api.revokeApiKey(token, activeProjectId, keyId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["api-keys", activeProjectId] });
      toast.success("API key turned off. Programs that used it can no longer connect.");
      setRevokeTarget(null);
      setErrorMessage(null);
    },
    onError: (err) => {
      setErrorMessage(err instanceof ApiError ? err.message : "We could not turn off this API key. Try again in a moment.");
    },
  });

  function handleCopyKey() {
    if (!newKeyResult?.api_key) return;
    navigator.clipboard
      .writeText(newKeyResult.api_key)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      })
      .catch(() => toast.error("We could not copy it for you. Select the key and copy it yourself."));
  }

  return (
    <>
      <ConsolePageHeader
        title="API keys"
        description="An API key is a secret password that lets a program or script use ComputeCoin for you, without logging in. You only need one if you plan to run jobs from code. Each key belongs to one project."
        action={
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              onClick={() => keysQuery.refetch()}
              disabled={keysQuery.isFetching || !activeProjectId}
            >
              <RefreshCw className={keysQuery.isFetching ? "animate-spin" : undefined} />
              Refresh
            </Button>
            <Button
              onClick={() => {
                setErrorMessage(null);
                setNewKeyResult(null);
                setCreateOpen(true);
              }}
              disabled={!activeProjectId}
            >
              <Plus /> Create an API key
            </Button>
          </div>
        }
      />

      <div className="space-y-6">
        {/* Project selector */}
        {projects.length > 1 && (
          <div className="flex min-w-0 items-center gap-3">
            <Label htmlFor="proj-select" className="text-sm text-muted-foreground whitespace-nowrap">
              Project:
            </Label>
            <select
              id="proj-select"
              value={activeProjectId}
              onChange={(e) => setSelectedProjectId(e.target.value)}
              className="min-w-0 max-w-full flex-1 truncate rounded-md border border-border bg-surface px-3 py-1.5 text-sm font-medium focus:outline-none focus:ring-1 focus:ring-primary sm:flex-none"
            >
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.id.slice(0, 8)}…)
                </option>
              ))}
            </select>
          </div>
        )}

        {projectsQuery.isLoading ? (
          <TableSkeleton rows={3} label="Loading projects" />
        ) : projects.length === 0 ? (
          <EmptyState
            icon={KeyRound}
            title="You need a project first"
            action={
              <Button asChild>
                <Link href="/dashboard/projects">Create a project</Link>
              </Button>
            }
          >
            An API key belongs to a project. Create a project first, then come back here.
          </EmptyState>
        ) : keysQuery.isLoading ? (
          <TableSkeleton rows={3} label="Loading API keys" />
        ) : keysQuery.isError ? (
          <ErrorState
            title="We could not load your API keys"
            message={keysQuery.error instanceof ApiError ? keysQuery.error.message : undefined}
            onRetry={() => keysQuery.refetch()}
            retrying={keysQuery.isFetching}
          />
        ) : keys.length === 0 ? (
          <EmptyState
            icon={KeyRound}
            title="This project has no API keys yet"
            action={
              <Button
                onClick={() => {
                  setErrorMessage(null);
                  setNewKeyResult(null);
                  setCreateOpen(true);
                }}
              >
                <Plus /> Create an API key
              </Button>
            }
          >
            Create an API key if you want to send jobs from a program, such as a Python or JavaScript script. If you only use this website, you do not need one.
          </EmptyState>
        ) : (
          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Start of the key</TableHead>
                  <TableHead className="hidden sm:table-cell">Created</TableHead>
                  <TableHead>Last used</TableHead>
                  <TableHead className="text-right">Action</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {keys.map((key) => (
                  <TableRow key={key.id}>
                    <TableCell className="font-medium">
                      {key.name}
                      {key.revoked_at ? (
                        <Badge variant="outline" className="ml-2">Turned off</Badge>
                      ) : null}
                    </TableCell>
                    <TableCell className="whitespace-nowrap font-mono text-xs">
                      {key.prefix}••••••••
                    </TableCell>
                    <TableCell className="hidden text-sm text-muted-foreground sm:table-cell">
                      <RelativeTime iso={key.created_at} />
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      <RelativeTime iso={key.last_used_at} empty="Never" />
                    </TableCell>
                    <TableCell className="text-right">
                      {key.revoked_at ? null : (
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-label={`Revoke API key ${key.name}`}
                          onClick={() => {
                            setErrorMessage(null);
                            setRevokeTarget(key);
                          }}
                          className="text-muted-foreground hover:text-danger"
                        >
                          <Trash2 className="h-4 w-4 mr-1" /> Turn off
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
        )}
      </div>

      {/* Create Key Dialog */}
      <Dialog open={createOpen} onOpenChange={(open) => {
        if (!open) {
          setCreateOpen(false);
          setNewKeyResult(null);
        }
      }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {newKeyResult ? "Your new API key" : "Create an API key"}
            </DialogTitle>
            <DialogDescription>
              {newKeyResult
                ? "Copy your secret API key now. For your safety, we will never show it again."
                : "Give your key a name so you remember what it is for, like my-laptop-script."}
            </DialogDescription>
          </DialogHeader>

          {errorMessage && (
            <div role="alert" className="rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
              {errorMessage}
            </div>
          )}

          {newKeyResult ? (
            <div className="space-y-4 py-2">
              <div className="rounded-lg border border-border bg-surface-2 p-3">
                <div className="flex items-center justify-between gap-2">
                  <code className="break-all font-mono text-sm text-primary">
                    {newKeyResult.api_key}
                  </code>
                  <Button variant="outline" size="sm" onClick={handleCopyKey} aria-label="Copy API key">
                    {copied ? <Check className="h-4 w-4 text-success" /> : <Copy className="h-4 w-4" />}
                    {copied ? "Copied" : "Copy"}
                  </Button>
                </div>
              </div>
              <p className="flex items-start gap-1.5 text-xs text-warning">
                <AlertTriangle aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                Keep this key somewhere safe, like a password manager. Do not share it. If you lose it, turn it off and create a new one.
              </p>
            </div>
          ) : (
            <form
              id="create-key-form"
              className="space-y-4 py-2"
              onSubmit={(e) => {
                e.preventDefault();
                if (keyName.trim() && !createMutation.isPending) createMutation.mutate();
              }}
            >
              <div className="space-y-1.5">
                <Label htmlFor="key-name">Name for this key</Label>
                <Input
                  id="key-name"
                  placeholder="For example: my-laptop-script"
                  value={keyName}
                  onChange={(e) => setKeyName(e.target.value)}
                  autoComplete="off"
                  required
                />
              </div>
            </form>
          )}

          <DialogFooter>
            {newKeyResult ? (
              <Button onClick={() => setCreateOpen(false)}>
                Done
              </Button>
            ) : (
              <>
                <Button variant="outline" onClick={() => setCreateOpen(false)} disabled={createMutation.isPending}>
                  Cancel
                </Button>
                <Button
                  type="submit"
                  form="create-key-form"
                  disabled={!keyName.trim() || createMutation.isPending}
                >
                  {createMutation.isPending ? "Creating…" : "Create key"}
                </Button>
              </>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Revoke Key Dialog */}
      <Dialog open={Boolean(revokeTarget)} onOpenChange={(open) => !open && setRevokeTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Turn off this API key?</DialogTitle>
            <DialogDescription>
              Turn off &quot;{revokeTarget?.name}&quot; ({revokeTarget?.prefix}…)? Any program or script that uses this key will stop working right away. You can&apos;t undo this. You can always create a new key.
            </DialogDescription>
          </DialogHeader>

          {errorMessage && revokeTarget ? (
            <div role="alert" className="rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
              {errorMessage}
            </div>
          ) : null}

          <DialogFooter>
            <Button variant="outline" onClick={() => setRevokeTarget(null)} disabled={revokeMutation.isPending}>
              Cancel
            </Button>
            <Button
              variant="default"
              className="bg-danger hover:bg-danger/90 text-white"
              onClick={() => revokeTarget && revokeMutation.mutate(revokeTarget.id)}
              disabled={revokeMutation.isPending}
            >
              {revokeMutation.isPending ? "Turning off…" : "Turn off key"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
