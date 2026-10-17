"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertCircle, BookOpen, RefreshCw } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { formatRelativeTime } from "@/lib/utils";

export default function AdminAuditLogsPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;

  const query = useQuery({
    queryKey: ["admin", "audit-logs"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listAdminAuditLogs(token),
    refetchInterval: 15000,
  });

  const logs = query.data ?? [];

  return (
    <>
      <ConsolePageHeader
        title="Audit logs"
        description="Immutable record of sensitive administrative actions fetched live from GET /v1/admin/audit-logs. Every entry includes actor, action, target, IP and outcome."
        action={
          <Button variant="outline" onClick={() => query.refetch()} disabled={query.isFetching}>
            <RefreshCw className={query.isFetching ? "animate-spin" : undefined} />
            Refresh
          </Button>
        }
      />

      <div className="space-y-6">
        {query.isLoading ? (
          <Card>
            <CardContent className="space-y-3 p-6">
              {Array.from({ length: 5 }).map((_, i) => (
                <Skeleton key={i} className="h-8 w-full" />
              ))}
            </CardContent>
          </Card>
        ) : query.isError ? (
          <Card className="border-danger/40">
            <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
              <AlertCircle className="h-8 w-8 text-danger" />
              <p className="text-sm text-muted-foreground">
                {query.error instanceof ApiError ? query.error.message : "Failed to load audit logs"}
              </p>
              <Button variant="outline" onClick={() => query.refetch()}>
                <RefreshCw /> Try again
              </Button>
            </CardContent>
          </Card>
        ) : logs.length === 0 ? (
          <Card className="border-dashed">
            <CardContent className="flex flex-col items-center gap-3 p-12 text-center">
              <BookOpen className="h-8 w-8 text-muted-foreground" />
              <h3 className="font-semibold">No audit log entries</h3>
              <p className="text-sm text-muted-foreground">
                Sensitive actions (refunds, suspensions, role changes, dispute resolutions) are logged here.
              </p>
            </CardContent>
          </Card>
        ) : (
          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Time</TableHead>
                  <TableHead>Actor</TableHead>
                  <TableHead>Action</TableHead>
                  <TableHead>Target</TableHead>
                  <TableHead>IP</TableHead>
                  <TableHead>Outcome</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {logs.map((log) => (
                  <TableRow key={log.id}>
                    <TableCell className="text-xs text-muted-foreground whitespace-nowrap">
                      {formatRelativeTime(log.created_at)}
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {log.actor_user_id ? `${log.actor_user_id.slice(0, 8)}…` : "system"}
                    </TableCell>
                    <TableCell>
                      <Badge variant="neutral">{log.action}</Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {log.resource_id ? `${log.resource_type ? `${log.resource_type}:` : ""}${log.resource_id.slice(0, 8)}…` : "—"}
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {log.ip ?? "—"}
                    </TableCell>
                    <TableCell>
                      <Badge variant={log.outcome === "success" ? "success" : "danger"}>
                        {log.outcome}
                      </Badge>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
        )}
      </div>
    </>
  );
}
