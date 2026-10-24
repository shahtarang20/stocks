"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertCircle, MessageSquareWarning, RefreshCw } from "lucide-react";
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

export default function AdminDisputesPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;

  const query = useQuery({
    queryKey: ["admin", "disputes"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listAdminDisputes(token),
    refetchInterval: 30000,
  });

  const disputes = query.data ?? [];

  return (
    <>
      <ConsolePageHeader
        title="Disputes"
        description="Open and resolved billing disputes across all customers from GET /v1/admin/disputes. Admins can resolve disputes and authorize refunds from Admin → Billing."
        action={
          <Button variant="outline" onClick={() => query.refetch()} disabled={query.isFetching}>
            <RefreshCw className={query.isFetching ? "animate-spin" : undefined} />
            Refresh
          </Button>
        }
      />

      {query.isLoading ? (
        <Card>
          <CardContent className="space-y-3 p-6">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-8 w-full" />
            ))}
          </CardContent>
        </Card>
      ) : query.isError ? (
        <Card className="border-danger/40">
          <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
            <AlertCircle className="h-8 w-8 text-danger" />
            <p className="text-sm text-muted-foreground">
              {query.error instanceof ApiError ? query.error.message : "Failed to load disputes"}
            </p>
            <Button variant="outline" onClick={() => query.refetch()}>
              <RefreshCw /> Try again
            </Button>
          </CardContent>
        </Card>
      ) : disputes.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center gap-3 p-12 text-center">
            <MessageSquareWarning className="h-8 w-8 text-muted-foreground" />
            <h3 className="font-semibold">No disputes</h3>
            <p className="text-sm text-muted-foreground">
              No billing disputes have been opened. Customers can open disputes on completed jobs.
            </p>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Dispute ID</TableHead>
                <TableHead>Job ID</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Reason</TableHead>
                <TableHead>Refund</TableHead>
                <TableHead>Opened</TableHead>
                <TableHead>Resolved</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {disputes.map((d) => (
                <TableRow key={d.dispute_id}>
                  <TableCell className="font-mono text-xs">
                    {d.dispute_id.slice(0, 8)}…
                  </TableCell>
                  <TableCell className="font-mono text-xs text-primary">
                    {d.job_id.slice(0, 8)}…
                  </TableCell>
                  <TableCell>
                    <Badge variant={d.status === "open" ? "warning" : d.status === "resolved" ? "success" : "neutral"}>
                      {d.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="max-w-[200px] truncate text-sm text-muted-foreground">
                    {d.reason ?? "—"}
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {d.refund_amount ?? "—"}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {formatRelativeTime(d.created_at)}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {d.resolved_at ? formatRelativeTime(d.resolved_at) : "—"}
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
