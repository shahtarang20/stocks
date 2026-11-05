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
        title="Bring your own containers and tools"
        description="This page is for people who write code. You can send jobs from the website, or from your own program with the ComputeCoin API."
      />
      <div className="container grid gap-10 py-12 lg:grid-cols-[1.6fr_1fr]">
        <Prose>
          <h2>What works today</h2>
          <p>
            You can create an account, add test credits, browse computers, get price quotes, send
            jobs and read results in the dashboard. Python and TypeScript client libraries are
            included in the project. Jobs run in practice mode (simulated) by default, so results
            are placeholders until real GPU jobs are switched on.
          </p>
          <h2>Workflow</h2>
          <ul>
            <li>Put your work in a container image (a packaged program that runs the same anywhere).</li>
            <li>Choose a computer by graphics card (GPU) model, GPU memory and region.</li>
            <li>Choose how private the job must be. Private options need special hardware on the host.</li>
            <li>Send the job. Credits are held and billed per second of run time.</li>
            <li>Download the itemized usage record to check your bill.</li>
          </ul>
          <h2>Using the API</h2>
          <p>
            The website talks to the ComputeCoin API. Your program can do the same. The web address
            of the API is set with <strong>NEXT_PUBLIC_API_BASE_URL</strong> (it defaults to{" "}
            <strong>http://localhost:8000</strong> on your own computer). These calls are the basics.
          </p>
          <ul>
            <li><strong>POST</strong> /auth/register — create an account</li>
            <li><strong>POST</strong> /auth/login — log in and get your access keys</li>
            <li><strong>POST</strong> /auth/refresh — get new access keys</li>
            <li><strong>GET</strong> /projects — list your projects (send your access key with the call)</li>
          </ul>
          <p>
            The API itself checks who you are on every call. Hiding a page in the website is only
            for convenience and is not a safety feature.
          </p>
        </Prose>

        <aside className="space-y-4">
          <div className="rounded-lg border border-border bg-surface p-5">
            <div className="mb-3 flex items-center gap-2 text-sm font-medium">
              <Terminal aria-hidden="true" className="h-4 w-4 text-primary" /> Try a log-in call
            </div>
            <pre className="overflow-x-auto rounded-md bg-background p-3 font-mono text-xs text-muted-foreground">
{`curl -X POST \\
  $API/auth/login \\
  -H 'Content-Type: application/json' \\
  -d '{"email":"you@dev.io",
       "password":"••••••"}'`}
            </pre>
            <p className="mt-3 text-xs text-muted-foreground">
              The reply contains your <code>access_token</code> and <code>refresh_token</code>. Keep
              them private.
            </p>
          </div>
          <div className="rounded-lg border border-border bg-surface p-5">
            <Badge variant="neutral">Preview</Badge>
            <p className="mt-3 text-sm text-muted-foreground">
              The client libraries are not on a public package site yet. Ask us how to get them, or use
              the website.
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
