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
import { connectWallet, ensureChain, getWalletProvider, isMainnetChain, signMessage, WalletError } from "@/lib/ethereum";
import { CopyButton, RelativeTime } from "@/components/console/bits";
import { useToast } from "@/components/console/toast";
import { HelpTip } from "@/components/ui/help-tip";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

const LINK_ERRORS: Record<string, string> = {
  email_not_verified: "Please confirm your email first. Use the button at the top of the page to get a new link.",
  address_linked_to_another_account: "That wallet is already linked to a different ComputeCoin account. Use another wallet, or unlink it from the other account first.",
  account_already_has_a_wallet: "Your account already has a wallet linked. Unlink it first if you want to use a different one.",
  message_expired: "The request timed out before you approved it. Press the button to try again.",
  signature_wrong_signer: "You approved with a different wallet account than the one we asked for. Pick the same account in your wallet and try again.",
  signature_invalid: "We could not check your approval. Press the button to try again.",
  wrong_chain: "Your wallet is set to a different network than this site uses. Switch networks in your wallet, then try again.",
  invalid_address: "That does not look like a wallet address. Check it and try again.",
};

function errorMessage(err: unknown): string {
  if (err instanceof WalletError) return err.message;
  const code = apiErrorCode(err);
  if (code && LINK_ERRORS[code]) return LINK_ERRORS[code];
  if (err instanceof ApiError) return err.status === 429 ? "Too many tries. Wait a minute, then try again." : err.message;
  return "Something went wrong. Check your internet connection and try again.";
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
  const [confirmUnlink, setConfirmUnlink] = useState(false);
  const toast = useToast();

  const query = useQuery({
    queryKey: ["wallet"],
    enabled: Boolean(accessToken),
    queryFn: () => api.getWallet(token),
  });
  const data = query.data;

  const linkMut = useMutation({
    mutationFn: async () => {
      if (!data) throw new WalletError("not_ready", "This page is still loading. Wait a moment, then try again.");
      setStep("Opening your wallet. Approve the connection in the wallet window…");
      const address = await connectWallet();
      setStep(`Switching to ${data.chain.chain_name ?? `network ${data.chain.chain_id}`}…`);
      await ensureChain(data.chain.chain_id);
      setStep("Getting a short message for you to sign…");
      const challenge = await api.walletLinkChallenge(token, address);
      setStep("Waiting for you to sign the message in your wallet…");
      const signature = await signMessage(address, challenge.message);
      setStep("Checking your signature…");
      return api.walletLink(token, { address, chain_id: challenge.chain_id, message: challenge.message, signature });
    },
    onSettled: () => setStep(null),
    onSuccess: () => {
      toast.success("Your wallet is linked.");
      return queryClient.invalidateQueries({ queryKey: ["wallet"] });
    },
  });

  const depositsEnabled = Boolean(data?.deposits?.enabled);
  const depositsQuery = useQuery({
    queryKey: ["wallet-deposits"],
    enabled: Boolean(accessToken) && depositsEnabled,
    queryFn: () => api.getWalletDeposits(token),
  });

  const unlinkMut = useMutation({
    mutationFn: () => api.walletUnlink(token),
    onSuccess: () => {
      toast.success("Your wallet is unlinked.");
      setConfirmUnlink(false);
      return queryClient.invalidateQueries({ queryKey: ["wallet"] });
    },
  });

  const hasWallet = typeof window !== "undefined" && Boolean(getWalletProvider());
  const net = data?.chain.chain_name ?? (data ? `chain ${data.chain.chain_id}` : "");
  const explorer = data?.chain.explorer_base?.replace(/\/$/, "");

  return (
    <>
      <ConsolePageHeader
        title="Crypto wallet"
        description="This page is optional. It shows your platform credits next to your COMPUTE tokens, which live on a blockchain in a crypto wallet you control (like MetaMask). They are two separate balances. You do not need a crypto wallet to run jobs."
        action={
          <Button variant="outline" onClick={() => { query.refetch(); if (depositsEnabled) depositsQuery.refetch(); }} disabled={query.isFetching}>
            <RefreshCw className={query.isFetching ? "animate-spin" : undefined} />
            Refresh
          </Button>
        }
      />

      <div className="max-w-2xl space-y-6">
        {data && isMainnetChain(data.chain.chain_id) ? (
          <div role="note" className="rounded-md border border-warning/40 bg-warning/10 px-4 py-3 text-sm">
            <p className="font-medium">Your COMPUTE tokens are real. Your platform credits are not.</p>
            <p className="text-muted-foreground">
              The token balance below is held on {net} and is controlled by your own wallet. Platform credits are
              test credits. You cannot turn tokens into credits, or credits into tokens, yet.
            </p>
          </div>
        ) : (
          <div role="note" className="rounded-md border border-warning/40 bg-warning/10 px-4 py-3 text-sm">
            <p className="font-medium">Test network: these coins are worth nothing</p>
            <p className="text-muted-foreground">
              The COMPUTE tokens shown here live on a practice blockchain (a test network). Platform credits are
              test credits too. Nothing on this page is real money. You cannot turn tokens into credits, or credits into tokens, yet.
            </p>
          </div>
        )}

        {query.isError ? (
          <div role="alert" className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            <span className="flex-1">{errorMessage(query.error)}</span>
            <Button size="sm" variant="outline" onClick={() => query.refetch()} disabled={query.isFetching}>
              <RefreshCw className={query.isFetching ? "animate-spin" : undefined} /> Try again
            </Button>
          </div>
        ) : null}

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              Platform credits <Badge variant="neutral">test credits</Badge>
            </CardTitle>
            <CardDescription>What you spend on jobs, and what hosts earn. These are kept by ComputeCoin, not on the blockchain.</CardDescription>
          </CardHeader>
          <CardContent>
            {query.isLoading ? (
              <Skeleton className="h-16 w-full" />
            ) : (
              <dl className="divide-y divide-border">
                <Row label="Ready to spend">
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
              {CREDITS_LABEL}. Go to <Link href="/dashboard/billing" className="text-primary hover:underline">Credits and billing</Link> to add credits and see your history.
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              Crypto wallet {data ? <Badge variant="outline">{net}</Badge> : null}
            </CardTitle>
            <CardDescription>Connect a wallet you own to see how many COMPUTE tokens it holds.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {query.isLoading ? (
              <Skeleton className="h-24 w-full" />
            ) : data?.wallet ? (
              <>
                <dl className="divide-y divide-border">
                  <Row label="Address">
                    <span className="inline-flex items-center gap-1">
                      <span className="font-mono text-xs" data-testid="wallet-address">{data.wallet.address}</span>
                      <CopyButton value={data.wallet.address} label="Copy wallet address" />
                    </span>
                  </Row>
                  <Row label="Connected"><RelativeTime iso={data.wallet.linked_at} /></Row>
                  <Row label={`${data.chain.symbol} balance`}>
                    {data.token_balance !== null ? (
                      <span className="font-mono tabular-nums" data-testid="token-balance">
                        {data.token_balance} {data.chain.symbol}
                      </span>
                    ) : !data.chain.configured ? (
                      <span className="text-muted-foreground">Not available: this site is not connected to the blockchain yet.</span>
                    ) : (
                      <span className="text-muted-foreground">Not available right now. Press Refresh to try again.</span>
                    )}
                  </Row>
                  <Row label="Token address">
                    <span className="inline-flex items-center gap-1">
                      <span className="font-mono text-xs">{data.chain.token_address}</span>
                      {data.chain.token_address ? <CopyButton value={data.chain.token_address} label="Copy token contract address" /> : null}
                    </span>
                  </Row>
                </dl>
                <div className="flex flex-wrap items-center gap-3">
                  {explorer ? (
                    <Button asChild variant="outline" size="sm">
                      <a href={`${explorer}/address/${data.wallet.address}`} target="_blank" rel="noopener noreferrer">
                        See it on the blockchain explorer <ExternalLink />
                      </a>
                    </Button>
                  ) : null}
                  <Button variant="outline" size="sm" onClick={() => { unlinkMut.reset(); setConfirmUnlink(true); }}>
                    Disconnect wallet
                  </Button>
                </div>
              </>
            ) : (
              <>
                <p className="text-sm text-muted-foreground">
                  Connecting proves the wallet is yours. You sign a short message in your wallet. No payment is made and nothing is spent. Your wallet does not need any coins to connect.
                </p>
                {!hasWallet ? (
                  <p className="text-sm text-muted-foreground">
                    We could not find a crypto wallet in this browser. Install a wallet such as MetaMask, then reload this page.
                  </p>
                ) : null}
                <Button onClick={() => linkMut.mutate()} disabled={linkMut.isPending || !data}>
                  {linkMut.isPending ? <Loader2 className="animate-spin" /> : null}
                  Connect my wallet
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
          </CardContent>
        </Card>

        {depositsEnabled && data?.deposits ? (
          <Card>
            <CardHeader>
              <CardTitle>Turn COMPUTE tokens into credits</CardTitle>
              <CardDescription>
                Send COMPUTE tokens from your connected wallet to the address below. After the transfer is confirmed, credits are added to your account.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <dl className="divide-y divide-border">
                <Row label="Rate">
                  <span data-testid="deposit-rate">1 {data.chain.symbol} = {data.deposits.credits_per_token} credits</span>
                </Row>
                <Row label="Allowed amount">
                  {data.deposits.min_tokens} to {data.deposits.max_tokens} {data.chain.symbol} per transfer
                </Row>
                <Row label="Credits are added after">
                  {data.deposits.confirmations} confirmations
                  <HelpTip term="confirmations">
                    A confirmation is a new block added to the blockchain after your transfer. More confirmations means it is safer. Waiting takes a few minutes.
                  </HelpTip>
                </Row>
              </dl>
              {data.wallet ? (
                <>
                  <p className="text-sm text-muted-foreground">Deposit address</p>
                  <CopyField value={data.deposits.address ?? ""} />
                  <p className="text-xs text-muted-foreground">
                    Send only from <span className="font-mono">{data.wallet.address}</span>. A transfer from any other
                    address will not turn into credits, and this page cannot give the tokens back.
                    Amounts are rounded down to the nearest cent of credit.
                  </p>
                </>
              ) : (
                <p className="text-sm text-muted-foreground">Connect your wallet above first. We only add credits for transfers from a wallet you have connected.</p>
              )}
              <div>
                <h3 className="mb-2 text-sm font-medium">Your transfers</h3>
                {depositsQuery.isLoading ? (
                  <Skeleton className="h-10 w-full" />
                ) : (depositsQuery.data ?? []).length === 0 ? (
                  <p className="text-sm text-muted-foreground">You have not sent any tokens yet.</p>
                ) : (
                  <ul className="divide-y divide-border text-sm" data-testid="deposit-list">
                    {(depositsQuery.data ?? []).map((d) => (
                      <li key={d.tx_hash} className="flex items-center justify-between gap-3 py-2">
                        <span className="min-w-0">
                          <span className="font-mono tabular-nums">{d.amount} {data.chain.symbol}</span>
                          <span className="ml-2 text-xs text-muted-foreground"><RelativeTime iso={d.created_at} /></span>
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

      <Dialog open={confirmUnlink} onOpenChange={(open) => { if (!open) setConfirmUnlink(false); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Disconnect this wallet?</DialogTitle>
            <DialogDescription>
              Your tokens stay in your wallet. Nothing is moved or spent. You will need to connect a wallet
              again before you can turn tokens into credits.
            </DialogDescription>
          </DialogHeader>
          {unlinkMut.isError ? (
            <div role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
              {errorMessage(unlinkMut.error)}
            </div>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmUnlink(false)} disabled={unlinkMut.isPending}>
              Keep it connected
            </Button>
            <Button variant="danger" onClick={() => unlinkMut.mutate()} disabled={unlinkMut.isPending}>
              {unlinkMut.isPending ? <Loader2 className="animate-spin" /> : null}
              Disconnect wallet
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
