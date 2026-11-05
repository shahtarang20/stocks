import type { Metadata } from "next";
import Link from "next/link";
import { PageHero } from "@/components/layout/page-hero";
import { Prose } from "@/components/content";
import { Button } from "@/components/ui/button";

export const metadata: Metadata = {
  title: "Become a host",
  description:
    "Share your computer's graphics card (GPU) on ComputeCoin. See what you need, how setup works, and what is and is not promised.",
};

export default function ProvidersPage() {
  return (
    <>
      <PageHero
        eyebrow="Become a host"
        title="Share your computer's graphics card"
        description="A host is someone who lets other people run jobs on their computer. Here is what you need, how setup works, and what we do not promise."
      />
      <div className="container space-y-8 py-12">
        <Prose>
          <h2>What you need</h2>
          <ul>
            <li>
              <strong>A Linux computer.</strong> Our setup guide is written for Ubuntu 22.04 or
              24.04. Windows and Mac are not supported yet.
            </li>
            <li>
              <strong>Docker.</strong> This is free software that keeps each job in its own
              closed box on your computer. You need it to run real jobs.
            </li>
            <li>
              <strong>Python 3.12 or newer.</strong> The ComputeCoin agent, a small program
              that connects your computer to the network, is written in Python.
            </li>
            <li>
              <strong>An internet connection.</strong> The agent only makes outgoing
              connections. You do not need to open any ports on your router.
            </li>
            <li>
              <strong>A graphics card (GPU), if you want to run GPU jobs.</strong> NVIDIA cards
              need driver 535.86 or newer, the NVIDIA Container Toolkit and at least 8 GB of GPU
              memory. AMD cards need ROCm 6.1 or 6.2. AMD support is new and has not been tried
              on a real AMD card yet. Use one brand of GPU per computer. Intel and Apple GPUs
              are not supported.
            </li>
            <li>
              <strong>Computers without a GPU</strong> can join too, but they never get GPU
              jobs. They only get jobs that need no GPU, and only if you turn that on in the
              agent settings.
            </li>
            <li>
              <strong>Disk and memory.</strong> We suggest at least 50 GB of free disk space,
              4 CPU cores and 16 GB of memory.
            </li>
          </ul>
          <h2>How setup works</h2>
          <ol>
            <li>Create an account and choose &ldquo;Become a host&rdquo;.</li>
            <li>Create a setup token. It is a one-time code that lasts 1 hour.</li>
            <li>Install the agent on your computer and connect it with the token.</li>
            <li>
              Your computer shows as online once the agent has been seen in the last 90
              seconds. Then set your price and when you are available.
            </li>
          </ol>
          <p>
            The{" "}
            <Link href="/provider/onboarding">host setup page</Link> walks you through each
            step with copy-and-paste commands and a troubleshooting list. You need to log in
            first.
          </p>
          <h2>What is real today</h2>
          <ul>
            <li>
              <strong>Jobs run in practice mode by default.</strong> Practice mode (also called
              simulated) shows how everything works, but the results are placeholders, not real
              AI output. Real GPU jobs are not switched on yet.
            </li>
            <li>
              <strong>Credits are test credits.</strong> What you earn is shown in test
              credits, not real money. Paying out real money to hosts is not live yet.
            </li>
            <li>
              <strong>Your computer&apos;s details are self-reported.</strong> The agent tells
              us what hardware you have. ComputeCoin has not checked it.
            </li>
          </ul>
          <h2>How your computer is protected</h2>
          <p>
            The agent does not run customer commands directly on your computer. Jobs run in a
            locked-down container with no access to your files and no internet by default. A
            container is a strong barrier, but it is not a perfect wall. Only share a computer
            you are comfortable using for this, and not one that holds private data.
          </p>
          <h2>What we do not promise</h2>
          <p>
            We do not promise any amount of work, uptime or earnings. What you earn depends on
            demand and your price. Your host console only shows what your computer has really
            earned.
          </p>
        </Prose>
        <div className="flex flex-wrap gap-3">
          <Button asChild>
            <Link href="/provider/onboarding">Start host setup</Link>
          </Button>
          <Button asChild variant="outline">
            <Link href="/help">Help and glossary</Link>
          </Button>
        </div>
      </div>
    </>
  );
}
