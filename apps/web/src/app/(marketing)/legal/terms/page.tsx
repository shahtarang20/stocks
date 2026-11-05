import type { Metadata } from "next";
import Link from "next/link";
import { PageHero } from "@/components/layout/page-hero";
import { Prose } from "@/components/content";

export const metadata: Metadata = {
  title: "Terms of Service",
  description: "Preview terms for ComputeCoin.",
};

export default function TermsPage() {
  return (
    <>
      <PageHero
        eyebrow="Legal"
        title="Terms of Service"
        description="Preview terms for the developer preview. These are a plain-language placeholder and are not final legal terms."
      />
      <div className="container space-y-8 py-12">
        <div className="max-w-3xl rounded-md border border-border bg-surface px-4 py-3 text-sm">
          <p className="font-medium">In short</p>
          <p className="mt-1 text-muted-foreground">
            This is a preview. Things can change or break, and example data is not real. Nothing here promises that the COMPUTE coin has or will keep any value. Credits are not coins. These are placeholder terms, not final legal terms.
          </p>
        </div>
        <Prose>
          <p className="text-xs">Last updated: preview version.</p>
          <h2>1. Preview service</h2>
          <p>
            ComputeCoin is provided as a developer preview. Features may change,
            break, or be removed. Demo data shown in the product is synthetic
            and must not be relied upon as real availability, performance, or
            pricing.
          </p>
          <h2>2. Accounts</h2>
          <p>
            You are responsible for maintaining the confidentiality of your
            credentials. Authorization is enforced by the API. You agree not to
            misuse the service or attempt to disrupt other users&apos; workloads.
          </p>
          <h2>3. Acceptable use</h2>
          <p>
            Workloads must comply with applicable law and must not infringe
            others&apos; rights. Providers must honor the isolation and
            availability obligations of their verification tier. See the{" "}
            <Link href="/legal/aup">Acceptable Use Policy</Link> (draft).
          </p>
          <h2>4. Payments and settlement</h2>
          <p>
            Where live compute is enabled, charges are metered per second and
            settled from escrow against verifiable completion. No token price or
            trading activity is offered through this product.
          </p>
          <h2>5. The COMPUTE token</h2>
          <p>
            COMPUTE is a software token used to settle compute jobs on this
            platform. This service does not sell it, set its price, or promise
            that it has or will keep any value, and nothing here is financial,
            investment, legal or tax advice. Where the token exists on a test
            network it has no monetary value. The token contracts have not been
            independently audited, and token owners can pause transfers.
            Platform credits are separate from tokens and are not convertible
            to them. You are responsible for checking whether holding or
            trading a token is lawful where you live.
          </p>
          <h2>6. No warranty</h2>
          <p>
            The preview is provided &quot;as is&quot; without warranties of any
            kind. We do not warrant that decentralized compute will be cheaper,
            faster, or more reliable than alternatives.
          </p>
          <h2>7. Changes</h2>
          <p>
            Final terms will be published before general availability and will
            supersede these preview terms.
          </p>
        </Prose>
      </div>
    </>
  );
}
