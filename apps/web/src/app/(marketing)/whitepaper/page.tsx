import type { Metadata } from "next";
import Link from "next/link";
import { PageHero } from "@/components/layout/page-hero";
import { Prose } from "@/components/content";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Cpu, ShieldCheck, Coins, Database, Lock, ArrowRight } from "lucide-react";

export const metadata: Metadata = {
  title: "Technical Whitepaper",
  description:
    "ComputeCoin ($COMPUTE): A Trust-Minimized Decentralized AI GPU Compute & Settlement Network.",
};

export default function WhitepaperPage() {
  return (
    <>
      <PageHero
        eyebrow="Technical Whitepaper v1.0"
        title="ComputeCoin: Trust-Minimized AI Compute"
        description="A decentralized physical infrastructure network (DePIN) connecting high-performance GPU hardware with developers through verifiable off-chain execution and non-custodial smart contract settlement."
      />

      <div className="container space-y-12 py-12">
        {/* Quick Highlights Grid */}
        <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
          <Card>
            <CardHeader className="flex flex-row items-center gap-3 space-y-0 pb-2">
              <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-primary/10 text-brand-primary">
                <Cpu className="h-5 w-5" />
              </span>
              <CardTitle className="text-base">Bare-Metal AI Compute</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              Permissionless GPU node registration with autonomous hardware detection
              for NVIDIA H100, A100, and RTX 4090 clusters.
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex flex-row items-center gap-3 space-y-0 pb-2">
              <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-primary/10 text-brand-primary">
                <Coins className="h-5 w-5" />
              </span>
              <CardTitle className="text-base">$COMPUTE Tokenomics</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              Fixed 1B supply, 20% discount on protocol fees, provider slashing bonding,
              and deflationary 25% protocol fee buy-back and burn.
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex flex-row items-center gap-3 space-y-0 pb-2">
              <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-primary/10 text-brand-primary">
                <ShieldCheck className="h-5 w-5" />
              </span>
              <CardTitle className="text-base">On-Chain Escrow</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              Ethereum & Base smart contracts guarantee non-custodial lock, deadline
              refunds, and cryptographic settlement proofs.
            </CardContent>
          </Card>
        </div>

        {/* Full Whitepaper Content */}
        <div className="rounded-xl border border-border bg-surface p-6 sm:p-10">
          <Prose>
            <h2>1. Executive Summary & Problem</h2>
            <p>
              Access to AI compute is currently dominated by centralized cloud
              hyperscalers who impose massive markups, opaque queuing, and rigid multi-month
              lock-in contracts. Meanwhile, independent data centers and compute clusters
              globally possess vast reserves of underutilized GPU hardware with no frictionless
              way to monetize them.
            </p>
            <p>
              <strong>ComputeCoin</strong> bridges this divide by providing an open,
              trust-minimized marketplace for AI GPU workloads. By separating high-speed
              off-chain scheduling from on-chain escrow and settlement, ComputeCoin delivers
              cloud-native performance with the security and transparency of decentralized
              networks.
            </p>

            <h2>2. Core Architectural Pillars</h2>
            <ul>
              <li>
                <strong>Autonomous Provider Daemon:</strong> The lightweight, proprietary{" "}
                <code>computecoin-agent</code> inspects the host&apos;s NVIDIA GPUs through NVML,
                registers the hardware it detects (self-reported and labelled &quot;Detected&quot;, not
                independently verified), and streams cryptographically signed heartbeats every 30
                seconds. Only NVIDIA GPUs are supported in this release; AMD (ROCm), Intel and
                Apple GPUs are not.
              </li>
              <li>
                <strong>Atomic Capacity Leases:</strong> Prevents double-booking across
                concurrent global quote requests using serialized PostgreSQL row locks and
                TTL-bounded Redis leases.
              </li>
              <li>
                <strong>Strict Invariants:</strong> Balance conservation enforces that sum of
                debits equals sum of credits at all times, with mathematical zero-synthetic-debt
                constraints.
              </li>
              <li>
                <strong>Containerized Workload Isolation:</strong> Workloads execute in
                hardened, isolated container sandboxes with strictly metered memory, storage,
                and network egress boundaries.
              </li>
            </ul>

            <h2>3. $COMPUTE Token Economics</h2>
            <p>
              The <strong>$COMPUTE</strong> token is an ERC-20 utility asset deployed on Ethereum
              and Base with a fixed genesis supply of <strong>1,000,000,000 tokens</strong>.
            </p>
            <ul>
              <li>
                <strong>40% Provider Rewards:</strong> Distributed over 48 months to hardware
                operators based on verified uptime and benchmarked FLOPS delivered.
              </li>
              <li>
                <strong>Fee Utility:</strong> Payments settled in $COMPUTE enjoy a 20% discount on
                network fees.
              </li>
              <li>
                <strong>Provider Staking:</strong> Operators stake $COMPUTE as a fidelity bond;
                unauthorized drops or fraudulent telemetry trigger automated stake slashing.
              </li>
              <li>
                <strong>Deflationary Burn:</strong> 25% of protocol revenue is routed to buy
                and permanently burn $COMPUTE on-chain.
              </li>
            </ul>

            <h2>4. Smart Contract Security & Verification</h2>
            <p>
              The settlement architecture relies on <code>ComputeSettlement.sol</code>:
            </p>
            <ul>
              <li>
                <strong>Non-Custodial Escrow:</strong> Customer tokens are locked at quote
                acceptance and released only upon cryptographic proof of job completion.
              </li>
              <li>
                <strong>Deadline Refund Protection:</strong> If a job fails or times out,
                customers can trigger an instant, unconditional refund directly on-chain.
              </li>
              <li>
                <strong>Circuit Breaker:</strong> Granular emergency pause functionality to
                safeguard funds against external cross-chain bridge disruptions.
              </li>
            </ul>

            <h2>5. Phased Roadmap</h2>
            <ol>
              <li>
                <strong>Phase 1 (Completed):</strong> Developer preview console, marketplace UI,
                fast API orchestration, and dual-mode auth live in production.
              </li>
              <li>
                <strong>Phase 2 (Current):</strong> Public Sepolia testnet deployment, provider node
                enrollment, and community faucets.
              </li>
              <li>
                <strong>Phase 3 (Next):</strong> External security audit (CertiK/OpenZeppelin),
                Base L2 mainnet deployment, and decentralized liquidity pool pairing.
              </li>
              <li>
                <strong>Phase 4:</strong> Zero-knowledge verification (zk-SNARKs) for trustless
                distributed machine learning inference.
              </li>
            </ol>
          </Prose>

          <div className="mt-10 flex flex-wrap gap-4 border-t border-border pt-6">
            <Button asChild>
              <Link href="/marketplace/gpus">
                Explore Marketplace <ArrowRight className="ml-2 h-4 w-4" />
              </Link>
            </Button>
            <Button asChild variant="outline">
              <Link href="/register">
                Join Developer Preview
              </Link>
            </Button>
          </div>
        </div>
      </div>
    </>
  );
}
