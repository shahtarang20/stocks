"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, FolderGit2, Plus, RefreshCw, Trash2 } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import {
  Card,
  CardContent,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
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
import { api, ApiError, type Project } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

function statusVariant(status?: string) {
  const s = status?.toLowerCase();
  if (s === "active" || s === "running") return "success" as const;
  if (s === "paused" || s === "pending") return "warning" as const;
  if (s === "archived" || s === "failed") return "neutral" as const;
  return "neutral" as const;
}

export default function ProjectsPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;
  const queryClient = useQueryClient();

  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null);
  const [projectName, setProjectName] = useState("");
  const [projectDescription, setProjectDescription] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery<
    Project[]
  >({
    queryKey: ["projects"],
    enabled: Boolean(accessToken),
    queryFn: () => api.getProjects(token),
  });

  const createMutation = useMutation({
    mutationFn: () => api.createProject(token, projectName.trim(), projectDescription.trim() || undefined),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      setCreateOpen(false);
      setProjectName("");
      setProjectDescription("");
      setErrorMessage(null);
    },
    onError: (err) => {
      setErrorMessage(err instanceof ApiError ? err.message : "Failed to create project");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (projectId: string) => api.deleteProject(token, projectId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      setDeleteTarget(null);
      setErrorMessage(null);
    },
    onError: (err) => {
      setErrorMessage(err instanceof ApiError ? err.message : "Failed to delete project");
    },
  });

  return (
    <>
      <ConsolePageHeader
        title="Projects"
        description="Organize API keys, budgets, and workloads into isolated projects."
        action={
          <div className="flex gap-2">
            <Button
              variant="outline"
              onClick={() => refetch()}
              disabled={isFetching}
            >
              <RefreshCw className={isFetching ? "animate-spin" : undefined} />
              Refresh
            </Button>
            <Button onClick={() => setCreateOpen(true)}>
              <Plus /> New project
            </Button>
          </div>
        }
      />

      {isLoading ? (
        <Card>
          <CardContent className="space-y-3 p-6">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="flex items-center gap-4">
                <Skeleton className="h-10 w-10 rounded-md" />
                <div className="flex-1 space-y-2">
                  <Skeleton className="h-4 w-1/3" />
                  <Skeleton className="h-3 w-1/2" />
                </div>
                <Skeleton className="h-6 w-16 rounded-full" />
              </div>
            ))}
          </CardContent>
        </Card>
      ) : isError ? (
        <Card className="border-danger/40">
          <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
            <span className="flex h-11 w-11 items-center justify-center rounded-full bg-danger/10 text-danger">
              <AlertCircle className="h-5 w-5" />
            </span>
            <h3 className="font-semibold">Couldn&apos;t load projects</h3>
            <p className="max-w-md text-sm text-muted-foreground">
              {error instanceof ApiError
                ? error.message
                : "An unexpected error occurred."}
            </p>
            <Button variant="outline" onClick={() => refetch()}>
              <RefreshCw /> Try again
            </Button>
          </CardContent>
        </Card>
      ) : !data || data.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center gap-3 p-12 text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-2 text-muted-foreground">
              <FolderGit2 className="h-6 w-6" />
            </span>
            <h3 className="font-semibold">No projects yet</h3>
            <p className="max-w-md text-sm text-muted-foreground">
              Create your first project to organize your compute jobs, datasets, and API keys.
            </p>
            <Button onClick={() => setCreateOpen(true)}>
              <Plus /> New project
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Created</TableHead>
                <TableHead>ID</TableHead>
                <TableHead className="text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.map((project) => (
                <TableRow key={project.id}>
                  <TableCell className="font-medium">
                    {project.name}
                    {project.description ? (
                      <p className="text-xs font-normal text-muted-foreground">
                        {project.description}
                      </p>
                    ) : null}
                  </TableCell>
                  <TableCell>
                    <Badge variant={statusVariant(project.status)}>
                      {project.status ?? "active"}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {project.created_at
                      ? new Date(project.created_at).toLocaleDateString()
                      : "—"}
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {project.id}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => setDeleteTarget(project)}
                      className="text-muted-foreground hover:text-danger"
                      aria-label="Delete project"
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}

      {/* Create Project Dialog */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create new project</DialogTitle>
            <DialogDescription>
              Projects group compute jobs, billing quotas, and API keys.
            </DialogDescription>
          </DialogHeader>

          {errorMessage && (
            <div className="rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
              {errorMessage}
            </div>
          )}

          <div className="space-y-4 py-2">
            <div className="space-y-1.5">
              <Label htmlFor="proj-name">Project name</Label>
              <Input
                id="proj-name"
                placeholder="e.g. production-vllm"
                value={projectName}
                onChange={(e) => setProjectName(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="proj-desc">Description (optional)</Label>
              <Input
                id="proj-desc"
                placeholder="e.g. LLM fine-tuning and inference pipelines"
                value={projectDescription}
                onChange={(e) => setProjectDescription(e.target.value)}
              />
            </div>
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateOpen(false)} disabled={createMutation.isPending}>
              Cancel
            </Button>
            <Button
              onClick={() => createMutation.mutate()}
              disabled={!projectName.trim() || createMutation.isPending}
            >
              {createMutation.isPending ? "Creating…" : "Create project"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Project Dialog */}
      <Dialog open={Boolean(deleteTarget)} onOpenChange={(open) => !open && setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete project</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete project &quot;{deleteTarget?.name}&quot;? All associated API keys will also be permanently revoked.
            </DialogDescription>
          </DialogHeader>

          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)} disabled={deleteMutation.isPending}>
              Cancel
            </Button>
            <Button
              variant="default"
              className="bg-danger hover:bg-danger/90 text-white"
              onClick={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
              disabled={deleteMutation.isPending}
            >
              {deleteMutation.isPending ? "Deleting…" : "Delete project"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
