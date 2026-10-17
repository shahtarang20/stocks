import type { Metadata } from "next";
import { PageHero } from "@/components/layout/page-hero";
import { Prose } from "@/components/content";

export const metadata: Metadata = {
  title: "About",
  description: "What ComputeCoin is building and the principles behind it.",
};

export default function AboutPage() {
  return (
    <>
      <PageHero
        eyebrow="About"
        title="Compute you can account for"
        description="ComputeCoin is building an open marketplace for AI GPU compute — one where supply is permissionless, metering is transparent, and claims are measured rather than asserted."
      />
      <div className="container space-y-8 py-12">
        <Prose>
          <h2>Why we are building this</h2>
          <p>
            Access to GPU capacity is concentrated and often opaque. An open
            network can widen supply and make pricing and performance legible —
            but only if it is honest about what it can and cannot prove. That
            honesty is a product requirement for us, not a marketing choice.
          </p>
          <h2>Our principles</h2>
          <ul>
            <li>Measured, not promised: we show how numbers are produced.</li>
            <li>No token theater: no price, market cap, or trading volume anywhere.</li>
            <li>Verifiable settlement: escrow released against real completion.</li>
            <li>Choice over lock-in: standard containers and tooling.</li>
          </ul>
          <h2>Where we are</h2>
          <p>
            This is a Phase 1 developer preview. The marketing surface and the
            account/console shell are real; live provisioning, job scheduling,
            and billing are under construction and are clearly labelled as demo
            wherever placeholder data appears.
          </p>
          <h2>Contact</h2>
          <p>
            For security reports, partnership inquiries, or provider onboarding
            questions during the preview, reach the team at{" "}
            <strong>hello@computecoin.example</strong>. (Placeholder address for
            the Phase 1 preview.)
          </p>
        </Prose>
      </div>
    </>
  );
}
