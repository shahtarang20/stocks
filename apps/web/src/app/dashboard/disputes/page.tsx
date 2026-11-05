"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw, ShieldAlert } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CopyId, EmptyState, ErrorState, RelativeTime, TableSkeleton } from "@/components/console/bits";
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
import { disputeStatusText, disputeStatusVariant } from "@/lib/billing";
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
        description="A dispute asks our team to look again at a charge you think is wrong. Your disputes are listed here. To start one, open a finished job and press Dispute."
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
        <TableSkeleton rows={3} label="Loading disputes" />
      ) : query.isError ? (
        <ErrorState
          title="We could not load your disputes"
          message={query.error instanceof ApiError ? query.error.message : undefined}
          onRetry={() => query.refetch()}
          retrying={query.isFetching}
        />
      ) : disputes.length === 0 ? (
        <EmptyState
          icon={ShieldAlert}
          title="You have not opened any disputes"
          action={
            <Button asChild>
              <Link href="/dashboard/jobs">Go to my jobs</Link>
            </Button>
          }
        >
          If a charge looks wrong, open that job and press Dispute. Tell us what looks wrong. The dispute will show up here, with our answer.
        </EmptyState>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Dispute</TableHead>
                <TableHead>Job</TableHead>
                <TableHead>What you told us</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Opened</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {disputes.map((d) => (
                <TableRow key={d.dispute_id}>
                  <TableCell>
                    <CopyId id={d.dispute_id} label="Copy dispute id" />
                  </TableCell>
                  <TableCell>
                    {d.job_id ? (
                      <Link
                        href={`/dashboard/jobs/${d.job_id}`}
                        title={d.job_id}
                        className="font-mono text-xs text-primary hover:underline"
                      >
                        {shortId(d.job_id)}
                      </Link>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                  <TableCell className="max-w-[10rem] truncate text-sm text-muted-foreground sm:max-w-xs" title={d.reason}>
                    {d.reason}
                  </TableCell>
                  <TableCell>
                    <Badge variant={disputeStatusVariant(d.status)}>
                      {disputeStatusText(d.status)}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    <RelativeTime iso={d.created_at} />
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
