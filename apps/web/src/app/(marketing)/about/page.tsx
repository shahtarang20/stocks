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
        title="Computing power you can check"
        description="ComputeCoin is a marketplace where people rent graphics cards (GPUs) from other people. We want prices, usage and claims to be easy to check."
      />
      <div className="container space-y-8 py-12">
        <Prose>
          <h2>Why we are building this</h2>
          <p>
            Powerful graphics cards are expensive, and a few big companies rent out most of them.
            Many people own a good graphics card that sits idle most of the day. We want to connect
            the two groups. This only works if we are honest about what we can and cannot prove.
          </p>
          <h2>Our principles</h2>
          <ul>
            <li>Measured, not promised: we show how numbers are worked out.</li>
            <li>No coin hype: we never show a coin price or trading numbers on this site.</li>
            <li>Fair billing: you pay for the time a job really ran.</li>
            <li>No lock-in: you can bring your own containers and tools.</li>
          </ul>
          <h2>Where we are</h2>
          <p>
            This is a preview. You can create an account, add test credits, send jobs and connect a
            computer as a host. Jobs run in practice mode (simulated) by default, so results are
            placeholders. Credits are test credits, not real money. Anything that is example data is
            labelled as such.
          </p>
          <h2>Contact</h2>
          <p>
            For security reports, partnership questions or help with becoming a host, write to{" "}
            <strong>hello@computecoin.example</strong>. This is a placeholder address while we are in
            preview, so it may not reach anyone yet.
          </p>
        </Prose>
      </div>
    </>
  );
}
