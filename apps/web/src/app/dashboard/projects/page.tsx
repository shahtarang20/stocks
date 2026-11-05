"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderGit2, Plus, RefreshCw, Trash2 } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import {
  Card,
  CardContent,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { CopyId, EmptyState, ErrorState, TableSkeleton } from "@/components/console/bits";
import { useToast } from "@/components/console/toast";
import { capitalize, formatDateTime } from "@/lib/format";
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
  const toast = useToast();

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
      toast.success(`Project "${projectName.trim()}" created.`);
      setCreateOpen(false);
      setProjectName("");
      setProjectDescription("");
      setErrorMessage(null);
    },
    onError: (err) => {
      setErrorMessage(err instanceof ApiError ? err.message : "We could not create the project. Check the name and try again.");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (projectId: string) => api.deleteProject(token, projectId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      toast.success("Project deleted.");
      setDeleteTarget(null);
      setErrorMessage(null);
    },
    onError: (err) => {
      setErrorMessage(err instanceof ApiError ? err.message : "We could not delete the project. Try again in a moment.");
    },
  });

  return (
    <>
      <ConsolePageHeader
        title="Projects"
        description="A project is a folder for one piece of work. It keeps its own API keys together (an API key lets a program send jobs for you). Most people only need one project, or none."
        action={
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              onClick={() => refetch()}
              disabled={isFetching}
            >
              <RefreshCw className={isFetching ? "animate-spin" : undefined} />
              Refresh
            </Button>
            <Button
              onClick={() => {
                setErrorMessage(null);
                setCreateOpen(true);
              }}
            >
              <Plus /> Create a project
            </Button>
          </div>
        }
      />

      {isLoading ? (
        <TableSkeleton rows={4} label="Loading projects" />
      ) : isError ? (
        <ErrorState
          title="We could not load your projects"
          message={error instanceof ApiError ? error.message : undefined}
          onRetry={() => refetch()}
          retrying={isFetching}
        />
      ) : !data || data.length === 0 ? (
        <EmptyState
          icon={FolderGit2}
          title="You have no projects yet"
          action={
            <Button
              onClick={() => {
                setErrorMessage(null);
                setCreateOpen(true);
              }}
            >
              <Plus /> Create a project
            </Button>
          }
        >
          You do not need a project to run jobs. Create one if you want to keep API keys for different work separate.
        </EmptyState>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="hidden sm:table-cell">Created</TableHead>
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
                      {capitalize(project.status ?? "active")}
                    </Badge>
                  </TableCell>
                  <TableCell className="hidden whitespace-nowrap text-sm text-muted-foreground sm:table-cell">
                    {formatDateTime(project.created_at)}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    <CopyId id={project.id} label="Copy project id" />
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => {
                        setErrorMessage(null);
                        setDeleteTarget(project);
                      }}
                      className="text-muted-foreground hover:text-danger"
                      aria-label={`Delete project ${project.name}`}
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
            <DialogTitle>Create a project</DialogTitle>
            <DialogDescription>
              A project keeps related API keys together.
            </DialogDescription>
          </DialogHeader>

          {errorMessage && (
            <div role="alert" className="rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
              {errorMessage}
            </div>
          )}

          <form
            id="create-project-form"
            className="space-y-4 py-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (projectName.trim() && !createMutation.isPending) createMutation.mutate();
            }}
          >
            <div className="space-y-1.5">
              <Label htmlFor="proj-name">Project name</Label>
              <Input
                id="proj-name"
                placeholder="e.g. production-vllm"
                value={projectName}
                onChange={(e) => setProjectName(e.target.value)}
                autoComplete="off"
                required
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="proj-desc">Description (optional)</Label>
              <Input
                id="proj-desc"
                placeholder="e.g. LLM fine-tuning and inference pipelines"
                value={projectDescription}
                onChange={(e) => setProjectDescription(e.target.value)}
                autoComplete="off"
              />
            </div>
          </form>

          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateOpen(false)} disabled={createMutation.isPending}>
              Cancel
            </Button>
            <Button
              type="submit"
              form="create-project-form"
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
              Delete the project &quot;{deleteTarget?.name}&quot;? Every API key in it will stop working for good. You can&apos;t undo this.
            </DialogDescription>
          </DialogHeader>

          {errorMessage && deleteTarget ? (
            <div role="alert" className="rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
              {errorMessage}
            </div>
          ) : null}

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
