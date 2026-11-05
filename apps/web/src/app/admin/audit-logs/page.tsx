"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertCircle, BookOpen, RefreshCw } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { HelpTip } from "@/components/ui/help-tip";
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
        title="Audit log"
        description="A permanent list of important actions, such as refunds, suspensions and payouts. Each line shows who did it, what they did, and whether it worked. Use it to check what happened and who did it."
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
                {query.error instanceof ApiError ? query.error.message : "We could not load the audit log. Select Try again."}
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
              <h3 className="font-semibold">Nothing in the audit log yet</h3>
              <p className="text-sm text-muted-foreground">
                Important actions (refunds, suspensions, role changes, closed disputes) are saved here as they happen.
              </p>
            </CardContent>
          </Card>
        ) : (
          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Time</TableHead>
                  <TableHead>Done by</TableHead>
                  <TableHead>Action</TableHead>
                  <TableHead>On what</TableHead>
                  <TableHead>
                    IP address
                    <HelpTip term="IP address">The internet address the action came from.</HelpTip>
                  </TableHead>
                  <TableHead>Result</TableHead>
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
                        {log.outcome === "success" ? "Worked" : log.outcome === "failure" || log.outcome === "failed" ? "Failed" : log.outcome}
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
