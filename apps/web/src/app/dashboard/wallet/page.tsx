"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, ExternalLink, Loader2, RefreshCw } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api, apiErrorCode, ApiError, CREDITS_LABEL } from "@/lib/api";
import { CopyField } from "@/components/provider/copy-field";
import { useAuth } from "@/lib/auth-context";
import { connectWallet, ensureChain, getWalletProvider, signMessage, WalletError } from "@/lib/ethereum";
import { formatRelativeTime } from "@/lib/utils";

const LINK_ERRORS: Record<string, string> = {
  email_not_verified: "Verify your email first. Use the banner at the top of the page.",
  address_linked_to_another_account: "That wallet is already linked to a different account.",
  account_already_has_a_wallet: "Your account already has a wallet on this network. Unlink it first to use another.",
  message_expired: "The signing request expired. Please try again.",
  signature_wrong_signer: "The signature came from a different wallet account than the one you selected.",
  signature_invalid: "The signature could not be verified. Please try again.",
  wrong_chain: "Your wallet is on a different network than this app uses.",
  invalid_address: "That does not look like a wallet address.",
};

function errorMessage(err: unknown): string {
  if (err instanceof WalletError) return err.message;
  const code = apiErrorCode(err);
  if (code && LINK_ERRORS[code]) return LINK_ERRORS[code];
  if (err instanceof ApiError) return err.status === 429 ? "Too many attempts. Please wait and try again." : err.message;
  return "Something went wrong. Please try again.";
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 py-2 text-sm">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-all text-right">{children}</dd>
    </div>
  );
}

export default function WalletPage() {
  const { accessToken } = useAuth();
  const token = accessToken as string;
  const queryClient = useQueryClient();
  const [step, setStep] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ["wallet"],
    enabled: Boolean(accessToken),
    queryFn: () => api.getWallet(token),
  });
  const data = query.data;

  const linkMut = useMutation({
    mutationFn: async () => {
      if (!data) throw new WalletError("not_ready", "Still loading. Try again in a moment.");
      setStep("Connecting to your wallet…");
      const address = await connectWallet();
      setStep(`Switching to ${data.chain.chain_name ?? `network ${data.chain.chain_id}`}…`);
      await ensureChain(data.chain.chain_id);
      setStep("Preparing the signing request…");
      const challenge = await api.walletLinkChallenge(token, address);
      setStep("Waiting for your signature…");
      const signature = await signMessage(address, challenge.message);
      setStep("Verifying…");
      return api.walletLink(token, { address, chain_id: challenge.chain_id, message: challenge.message, signature });
    },
    onSettled: () => setStep(null),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["wallet"] }),
  });

  const depositsEnabled = Boolean(data?.deposits?.enabled);
  const depositsQuery = useQuery({
    queryKey: ["wallet-deposits"],
    enabled: Boolean(accessToken) && depositsEnabled,
    queryFn: () => api.getWalletDeposits(token),
  });

  const unlinkMut = useMutation({
    mutationFn: () => api.walletUnlink(token),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["wallet"] }),
  });

  const hasWallet = typeof window !== "undefined" && Boolean(getWalletProvider());
  const net = data?.chain.chain_name ?? (data ? `chain ${data.chain.chain_id}` : "");
  const explorer = data?.chain.explorer_base?.replace(/\/$/, "");

  return (
    <>
      <ConsolePageHeader
        title="Wallet"
        description="Your credits on the platform, and your COMPUTE tokens on the blockchain. They are separate balances."
        action={
          <Button variant="outline" onClick={() => { query.refetch(); if (depositsEnabled) depositsQuery.refetch(); }} disabled={query.isFetching}>
            <RefreshCw className={query.isFetching ? "animate-spin" : undefined} />
            Refresh
          </Button>
        }
      />

      <div className="max-w-2xl space-y-6">
        <div role="note" className="rounded-md border border-warning/40 bg-warning/10 px-4 py-3 text-sm">
          <p className="font-medium">Test network, no monetary value</p>
          <p className="text-muted-foreground">
            COMPUTE tokens shown here exist on a test network only. Platform credits are simulated.
            Nothing on this page is money, and tokens are not converted to credits (or back) yet.
          </p>
        </div>

        {query.isError ? (
          <div role="alert" className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{errorMessage(query.error)}</span>
          </div>
        ) : null}

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              Platform credits <Badge variant="neutral">simulated</Badge>
            </CardTitle>
            <CardDescription>What you spend on jobs and hosts earn. Held in the platform ledger.</CardDescription>
          </CardHeader>
          <CardContent>
            {query.isLoading ? (
              <Skeleton className="h-16 w-full" />
            ) : (
              <dl className="divide-y divide-border">
                <Row label="Available">
                  <span className="font-mono tabular-nums" data-testid="credits-available">{data?.credits.available}</span>
                </Row>
                <Row label="Held by running jobs">
                  <span className="font-mono tabular-nums">{data?.credits.reserved}</span>
                </Row>
                <Row label="Total">
                  <span className="font-mono tabular-nums">{data?.credits.balance}</span>
                </Row>
              </dl>
            )}
            <p className="mt-2 text-xs text-muted-foreground">
              {CREDITS_LABEL}. See <Link href="/dashboard/billing" className="text-primary hover:underline">Billing</Link> for the full ledger.
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              On-chain wallet {data ? <Badge variant="outline">{net}</Badge> : null}
            </CardTitle>
            <CardDescription>Link a wallet you control to see your COMPUTE token balance.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {query.isLoading ? (
              <Skeleton className="h-24 w-full" />
            ) : data?.wallet ? (
              <>
                <dl className="divide-y divide-border">
                  <Row label="Address">
                    <span className="font-mono text-xs" data-testid="wallet-address">{data.wallet.address}</span>
                  </Row>
                  <Row label="Linked">{formatRelativeTime(data.wallet.linked_at)}</Row>
                  <Row label={`${data.chain.symbol} balance`}>
                    {data.token_balance !== null ? (
                      <span className="font-mono tabular-nums" data-testid="token-balance">
                        {data.token_balance} {data.chain.symbol}
                      </span>
                    ) : !data.chain.configured ? (
                      <span className="text-muted-foreground">Not available: this server has no chain connection configured.</span>
                    ) : (
                      <span className="text-muted-foreground">Unavailable right now. Try Refresh.</span>
                    )}
                  </Row>
                  <Row label="Token contract">
                    <span className="font-mono text-xs">{data.chain.token_address}</span>
                  </Row>
                </dl>
                <div className="flex flex-wrap items-center gap-3">
                  {explorer ? (
                    <Button asChild variant="outline" size="sm">
                      <a href={`${explorer}/address/${data.wallet.address}`} target="_blank" rel="noopener noreferrer">
                        View on explorer <ExternalLink />
                      </a>
                    </Button>
                  ) : null}
                  <Button variant="outline" size="sm" onClick={() => unlinkMut.mutate()} disabled={unlinkMut.isPending}>
                    {unlinkMut.isPending ? <Loader2 className="animate-spin" /> : null}
                    Unlink wallet
                  </Button>
                </div>
              </>
            ) : (
              <>
                <p className="text-sm text-muted-foreground">
                  Linking proves the wallet is yours. You sign a short message; no transaction is sent and
                  nothing is spent. Your wallet needs no funds to link.
                </p>
                {!hasWallet ? (
                  <p className="text-sm text-muted-foreground">
                    No browser wallet detected. Install MetaMask (or another wallet) and reload this page.
                  </p>
                ) : null}
                <Button onClick={() => linkMut.mutate()} disabled={linkMut.isPending || !data}>
                  {linkMut.isPending ? <Loader2 className="animate-spin" /> : null}
                  Connect and link wallet
                </Button>
                {step ? <p role="status" className="text-sm text-muted-foreground">{step}</p> : null}
              </>
            )}
            {linkMut.isError ? (
              <div role="alert" className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>{errorMessage(linkMut.error)}</span>
              </div>
            ) : null}
            {unlinkMut.isError ? (
              <div role="alert" className="text-sm text-danger">{errorMessage(unlinkMut.error)}</div>
            ) : null}
          </CardContent>
        </Card>

        {depositsEnabled && data?.deposits ? (
          <Card>
            <CardHeader>
              <CardTitle>Deposit COMPUTE for credits</CardTitle>
              <CardDescription>
                Send COMPUTE from your linked wallet to the address below and it is credited to your balance.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <dl className="divide-y divide-border">
                <Row label="Rate">
                  <span data-testid="deposit-rate">1 {data.chain.symbol} = {data.deposits.credits_per_token} credits</span>
                </Row>
                <Row label="Allowed size">
                  {data.deposits.min_tokens} to {data.deposits.max_tokens} {data.chain.symbol} per transfer
                </Row>
                <Row label="Credited after">{data.deposits.confirmations} block confirmations</Row>
              </dl>
              {data.wallet ? (
                <>
                  <p className="text-sm text-muted-foreground">Deposit address</p>
                  <CopyField value={data.deposits.address ?? ""} />
                  <p className="text-xs text-muted-foreground">
                    Send only from <span className="font-mono">{data.wallet.address}</span>. A transfer from any other
                    address is not credited automatically, and the tokens cannot be returned by this page.
                    Amounts are rounded down to a whole cent of credit.
                  </p>
                </>
              ) : (
                <p className="text-sm text-muted-foreground">Link your wallet above first. Deposits are only credited from a wallet you have linked.</p>
              )}
              <div>
                <h3 className="mb-2 text-sm font-medium">Your deposits</h3>
                {depositsQuery.isLoading ? (
                  <Skeleton className="h-10 w-full" />
                ) : (depositsQuery.data ?? []).length === 0 ? (
                  <p className="text-sm text-muted-foreground">No deposits yet.</p>
                ) : (
                  <ul className="divide-y divide-border text-sm" data-testid="deposit-list">
                    {(depositsQuery.data ?? []).map((d) => (
                      <li key={d.tx_hash} className="flex items-center justify-between gap-3 py-2">
                        <span className="min-w-0">
                          <span className="font-mono tabular-nums">{d.amount} {data.chain.symbol}</span>
                          <span className="ml-2 text-xs text-muted-foreground">{formatRelativeTime(d.created_at)}</span>
                        </span>
                        <span className="flex items-center gap-2">
                          {d.status === "credited" ? (
                            <span className="font-mono tabular-nums">+{d.credits} credits</span>
                          ) : (
                            <span className="text-xs text-muted-foreground">{d.reason?.replace(/_/g, " ")}</span>
                          )}
                          <Badge variant={d.status === "credited" ? "success" : "warning"}>{d.status}</Badge>
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </CardContent>
          </Card>
        ) : null}
      </div>
    </>
  );
}
