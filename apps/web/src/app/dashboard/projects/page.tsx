"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertCircle, FolderGit2, Plus, RefreshCw } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import {
  Card,
  CardContent,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
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

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery<
    Project[]
  >({
    queryKey: ["projects"],
    enabled: Boolean(accessToken),
    queryFn: () => api.getProjects(accessToken as string),
  });

  return (
    <>
      <ConsolePageHeader
        title="Projects"
        description="Projects are fetched live from GET /projects on the ComputeCoin API."
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
            <Button disabled title="Project creation lands in a later phase">
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
            <p className="text-xs text-muted-foreground">
              This usually means the ComputeCoin API is not reachable at the
              configured base URL.
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
              Your account has no projects. When the backend returns projects
              from <code>GET /projects</code>, they will appear here.
            </p>
            <Button disabled>
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
                      {project.status ?? "unknown"}
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
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
    </>
  );
}
