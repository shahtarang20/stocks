import type { Metadata } from "next";
import Link from "next/link";
import { PageHero } from "@/components/layout/page-hero";
import { Prose } from "@/components/content";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export const metadata: Metadata = {
  title: "What you can do",
  description:
    "Everything you can run on ComputeCoin as a customer (photos, video, audio, AI models, 3D, science) and what a host can do, plus what is not offered.",
};

interface Item {
  name: string;
  text: string;
}

interface Group {
  title: string;
  intro: string;
  items: Item[];
}

/** Each entry below is something the platform really has: a Kit in the catalogue, or a feature of the service. */
const CUSTOMER_GROUPS: Group[] = [
  {
    title: "Photos and images",
    intro: "Make pictures sharper, bigger, or from a written description.",
    items: [
      {
        name: "Make photos bigger and sharper",
        text: "Upscale one picture, or a whole folder in a .zip, 2 or 4 times larger (Real-ESRGAN).",
      },
      {
        name: "Make pictures from text",
        text: "Describe a picture and get images back (FLUX.1 schnell). The same seed gives the same picture, so results can be checked.",
      },
      {
        name: "Run a ComfyUI workflow",
        text: "Run your own ComfyUI image workflow without installing anything, with a fixed list of safe building blocks.",
      },
      {
        name: "Turn photos into a 3D scene",
        text: "Train a 3D Gaussian splat from a set of photos, optionally running COLMAP first.",
      },
    ],
  },
  {
    title: "Video and animation",
    intro: "Convert, sharpen, create and render video.",
    items: [
      {
        name: "Convert video",
        text: "Re-encode a video to H.264 or H.265, using the graphics card's video encoder when there is one.",
      },
      {
        name: "Sharpen video, frame by frame",
        text: "Upscale numbered frames 2 or 4 times. Chain it with the other steps in a pipeline for a full upscale.",
      },
      {
        name: "Make video from text",
        text: "Text-to-video with CogVideoX-2B, using a fixed seed so a re-run can be compared.",
      },
      {
        name: "Render 3D and animation",
        text: "Render a frame range of your Blender (.blend) file with Cycles or Eevee. Add-on scripts that run automatically are switched off.",
      },
    ],
  },
  {
    title: "Voice and audio",
    intro: "Turn speech into text and text into speech.",
    items: [
      {
        name: "Speech to text",
        text: "Transcribe one audio or video file with Whisper.",
      },
      {
        name: "Text to speech",
        text: "Turn each text into a spoken audio file (Kokoro-82M).",
      },
    ],
  },
  {
    title: "AI models",
    intro: "Use, train and serve language models.",
    items: [
      {
        name: "Chat and text generation",
        text: "Send a prompt and get an answer back, as a job or through the chat endpoint. Answers can stream as they are written.",
      },
      {
        name: "Embeddings",
        text: "Turn text into numbers that a program can compare, for one text or a whole list at once.",
      },
      {
        name: "Fine-tune an open model",
        text: "Train a LoRA adapter on a base model with your own JSONL data, on one graphics card or several.",
      },
      {
        name: "Run your own model server",
        text: "Host an open model behind an OpenAI-style web address (vLLM) as a service that other programs can call.",
      },
      {
        name: "Many prompts at once",
        text: "Submit a batch of prompts as one order and follow it as a group.",
      },
    ],
  },
  {
    title: "Science and data",
    intro: "Run calculations, with or without a graphics card.",
    items: [
      {
        name: "Run your own Python script",
        text: "Run one script (numpy, scipy, pandas) as untrusted code in a closed box: no internet and a time limit. No graphics card is needed.",
      },
    ],
  },
  {
    title: "Your own tools and workflows",
    intro: "For people who want more control.",
    items: [
      {
        name: "Notebooks and terminals",
        text: "Open a JupyterLab notebook or a terminal on a computer with a graphics card, live in your browser, and stop it when you are done.",
      },
      {
        name: "Bring your own container",
        text: "Run your own software package on a host that offers the stronger isolation it needs.",
      },
      {
        name: "Pipelines and many runs",
        text: "Chain steps so one result feeds the next, or run the same job many times with different settings.",
      },
      {
        name: "Book a computer",
        text: "Reserve a computer for a time so it is ready when you need it.",
      },
    ],
  },
];

const PROTECTIONS: Item[] = [
  {
    name: "You pay for finished work",
    text: "If a job fails, times out, or the host's computer stops it, the amount held for it is returned. If the host causes the loss, we also try one more computer for free.",
  },
  {
    name: "Private jobs",
    text: "For secret work, pick a private level. Your input is locked before it leaves your computer and only an approved host can open it. This needs hosts with special hardware, so fewer are available, and it is still being proven on real hardware.",
  },
  {
    name: "Dedicated or shared computers",
    text: "Every computer is labelled. A dedicated computer only runs ComputeCoin jobs. A shared PC is also used by its owner, so a job may be slower or moved. Tick one box on the job form to use dedicated computers only. The owner declares this label.",
  },
  {
    name: "Closed boxes",
    text: "Every job runs in a locked container: no internet by default, no access to the host's files.",
  },
];

const HOST_ITEMS: Item[] = [
  {
    name: "Lend your graphics card and earn credits",
    text: "Install the agent on a Linux computer with Docker. Customers' jobs run in locked boxes on your machine, and you earn credits for each job that finishes.",
  },
  {
    name: "Set your own terms",
    text: "Choose your price, pause your computer for maintenance, and say whether it is dedicated or shared.",
  },
  {
    name: "Keep your computer safe",
    text: "The agent stops taking jobs when the card gets too hot and can limit its power use.",
  },
  {
    name: "See what you earn",
    text: "Your host console shows your computers, jobs, earnings and payouts.",
  },
  {
    name: "Take private jobs (special hardware)",
    text: "A Linux host with proven hardware, such as a TPM, can take private jobs. These are the most protected.",
  },
  {
    name: "Computers without a graphics card",
    text: "They can take jobs that need no graphics card, such as Python scripts, once you switch that on.",
  },
];

const NOT_OFFERED: Item[] = [
  {
    name: "Playing games or a remote desktop",
    text: "ComputeCoin runs jobs and returns results. It does not stream a game or a desktop to you, and a game cannot use a faraway graphics card over the internet.",
  },
  {
    name: "Windows or Mac hosts today",
    text: "Hosting is for Linux. A Windows PC with an NVIDIA card is planned for ordinary jobs only (never private jobs). Mac is not supported.",
  },
  {
    name: "AMD graphics cards",
    text: "They are supported in the software but have not been tried on a real AMD card yet.",
  },
  {
    name: "Anything that breaks the rules",
    text: "Jobs and hosts must follow the acceptable use policy.",
  },
];

function ItemGrid({ items }: { items: Item[] }) {
  return (
    <ul className="grid gap-3 sm:grid-cols-2">
      {items.map((it) => (
        <li key={it.name} className="rounded-md border border-border bg-surface p-4">
          <p className="font-medium">{it.name}</p>
          <p className="mt-1 text-sm text-muted-foreground">{it.text}</p>
        </li>
      ))}
    </ul>
  );
}

export default function WhatYouCanDoPage() {
  return (
    <>
      <PageHero
        eyebrow="What you can do"
        title="Rent a graphics card for real work, or lend yours"
        description="ComputeCoin connects people who need a powerful graphics card (GPU) with people who have one. Here is what each side can do today, and what is not offered."
      />
      <div className="container space-y-12 py-12">
        <section aria-labelledby="customers" className="space-y-6">
          <div>
            <h2 id="customers" className="text-2xl font-semibold">
              If you need computing power
            </h2>
            <p className="mt-1 text-muted-foreground">
              You send a job from your browser or a program. A host runs it and you get the result back. You do not
              need a graphics card of your own, and you can use any kind of computer.
            </p>
          </div>

          <div className="grid gap-6">
            {CUSTOMER_GROUPS.map((group) => (
              <Card key={group.title}>
                <CardHeader>
                  <CardTitle className="text-lg">{group.title}</CardTitle>
                  <CardDescription>{group.intro}</CardDescription>
                </CardHeader>
                <CardContent>
                  <ItemGrid items={group.items} />
                </CardContent>
              </Card>
            ))}
          </div>

          <Prose>
            <p>
              <strong>Which of these you can run right now</strong> depends on which Kits are published and which
              computers are online. Open the{" "}
              <Link href="/dashboard/kits">Kit list</Link> for the live catalogue and the{" "}
              <Link href="/marketplace/gpus">computers for rent</Link> to see what is available. Each Kit names the
              models and software it uses, and their own licences apply.
            </p>
          </Prose>

          <div>
            <h3 className="text-lg font-semibold">How your work is protected</h3>
            <div className="mt-3">
              <ItemGrid items={PROTECTIONS} />
            </div>
          </div>
        </section>

        <section aria-labelledby="hosts" className="space-y-6">
          <div>
            <h2 id="hosts" className="text-2xl font-semibold">
              If you have a graphics card to share
            </h2>
            <p className="mt-1 text-muted-foreground">
              A host lets other people run jobs on their computer and earns credits for it. You keep control of your
              machine.
            </p>
          </div>
          <ItemGrid items={HOST_ITEMS} />
        </section>

        <section aria-labelledby="not-offered" className="space-y-6">
          <div>
            <h2 id="not-offered" className="text-2xl font-semibold">
              What is not offered
            </h2>
            <p className="mt-1 text-muted-foreground">We would rather say this plainly than let you find out later.</p>
          </div>
          <ItemGrid items={NOT_OFFERED} />
        </section>

        <div className="flex flex-wrap gap-3">
          <Button asChild>
            <Link href="/dashboard/kits">Browse the Kits</Link>
          </Button>
          <Button asChild variant="outline">
            <Link href="/marketplace/gpus">See computers for rent</Link>
          </Button>
          <Button asChild variant="outline">
            <Link href="/providers">Become a host</Link>
          </Button>
          <Button asChild variant="ghost">
            <Link href="/legal/aup">Acceptable use policy</Link>
          </Button>
        </div>
      </div>
    </>
  );
}
