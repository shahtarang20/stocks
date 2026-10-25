import type { Metadata } from "next";
import Link from "next/link";
import { PageHero } from "@/components/layout/page-hero";
import { Prose } from "@/components/content";
import { Button } from "@/components/ui/button";

export const metadata: Metadata = {
  title: "Providers",
  description:
    "Supply GPU capacity to the ComputeCoin network with clear payout terms and no exclusivity.",
};

export default function ProvidersPage() {
  return (
    <>
      <PageHero
        eyebrow="Providers"
        title="Put idle GPUs to work"
        description="Operators and data centers can list capacity, keep utilization high, and get paid on clear terms. No exclusivity, no proprietary stack."
      />
      <div className="container space-y-8 py-12">
        <Prose>
          <h2>How it works for providers</h2>
          <ul>
            <li>Register hardware and complete the verification process for your target tier.</li>
            <li>Set availability and accept jobs that match your capacity.</li>
            <li>Workloads run in isolated containers with defined tenant-isolation obligations.</li>
            <li>Payouts are released from escrow against verifiable job completion.</li>
          </ul>
          <h2>Verification tiers</h2>
          <p>
            Buyers filter by assurance level, so higher verification generally
            means access to more demand. Tiers reflect the depth of attestation
            performed — from self-reported specs through to independently
            verified hardware and isolation controls.
          </p>
          <h2>What we do not claim</h2>
          <p>
            We do not promise guaranteed utilization or a fixed return. Earnings
            depend on real demand, your pricing, and your verification tier.
            Your provider console shows only what your machines have actually
            earned, straight from the ledger.
          </p>
        </Prose>
        <div className="flex flex-wrap gap-3">
          <Button asChild>
            <Link href="/provider/onboarding">Start provider onboarding</Link>
          </Button>
          <Button asChild variant="outline">
            <Link href="/security">Review security model</Link>
          </Button>
        </div>
      </div>
    </>
  );
}
