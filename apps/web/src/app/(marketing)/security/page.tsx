import type { Metadata } from "next";
import { PageHero } from "@/components/layout/page-hero";
import { Prose } from "@/components/content";
import { DemoBanner } from "@/components/demo-banner";

export const metadata: Metadata = {
  title: "Security",
  description:
    "ComputeCoin security model: isolation, attestation, escrowed settlement, and honest Phase 1 status.",
};

export default function SecurityPage() {
  return (
    <>
      <PageHero
        eyebrow="Security"
        title="Security model"
        description="How workloads are isolated, how providers are attested, and how settlement is protected. We describe the design and are explicit about what is implemented versus planned."
      />
      <div className="container space-y-8 py-12">
        <DemoBanner>
          this page describes the intended security model. Several controls are
          in design or partial implementation during Phase 1 and are noted as
          such below. Nothing here should be read as a completed audit.
        </DemoBanner>
        <Prose>
          <h2>Workload isolation</h2>
          <p>
            Workloads run in isolated containers. Providers accept defined
            tenant-isolation obligations as a condition of listing. Independent
            verification of isolation controls is part of the higher
            attestation tiers.
          </p>
          <h2>Provider attestation</h2>
          <p>
            Providers are placed into verification tiers ranging from
            self-reported specifications to independently verified hardware.
            Buyers choose the minimum tier acceptable for a given workload.
          </p>
          <h2>Settlement integrity</h2>
          <p>
            Payment is escrowed when a job is provisioned and released against
            verifiable completion signals, reducing counterparty risk for both
            sides of the market.
          </p>
          <h2>Account security (Phase 1 status)</h2>
          <ul>
            <li>
              <strong>Implemented:</strong> email/password auth with access and
              refresh tokens issued by the API.
            </li>
            <li>
              <strong>Implemented:</strong> the web console signs you in with
              httpOnly, Secure cookies set by the API, with CSRF protection.
              Page scripts cannot read them, and no token is written to
              browser storage.
            </li>
            <li>
              <strong>Smart contracts:</strong> the token and escrow contracts
              have unit tests but have <em>not</em> been audited by an
              independent firm.
            </li>
            <li>
              <strong>Enforcement:</strong> authorization is enforced
              server-side on the API. Client-side route guards are UX only.
            </li>
          </ul>
          <h2>Responsible disclosure</h2>
          <p>
            A coordinated disclosure process will be published before general
            availability. During the preview, report concerns through the
            contact channel on the About page.
          </p>
        </Prose>
      </div>
    </>
  );
}
