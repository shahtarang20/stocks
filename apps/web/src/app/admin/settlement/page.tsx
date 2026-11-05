"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  Coins,
  Copy,
  ExternalLink,
  Key,
  Layers,
  Lock,
  RefreshCw,
  Scale,
  Shield,
  ShieldCheck,
  TrendingUp,
} from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { HelpTip } from "@/components/ui/help-tip";
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
  api,
  type ContractDeploymentInfo,
  type EscrowResponse,
  type OnChainReconciliationSummary,
  type OnChainTransactionResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

function formatUnits(weiStr?: string | null): string {
  if (!weiStr) return "0";
  try {
    const val = BigInt(weiStr);
    const divisor = BigInt("1000000000000000000"); // 10^18
    const whole = val / divisor;
    const remainder = val % divisor;
    const remStr = remainder.toString().padStart(18, "0").slice(0, 4);
    return `${whole}.${remStr} COMPUTE`;
  } catch {
    return `${weiStr} units`;
  }
}

function truncateAddress(addr?: string | null): string {
  if (!addr) return "—";
  if (addr.length <= 12) return addr;
  return `${addr.slice(0, 6)}...${addr.slice(-4)}`;
}

export default function AdminSettlementPage() {
  const { accessToken } = useAuth();
  const queryClient = useQueryClient();
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [selectedVoucher, setSelectedVoucher] = useState<string | null>(null);
  const [voucherJobId, setVoucherJobId] = useState("");
  const [finalCost, setFinalCost] = useState("40000000000000000000"); // 40 COMPUTE
  const [platformFee, setPlatformFee] = useState("4000000000000000000"); // 4 COMPUTE
  const [actionSuccess, setActionSuccess] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // 1. Contract info
  const { data: contracts, isLoading: contractsLoading } = useQuery({
    queryKey: ["settlementContracts"],
    queryFn: () => api.getContractInfo(),
    staleTime: 60_000,
  });

  // 2. Reconciliation summary
  const { data: recon, isLoading: reconLoading, refetch: refetchRecon } = useQuery({
    queryKey: ["settlementReconciliation"],
    queryFn: () => (accessToken ? api.getSettlementReconciliation(accessToken) : null),
    enabled: Boolean(accessToken),
  });

  // 3. Escrows
  const { data: escrows, isLoading: escrowsLoading, refetch: refetchEscrows } = useQuery({
    queryKey: ["settlementEscrows"],
    queryFn: () => (accessToken ? api.getSettlementEscrows(accessToken) : []),
    enabled: Boolean(accessToken),
  });

  // 4. On-chain transactions
  const { data: txs, isLoading: txsLoading, refetch: refetchTxs } = useQuery({
    queryKey: ["settlementTransactions"],
    queryFn: () => (accessToken ? api.getSettlementTransactions(accessToken) : []),
    enabled: Boolean(accessToken),
  });

  // Mutations
  const issueVoucherMutation = useMutation({
    mutationFn: async () => {
      if (!accessToken || !voucherJobId) return;
      return api.issueSettlementVoucher(accessToken, {
        job_id: voucherJobId,
        final_cost_units: finalCost,
        platform_fee_units: platformFee,
      });
    },
    onSuccess: (res) => {
      if (res) {
        setSelectedVoucher(res.signature);
        setActionSuccess(`Payment voucher created for job ${res.job_id}. It is shown below.`);
        setActionError(null);
        queryClient.invalidateQueries({ queryKey: ["settlementEscrows"] });
      }
    },
    onError: (err: any) => {
      setActionError(err?.message || "We could not create the voucher. Check the job ID and the amounts, then try again.");
      setActionSuccess(null);
    },
  });

  const confirmSettlementMutation = useMutation({
    mutationFn: async (jobId: string) => {
      if (!accessToken) return;
      return api.confirmSettlement(accessToken, {
        job_id: jobId,
        settle_tx_hash: "0x" + Array.from({ length: 64 }, () => Math.floor(Math.random() * 16).toString(16)).join(""),
        block_number: 104523,
      });
    },
    onSuccess: () => {
      setActionSuccess("Marked as paid out. ComputeCoin's records are updated.");
      queryClient.invalidateQueries({ queryKey: ["settlementEscrows"] });
      queryClient.invalidateQueries({ queryKey: ["settlementReconciliation"] });
      queryClient.invalidateQueries({ queryKey: ["settlementTransactions"] });
    },
    onError: (err: any) => {
      setActionError(err?.message || "We could not mark this as paid out. Refresh the page and try again.");
    },
  });

  const confirmRefundMutation = useMutation({
    mutationFn: async (jobId: string) => {
      if (!accessToken) return;
      return api.confirmRefund(accessToken, {
        job_id: jobId,
        refund_tx_hash: "0x" + Array.from({ length: 64 }, () => Math.floor(Math.random() * 16).toString(16)).join(""),
        block_number: 104524,
      });
    },
    onSuccess: () => {
      setActionSuccess("Marked as refunded. The held amount is released in ComputeCoin's records.");
      queryClient.invalidateQueries({ queryKey: ["settlementEscrows"] });
      queryClient.invalidateQueries({ queryKey: ["settlementReconciliation"] });
      queryClient.invalidateQueries({ queryKey: ["settlementTransactions"] });
    },
    onError: (err: any) => {
      setActionError(err?.message || "We could not mark this as refunded. Refresh the page and try again.");
    },
  });

  const copyToClipboard = (text: string, keyName: string) => {
    navigator.clipboard.writeText(text);
    setCopiedKey(keyName);
    setTimeout(() => setCopiedKey(null), 2000);
  };

  const handleRefreshAll = () => {
    refetchRecon();
    refetchEscrows();
    refetchTxs();
  };

  return (
    <div className="space-y-6">
      <ConsolePageHeader
        title="Blockchain payments"
        description="This page tracks job payments made with the COMPUTE token on the blockchain. Check that the numbers match ComputeCoin's own records, and settle or refund a held payment when a job ends."
        action={
          <Button variant="outline" size="sm" onClick={handleRefreshAll}>
            <RefreshCw className="mr-2 h-4 w-4" />
            Refresh
          </Button>
        }
      />

      {actionSuccess && (
        <div className="flex items-center gap-2 rounded-lg border border-emerald-500/20 bg-emerald-500/10 p-4 text-emerald-400 text-sm">
          <CheckCircle2 className="h-5 w-5 shrink-0" />
          <span>{actionSuccess}</span>
        </div>
      )}

      {actionError && (
        <div className="flex items-center gap-2 rounded-lg border border-rose-500/20 bg-rose-500/10 p-4 text-rose-400 text-sm">
          <AlertCircle className="h-5 w-5 shrink-0" />
          <span>{actionError}</span>
        </div>
      )}

      {/* Protocol Contracts Registry */}
      <Card className="border-border/60 bg-card/60 backdrop-blur-sm">
        <CardHeader>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <ShieldCheck className="h-5 w-5 text-primary" />
              <CardTitle className="text-lg">Blockchain contracts</CardTitle>
            </div>
            <Badge variant="outline" className="border-emerald-500/30 text-emerald-400">
              {contracts?.network_name || "Network not shown"}
            </Badge>
          </div>
          <CardDescription>
            Smart contracts are small programs on the blockchain. These ones hold a customer&apos;s payment while a job runs, take the platform fee, and pay the host. They are a test setup and have not been checked by an outside security firm.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {contractsLoading ? (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
              {[...Array(4)].map((_, i) => (
                <Skeleton key={i} className="h-24 w-full" />
              ))}
            </div>
          ) : contracts ? (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
              <div className="rounded-lg border border-border/50 bg-background/50 p-3 space-y-1">
                <span className="text-xs text-muted-foreground flex items-center gap-1 font-mono">
                  <Coins className="h-3 w-3 text-amber-400" />
                  COMPUTE token ({contracts.token_symbol})
                </span>
                <div className="font-mono text-xs truncate flex items-center justify-between">
                  <span title={contracts.token_address}>{truncateAddress(contracts.token_address)}</span>
                  <button
                    onClick={() => copyToClipboard(contracts.token_address, "token")}
                    className="text-muted-foreground hover:text-foreground ml-1"
                  >
                    <Copy className="h-3 w-3" />
                  </button>
                </div>
                <div className="text-[10px] text-muted-foreground">Supply: 1,000,000,000 COMPUTE</div>
              </div>

              <div className="rounded-lg border border-border/50 bg-background/50 p-3 space-y-1">
                <span className="text-xs text-muted-foreground flex items-center gap-1 font-mono">
                  <Lock className="h-3 w-3 text-cyan-400" />
                  Payment holding contract
                </span>
                <div className="font-mono text-xs truncate flex items-center justify-between">
                  <span title={contracts.settlement_contract_address}>{truncateAddress(contracts.settlement_contract_address)}</span>
                  <button
                    onClick={() => copyToClipboard(contracts.settlement_contract_address, "settlement")}
                    className="text-muted-foreground hover:text-foreground ml-1"
                  >
                    <Copy className="h-3 w-3" />
                  </button>
                </div>
                <div className="text-[10px] text-muted-foreground">Holds payments until a job ends</div>
              </div>

              <div className="rounded-lg border border-border/50 bg-background/50 p-3 space-y-1">
                <span className="text-xs text-muted-foreground flex items-center gap-1 font-mono">
                  <Key className="h-3 w-3 text-purple-400" />
                  Platform signing key
                </span>
                <div className="font-mono text-xs truncate flex items-center justify-between">
                  <span title={contracts.platform_signer_address}>{truncateAddress(contracts.platform_signer_address)}</span>
                  <button
                    onClick={() => copyToClipboard(contracts.platform_signer_address, "signer")}
                    className="text-muted-foreground hover:text-foreground ml-1"
                  >
                    <Copy className="h-3 w-3" />
                  </button>
                </div>
                <div className="text-[10px] text-muted-foreground">Signs payment vouchers</div>
              </div>

              <div className="rounded-lg border border-border/50 bg-background/50 p-3 space-y-1">
                <span className="text-xs text-muted-foreground flex items-center gap-1 font-mono">
                  <Scale className="h-3 w-3 text-emerald-400" />
                  Platform fee wallet
                </span>
                <div className="font-mono text-xs truncate flex items-center justify-between">
                  <span title={contracts.treasury_address}>{truncateAddress(contracts.treasury_address)}</span>
                  <button
                    onClick={() => copyToClipboard(contracts.treasury_address, "treasury")}
                    className="text-muted-foreground hover:text-foreground ml-1"
                  >
                    <Copy className="h-3 w-3" />
                  </button>
                </div>
                <div className="text-[10px] text-muted-foreground">Where platform fees go</div>
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>

      {/* Global On-Chain Settlement Metrics */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <Card className="border-border/60 bg-card/60">
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
              Held payments
            </CardTitle>
          </CardHeader>
          <CardContent>
            {reconLoading ? (
              <Skeleton className="h-8 w-16" />
            ) : (
              <div className="text-2xl font-bold">{recon?.total_escrows ?? 0}</div>
            )}
            <div className="text-xs text-muted-foreground mt-1 flex items-center gap-1">
              <Layers className="h-3 w-3" /> Jobs with a held payment
            </div>
          </CardContent>
        </Card>

        <Card className="border-border/60 bg-card/60">
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
              Amount held now
            </CardTitle>
          </CardHeader>
          <CardContent>
            {reconLoading ? (
              <Skeleton className="h-8 w-24" />
            ) : (
              <div className="text-2xl font-bold font-mono text-cyan-400">
                {formatUnits(recon?.active_locked_amount_units)}
              </div>
            )}
            <div className="text-xs text-muted-foreground mt-1 flex items-center gap-1">
              <Lock className="h-3 w-3 text-cyan-400" /> Waiting for jobs to end
            </div>
          </CardContent>
        </Card>

        <Card className="border-border/60 bg-card/60">
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
              Paid out to hosts
            </CardTitle>
          </CardHeader>
          <CardContent>
            {reconLoading ? (
              <Skeleton className="h-8 w-24" />
            ) : (
              <div className="text-2xl font-bold font-mono text-emerald-400">
                {formatUnits(recon?.total_settled_amount_units)}
              </div>
            )}
            <div className="text-xs text-muted-foreground mt-1 flex items-center gap-1">
              <TrendingUp className="h-3 w-3 text-emerald-400" /> Paid using vouchers
            </div>
          </CardContent>
        </Card>

        <Card className="border-border/60 bg-card/60">
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
              Records match
            </CardTitle>
          </CardHeader>
          <CardContent>
            {reconLoading ? (
              <Skeleton className="h-8 w-28" />
            ) : recon?.ledger_parity_verified ? (
              <div className="flex items-center gap-2 text-emerald-400 font-semibold text-lg">
                <CheckCircle2 className="h-5 w-5" />
                Records match
              </div>
            ) : (
              <div className="flex items-center gap-2 text-amber-400 font-semibold text-lg">
                <AlertCircle className="h-5 w-5" />
                Check still running
              </div>
            )}
            <div className="text-xs text-muted-foreground mt-1">
              ComputeCoin records compared with the blockchain
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Voucher Generation Tool */}
      <Card className="border-border/60 bg-card/60">
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Key className="h-4 w-4 text-primary" />
            Create a payment voucher
          </CardTitle>
          <CardDescription>
            A voucher is a signed note that lets a host collect their payment from the holding contract. Enter the job and the amounts, then create it.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="space-y-1">
              <Label className="text-xs">Job ID</Label>
              <Input
                placeholder="Job ID (e.g. 550e8400-e29b-41d4-a716-446655440000)"
                value={voucherJobId}
                onChange={(e) => setVoucherJobId(e.target.value)}
                className="font-mono text-xs"
              />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">
                Final cost (smallest units)
                <HelpTip term="smallest units">The token is counted in very small units. 1 COMPUTE is 1,000,000,000,000,000,000 of them (also called wei). The box starts at 40 COMPUTE.</HelpTip>
              </Label>
              <Input
                placeholder="40000000000000000000"
                value={finalCost}
                onChange={(e) => setFinalCost(e.target.value)}
                className="font-mono text-xs"
              />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">Platform fee (smallest units)</Label>
              <Input
                placeholder="4000000000000000000"
                value={platformFee}
                onChange={(e) => setPlatformFee(e.target.value)}
                className="font-mono text-xs"
              />
            </div>
          </div>

          <div className="flex items-center gap-3">
            <Button
              size="sm"
              disabled={!voucherJobId || issueVoucherMutation.isPending}
              onClick={() => issueVoucherMutation.mutate()}
            >
              Create voucher
            </Button>
            {selectedVoucher && (
              <span className="text-xs font-mono text-muted-foreground truncate max-w-xl">
                Voucher signature: {selectedVoucher.slice(0, 32)}...
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* On-Chain Escrows Table */}
      <Card className="border-border/60 bg-card/60">
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Lock className="h-4 w-4 text-cyan-400" />
            Held payments
          </CardTitle>
          <CardDescription>
            Each row is a payment held by the contract for one job. Settle pays the host. Refund gives the payment back to the customer. These two buttons only update ComputeCoin&apos;s records. They do not send anything to the blockchain, and the transaction code they save is a placeholder.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {escrowsLoading ? (
            <div className="space-y-2">
              {[...Array(3)].map((_, i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </div>
          ) : !escrows || escrows.length === 0 ? (
            <div className="text-center py-8 text-muted-foreground text-sm">
              No held payments yet. A row appears here when a job&apos;s payment is locked in the contract.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Job</TableHead>
                  <TableHead>Customer</TableHead>
                  <TableHead>Host</TableHead>
                  <TableHead>Amount held</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>
                    Matches records
                    <HelpTip term="matches records">Yes means this payment was checked against ComputeCoin&apos;s own records and they agree.</HelpTip>
                  </TableHead>
                  <TableHead className="text-right">What to do</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {escrows.map((escrow) => (
                  <TableRow key={escrow.id}>
                    <TableCell className="font-mono text-xs" title={escrow.job_id}>
                      {escrow.job_id.slice(0, 8)}...
                    </TableCell>
                    <TableCell className="font-mono text-xs" title={escrow.customer_id}>
                      {escrow.customer_id.slice(0, 8)}...
                    </TableCell>
                    <TableCell className="font-mono text-xs" title={escrow.provider_id}>
                      {escrow.provider_id.slice(0, 8)}...
                    </TableCell>
                    <TableCell className="font-mono text-xs font-semibold">
                      {formatUnits(escrow.locked_amount_units)}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant="outline"
                        className={
                          escrow.status === "settled"
                            ? "border-emerald-500/30 text-emerald-400 bg-emerald-500/10"
                            : escrow.status === "refunded"
                            ? "border-amber-500/30 text-amber-400 bg-amber-500/10"
                            : "border-cyan-500/30 text-cyan-400 bg-cyan-500/10"
                        }
                      >
                        {escrow.status === "locked" ? "Held" : escrow.status === "settled" ? "Paid out" : escrow.status === "refunded" ? "Refunded" : escrow.status}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      {escrow.reconciled ? (
                        <span className="text-xs text-emerald-400 flex items-center gap-1">
                          <CheckCircle2 className="h-3 w-3" /> Yes
                        </span>
                      ) : (
                        <span className="text-xs text-muted-foreground flex items-center gap-1">
                          Pending
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="text-right space-x-2">
                      {escrow.status === "locked" && (
                        <>
                          <Button
                            size="sm"
                            variant="outline"
                            className="text-xs h-7"
                            onClick={() => confirmSettlementMutation.mutate(escrow.job_id)}
                          >
                            Pay the host
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            className="text-xs h-7 text-rose-400 hover:text-rose-300"
                            onClick={() => confirmRefundMutation.mutate(escrow.job_id)}
                          >
                            Refund the customer
                          </Button>
                        </>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* On-Chain Transaction Audit Log */}
      <Card className="border-border/60 bg-card/60">
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Layers className="h-4 w-4 text-purple-400" />
            Blockchain transactions
          </CardTitle>
          <CardDescription>
            Payments seen on the blockchain, compared with ComputeCoin&apos;s own records. A check mark means they agree.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {txsLoading ? (
            <div className="space-y-2">
              {[...Array(3)].map((_, i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </div>
          ) : !txs || txs.length === 0 ? (
            <div className="text-center py-6 text-muted-foreground text-sm">
              No blockchain transactions yet. They appear here after a payment is made.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>
                    Transaction code
                    <HelpTip term="transaction code">A unique code that identifies one payment on the blockchain.</HelpTip>
                  </TableHead>
                  <TableHead>Kind</TableHead>
                  <TableHead>Sent from</TableHead>
                  <TableHead>Amount</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Matches records</TableHead>
                  <TableHead className="text-right">Time</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {txs.map((tx) => (
                  <TableRow key={tx.id}>
                    <TableCell className="font-mono text-xs">
                      {truncateAddress(tx.tx_hash)}
                    </TableCell>
                    <TableCell>
                      <Badge variant="neutral" className="font-mono text-[10px] uppercase">
                        {tx.tx_type}
                      </Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {truncateAddress(tx.sender_address)}
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {formatUnits(tx.amount_units)}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant="outline"
                        className={
                          tx.status === "reconciled" || tx.status === "confirmed"
                            ? "border-emerald-500/30 text-emerald-400"
                            : "border-amber-500/30 text-amber-400"
                        }
                      >
                        {tx.status}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      {tx.reconciled_with_ledger ? (
                        <CheckCircle2 className="h-4 w-4 text-emerald-400" />
                      ) : (
                        <span className="text-xs text-muted-foreground">—</span>
                      )}
                    </TableCell>
                    <TableCell className="text-right text-xs text-muted-foreground">
                      {new Date(tx.created_at).toLocaleTimeString()}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
