import type { Metadata } from "next";
import Link from "next/link";
import { PageHero } from "@/components/layout/page-hero";
import { Prose } from "@/components/content";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Cpu, ShieldCheck, Coins, ArrowRight } from "lucide-react";

export const metadata: Metadata = {
  title: "Technical Whitepaper",
  description:
    "ComputeCoin ($COMPUTE): a marketplace for AI GPU compute, settled on-chain. What is built, what is not, and the risks.",
};

const STATUS: Array<[string, string]> = [
  ["Web console, REST API, Python and TypeScript SDKs", "Built, in developer preview"],
  ["Provider agent (NVIDIA GPUs only)", "Built. Reported hardware is self-reported (\"Detected\"), not independently verified"],
  ["Job execution", "Container runner implemented. Not yet validated on real GPU hardware; jobs are simulated by default"],
  ["Customer credits and payments", "Credits are test credits. Stripe test-mode rail built; live payments not enabled"],
  ["COMPUTE token contract (ERC-20)", "Built and unit-tested. Not audited"],
  ["Settlement (escrow) contract", "Built and unit-tested. Not audited"],
  ["Public deployment", "Planned first on BNB Smart Chain testnet (no monetary value). Not on mainnet"],
  ["External security audit", "Not done"],
  ["Legal review of the token", "Not done"],
];

const DISTRIBUTION: Array<[string, string, string]> = [
  ["Liquidity", "10%", "Seeds the PancakeSwap pool; LP tokens locked or burned"],
  ["Treasury", "25%", "Multisig"],
  ["Ecosystem", "25%", "Vesting contract, linear over 48 months"],
  ["Team", "15%", "Vesting contract, 12-month cliff, then linear over 48 months"],
  ["Community", "15%", "Multisig"],
  ["Reserve", "10%", "Vesting contract, 6-month cliff, then linear over 24 months"],
];

export default function WhitepaperPage() {
  return (
    <>
      <PageHero
        eyebrow="Technical Whitepaper v1.1"
        title="ComputeCoin: AI compute, settled on-chain"
        description="A marketplace connecting NVIDIA GPU owners with developers, with optional on-chain escrow in the COMPUTE token. This page separates what is built from what is not."
      />

      <div className="container space-y-12 py-12">
        <div role="note" className="rounded-md border border-warning/40 bg-warning/10 px-4 py-3 text-sm">
          <p className="font-medium">Developer preview. Not an offer or investment advice.</p>
          <p className="text-muted-foreground">
            The smart contracts have not been audited by an independent firm. Nothing here is a promise of
            future value. Read the risks section before doing anything with the token.
          </p>
        </div>

        <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
          <Card>
            <CardHeader className="flex flex-row items-center gap-3 space-y-0 pb-2">
              <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-primary/10 text-brand-primary">
                <Cpu className="h-5 w-5" />
              </span>
              <CardTitle className="text-base">NVIDIA GPU marketplace</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              Providers run an agent on their GPUs; customers submit containerised jobs and are metered per second.
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex flex-row items-center gap-3 space-y-0 pb-2">
              <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-primary/10 text-brand-primary">
                <Coins className="h-5 w-5" />
              </span>
              <CardTitle className="text-base">$COMPUTE token</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              Fixed supply of 1,000,000,000, minted once with no mint function. Used as the settlement asset of the
              escrow contract.
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex flex-row items-center gap-3 space-y-0 pb-2">
              <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-primary/10 text-brand-primary">
                <ShieldCheck className="h-5 w-5" />
              </span>
              <CardTitle className="text-base">On-chain escrow</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              Budgets are locked per job, settled against a signed voucher, and refundable after the deadline with no
              voucher.
            </CardContent>
          </Card>
        </div>

        <div className="rounded-xl border border-border bg-surface p-6 sm:p-10">
          <Prose>
            <h2>1. Summary</h2>
            <p>
              AI teams rent GPUs from a few large clouds at high margins, while independent data centres and
              workstation owners leave capacity idle. ComputeCoin connects the two. Job scheduling and metering run
              off-chain for speed. An optional on-chain escrow settles payments in the <strong>$COMPUTE</strong>{" "}
              token on BNB Smart Chain.
            </p>

            <h2>2. Status today</h2>
            <div className="overflow-x-auto">
              <table>
                <thead>
                  <tr>
                    <th>Component</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {STATUS.map(([k, v]) => (
                    <tr key={k}>
                      <td>{k}</td>
                      <td>{v}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <h2>3. Architecture</h2>
            <ul>
              <li>
                <strong>Off-chain orchestration:</strong> an API schedules jobs, reserves machine capacity atomically
                (database row locks and expiring leases), meters runtime per second, and keeps a double-entry ledger
                in which debits equal credits.
              </li>
              <li>
                <strong>Providers:</strong> <code>computecoin-agent</code> detects NVIDIA GPUs through NVML, sends
                signed heartbeats every 30 seconds, and runs workloads in a locked-down container. A machine that
                misses heartbeats for 90 seconds is marked offline.
              </li>
              <li>
                <strong>Quotes and reservations:</strong> a quote fixes the price for a short window (120 seconds by
                default). Accepting it reserves funds; completion captures the metered cost, failure releases the
                hold.
              </li>
            </ul>

            <h2>4. The $COMPUTE token</h2>
            <ul>
              <li>ERC-20 with EIP-2612 permit, 18 decimals. Symbol <code>COMPUTE</code>.</li>
              <li>
                Supply of 1,000,000,000 is minted once, in the constructor. There is no mint function, so supply
                cannot increase.
              </li>
              <li>Any holder may burn their own tokens. Nothing burns tokens automatically.</li>
              <li>
                <strong>Pause switch:</strong> the token owner can pause all transfers, which also halts trading. Who
                holds that power (a multisig, a timelock, or nobody if ownership is renounced) is published with the
                launch.
              </li>
              <li>
                Today the token is the settlement asset of the escrow contract, and COMPUTE sent from a linked wallet
                can be credited to a platform balance. The credit deposit is built and tested but switched off by
                default.
              </li>
            </ul>

            <h3>Proposed distribution</h3>
            <p>Final figures and addresses are published at launch.</p>
            <div className="overflow-x-auto">
              <table>
                <thead>
                  <tr>
                    <th>Allocation</th>
                    <th>Share</th>
                    <th>Terms</th>
                  </tr>
                </thead>
                <tbody>
                  {DISTRIBUTION.map(([a, s, t]) => (
                    <tr key={a}>
                      <td>{a}</td>
                      <td>{s}</td>
                      <td>{t}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <h3>Not built</h3>
            <p>
              The following are ideas, not features. They are not implemented and nothing here commits the project to
              build them: provider staking and slashing, fee discounts for paying in COMPUTE, automatic buy-back and
              burn, on-chain governance voting, mining or emission rewards, and bridges to other chains.
            </p>

            <h2>5. Settlement contract</h2>
            <ul>
              <li>
                <strong>Lock:</strong> the customer locks the job&apos;s maximum budget. The escrow is keyed by the
                customer&apos;s address and the job id, so nobody can occupy another customer&apos;s job id.
              </li>
              <li>
                <strong>Settle:</strong> the provider, the fee treasury and the customer (unused budget) are paid
                against a voucher signed by the platform. A voucher is bound to the customer, job, amounts, nonce,
                expiry, chain and contract, so it cannot be replayed or moved.
              </li>
              <li>
                <strong>Refund:</strong> after the deadline the customer can always withdraw the locked amount with no
                voucher. Refunds keep working while the contract is paused.
              </li>
              <li>
                <strong>Fee cap:</strong> the protocol fee is capped at 30% of the final cost by an immutable
                constant, so a compromised signing key cannot sweep an escrow.
              </li>
              <li>
                <strong>What remains trusted:</strong> the final cost is set by a voucher the platform signs. The
                customer is protected by the budget they locked and by the deadline refund. The owner can rotate the
                signer and fee treasury and can pause new locks and settlements.
              </li>
            </ul>

            <h2>6. Trading</h2>
            <p>
              When the token is launched publicly, liquidity is seeded on PancakeSwap V2 on BNB Smart Chain against
              BNB. The first deposit sets the opening price, so the pool and its price are published before anyone
              trades. The tooling refuses to proceed if the pair was already funded by someone else, bounds the
              slippage of the deposit, and sends the LP tokens to a burn address or a time lock, so the project cannot
              withdraw the pool. The market price is set by trading, not by the project. Only the contract and pool
              addresses published here and on BscScan are genuine.
            </p>

            <h2>7. Risks</h2>
            <ul>
              <li><strong>Smart-contract risk:</strong> the contracts are unaudited; bugs can lose funds permanently.</li>
              <li>
                <strong>Control risk:</strong> the owner can pause token transfers and the settlement contract, and
                can change the platform signer and fee treasury.
              </li>
              <li><strong>Operator trust:</strong> metering and voucher signing are run by the platform.</li>
              <li><strong>Execution risk:</strong> real-GPU execution is unvalidated; jobs are simulated in this preview.</li>
              <li><strong>Market risk:</strong> a token can lose all of its value; liquidity may be thin and prices volatile.</li>
              <li><strong>Regulatory risk:</strong> the legal treatment of tokens differs by country and is not yet reviewed.</li>
            </ul>

            <h2>8. Roadmap (no dates are promised)</h2>
            <ol>
              <li>Deploy to BNB Smart Chain testnet, verify the contracts on BscScan, publish addresses, run a public testnet pool.</li>
              <li>Validate real GPU execution on hardware and enable live payments.</li>
              <li>Independent smart-contract audit; fix and re-verify every finding; freeze the audited bytecode.</li>
              <li>Legal review of the token and its distribution.</li>
              <li>Only after steps 3 and 4: consider a mainnet deployment.</li>
            </ol>
          </Prose>

          <div className="mt-10 flex flex-wrap gap-4 border-t border-border pt-6">
            <Button asChild>
              <Link href="/marketplace/gpus">
                Explore Marketplace <ArrowRight className="ml-2 h-4 w-4" />
              </Link>
            </Button>
            <Button asChild variant="outline">
              <Link href="/register">Join Developer Preview</Link>
            </Button>
          </div>
        </div>
      </div>
    </>
  );
}
