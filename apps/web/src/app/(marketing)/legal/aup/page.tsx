import type { Metadata } from "next";
import { PageHero } from "@/components/layout/page-hero";
import { Prose } from "@/components/content";

export const metadata: Metadata = {
  title: "Acceptable Use Policy (DRAFT)",
  description: "DRAFT acceptable use policy for ComputeCoin. Not in force; pending legal review.",
};

export default function AupPage() {
  return (
    <>
      <PageHero
        eyebrow="Legal"
        title="Acceptable Use Policy (DRAFT)"
        description="DRAFT for counsel. This text is not final, is not in force, and must be reviewed and approved by legal counsel before it is relied on or required at sign-up."
      />
      <div className="container space-y-8 py-12">
        <div className="max-w-3xl rounded-md border border-border bg-surface px-4 py-3 text-sm">
          <p className="font-medium">In short</p>
          <p className="mt-1 text-muted-foreground">
            This is a draft and is not in force yet. It says what jobs are not allowed (for example coin mining, malware and illegal content) and that hosts must run the unchanged ComputeCoin agent and must not look at customer data. It still needs a lawyer to review it.
          </p>
        </div>
        <Prose>
          <p className="text-xs">DRAFT version: draft-0. Not legal advice. Do not rely on this text.</p>
          <h2>1. Scope</h2>
          <p>
            This policy applies to everyone who submits workloads (customers) and
            everyone who offers machines (hosts).
          </p>
          <h2>2. Prohibited workloads</h2>
          <ul>
            <li>
              Cryptocurrency mining or any workload whose purpose is to earn
              mining rewards, including connections to mining pools.
            </li>
            <li>Malware, botnets, denial-of-service tools, or attempts to attack other systems or the platform.</li>
            <li>Generating, storing or distributing content that is illegal where it is processed or where the customer is located.</li>
            <li>Workloads that infringe others&apos; rights or violate applicable law.</li>
            <li>Attempts to evade isolation, metering, verification or this policy.</li>
          </ul>
          <h2>3. Host obligations</h2>
          <p>
            Hosts must run the platform agent unmodified, must not inspect
            customer data beyond what their privacy tier permits, and must not
            run their own workloads on capacity that has been sold.
          </p>
          <h2>4. Monitoring and enforcement</h2>
          <p>
            The platform may detect suspected prohibited use from resource
            utilisation patterns and network behaviour, stop affected jobs,
            suspend new submissions pending review, and withhold or reverse
            payments where permitted. Automated signals can be wrong; flagged
            accounts are reviewed by a person. [Counsel to define review times
            and the appeal process.]
          </p>
          <h2>5. Reporting and takedown</h2>
          <p>
            Report suspected abuse from your account in the console. [Counsel to
            define notice formats, response times and the counter-notice process.]
          </p>
        </Prose>
      </div>
    </>
  );
}
