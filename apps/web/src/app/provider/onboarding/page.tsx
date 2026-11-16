"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  AlertCircle,
  AlertTriangle,
  Check,
  Info,
  Loader2,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { CopyField } from "@/components/provider/copy-field";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { HelpTip } from "@/components/ui/help-tip";
import { MachineStatusBadge } from "@/components/provider/machine-status-badge";
import { HostRentalGuide } from "@/components/provider/host-rental-guide";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  api,
  ApiError,
  API_BASE_URL,
  enrollmentTokenStatus,
  type EnrollmentToken,
  type EnrollmentTokenCreated,
  type Machine,
  type EnrollmentTokenStatus,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { RelativeTime } from "@/components/console/bits";
import { useToast } from "@/components/console/toast";
import { formatDateTime } from "@/lib/format";
import { buildInstallCommand, buildOneClickInstallCommand, buildUnattendedInstallCommand, uninstallCommand } from "@/lib/install-command";

const TOKEN_STATUS_VARIANT: Record<
  EnrollmentTokenStatus,
  "success" | "neutral" | "warning" | "danger"
> = {
  active: "success",
  used: "neutral",
  expired: "warning",
  revoked: "danger",
};

const TOKEN_STATUS_LABEL: Record<EnrollmentTokenStatus, string> = {
  active: "Ready to use",
  used: "Already used",
  expired: "Expired",
  revoked: "Cancelled",
};

function StepHeader({
  n,
  title,
  done,
}: {
  n: number;
  title: string;
  done?: boolean;
}) {
  return (
    <div className="flex items-center gap-3">
      <span
        className={[
          "flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-sm font-semibold",
          done
            ? "bg-success/15 text-success"
            : "bg-primary text-primary-foreground",
        ].join(" ")}
      >
        {done ? <Check aria-hidden="true" className="h-4 w-4" /> : n}
        {done ? <span className="sr-only">Step {n} done</span> : null}
      </span>
      <h2 className="text-base font-semibold leading-tight tracking-tight">
        {title}
      </h2>
    </div>
  );
}

/** A short list of requirements with a bold lead-in on each line. */
function Checklist({ items }: { items: { lead: string; text: React.ReactNode }[] }) {
  return (
    <ul className="space-y-2 text-sm">
      {items.map((it) => (
        <li key={it.lead} className="flex items-start gap-2">
          <Check aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-success" />
          <span>
            <span className="font-medium">{it.lead}</span>{" "}
            <span className="text-muted-foreground">{it.text}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

/** One "something went wrong" entry in the help list. */
function Fix({ problem, children }: { problem: string; children: React.ReactNode }) {
  return (
    <details className="group rounded-md border border-border px-4 py-3 text-sm">
      <summary className="cursor-pointer font-medium marker:text-muted-foreground">
        {problem}
      </summary>
      <div className="mt-2 space-y-2 text-muted-foreground">{children}</div>
    </details>
  );
}

function Code({ children }: { children: React.ReactNode }) {
  return <code className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-xs text-foreground">{children}</code>;
}

export default function ProviderOnboardingPage() {
  const { accessToken, refreshUser } = useAuth();
  const queryClient = useQueryClient();
  const token = accessToken as string;
  const toast = useToast();

  // The raw setup token, held in state only until the host dismisses it.
  // Honesty constraint: never persisted (no storage), shown once.
  const [newToken, setNewToken] = useState<EnrollmentTokenCreated | null>(null);
  const [label, setLabel] = useState("");
  // Ticked: the one-line install also turns container rental on (and tests the computer). Off unless the host asks.
  const [allowRental, setAllowRental] = useState(false);
  // The one-click button: create a token, then copy one finished line that carries it.
  const [pendingCopy, setPendingCopy] = useState(false);
  const [oneClickNote, setOneClickNote] = useState<"copied" | "manual" | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<EnrollmentToken | null>(null);
  const [registered, setRegistered] = useState(false);

  const tokensQuery = useQuery<EnrollmentToken[]>({
    queryKey: ["provider", "enrollment-tokens"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listEnrollmentTokens(token),
  });

  // If we can list tokens, the user already has the host role.
  const isProvider = registered || tokensQuery.isSuccess;

  // Lets the page say "your computer is online" without leaving the guide.
  const machinesQuery = useQuery<Machine[]>({
    queryKey: ["provider", "machines"],
    enabled: Boolean(accessToken) && isProvider,
    queryFn: () => api.listMachines(token),
    refetchInterval: 15_000,
  });

  const registerMut = useMutation({
    mutationFn: () => api.providerRegister(token),
    onSuccess: () => {
      toast.success("Your account is now a host account.");
      setRegistered(true);
      void refreshUser(); // so the host pages stop showing the "not a host yet" prompt
      queryClient.invalidateQueries({
        queryKey: ["provider", "enrollment-tokens"],
      });
    },
  });

  const createMut = useMutation({
    mutationFn: () =>
      api.createEnrollmentToken(token, label.trim() ? { label: label.trim() } : {}),
    onSuccess: (created) => {
      toast.success("Setup token created. Copy it now. It is shown only once.");
      setNewToken(created);
      setLabel("");
      queryClient.invalidateQueries({
        queryKey: ["provider", "enrollment-tokens"],
      });
    },
  });

  const revokeMut = useMutation({
    mutationFn: (id: string) => api.revokeEnrollmentToken(token, id),
    onSuccess: () => {
      toast.success("Setup token cancelled.");
      setRevokeTarget(null);
      queryClient.invalidateQueries({
        queryKey: ["provider", "enrollment-tokens"],
      });
    },
  });

  const tokens = tokensQuery.data ?? [];
  const machines = machinesQuery.data ?? [];
  const onlineCount = machines.filter((m) => m.status === "online").length;
  const activeTokenValue = newToken?.enrollment_token ?? "PASTE_YOUR_TOKEN_HERE";
  // The address of this website, known only in the browser: it is what the one-line install downloads its script from.
  const [origin, setOrigin] = useState<string | null>(null);
  useEffect(() => {
    setOrigin(window.location.origin);
  }, []);
  const commandInput = {
    origin,
    server: API_BASE_URL,
    token: newToken?.enrollment_token,
    packageBase: process.env.NEXT_PUBLIC_AGENT_PACKAGE_BASE,
    allowRental,
  };
  // The pasted line has no token in it: the installer asks for it with typing hidden, so it never shows in the list of
  // running programs. The unattended form (token inside) is only for scripts that cannot answer a prompt.
  const installCommand = buildInstallCommand(commandInput);
  const oneClickCommand = buildOneClickInstallCommand(commandInput);
  const copyOneClick = (command: string) => {
    const done = navigator.clipboard?.writeText(command);
    if (!done) return setOneClickNote("manual");
    done.then(() => setOneClickNote("copied"), () => setOneClickNote("manual"));
  };
  // After the token is created, copy the finished line (the browser may refuse without a fresh click: then the host presses the copy button).
  useEffect(() => {
    if (!pendingCopy || !oneClickCommand) return;
    setPendingCopy(false);
    copyOneClick(oneClickCommand);
  }, [pendingCopy, oneClickCommand]); // eslint-disable-line react-hooks/exhaustive-deps
  const unattendedCommand = buildUnattendedInstallCommand(commandInput);
  const removeCommand = uninstallCommand(origin);

  return (
    <>
      <ConsolePageHeader
        title="Add my computer"
        description="Six steps to share your computer on ComputeCoin, plus one optional step to let customers run their own code. Do them in order. You can come back to this page at any time."
        action={
          <Button asChild variant="outline">
            <Link href="/help">Help and glossary</Link>
          </Button>
        }
      />

      <div className="space-y-6">
        <div
          role="note"
          className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-4 py-3 text-sm"
        >
          <AlertTriangle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
          <div>
            <p className="font-medium">Where things stand today</p>
            <p className="text-muted-foreground">
              ComputeCoin is a preview. Jobs run in{" "}
              <strong className="font-medium text-foreground">practice mode</strong> (simulated) by
              default: your computer joins, shows as online and takes practice jobs, but the results
              are placeholders. Running real jobs on a real graphics card has not been tested on real
              machines yet. Earnings are test credits, not real money. We cannot promise any
              earnings.
            </p>
          </div>
        </div>

        {/* ---------------------------------------------------------------- */}
        {/* Step 1 — Check your computer                                      */}
        {/* ---------------------------------------------------------------- */}
        <Card>
          <CardHeader>
            <StepHeader n={1} title="Check your computer" />
            <CardDescription className="sm:pl-11">
              Not every computer can join. Check this list before you install anything.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4 sm:pl-11">
            <Checklist
              items={[
                {
                  lead: "Linux.",
                  text: (
                    <>
                      Our setup guide is written for Ubuntu 22.04 or 24.04. Mac is not supported. Windows PCs
                      with an NVIDIA card are planned but cannot be installed yet. When they can, they will
                      only take ordinary jobs (no private jobs) and NVIDIA cards only. See{" "}
                      <Link className="underline underline-offset-2" href="/share/windows">what a Windows PC would need</Link>.
                    </>
                  ),
                },
                {
                  lead: "Python 3.12 or newer.",
                  text: (
                    <>
                      Check with <Code>python3 --version</Code>. Ubuntu 24.04 has it. Ubuntu 22.04 comes
                      with an older Python, so you must add 3.12 first.
                    </>
                  ),
                },
                {
                  lead: "Docker (version 24 or newer).",
                  text: "Docker keeps customer jobs in a locked box. Without it, your computer can only take practice jobs. A job that needs a real graphics card fails. It is never faked.",
                },
                {
                  lead: "Internet.",
                  text: "The agent only connects out to ComputeCoin. You do not need to open any ports on your router.",
                },
                {
                  lead: "Room to work.",
                  text: "We suggest at least 4 CPU cores, 16 GB of memory and 50 GB of free disk space. Smaller computers may be refused jobs.",
                },
              ]}
            />

            <div className="space-y-2">
              <h3 className="text-sm font-semibold">Which graphics card (GPU)?</h3>
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-md border border-border p-3 text-sm">
                  <p className="font-medium">NVIDIA</p>
                  <ul className="mt-1 list-disc space-y-1 pl-5 text-muted-foreground">
                    <li>At least 8 GB of video memory. RTX 20 series or newer, or a data-centre card.</li>
                    <li>NVIDIA driver 535.86 or newer.</li>
                    <li>
                      NVIDIA Container Toolkit
                      <HelpTip term="NVIDIA Container Toolkit">
                        A free NVIDIA add-on that lets Docker use your graphics card.
                      </HelpTip>
                    </li>
                  </ul>
                </div>
                <div className="rounded-md border border-border p-3 text-sm">
                  <p className="font-medium">AMD</p>
                  <ul className="mt-1 list-disc space-y-1 pl-5 text-muted-foreground">
                    <li>Radeon RX 7900 series or Instinct MI250 / MI300X.</li>
                    <li>
                      ROCm 6.1 or 6.2
                      <HelpTip term="ROCm">
                        AMD&apos;s driver software for running programs on its graphics cards.
                      </HelpTip>
                    </li>
                    <li>Your user must be in the <Code>render</Code> and <Code>video</Code> groups.</li>
                    <li>AMD support is new. It has not been tried on a real AMD card yet.</li>
                  </ul>
                </div>
              </div>
              <p className="text-sm text-muted-foreground">
                One brand per computer. If your computer has both NVIDIA and AMD cards, you must pick
                one in step 4. Intel and Apple graphics are not detected.
              </p>
            </div>

            <div className="rounded-md border border-border bg-surface-2/50 p-3 text-sm">
              <p className="font-medium">No graphics card?</p>
              <p className="mt-1 text-muted-foreground">
                The agent runs without one. It shows 0 GPUs and never gets GPU jobs. It can only take
                CPU-only Kits if you turn that on by adding{" "}
                <Code>cpu_only_kits_enabled = true</Code> to <Code>~/.computecoin/agent.toml</Code>.
              </p>
            </div>

            <details className="rounded-md border border-border px-4 py-3 text-sm">
              <summary className="cursor-pointer font-medium">
                Optional: take sandboxed jobs (gVisor)
              </summary>
              <p className="mt-2 text-muted-foreground">
                A sandbox puts an extra wall between a customer&apos;s job and your computer. Customers can ask for it,
                and a computer that offers it can take those jobs. You do not need it to start.
              </p>
              <ol className="mt-2 list-decimal space-y-1 pl-5 text-muted-foreground">
                <li>Install gVisor (its program is called <Code>runsc</Code>) from gVisor&apos;s own website.</li>
                <li>
                  Register it with Docker: <Code>sudo runsc install</Code>, then restart Docker.
                </li>
                <li>Restart the Computecoin app. It says on start whether the sandbox is ready.</li>
              </ol>
              <p className="mt-2 text-muted-foreground">
                Support for graphics cards inside gVisor is still limited, so check gVisor&apos;s own notes for your card.
                The &ldquo;My computers&rdquo; page shows &ldquo;Sandbox ready&rdquo; once it works.
              </p>
            </details>

            <details className="rounded-md border border-border px-4 py-3 text-sm">
              <summary className="cursor-pointer font-medium">
                Optional: private jobs need special hardware
              </summary>
              <p className="mt-2 text-muted-foreground">
                Customers can ask for private jobs. Your computer can take them only if it has a TPM 2.0
                chip{" "}
                <HelpTip term="TPM">
                  A small security chip on many motherboards. It can prove your computer has not been
                  tampered with.
                </HelpTip>
                . The highest levels also need memory encryption, or a server processor with
                confidential-computing support plus an NVIDIA card that supports it. Most home
                computers do not have this. You do not need it to start. Add{" "}
                <Code>--private-tier</Code> to the start command only if you know your computer
                qualifies.
              </p>
            </details>

            <p className="flex items-start gap-2 rounded-md bg-surface-2/50 px-3 py-2 text-xs text-muted-foreground">
              <Info aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                Customer jobs run inside a locked container: no network by default, no access to your
                files, no admin rights. A container is strong protection but not a perfect wall. Use a
                computer that holds nothing you could not afford to lose.
              </span>
            </p>
          </CardContent>
        </Card>

        {/* ---------------------------------------------------------------- */}
        {/* Step 2 — Install                                                  */}
        {/* ---------------------------------------------------------------- */}
        <Card>
          <CardHeader>
            <StepHeader n={2} title="Install the agent" />
            <CardDescription className="sm:pl-11">
              The agent is a small program that runs on your computer. It tells ComputeCoin what your
              computer has and runs the jobs it gets. Open a terminal and run these commands.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4 sm:pl-11">
            <p className="rounded-md bg-surface-2/50 px-3 py-2 text-sm text-muted-foreground">
              <strong className="font-medium text-foreground">You can skip this step.</strong> The one-line install in
              step 3c installs the agent for you and keeps it running. Use the commands below only if you would rather
              install it yourself.
            </p>
            <details className="rounded-md border border-border px-4 py-3 text-sm">
              <summary className="cursor-pointer font-medium">Install by hand (advanced)</summary>
            <div className="mt-3 space-y-4">
            <p className="text-sm text-muted-foreground">
              A manual install needs the <Code>provider-agent</Code> folder from the ComputeCoin team. Open a terminal
              inside it.
            </p>
            <CopyField
              label="A. Install the agent (Linux)"
              value={`cd provider-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e .`}
            />
            <CopyField
              label="B. Add graphics card support. NVIDIA owners run this line:"
              value={`pip install -e ".[gpu]"`}
            />
            <CopyField
              label="B. AMD owners run this line instead:"
              value={`pip install -e ".[amd]"`}
            />
            <CopyField
              label="C. Check that Docker works"
              value="docker run --rm hello-world"
            />
            <CopyField
              label="D. See what the agent can find on your computer"
              value="computecoin-agent detect"
            />
            <p className="text-sm text-muted-foreground">
              Step D prints a report. Look for your graphics card in the <Code>gpus</Code> list. If the list
              is empty, see the fixes in step 4. Each time you open a new terminal, run{" "}
              <Code>source .venv/bin/activate</Code> in the folder first.
            </p>
            </div>
            </details>
            <details className="rounded-md border border-border px-4 py-3 text-sm">
              <summary className="cursor-pointer font-medium">
                Set up Docker and your graphics card for real jobs
              </summary>
              <div className="mt-3 space-y-3 text-muted-foreground">
                <p>
                  Install Docker Engine from Docker&apos;s official Linux guide, then:
                </p>
                <CopyField
                  label="NVIDIA: let Docker use your card, then test it"
                  value={`sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi`}
                />
                <CopyField
                  label="AMD: check the card is visible, then add yourself to the groups"
                  value={`ls -la /dev/kfd /dev/dri/renderD*
sudo usermod -aG render,video $USER`}
                />
                <p>
                  For AMD, log out and back in after the second command. If the NVIDIA test does not show your
                  card, do not start the agent yet.
                </p>
              </div>
            </details>
          </CardContent>
        </Card>

        {/* ---------------------------------------------------------------- */}
        {/* Step 3 — Connect                                                  */}
        {/* ---------------------------------------------------------------- */}
        <Card>
          <CardHeader>
            <StepHeader n={3} title="Connect your computer" done={machines.length > 0} />
            <CardDescription className="sm:pl-11">
              You need a host account, then a one-time setup token. The token links one computer to your
              account.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-5 sm:pl-11">
            {/* 3a: become a host */}
            <div className="space-y-2">
              <h3 className="text-sm font-semibold">3a. Turn on hosting for your account</h3>
              {isProvider ? (
                <p className="flex items-center gap-2 text-sm text-success">
                  <Check aria-hidden="true" className="h-4 w-4" /> Hosting is on for your account.
                </p>
              ) : (
                <div className="space-y-3">
                  <p className="text-sm text-muted-foreground">
                    Your account keeps working as a customer account too. It is safe to press this more
                    than once.
                  </p>
                  {registerMut.isError ? (
                    <ErrorNote
                      error={registerMut.error}
                      fallback="We could not turn on hosting. Check your internet connection and try again."
                    />
                  ) : null}
                  <Button
                    onClick={() => registerMut.mutate()}
                    disabled={registerMut.isPending}
                  >
                    {registerMut.isPending ? (
                      <>
                        <Loader2 className="animate-spin" /> Turning on…
                      </>
                    ) : (
                      "Become a host"
                    )}
                  </Button>
                </div>
              )}
            </div>

            {/* 3b: setup token */}
            <div className="space-y-3">
              <h3 className="text-sm font-semibold">
                3b. Create a setup token
                <HelpTip term="setup token">
                  A one-time code that links one computer to your account. Some screens call it an
                  enrollment token. Treat it like a password.
                </HelpTip>
              </h3>
              <p className="text-sm text-muted-foreground">
                The token works once and is usually valid for 1 hour. We show it only one time, so copy it
                before you leave this page.
              </p>

              {newToken ? (
                <div className="space-y-3 rounded-lg border border-warning/50 bg-warning/10 p-4">
                  <div className="flex items-start justify-between gap-3">
                    <p className="flex items-start gap-2 text-sm font-medium">
                      <AlertTriangle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
                      <span>
                        <span className="text-warning">Copy it now.</span> You will not be able to see this
                        token again. It stops working <RelativeTime iso={newToken.expires_at} /> (
                        {formatDateTime(newToken.expires_at)}).
                      </span>
                    </p>
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label="Dismiss token"
                      onClick={() => {
                        setNewToken(null);
                        setOneClickNote(null);
                      }}
                    >
                      <X />
                    </Button>
                  </div>
                  <CopyField value={newToken.enrollment_token} />
                </div>
              ) : null}

              {isProvider ? (
                <div className="space-y-3">
                  {createMut.isError ? (
                    <ErrorNote
                      error={createMut.error}
                      fallback="We could not create a setup token. Check your internet connection and try again."
                    />
                  ) : null}
                  <form
                    className="flex flex-col gap-3 sm:flex-row sm:items-end"
                    onSubmit={(e) => {
                      e.preventDefault();
                      if (!createMut.isPending) createMut.mutate();
                    }}
                  >
                    <div className="flex-1 space-y-1.5">
                      <Label htmlFor="token-label">Name for this computer (optional)</Label>
                      <Input
                        id="token-label"
                        placeholder="For example: gaming PC"
                        value={label}
                        maxLength={120}
                        onChange={(e) => setLabel(e.target.value)}
                        autoComplete="off"
                      />
                    </div>
                    <Button type="submit" disabled={createMut.isPending}>
                      {createMut.isPending ? (
                        <>
                          <Loader2 className="animate-spin" /> Creating…
                        </>
                      ) : (
                        <>
                          <Plus /> Create setup token
                        </>
                      )}
                    </Button>
                  </form>
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  Finish step 3a first. Then you can create a token here.
                </p>
              )}
            </div>

            {/* 3c: run enroll */}
            <div className="space-y-3">
              <h3 className="text-sm font-semibold">3c. Install and connect (one line)</h3>
              <p className="text-sm text-muted-foreground">
                Open a terminal on the computer you want to share and paste this one line. It asks for your password
                (sudo). It checks the computer, installs the agent, connects the computer to your account, and starts
                the agent and its heat watchdog so they keep running after a restart.
              </p>
              <div className="space-y-1">
                <label className="flex items-start gap-2 text-sm">
                  <input
                    type="checkbox"
                    className="mt-1"
                    checked={allowRental}
                    onChange={(e) => setAllowRental(e.target.checked)}
                    aria-describedby="allow-rental-d"
                  />
                  <span className="font-medium">Also let customers run their own code on this computer (container rental)</span>
                </label>
                <p id="allow-rental-d" className="pl-6 text-xs text-muted-foreground">
                  Optional. Ticking this adds one word to the line below. The same install then also turns container rental on and tests the
                  computer, so you do not need the separate rental commands further down. Read &ldquo;Optional: let customers run their own
                  code&rdquo; first. You can turn it off any time.
                </p>
              </div>
              <div className="space-y-2 rounded-md border border-border bg-surface-2/40 p-3" data-testid="one-click">
                <p className="text-sm">
                  <span className="font-medium">Easiest.</span>{" "}
                  <span className="text-muted-foreground">
                    Press the button. It creates a setup token and copies one finished line that already has the token in it. Paste it in a
                    terminal on the computer you want to share and type your password when asked. Nothing else is asked.
                  </span>
                </p>
                <Button
                  type="button"
                  disabled={!isProvider || createMut.isPending}
                  onClick={() => {
                    if (oneClickCommand) {
                      copyOneClick(oneClickCommand);
                    } else {
                      setOneClickNote(null);
                      setPendingCopy(true);
                      if (!createMut.isPending) createMut.mutate();
                    }
                  }}
                >
                  {createMut.isPending ? (
                    <>
                      <Loader2 className="animate-spin" /> Creating…
                    </>
                  ) : oneClickCommand ? (
                    "Copy the install command again"
                  ) : (
                    "Create token and copy the install command"
                  )}
                </Button>
                {!isProvider ? <p className="text-xs text-muted-foreground">Finish step 3a first.</p> : null}
                {oneClickCommand ? (
                  <div className="space-y-1.5">
                    {oneClickNote === "copied" ? (
                      <p role="status" className="text-xs text-success">Copied. Paste it in the terminal now. It works once and stops working in about an hour.</p>
                    ) : oneClickNote === "manual" ? (
                      <p role="status" className="text-xs text-warning">Your browser did not let us copy it. Press the copy button below.</p>
                    ) : null}
                    <CopyField label="One-click install command (it has your token in it, so do not share it)" value={oneClickCommand} />
                  </div>
                ) : null}
              </div>
              {installCommand ? (
                <CopyField label="Install and connect (Linux)" value={installCommand} />
              ) : (
                <p className="text-xs text-muted-foreground">
                  The one-line command is not available right now (this website&apos;s address could not be read). Use the
                  manual commands below.
                </p>
              )}
              <div className="space-y-1.5">
                <p className="text-xs text-muted-foreground">
                  It will ask for your setup token (your typing is hidden). Paste this when asked:
                </p>
                <CopyField label="Your setup token" value={activeTokenValue} />
                {!newToken ? (
                  <p className="text-xs text-muted-foreground">
                    Create a token in step 3b first. Until then <Code>PASTE_YOUR_TOKEN_HERE</Code> is a placeholder.
                  </p>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    The token works once and expires. Docker and your graphics driver must already be installed (step 1);
                    the installer tells you plainly if something is missing.
                  </p>
                )}
              </div>
              <p className="text-xs text-muted-foreground">
                When it finishes, open <strong className="font-medium text-foreground">My computers</strong>: your
                computer should show as online within a minute. To remove it later, run{" "}
                {removeCommand ? <Code>{removeCommand}</Code> : <Code>install.sh --uninstall</Code>}.
              </p>
              {unattendedCommand ? (
                <details className="rounded-md border border-border px-4 py-3 text-sm">
                  <summary className="cursor-pointer font-medium">Install without a prompt (for scripts)</summary>
                  <div className="mt-3 space-y-2">
                    <p className="text-sm text-muted-foreground">
                      For automation that cannot type. The token is part of the command, so other people using that
                      computer could read it while the install runs. Cancel the token afterwards if the computer is shared.
                    </p>
                    <CopyField label="Install and connect, no prompt" value={unattendedCommand} />
                  </div>
                </details>
              ) : null}
              <details className="rounded-md border border-border px-4 py-3 text-sm">
                <summary className="cursor-pointer font-medium">Connect by hand (if you installed it yourself)</summary>
                <div className="mt-3 space-y-3">
              <p className="text-sm text-muted-foreground">
                Run these in the terminal where you installed the agent. Server address:{" "}
                <code className="break-all font-mono text-xs">{API_BASE_URL}</code>
              </p>
              {!newToken ? (
                <p className="text-xs text-muted-foreground">
                  Create a token above and it will appear in the first command. Until then{" "}
                  <Code>PASTE_YOUR_TOKEN_HERE</Code> is a placeholder.
                </p>
              ) : null}
              <CopyField
                label="1. Connect this computer (run once)"
                value={`computecoin-agent enroll --token ${activeTokenValue} --server ${API_BASE_URL}`}
              />
              <p className="text-xs text-muted-foreground">
                When it works, you will see &quot;Enrolled machine&quot; and a line saying where your
                private connection key was saved (<Code>~/.computecoin/agent.toml</Code>). Keep that file
                private.
              </p>
              <CopyField
                label="2. Start the agent (keeps your computer online and takes jobs)"
                value={`computecoin-agent start --server ${API_BASE_URL}`}
              />
              <p className="text-xs text-muted-foreground">
                Leave this terminal open. Press Ctrl+C to stop sharing your computer at any time.
              </p>
                </div>
              </details>
            </div>

            {/* Existing tokens list. */}
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold">Your setup tokens</h3>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => tokensQuery.refetch()}
                  disabled={tokensQuery.isFetching}
                >
                  <RefreshCw
                    className={tokensQuery.isFetching ? "animate-spin" : undefined}
                  />
                  Refresh
                </Button>
              </div>

              {tokensQuery.isLoading ? (
                <div className="space-y-2">
                  {Array.from({ length: 2 }).map((_, i) => (
                    <Skeleton key={i} className="h-10 w-full" />
                  ))}
                </div>
              ) : tokensQuery.isError ? (
                <ErrorNote
                  error={tokensQuery.error}
                  fallback="We could not load your setup tokens. Press Refresh to try again."
                />
              ) : tokens.length === 0 ? (
                <p className="rounded-md border border-dashed border-border px-4 py-6 text-center text-sm text-muted-foreground">
                  You have no setup tokens yet. Create one in step 3b to connect a computer.
                </p>
              ) : (
                <div className="rounded-md border border-border">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Name</TableHead>
                        <TableHead>Status</TableHead>
                        <TableHead>Stops working</TableHead>
                        <TableHead className="hidden sm:table-cell">Created</TableHead>
                        <TableHead className="text-right">Action</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {tokens.map((t) => {
                        const status = enrollmentTokenStatus(t);
                        return (
                          <TableRow key={t.id}>
                            <TableCell className="font-medium">
                              {t.label || (
                                <span className="text-muted-foreground">
                                  (no name)
                                </span>
                              )}
                            </TableCell>
                            <TableCell>
                              <Badge variant={TOKEN_STATUS_VARIANT[status]}>
                                {TOKEN_STATUS_LABEL[status]}
                              </Badge>
                            </TableCell>
                            <TableCell className="text-sm text-muted-foreground">
                              <RelativeTime iso={t.expires_at} />
                            </TableCell>
                            <TableCell className="hidden text-sm text-muted-foreground sm:table-cell">
                              <RelativeTime iso={t.created_at} />
                            </TableCell>
                            <TableCell className="text-right">
                              <Button
                                variant="ghost"
                                size="sm"
                                disabled={status !== "active"}
                                aria-label={`Revoke enrollment token ${t.label || ""}`.trim()}
                                onClick={() => {
                                  revokeMut.reset();
                                  setRevokeTarget(t);
                                }}
                              >
                                <Trash2 /> Cancel token
                              </Button>
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </div>
              )}
            </div>
          </CardContent>
        </Card>

        {/* ---------------------------------------------------------------- */}
        {/* Step 4 — See it online                                            */}
        {/* ---------------------------------------------------------------- */}
        <Card>
          <CardHeader>
            <StepHeader n={4} title="See your computer online" done={onlineCount > 0} />
            <CardDescription className="sm:pl-11">
              &quot;Online&quot; means ComputeCoin heard from your computer in the last 90 seconds. The
              agent checks in every 30 seconds.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4 sm:pl-11">
            <div
              role="status"
              className="flex flex-wrap items-center gap-3 rounded-md border border-border bg-surface-2/50 px-4 py-3 text-sm"
            >
              {!isProvider ? (
                <span className="text-muted-foreground">Finish step 3 first.</span>
              ) : machinesQuery.isLoading ? (
                <span className="text-muted-foreground">Looking for your computers…</span>
              ) : machinesQuery.isError ? (
                <span className="text-muted-foreground">
                  We could not check right now. Open Hardware to see your computers.
                </span>
              ) : machines.length === 0 ? (
                <span className="text-muted-foreground">
                  No computer has connected yet. This page checks again every 15 seconds.
                </span>
              ) : (
                <>
                  <span className="font-medium">
                    {onlineCount} of {machines.length} {machines.length === 1 ? "computer" : "computers"} online
                  </span>
                  {machines.slice(0, 4).map((m) => (
                    <span key={m.machine_id} className="inline-flex items-center gap-1.5">
                      <span className="text-muted-foreground">{m.hostname}</span>
                      <MachineStatusBadge status={m.status} />
                    </span>
                  ))}
                </>
              )}
              <Button asChild variant="outline" size="sm" className="ml-auto">
                <Link href="/provider/hardware">Open my computers</Link>
              </Button>
            </div>

            <div className="space-y-2">
              <h3 className="text-sm font-semibold">If something goes wrong</h3>
              <Fix problem="The connect command says the token is rejected">
                <p>
                  The token was already used, has expired (they last about 1 hour), was cancelled, or was
                  copied with a typo. Create a new token in step 3b and run the connect command again.
                </p>
              </Fix>
              <Fix problem="The one-line install stopped and printed a reason">
                <p>
                  It stops before changing anything when something it needs is missing: Docker, a running Docker
                  service, systemd, or enough rights (run it with <Code>sudo</Code>). It says exactly which one and what
                  to do. Fix that, then paste the same line again. It is safe to run more than once.
                </p>
              </Fix>
              <Fix problem="The installer says this computer has a saved connection the server no longer accepts">
                <p>
                  This happens if you removed the computer on the website and are adding it again. Create a new token in
                  step 3b and add <Code>--reenroll</Code> to the end of the one-line command. The old connection is kept
                  aside, not deleted.
                </p>
              </Fix>
              <Fix problem="The installer says a customer job is running">
                <p>
                  Upgrading or removing the agent would stop that job. Wait for it to finish, or add{" "}
                  <Code>--force</Code> to the command: the job is stopped, the customer is not charged, and it runs again
                  on another computer.
                </p>
              </Fix>
              <Fix problem="The installer finished but the computer does not show as online">
                <p>
                  Look at the agent&apos;s log with <Code>journalctl -u computecoin-agent -n 50 --no-pager</Code>. The
                  most common causes are Docker not running, the graphics driver, or a setup token that already expired.
                  Create a new token in step 3b and run the line again.
                </p>
              </Fix>
              <Fix problem="It says computecoin-agent: command not found">
                <p>
                  Open the <Code>provider-agent</Code> folder and run <Code>source .venv/bin/activate</Code>. Then
                  try again.
                </p>
              </Fix>
              <Fix problem="Docker is not running">
                <p>
                  Start it with <Code>sudo systemctl start docker</Code>. Check with <Code>docker ps</Code>. If you
                  see &quot;permission denied&quot;, add yourself to the docker group with{" "}
                  <Code>sudo usermod -aG docker $USER</Code>, then log out and back in.
                </p>
                <p>
                  Without Docker your computer can still connect, but it can only take practice jobs.
                </p>
              </Fix>
              <Fix problem="No graphics card found (0 GPUs)">
                <p>
                  Run <Code>computecoin-agent detect</Code> and read the notes at the bottom. NVIDIA: the command{" "}
                  <Code>nvidia-smi</Code> must work, and you must have run the NVIDIA line in step 2B. AMD: you
                  must have run the AMD line, and <Code>/dev/kfd</Code> must exist. Intel and Apple graphics are
                  not detected.
                </p>
              </Fix>
              <Fix problem="It says both NVIDIA and AMD were found">
                <p>
                  Pick one. Add <Code>gpu_vendor = &quot;nvidia&quot;</Code> (or <Code>&quot;amd&quot;</Code>) to{" "}
                  <Code>~/.computecoin/agent.toml</Code>, then run the connect command again.
                </p>
              </Fix>
              <Fix problem="My computer shows offline">
                <p>
                  Check that the agent is still running in its terminal and that you see lines starting with{" "}
                  <Code>[heartbeat]</Code>. Also check that your computer is awake, that the internet works, and
                  that no firewall blocks outgoing connections to the server address in step 3c.
                </p>
                <p>
                  If you see &quot;heartbeat rejected (re-enroll needed)&quot;, remove the computer on the
                  Hardware page. Then create a new token and connect again.
                </p>
              </Fix>
              <Fix problem="It says my graphics card is too hot">
                <p>
                  The agent stops taking jobs at 85 °C and starts again at 80 °C. Check fans and airflow.
                </p>
              </Fix>
            </div>
          </CardContent>
        </Card>

        {/* ---------------------------------------------------------------- */}
        {/* Optional — container rental                                       */}
        {/* ---------------------------------------------------------------- */}
        <Card id="container-rental">
          <CardHeader>
            <div className="flex flex-wrap items-center gap-3">
              <span
                aria-hidden="true"
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-surface-2 text-sm font-semibold text-muted-foreground"
              >
                +
              </span>
              <h2 className="text-base font-semibold leading-tight tracking-tight">
                Optional: let customers run their own code (container rental)
              </h2>
              <Badge variant="neutral">Optional</Badge>
            </div>
            <CardDescription className="sm:pl-11">
              You can skip this and still earn from Kits and ordinary jobs. Do it after your computer shows as online (step 4). It is off
              until you turn it on.
            </CardDescription>
          </CardHeader>
          <CardContent className="sm:pl-11">
            <HostRentalGuide />
          </CardContent>
        </Card>

        {/* ---------------------------------------------------------------- */}
        {/* Step 5 — Price and availability                                   */}
        {/* ---------------------------------------------------------------- */}
        <Card>
          <CardHeader>
            <StepHeader n={5} title="Set your price and when you are available" />
            <CardDescription className="sm:pl-11">
              A computer only gets jobs when it is online, switched on for jobs, and has a price.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 sm:pl-11">
            <ol className="list-decimal space-y-1.5 pl-5 text-sm text-muted-foreground">
              <li>Open <strong className="font-medium text-foreground">My computers</strong>.</li>
              <li>Open the menu (three dots) next to your computer and choose <strong className="font-medium text-foreground">Edit price and availability</strong>.</li>
              <li>
                Set a <strong className="font-medium text-foreground">price per hour</strong>. Customers see it.
                Leave it blank and your computer gets no jobs.
              </li>
              <li>
                Choose <strong className="font-medium text-foreground">Online</strong> to accept jobs,{" "}
                <strong className="font-medium text-foreground">Offline</strong> or{" "}
                <strong className="font-medium text-foreground">Maintenance</strong> to pause.
              </li>
              <li>Choose how many jobs can run at the same time, and your region.</li>
            </ol>
            <Button asChild>
              <Link href="/provider/hardware">Open my computers</Link>
            </Button>
          </CardContent>
        </Card>

        {/* ---------------------------------------------------------------- */}
        {/* Step 6 — Earnings and payouts                                     */}
        {/* ---------------------------------------------------------------- */}
        <Card>
          <CardHeader>
            <StepHeader n={6} title="How earnings and payouts work today" />
            <CardDescription className="sm:pl-11">
              The honest picture for this preview.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 sm:pl-11">
            <Checklist
              items={[
                {
                  lead: "You earn when a job finishes.",
                  text: "Not when it starts and not when it fails. If your computer drops out mid-job, the job moves to another computer and you are not paid for unfinished work.",
                },
                {
                  lead: "Earnings are test credits.",
                  text: "They are not real money in this preview.",
                },
                {
                  lead: "Payouts are requested by you and approved by a person.",
                  text: "You ask on the Payouts page. An administrator approves it. Real bank payouts are not switched on yet.",
                },
                {
                  lead: "No promises.",
                  text: "What you earn depends on how many jobs customers send to your computer. We cannot promise any amount, any amount of uptime, or any speed.",
                },
              ]}
            />
            <div className="flex flex-wrap gap-2">
              <Button asChild variant="outline">
                <Link href="/provider/earnings">See my earnings</Link>
              </Button>
              <Button asChild variant="outline">
                <Link href="/provider/payouts">Go to payouts</Link>
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Cancel-token confirmation dialog. */}
      <Dialog
        open={Boolean(revokeTarget)}
        onOpenChange={(open) => {
          if (!open) setRevokeTarget(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Cancel this setup token?</DialogTitle>
            <DialogDescription>
              {revokeTarget?.label
                ? `"${revokeTarget.label}" `
                : "This token "}
              will stop working right away and can no longer connect a computer. You cannot undo this. You can
              always create a new token.
            </DialogDescription>
          </DialogHeader>
          {revokeMut.isError ? (
            <ErrorNote
              error={revokeMut.error}
              fallback="We could not cancel the token. Try again in a moment."
            />
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setRevokeTarget(null)}>
              Keep token
            </Button>
            <Button
              variant="danger"
              disabled={revokeMut.isPending}
              onClick={() =>
                revokeTarget && revokeMut.mutate(revokeTarget.id)
              }
            >
              {revokeMut.isPending ? (
                <>
                  <Loader2 className="animate-spin" /> Cancelling…
                </>
              ) : (
                "Cancel token"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function ErrorNote({
  error,
  fallback,
}: {
  error: unknown;
  fallback: string;
}) {
  const message = error instanceof ApiError ? error.message : fallback;
  return (
    <div
      role="alert"
      className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
    >
      <AlertCircle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
      <span>{message}</span>
    </div>
  );
}
