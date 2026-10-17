import type { Metadata } from "next";
import { PageHero } from "@/components/layout/page-hero";
import { Prose } from "@/components/content";

export const metadata: Metadata = {
  title: "Privacy Policy",
  description: "Phase 1 preview privacy policy for ComputeCoin.",
};

export default function PrivacyPage() {
  return (
    <>
      <PageHero
        eyebrow="Legal"
        title="Privacy Policy"
        description="How the Phase 1 preview handles your data. A plain-language placeholder, not final legal terms."
      />
      <div className="container space-y-8 py-12">
        <Prose>
          <p className="text-xs">Last updated: Phase 1 preview.</p>
          <h2>Data we collect</h2>
          <p>
            For accounts, we process your email address and a hashed password
            via the API. When you use the console, we process project and usage
            metadata needed to operate the service.
          </p>
          <h2>Tokens in your browser</h2>
          <p>
            During Phase 1, the console stores authentication tokens in your
            browser&apos;s local storage for development convenience. This is a
            known limitation; tokens will move to secure, httpOnly cookies
            before production. Clearing your browser storage signs you out.
          </p>
          <h2>How we use data</h2>
          <ul>
            <li>To authenticate you and operate your account.</li>
            <li>To provide and improve the preview console.</li>
            <li>To meet legal and security obligations.</li>
          </ul>
          <h2>What we do not do</h2>
          <p>
            We do not sell your personal data, and this product does not offer
            or track any token price, market cap, or trading activity.
          </p>
          <h2>Your choices</h2>
          <p>
            You may request deletion of your preview account by contacting the
            team. Final data-handling terms will be published before general
            availability.
          </p>
        </Prose>
      </div>
    </>
  );
}
