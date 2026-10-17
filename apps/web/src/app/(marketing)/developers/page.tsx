import type { Metadata } from "next";
import Link from "next/link";
import { Terminal } from "lucide-react";
import { PageHero } from "@/components/layout/page-hero";
import { Prose } from "@/components/content";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

export const metadata: Metadata = {
  title: "Developers",
  description:
    "Developer overview for ComputeCoin: bring your containers, standard tooling, transparent metering.",
};

export default function DevelopersPage() {
  return (
    <>
      <PageHero
        eyebrow="Developers"
        title="Bring your workloads, not a new SDK"
        description="Run containerized training and inference on the network using tooling you already use. The API below is the Phase 1 contract the console is built against."
      />
      <div className="container grid gap-10 py-12 lg:grid-cols-[1.6fr_1fr]">
        <Prose>
          <h2>What works today</h2>
          <p>
            Phase 1 ships the account and console surface: registration, login,
            token refresh, and project listing against the ComputeCoin API.
            Job submission and live provisioning are stubbed in the console and
            land as the provider network comes online.
          </p>
          <h2>Workflow</h2>
          <ul>
            <li>Package your workload as an OCI container image.</li>
            <li>Select hardware by GPU model, VRAM, interconnect, and region.</li>
            <li>Choose a provider verification tier that fits your risk profile.</li>
            <li>Submit the job; payment is escrowed and metered per second.</li>
            <li>Export the itemized usage record for reconciliation.</li>
          </ul>
          <h2>API contract (Phase 1)</h2>
          <p>
            The console talks to a FastAPI backend. The base URL is configured
            with <strong>NEXT_PUBLIC_API_BASE_URL</strong> (defaults to{" "}
            <strong>http://localhost:8000</strong>).
          </p>
          <ul>
            <li><strong>POST</strong> /auth/register — create an account</li>
            <li><strong>POST</strong> /auth/login — returns access + refresh tokens</li>
            <li><strong>POST</strong> /auth/refresh — rotate tokens</li>
            <li><strong>GET</strong> /projects — list your projects (Bearer auth)</li>
          </ul>
          <p>
            Authorization is enforced server-side on the API. The console&apos;s
            client-side route guard is a convenience for UX only.
          </p>
        </Prose>

        <aside className="space-y-4">
          <div className="rounded-lg border border-border bg-surface p-5">
            <div className="mb-3 flex items-center gap-2 text-sm font-medium">
              <Terminal className="h-4 w-4 text-primary" /> Quick auth call
            </div>
            <pre className="overflow-x-auto rounded-md bg-background p-3 font-mono text-xs text-muted-foreground">
{`curl -X POST \\
  $API/auth/login \\
  -H 'Content-Type: application/json' \\
  -d '{"email":"you@dev.io",
       "password":"••••••"}'`}
            </pre>
            <p className="mt-3 text-xs text-muted-foreground">
              Returns <code>access_token</code> and <code>refresh_token</code>.
            </p>
          </div>
          <div className="rounded-lg border border-border bg-surface p-5">
            <Badge variant="neutral">Phase 1</Badge>
            <p className="mt-3 text-sm text-muted-foreground">
              A published client library and job API are planned. For now, the
              console demonstrates the account and project surface end to end.
            </p>
            <Button asChild className="mt-4 w-full" size="sm">
              <Link href="/register">Create an account</Link>
            </Button>
          </div>
        </aside>
      </div>
    </>
  );
}
