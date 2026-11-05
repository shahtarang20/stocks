import type { Metadata } from "next";
import { PageHero } from "@/components/layout/page-hero";
import { Prose } from "@/components/content";

export const metadata: Metadata = {
  title: "Privacy Policy",
  description: "Preview privacy policy for ComputeCoin.",
};

export default function PrivacyPage() {
  return (
    <>
      <PageHero
        eyebrow="Legal"
        title="Privacy Policy"
        description="How the preview handles your data. A plain-language placeholder, not final legal terms."
      />
      <div className="container space-y-8 py-12">
        <div className="max-w-3xl rounded-md border border-border bg-surface px-4 py-3 text-sm">
          <p className="font-medium">In short</p>
          <p className="mt-1 text-muted-foreground">
            We keep your email address and a scrambled form of your password. We use cookies only to keep you signed in. We do not sell your personal data. This is a plain-language placeholder, not final legal terms.
          </p>
        </div>
        <Prose>
          <p className="text-xs">Last updated: preview version.</p>
          <h2>Data we collect</h2>
          <p>
            For accounts, we process your email address and a hashed password
            via the API. When you use the console, we process project and usage
            metadata needed to operate the service.
          </p>
          <h2>Cookies</h2>
          <p>
            The console keeps you signed in with httpOnly, Secure cookies set
            by the API, plus a CSRF cookie. They are strictly necessary for
            sign-in; we do not use advertising or tracking cookies. Clearing
            your cookies signs you out.
          </p>
          <h2>Wallet addresses</h2>
          <p>
            If you link a wallet, we store its address with your account and
            read its public on-chain COMPUTE balance. Blockchain data is public
            and cannot be deleted by us.
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
