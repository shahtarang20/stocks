"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, CheckCircle2, Loader2, RefreshCw } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, apiErrorCode, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { formatRelativeTime } from "@/lib/utils";

function message(err: unknown): string {
  const code = apiErrorCode(err);
  if (code === "token_deposits_disabled") return "Token deposits are not enabled on this server.";
  if (code === "chain_unavailable") return "The blockchain could not be reached right now.";
  return err instanceof ApiError ? err.message : "Something went wrong.";
}

export default function TokenDepositsPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState("");
  const enabled = Boolean(accessToken);

  const recQuery = useQuery({
    queryKey: ["admin", "token-deposits", "reconciliation"],
    enabled,
    retry: false,
    queryFn: () => api.adminTokenDepositReconciliation(token),
  });
  const listQuery = useQuery({
    queryKey: ["admin", "token-deposits", "list", filter],
    enabled,
    retry: false,
    queryFn: () => api.adminListTokenDeposits(token, filter || undefined),
  });
  const scanMut = useMutation({
    mutationFn: () => api.adminScanTokenDeposits(token),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["admin", "token-deposits"] }),
  });

  const rec = recQuery.data;
  const forbidden = recQuery.error instanceof ApiError && recQuery.error.status === 403;
  const disabled = apiErrorCode(recQuery.error) === "token_deposits_disabled" || forbidden;

  return (
    <>
      <ConsolePageHeader
        title="Token deposits"
        description="COMPUTE tokens received at the platform address, and whether the chain and the ledger agree."
        action={
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => { recQuery.refetch(); listQuery.refetch(); }} disabled={recQuery.isFetching}>
              <RefreshCw className={recQuery.isFetching ? "animate-spin" : undefined} />
              Refresh
            </Button>
            <Button onClick={() => scanMut.mutate()} disabled={scanMut.isPending || disabled}>
              {scanMut.isPending ? <Loader2 className="animate-spin" /> : null}
              Scan now
            </Button>
          </div>
        }
      />

      <div className="space-y-6">
        {forbidden ? (
          <Card className="border-dashed">
            <CardContent className="p-8 text-center text-sm text-muted-foreground">
              Administrator access is required to view token deposits.
            </CardContent>
          </Card>
        ) : disabled ? (
          <Card className="border-dashed">
            <CardContent className="p-8 text-center text-sm text-muted-foreground">
              Token deposits are switched off. They are enabled only when the server has a deposit address, a
              credits-per-token rate and a chain connection configured.
            </CardContent>
          </Card>
        ) : null}

        {scanMut.isError ? (
          <div role="alert" className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            {message(scanMut.error)}
          </div>
        ) : null}
        {scanMut.data ? (
          <p role="status" className="text-sm text-muted-foreground" data-testid="scan-result">
            Scan finished: {String(scanMut.data.credited ?? 0)} credited, {String(scanMut.data.unclaimed ?? 0)} unclaimed,{" "}
            {String(scanMut.data.rejected ?? 0)} rejected.
          </p>
        ) : null}

        {!disabled ? (
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                Reconciliation
                {rec ? (
                  <Badge variant={rec.ok ? "success" : "danger"} data-testid="rec-status">
                    {rec.ok ? "Balanced" : "MISMATCH"}
                  </Badge>
                ) : null}
              </CardTitle>
              <CardDescription>
                The deposit address must hold at least what has been recorded as received, and the credits issued
                must equal the ledger entries posted for them.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {recQuery.isLoading ? (
                <Skeleton className="h-24 w-full" />
              ) : recQuery.isError ? (
                <p role="alert" className="text-sm text-danger">{message(recQuery.error)}</p>
              ) : rec ? (
                <dl className="grid gap-x-8 gap-y-2 text-sm sm:grid-cols-2">
                  {[
                    ["On-chain balance of the deposit address", rec.on_chain_balance],
                    ["Recorded as received", rec.recorded_deposits],
                    ["Not yet recorded (still confirming)", rec.not_yet_recorded],
                    ["Credited to accounts", rec.credited_tokens],
                    ["Unclaimed (sender not linked)", rec.unclaimed_tokens],
                    ["Rejected (outside limits)", rec.rejected_tokens],
                    ["Credits issued", rec.credits_issued],
                    ["Ledger credits posted", rec.ledger_credits],
                  ].map(([k, v]) => (
                    <div key={k} className="flex justify-between gap-4 border-b border-border py-1.5">
                      <dt className="text-muted-foreground">{k}</dt>
                      <dd className="font-mono tabular-nums">{v}</dd>
                    </div>
                  ))}
                </dl>
              ) : null}
              {rec && !rec.ok ? (
                <p role="alert" className="mt-3 flex items-start gap-2 text-sm text-danger">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  {!rec.tokens_ok ? "The deposit address holds less than what was credited. " : ""}
                  {!rec.ledger_ok ? "Credits issued do not match the ledger. " : ""}
                  Investigate before crediting anything more.
                </p>
              ) : rec ? (
                <p className="mt-3 flex items-center gap-2 text-sm text-success">
                  <CheckCircle2 className="h-4 w-4" /> Chain and ledger agree.
                </p>
              ) : null}
            </CardContent>
          </Card>
        ) : null}

        {!disabled ? (
          <Card>
            <CardHeader className="flex-row items-center justify-between gap-4 space-y-0">
              <CardTitle>Deposits</CardTitle>
              <select
                aria-label="Filter by status"
                id="deposit-filter"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm"
              >
                <option value="">All</option>
                <option value="credited">Credited</option>
                <option value="unclaimed">Unclaimed</option>
                <option value="rejected">Rejected</option>
              </select>
            </CardHeader>
            <CardContent>
              {listQuery.isLoading ? (
                <Skeleton className="h-20 w-full" />
              ) : (listQuery.data ?? []).length === 0 ? (
                <p className="text-sm text-muted-foreground">No deposits.</p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>When</TableHead>
                      <TableHead>From</TableHead>
                      <TableHead className="text-right">COMPUTE</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead className="text-right">Credits</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {(listQuery.data ?? []).map((d) => (
                      <TableRow key={d.tx_hash}>
                        <TableCell className="text-sm text-muted-foreground">{formatRelativeTime(d.created_at)}</TableCell>
                        <TableCell className="font-mono text-xs">{d.from_address ? `${d.from_address.slice(0, 8)}…${d.from_address.slice(-4)}` : "—"}</TableCell>
                        <TableCell className="text-right font-mono tabular-nums">{d.amount}</TableCell>
                        <TableCell>
                          <Badge variant={d.status === "credited" ? "success" : "warning"}>{d.status}</Badge>
                          {d.reason ? <span className="ml-2 text-xs text-muted-foreground">{d.reason.replace(/_/g, " ")}</span> : null}
                        </TableCell>
                        <TableCell className="text-right font-mono tabular-nums">{d.credits ?? "—"}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        ) : null}
      </div>
    </>
  );
}
