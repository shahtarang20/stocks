import Link from "next/link";
import {
  ArrowRight,
  ShieldCheck,
  Boxes,
  Receipt,
  Workflow,
  Scale,
  Server,
  Cpu,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

const BENEFITS = [
  {
    icon: Scale,
    title: "Pay only for the time you use",
    body: "You are billed by the second while your job runs. There is no long-term commitment.",
  },
  {
    icon: Boxes,
    title: "Choose the computer",
    body: "Filter by graphics card (GPU) model, GPU memory and region, then pick what fits your job.",
  },
  {
    icon: Receipt,
    title: "See what you paid for",
    body: "Every job has an itemized usage record. You can compare it with your invoice.",
  },
  {
    icon: ShieldCheck,
    title: "Credits are held, not spent early",
    body: "Credits are set aside while a job runs. What you do not use goes back to you.",
  },
  {
    icon: Workflow,
    title: "Failed jobs are not charged as done",
    body: "If a job fails or you cancel it, it is never billed as a finished job.",
  },
  {
    icon: Server,
    title: "Share your own computer",
    body: "Have a computer with an NVIDIA or AMD graphics card? You can let others use it and earn credits.",
  },
];

const STEPS = [
  {
    n: 1,
    title: "Create an account and add credits",
    body: "Sign up with your email. In this preview, credits are test credits, not real money.",
  },
  {
    n: 2,
    title: "Pick a tool and a computer",
    body: "Choose a ready-made tool (called a Kit), or write your own prompt. Then choose a computer to run it on.",
  },
  {
    n: 3,
    title: "Run it and get your results",
    body: "Start the job, watch its progress, and download the results when it finishes.",
  },
];

export default function HomePage() {
  return (
    <>
      {/* Hero */}
      <section className="relative overflow-hidden border-b border-border">
        <div className="pointer-events-none absolute inset-0 hero-grid" aria-hidden />
        <div className="pointer-events-none absolute inset-0 hero-glow" aria-hidden />
        <div className="container relative py-20 sm:py-28">
          <div className="mx-auto max-w-3xl text-center">
            <Link
              href="/status"
              className="inline-flex items-center gap-2 rounded-full border border-border bg-surface/60 px-3 py-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
            >
              <span className="flex h-1.5 w-1.5 rounded-full bg-primary" />
              Preview version: test credits, no real money
            </Link>
            <h1 className="mt-6 text-balance text-4xl font-bold tracking-tight sm:text-6xl">
              Rent a graphics card for your AI work, or share yours
            </h1>
            <p className="mx-auto mt-6 max-w-2xl text-pretty text-lg text-muted-foreground">
              ComputeCoin connects people who need computing power with people who have a
              spare computer. Pay by the second for what you use.
            </p>
            <div className="mt-9 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <Button asChild size="lg">
                <Link href="/register">
                  Create a free account <ArrowRight />
                </Link>
              </Button>
              <Button asChild size="lg" variant="outline">
                <Link href="/marketplace">See computers for rent</Link>
              </Button>
            </div>

            {/* Honesty note */}
            <div className="mx-auto mt-10 max-w-xl rounded-lg border border-border bg-surface/50 px-5 py-4 text-left text-sm text-muted-foreground">
              <p className="font-medium text-foreground">Where things stand today</p>
              <p className="mt-1">
                This is a preview. Credits are test credits, not real money. By default,
                jobs run in practice mode (also called simulated): the whole process works,
                but the results are placeholders, not real AI output. We make no promises
                about price, speed or earnings.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* Who is it for */}
      <section className="container py-16 sm:py-20">
        <div className="mx-auto max-w-2xl text-center">
          <h2 className="text-2xl font-bold tracking-tight sm:text-3xl">Who is it for?</h2>
        </div>
        <div className="mx-auto mt-10 grid max-w-4xl gap-4 sm:grid-cols-2">
          <Card>
            <CardContent className="flex h-full flex-col gap-3 p-6">
              <span className="flex h-10 w-10 items-center justify-center rounded-md bg-primary/10 text-primary">
                <Cpu className="h-5 w-5" aria-hidden="true" />
              </span>
              <h3 className="font-semibold">I need computing power</h3>
              <p className="text-sm text-muted-foreground">
                Students, freelancers and small teams who want to run AI jobs, render video or
                process data without buying a powerful computer.
              </p>
              <Button asChild className="mt-auto self-start">
                <Link href="/register">Create an account</Link>
              </Button>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="flex h-full flex-col gap-3 p-6">
              <span className="flex h-10 w-10 items-center justify-center rounded-md bg-primary/10 text-primary">
                <Server className="h-5 w-5" aria-hidden="true" />
              </span>
              <h3 className="font-semibold">I have a computer to share</h3>
              <p className="text-sm text-muted-foreground">
                If your computer runs Linux and has an NVIDIA or AMD graphics card, you can let
                others use it. Windows PCs with an NVIDIA card are planned. See what you need first.
              </p>
              <Button asChild variant="outline" className="mt-auto self-start">
                <Link href="/providers">Become a host</Link>
              </Button>
            </CardContent>
          </Card>
        </div>
      </section>

      {/* How it works */}
      <section className="border-y border-border bg-surface/30">
        <div className="container py-16 sm:py-20">
          <div className="mx-auto max-w-2xl text-center">
            <h2 className="text-2xl font-bold tracking-tight sm:text-3xl">How it works</h2>
            <p className="mt-3 text-muted-foreground">Three steps to run your first job.</p>
          </div>
          <ol className="mx-auto mt-10 grid max-w-5xl gap-4 md:grid-cols-3">
            {STEPS.map((step) => (
              <li key={step.n}>
                <Card className="h-full">
                  <CardContent className="flex flex-col gap-3 p-6">
                    <span
                      aria-hidden="true"
                      className="flex h-8 w-8 items-center justify-center rounded-full bg-primary text-sm font-semibold text-primary-foreground"
                    >
                      {step.n}
                    </span>
                    <h3 className="font-semibold">{step.title}</h3>
                    <p className="text-sm text-muted-foreground">{step.body}</p>
                  </CardContent>
                </Card>
              </li>
            ))}
          </ol>
        </div>
      </section>

      {/* Benefits */}
      <section className="container py-16 sm:py-20">
        <div className="mx-auto max-w-2xl text-center">
          <h2 className="text-2xl font-bold tracking-tight sm:text-3xl">What you get</h2>
          <p className="mt-3 text-muted-foreground">
            We show how numbers are measured. We do not promise that this is always cheaper or
            faster than other options, so compare before you decide.
          </p>
        </div>
        <div className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {BENEFITS.map((d) => (
            <Card key={d.title} className="animate-fade-in">
              <CardContent className="flex flex-col gap-3 p-6">
                <span className="flex h-10 w-10 items-center justify-center rounded-md bg-primary/10 text-primary">
                  <d.icon className="h-5 w-5" aria-hidden="true" />
                </span>
                <h3 className="font-semibold">{d.title}</h3>
                <p className="text-sm text-muted-foreground">{d.body}</p>
              </CardContent>
            </Card>
          ))}
        </div>
      </section>

      {/* CTA */}
      <section className="border-t border-border bg-surface/30">
        <div className="container flex flex-col items-center gap-6 py-16 text-center sm:py-20">
          <h2 className="max-w-xl text-2xl font-bold tracking-tight sm:text-3xl">
            Try it with test credits
          </h2>
          <p className="max-w-xl text-muted-foreground">
            Create an account, run a practice job and see how it works. No real money is
            involved in this preview. Need help with a word? Open the{" "}
            <Link href="/help" className="text-primary underline underline-offset-2">
              help and glossary
            </Link>
            .
          </p>
          <div className="flex flex-col gap-3 sm:flex-row">
            <Button asChild size="lg">
              <Link href="/register">
                Create a free account <ArrowRight />
              </Link>
            </Button>
            <Button asChild size="lg" variant="outline">
              <Link href="/developers">For developers</Link>
            </Button>
          </div>
        </div>
      </section>
    </>
  );
}
