import type { Metadata } from "next";
import { PageHero } from "@/components/layout/page-hero";
import { Prose } from "@/components/content";
import { DemoBanner } from "@/components/demo-banner";

export const metadata: Metadata = {
  title: "Security",
  description:
    "How ComputeCoin keeps jobs apart, how computers are checked, how payments are protected, and what is not finished.",
};

export default function SecurityPage() {
  return (
    <>
      <PageHero
        eyebrow="Security"
        title="How we keep things safe"
        description="How jobs are kept apart, how hosts are checked, and how payments are protected. We say clearly what is built and what is only planned."
      />
      <div className="container space-y-8 py-12">
        <DemoBanner>
          this page describes how we want security to work. Some parts are not
          finished and are marked below. No independent security audit has been
          done yet.
        </DemoBanner>
        <Prose>
          <h2>Keeping jobs apart</h2>
          <p>
            Each job runs in a locked-down container, which is like a closed box on the host&apos;s
            computer. The job cannot see the host&apos;s files and has no internet by default. A
            container is a strong barrier, but it is not a perfect wall. For jobs that need more
            protection, there are private options. They only work on hosts with special hardware, and
            you choose them when you create a job.
          </p>
          <h2>Checking hosts</h2>
          <p>
            A host&apos;s computer reports its own hardware, such as its graphics card and memory.
            ComputeCoin does not check this yet, so the site labels it &ldquo;Reported by the
            computer&rdquo;. For private jobs, the computer must also pass extra hardware checks
            before it gets the job.
          </p>
          <h2>Protecting payments</h2>
          <p>
            When a job starts, credits are held for it. When the job ends, the host is credited for
            the time used and the rest goes back to the customer. Every step is written to a ledger
            you can inspect.
          </p>
          <h2>Your account</h2>
          <ul>
            <li>
              <strong>Built:</strong> you log in with your email and a password. Passwords are
              stored in a scrambled form that cannot be turned back into the password.
            </li>
            <li>
              <strong>Built:</strong> the website keeps you logged in with a protected cookie that
              scripts on the page cannot read. Requests that change something carry an extra check
              so other websites cannot act for you.
            </li>
            <li>
              <strong>Smart contracts:</strong> these are the programs that run the coin and the
              on-chain escrow. They have automated tests but have <em>not</em> been checked by an
              independent security firm.
            </li>
            <li>
              <strong>Permissions:</strong> the ComputeCoin service checks what you are allowed to do
              on every request. Hiding a page in the website is only for convenience.
            </li>
          </ul>
          <h2>Reporting a security problem</h2>
          <p>
            A full process for reporting problems will be published before the public launch. For
            now, use the contact address on the About page.
          </p>
        </Prose>
      </div>
    </>
  );
}
