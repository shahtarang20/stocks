"use client";

import { Shield, ShieldAlert, ShieldCheck, Lock, Eye, AlertTriangle } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { DemoBanner } from "@/components/demo-banner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { HelpTip } from "@/components/ui/help-tip";

const SECURITY_CONTROLS = [
  {
    category: "Signing in",
    controls: [
      { name: "Passwords are stored scrambled (Argon2id hashing), never as plain text", status: "active" },
      { name: "Sign-in passes expire after 15 minutes", status: "active" },
      { name: "Renewal keys change every time and a stolen one is caught if reused", status: "active" },
      { name: "Email address must be confirmed", status: "active" },
      { name: "Limits on how often someone can try to sign in", status: "active" },
      { name: "Second sign-in step (multi-factor)", status: "planned" },
    ],
  },
  {
    category: "Who can do what",
    controls: [
      { name: "Roles (guest, customer, host, admin) are enforced on the server", status: "active" },
      { name: "API keys belong to one project and are stored scrambled", status: "active" },
      { name: "Sensitive actions are saved in the audit log", status: "active" },
      { name: "Money changes need the right role", status: "active" },
    ],
  },
  {
    category: "Keeping jobs apart from the host",
    controls: [
      { name: "Jobs cannot control Docker on the host", status: "active" },
      { name: "Jobs cannot see the host's files", status: "active" },
      { name: "Limits on how much processor, memory, GPU and disk a job can use", status: "planned" },
      { name: "Limits on what a job can send out to the internet", status: "planned" },
      { name: "Extra-strong walls around jobs (gVisor, microVM)", status: "future" },
      { name: "Hardware that keeps jobs secret even from the host (confidential computing)", status: "future" },
    ],
  },
  {
    category: "Data and secrets",
    controls: [
      { name: "Secrets are kept out of logs and out of the website code", status: "active" },
      { name: "Download links for results expire quickly", status: "active" },
      { name: "Result files have a fingerprint (checksum) to spot changes", status: "active" },
      { name: "Data is encrypted while it travels (TLS)", status: "active" },
      { name: "Data is encrypted while stored", status: "planned" },
      { name: "Uploaded files are scanned for malware", status: "future" },
    ],
  },
  {
    category: "Network and API",
    controls: [
      { name: "Only our own website can call the API from a browser (CORS)", status: "active" },
      { name: "Incoming data is checked before use", status: "active" },
      { name: "Protection against database attacks (SQL injection)", status: "active" },
      { name: "Protection against the server being tricked into calling other servers (SSRF)", status: "planned" },
      { name: "Protection against floods of traffic (DDoS)", status: "future" },
    ],
  },
];

function statusBadge(status: string) {
  if (status === "active") return <Badge variant="success">Working now</Badge>;
  if (status === "planned") return <Badge variant="warning">Being built</Badge>;
  return <Badge variant="neutral">Later</Badge>;
}

const countOf = (status: string) =>
  SECURITY_CONTROLS.reduce((n, c) => n + c.controls.filter((x) => x.status === status).length, 0);

export default function AdminSecurityPage() {
  return (
    <>
      <ConsolePageHeader
        title="Security status"
        description="A list of the protections ComputeCoin has today, the ones still being built, and the ones planned for later. Read it to know what to tell customers and hosts. Do not promise more than it says."
      />

      <div className="space-y-6">
        <DemoBanner>
          This list was written by the ComputeCoin team. No outside company has checked it. It is not a replacement for an independent security audit.
        </DemoBanner>

        <div className="grid gap-4 sm:grid-cols-3">
          <Card>
            <CardContent className="flex items-center gap-3 p-5">
              <ShieldCheck className="h-8 w-8 text-success" />
              <div>
                <p className="text-2xl font-bold">{countOf("active")}</p>
                <p className="text-sm text-muted-foreground">Working now</p>
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="flex items-center gap-3 p-5">
              <AlertTriangle className="h-8 w-8 text-warning" />
              <div>
                <p className="text-2xl font-bold">{countOf("planned")}</p>
                <p className="text-sm text-muted-foreground">Being built</p>
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="flex items-center gap-3 p-5">
              <Shield className="h-8 w-8 text-muted-foreground" />
              <div>
                <p className="text-2xl font-bold">{countOf("future")}</p>
                <p className="text-sm text-muted-foreground">Planned for later</p>
              </div>
            </CardContent>
          </Card>
        </div>

        {SECURITY_CONTROLS.map((cat) => (
          <Card key={cat.category}>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <Lock className="h-4 w-4 text-primary" />
                {cat.category}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <ul className="space-y-2">
                {cat.controls.map((ctrl) => (
                  <li key={ctrl.name} className="flex items-center justify-between gap-4">
                    <span className="text-sm text-muted-foreground">{ctrl.name}</span>
                    {statusBadge(ctrl.status)}
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>
        ))}

        <Card className="border-warning/40 bg-warning/5">
          <CardContent className="flex gap-3 p-5">
            <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-warning" />
            <div className="text-sm">
              <p className="font-medium text-foreground">
                Reporting a security problem
                <HelpTip term="responsible disclosure">Telling the team privately first, so they can fix a weakness before anyone can misuse it.</HelpTip>
              </p>
              <p className="mt-1 text-muted-foreground">
                If you find a security problem, tell the security team directly. Do not post it in a public place. The full list of known risks is in the file docs/security/threat-model.md in the project folder.
              </p>
            </div>
          </CardContent>
        </Card>
      </div>
    </>
  );
}
