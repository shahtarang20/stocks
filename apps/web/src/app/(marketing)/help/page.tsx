import type { Metadata } from "next";
import Link from "next/link";
import { PageHero } from "@/components/layout/page-hero";
import { Prose } from "@/components/content";

export const metadata: Metadata = {
  title: "Help and glossary",
  description:
    "Plain answers for people who rent computing power and people who share their computer, plus a glossary of every hard word we use.",
};

/** One entry per hard word. Keep each answer to one or two short sentences. */
const GLOSSARY: { term: string; meaning: string }[] = [
  { term: "Agent", meaning: "A small program you install on your computer if you are a host. It tells ComputeCoin what your computer has and runs the jobs it receives." },
  { term: "API key", meaning: "A secret password for a program, not a person. A program uses it to send jobs for you. Keep it private." },
  { term: "Booking", meaning: "A short hold on a spot on a host's computer before you run a job. You are not charged for a booking." },
  { term: "Computer", meaning: "The machine a host shares. It can be a desktop, a laptop or a server." },
  { term: "Credits", meaning: "What you spend on jobs and what hosts earn. In this preview they are test credits, not real money." },
  { term: "CPU-only", meaning: "A computer or job that does not use a graphics card. A host must turn this on before a computer takes CPU-only work." },
  { term: "Dispute", meaning: "A request to look again at a charge you think is wrong. Open it from the job." },
  { term: "Docker", meaning: "Free software that runs each job in its own locked box on the host's computer. Hosts need it to run real jobs." },
  { term: "GPU (graphics card)", meaning: "The part of a computer that does heavy maths very fast. AI work and image or video work use it." },
  { term: "Held credits", meaning: "Credits set aside while a job runs. When the job ends you pay only for the time it used, and the rest comes back." },
  { term: "Host", meaning: "A person who shares their computer on ComputeCoin and earns credits when jobs finish on it. Some screens and web addresses say \"provider\". It means the same thing." },
  { term: "Invoice", meaning: "A record of what you were charged for one job." },
  { term: "Job", meaning: "One piece of work you send to be run, such as turning audio into text or making an image." },
  { term: "Kit", meaning: "A ready-made tool. You pick one, answer a few questions, upload your files and start. No set-up needed." },
  { term: "Online", meaning: "ComputeCoin heard from the host's computer in the last 90 seconds. Only online computers get jobs." },
  { term: "Payout", meaning: "A request by a host to take earnings out. A person at ComputeCoin approves it. Real bank payouts are not switched on yet." },
  { term: "Practice mode (simulated)", meaning: "The job is practised, not truly run. The results are placeholders and are marked as such. Today jobs run this way by default." },
  { term: "Private jobs", meaning: "Jobs that ask for extra protection from the host. They need special security hardware in the host's computer, so few computers can take them. This feature has not been tested on real machines yet." },
  { term: "Quote", meaning: "A price offer for a job. It holds for a short time (120 seconds by default)." },
  { term: "Session", meaning: "A live, hands-on connection to a rented computer, like a remote desktop or a terminal." },
  { term: "Setup token", meaning: "A one-time code that links one computer to a host's account. Some screens call it an enrollment token. It works once and lasts about an hour." },
  { term: "Test network", meaning: "A practice copy of a blockchain that uses coins with no value. We start there before any real network." },
  { term: "TPM", meaning: "A small security chip on many motherboards. It helps prove a computer has not been tampered with. Needed only for private jobs." },
  { term: "Video memory (VRAM)", meaning: "The memory on a graphics card. Bigger jobs need more of it." },
  { term: "Wallet", meaning: "A crypto wallet, such as MetaMask. You only need one if you want to use the COMPUTE token. It is optional." },
];

export default function HelpPage() {
  return (
    <>
      <PageHero
        eyebrow="Help"
        title="Help and glossary"
        description="Plain answers for people who rent computing power and for people who share their computer."
      />
      <div className="container space-y-10 py-12">
        <div role="note" className="max-w-3xl rounded-lg border border-border bg-surface px-4 py-3 text-sm">
          <p className="font-semibold">Where things stand today</p>
          <p className="mt-1 text-muted-foreground">
            ComputeCoin is a preview. Credits are test credits. Jobs run in practice mode by default, so results are
            placeholders. Nothing here is real money.
          </p>
        </div>

        <nav aria-label="On this page" className="max-w-3xl text-sm">
          <p className="font-semibold">On this page</p>
          <ul className="mt-2 flex flex-wrap gap-x-5 gap-y-1">
            <li><a className="text-primary underline underline-offset-2" href="#rent">Rent computing power</a></li>
            <li><a className="text-primary underline underline-offset-2" href="#host">Share your computer</a></li>
            <li><a className="text-primary underline underline-offset-2" href="#problems">If something goes wrong</a></li>
            <li><a className="text-primary underline underline-offset-2" href="#glossary">Glossary</a></li>
          </ul>
        </nav>

        <section id="rent" className="scroll-mt-24">
          <Prose>
          <h2>Rent computing power</h2>
          <p>
            You send a <strong>job</strong>. A host&apos;s computer runs it. You get the results back and pay for the time the
            job really used.
          </p>
          <ol>
            <li>
              <strong>Create an account</strong> and <Link href="/dashboard/billing">add credits</Link>. In this preview they
              are test credits.
            </li>
            <li>
              <strong>Pick a Kit</strong> (a ready-made tool) or <strong>write your own job</strong>. Check the price before
              you start.
            </li>
            <li>
              <strong>Submit it and watch it run.</strong> The job page shows what is happening.
            </li>
            <li>
              <strong>Read your results.</strong> You can download the files from the job page.
            </li>
          </ol>
          <h3>What am I paying for?</h3>
          <p>
            You pay for the time your job really ran on the host&apos;s computer. While it runs, credits are held. When it ends,
            you are charged for the time used and the rest comes back. If a job fails, you are not charged as if it succeeded.
          </p>
          <h3>What is practice mode?</h3>
          <p>
            Today most jobs run in practice mode (simulated). The job is practised, not truly run. Results are placeholders and
            are labelled that way. We will say clearly when real runs are switched on.
          </p>
        </Prose>
        </section>

        <section id="host" className="scroll-mt-24">
          <Prose>
          <h2>Share your computer</h2>
          <p>
            Hosts earn credits when customer jobs finish on their computer. We cannot say how much you will earn, or how many
            jobs you will get.
          </p>
          <h3>What your computer needs today</h3>
          <ul>
            <li><strong>Linux.</strong> Our guide is for Ubuntu 22.04 or 24.04. Windows and Mac are not supported yet.</li>
            <li><strong>Python 3.12 or newer</strong> and an internet connection. The agent only connects out. You do not need to open ports.</li>
            <li><strong>Docker</strong> to run real jobs. Without it, your computer can only take practice jobs.</li>
            <li>
              <strong>A graphics card (GPU)</strong> for GPU jobs: NVIDIA (driver 535.86 or newer, at least 8 GB of video memory) or
              AMD (ROCm 6.1 or 6.2, Radeon RX 7900 series or Instinct). AMD support is new and has not been tried on a real card
              yet. One brand per computer.
            </li>
            <li><strong>No graphics card?</strong> You can still connect, but you will not get GPU jobs.</li>
            <li><strong>Private jobs</strong> need a TPM chip and more. Most home computers cannot take them. You do not need this to start.</li>
          </ul>
          <p>
            Ready? Open the <Link href="/provider/onboarding">step-by-step setup guide</Link>. You will need to be signed in.
          </p>
          <h3>How you are paid today</h3>
          <p>
            You earn when a job finishes. You then ask for a payout and an administrator approves it. Everything uses test
            credits. Real bank payouts are not switched on yet.
          </p>
          <h3>Is it safe for my computer?</h3>
          <p>
            Customer jobs run inside a locked container. They get no network by default, no access to your files and no admin
            rights. A container is strong protection but not a perfect wall, so use a computer that holds nothing you cannot
            afford to lose.
          </p>
        </Prose>
        </section>

        <section id="problems" className="scroll-mt-24">
          <Prose>
          <h2>If something goes wrong</h2>
          <ul>
            <li><strong>A job failed.</strong> Open the job. It explains what happened and what to do. You are not charged as if it worked.</li>
            <li><strong>I do not have enough credits.</strong> Add credits on the <Link href="/dashboard/billing">Credits and billing</Link> page.</li>
            <li><strong>I think a charge is wrong.</strong> Open a dispute from the job.</li>
            <li><strong>My computer does not show as online.</strong> See the fixes in step 4 of the <Link href="/provider/onboarding">setup guide</Link>.</li>
            <li><strong>Is ComputeCoin working?</strong> Check the <Link href="/status">status page</Link>.</li>
          </ul>
        </Prose>
        </section>

        <section id="glossary" className="scroll-mt-24">
          <Prose>
          <h2>Glossary</h2>
          <p>Every hard word we use, in plain language.</p>
          <dl className="space-y-4">
            {GLOSSARY.map((g) => (
              <div key={g.term}>
                <dt className="font-semibold text-foreground">{g.term}</dt>
                <dd className="mt-0.5">{g.meaning}</dd>
              </div>
            ))}
          </dl>
        </Prose>
        </section>
      </div>
    </>
  );
}
