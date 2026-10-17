"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, RefreshCw, ShieldAlert } from "lucide-react";
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
import { api, ApiError, type Dispute } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { disputeStatusVariant, humanizeStatus } from "@/lib/billing";
import { formatRelativeTime } from "@/lib/utils";
import { shortId } from "@/lib/jobs";

export default function DisputesPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;

  const query = useQuery<Dispute[]>({
    queryKey: ["disputes"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listDisputes(token),
  });

  const disputes = query.data ?? [];

  return (
    <>
      <ConsolePageHeader
        title="Disputes"
        description="Disputes you have opened on settled jobs. Open a dispute from a completed job's detail page."
        action={
          <Button
            variant="outline"
            onClick={() => query.refetch()}
            disabled={query.isFetching || !accessToken}
          >
            <RefreshCw className={query.isFetching ? "animate-spin" : undefined} />
            Refresh
          </Button>
        }
      />

      {query.isLoading ? (
        <Card>
          <CardContent className="space-y-3 p-6">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </CardContent>
        </Card>
      ) : query.isError ? (
        <Card className="border-danger/40">
          <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
            <span className="flex h-11 w-11 items-center justify-center rounded-full bg-danger/10 text-danger">
              <AlertCircle className="h-5 w-5" />
            </span>
            <h3 className="font-semibold">Couldn&apos;t load disputes</h3>
            <p className="max-w-md text-sm text-muted-foreground">
              {query.error instanceof ApiError
                ? query.error.message
                : "An unexpected error occurred."}
            </p>
            <Button variant="outline" onClick={() => query.refetch()}>
              <RefreshCw /> Try again
            </Button>
          </CardContent>
        </Card>
      ) : disputes.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center gap-3 p-12 text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-2 text-muted-foreground">
              <ShieldAlert className="h-6 w-6" />
            </span>
            <h3 className="font-semibold">No disputes</h3>
            <p className="max-w-md text-sm text-muted-foreground">
              If a settled charge looks wrong, open a dispute from that job&apos;s
              detail page and it will appear here.
            </p>
            <Button asChild>
              <Link href="/dashboard/jobs">Go to jobs</Link>
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Dispute</TableHead>
                <TableHead>Job</TableHead>
                <TableHead>Reason</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Opened</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {disputes.map((d) => (
                <TableRow key={d.dispute_id}>
                  <TableCell className="font-mono text-xs">
                    {shortId(d.dispute_id)}
                  </TableCell>
                  <TableCell>
                    {d.job_id ? (
                      <Link
                        href={`/dashboard/jobs/${d.job_id}`}
                        className="font-mono text-xs text-primary hover:underline"
                      >
                        {shortId(d.job_id)}
                      </Link>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                  <TableCell className="max-w-xs truncate text-sm text-muted-foreground">
                    {d.reason}
                  </TableCell>
                  <TableCell>
                    <Badge variant={disputeStatusVariant(d.status)}>
                      {humanizeStatus(d.status)}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {formatRelativeTime(d.created_at)}
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
