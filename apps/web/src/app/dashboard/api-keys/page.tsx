"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Check, Copy, KeyRound, Plus, RefreshCw, Trash2 } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
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
import { formatRelativeTime } from "@/lib/utils";

export default function ApiKeysPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;
  const queryClient = useQueryClient();

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
      setNewKeyResult(data);
      setKeyName("");
      setErrorMessage(null);
    },
    onError: (err) => {
      setErrorMessage(err instanceof ApiError ? err.message : "Failed to create API key");
    },
  });

  const revokeMutation = useMutation({
    mutationFn: (keyId: string) => api.revokeApiKey(token, activeProjectId, keyId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["api-keys", activeProjectId] });
      setRevokeTarget(null);
      setErrorMessage(null);
    },
    onError: (err) => {
      setErrorMessage(err instanceof ApiError ? err.message : "Failed to revoke API key");
    },
  });

  function handleCopyKey() {
    if (!newKeyResult?.api_key) return;
    navigator.clipboard.writeText(newKeyResult.api_key);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <>
      <ConsolePageHeader
        title="API keys"
        description="Manage project-scoped API keys for programmatic access to the ComputeCoin API and OpenAI-compatible inference endpoints."
        action={
          <div className="flex gap-2">
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
              <Plus /> Create key
            </Button>
          </div>
        }
      />

      <div className="space-y-6">
        {/* Project selector */}
        {projects.length > 1 && (
          <div className="flex items-center gap-3">
            <Label htmlFor="proj-select" className="text-sm text-muted-foreground whitespace-nowrap">
              Active Project:
            </Label>
            <select
              id="proj-select"
              value={activeProjectId}
              onChange={(e) => setSelectedProjectId(e.target.value)}
              className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm font-medium focus:outline-none focus:ring-1 focus:ring-primary"
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
          <Card>
            <CardContent className="space-y-3 p-6">
              {Array.from({ length: 3 }).map((_, i) => (
                <Skeleton key={i} className="h-8 w-full" />
              ))}
            </CardContent>
          </Card>
        ) : projects.length === 0 ? (
          <Card className="border-dashed">
            <CardContent className="flex flex-col items-center gap-3 p-12 text-center">
              <KeyRound className="h-8 w-8 text-muted-foreground" />
              <h3 className="font-semibold">No projects available</h3>
              <p className="text-sm text-muted-foreground">
                You must create a project before generating API keys. Head over to Projects to create one.
              </p>
            </CardContent>
          </Card>
        ) : keysQuery.isLoading ? (
          <Card>
            <CardContent className="space-y-3 p-6">
              {Array.from({ length: 3 }).map((_, i) => (
                <Skeleton key={i} className="h-8 w-full" />
              ))}
            </CardContent>
          </Card>
        ) : keysQuery.isError ? (
          <Card className="border-danger/40">
            <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
              <AlertCircle className="h-8 w-8 text-danger" />
              <p className="text-sm text-muted-foreground">
                {keysQuery.error instanceof ApiError ? keysQuery.error.message : "Failed to load API keys"}
              </p>
            </CardContent>
          </Card>
        ) : keys.length === 0 ? (
          <Card className="border-dashed">
            <CardContent className="flex flex-col items-center gap-3 p-12 text-center">
              <KeyRound className="h-8 w-8 text-muted-foreground" />
              <h3 className="font-semibold">No API keys in this project</h3>
              <p className="text-sm text-muted-foreground">
                Generate an API key to access ComputeCoin via Python SDK, TypeScript SDK, or curl.
              </p>
              <Button onClick={() => setCreateOpen(true)}>
                <Plus /> Create key
              </Button>
            </CardContent>
          </Card>
        ) : (
          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Label</TableHead>
                  <TableHead>Key Prefix</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Last used</TableHead>
                  <TableHead className="text-right">Action</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {keys.map((key) => (
                  <TableRow key={key.id}>
                    <TableCell className="font-medium">{key.name}</TableCell>
                    <TableCell className="font-mono text-xs">
                      {key.prefix}••••••••
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {formatRelativeTime(key.created_at)}
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {key.last_used_at ? formatRelativeTime(key.last_used_at) : "Never"}
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setRevokeTarget(key)}
                        className="text-muted-foreground hover:text-danger"
                      >
                        <Trash2 className="h-4 w-4 mr-1" /> Revoke
                      </Button>
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
              {newKeyResult ? "API key generated" : "Create new API key"}
            </DialogTitle>
            <DialogDescription>
              {newKeyResult
                ? "Copy your secret API key now. For security, it will never be displayed again."
                : "Give your API key a descriptive name (e.g. ci-cd, vllm-prod)."}
            </DialogDescription>
          </DialogHeader>

          {errorMessage && (
            <div className="rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
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
                  <Button variant="outline" size="sm" onClick={handleCopyKey}>
                    {copied ? <Check className="h-4 w-4 text-success" /> : <Copy className="h-4 w-4" />}
                    {copied ? "Copied" : "Copy"}
                  </Button>
                </div>
              </div>
              <p className="text-xs text-warning">
                ⚠️ Store this key safely. You will not be able to retrieve it again.
              </p>
            </div>
          ) : (
            <div className="space-y-4 py-2">
              <div className="space-y-1.5">
                <Label htmlFor="key-name">Key label</Label>
                <Input
                  id="key-name"
                  placeholder="e.g. inference-client"
                  value={keyName}
                  onChange={(e) => setKeyName(e.target.value)}
                />
              </div>
            </div>
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
                  onClick={() => createMutation.mutate()}
                  disabled={!keyName.trim() || createMutation.isPending}
                >
                  {createMutation.isPending ? "Generating…" : "Generate key"}
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
            <DialogTitle>Revoke API key</DialogTitle>
            <DialogDescription>
              Are you sure you want to revoke &quot;{revokeTarget?.name}&quot; ({revokeTarget?.prefix}…)? Any applications or scripts using this key will immediately be denied access.
            </DialogDescription>
          </DialogHeader>

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
              {revokeMutation.isPending ? "Revoking…" : "Revoke key"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
