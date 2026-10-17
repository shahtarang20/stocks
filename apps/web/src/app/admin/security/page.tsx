"use client";

import { Shield, ShieldAlert, ShieldCheck, Lock, Eye, AlertTriangle } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { DemoBanner } from "@/components/demo-banner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

const SECURITY_CONTROLS = [
  {
    category: "Authentication",
    controls: [
      { name: "Argon2id password hashing", status: "active" },
      { name: "Short-lived JWTs (15 min)", status: "active" },
      { name: "Rotating refresh tokens with reuse detection", status: "active" },
      { name: "Email verification required", status: "active" },
      { name: "Rate limiting on auth endpoints", status: "active" },
      { name: "Multi-factor authentication", status: "planned" },
    ],
  },
  {
    category: "Authorization",
    controls: [
      { name: "Server-side RBAC (guest/customer/provider/admin)", status: "active" },
      { name: "Project-scoped API keys (hashed, prefix shown)", status: "active" },
      { name: "Audit logs for sensitive actions", status: "active" },
      { name: "Separation of duties for financial adjustments", status: "active" },
    ],
  },
  {
    category: "Workload isolation",
    controls: [
      { name: "No docker socket exposed to workloads", status: "active" },
      { name: "No host filesystem mounts", status: "active" },
      { name: "Resource quotas (CPU/RAM/GPU/disk)", status: "planned" },
      { name: "Restricted network egress", status: "planned" },
      { name: "gVisor / microVM isolation", status: "future" },
      { name: "Confidential computing / attestation", status: "future" },
    ],
  },
  {
    category: "Data & secrets",
    controls: [
      { name: "Secrets never in logs or frontend bundles", status: "active" },
      { name: "Short-lived signed artifact URLs", status: "active" },
      { name: "Checksums on all artifacts", status: "active" },
      { name: "Encryption in transit (TLS)", status: "active" },
      { name: "Encryption at rest", status: "planned" },
      { name: "Malware scanning on uploads", status: "future" },
    ],
  },
  {
    category: "Network & API",
    controls: [
      { name: "CORS restricted to configured frontend", status: "active" },
      { name: "Input validation (Pydantic)", status: "active" },
      { name: "SQL injection protection (ORM)", status: "active" },
      { name: "SSRF defenses", status: "planned" },
      { name: "DDoS protection", status: "future" },
    ],
  },
];

function statusBadge(status: string) {
  if (status === "active") return <Badge variant="success">Active</Badge>;
  if (status === "planned") return <Badge variant="warning">Planned</Badge>;
  return <Badge variant="neutral">Future phase</Badge>;
}

export default function AdminSecurityPage() {
  return (
    <>
      <ConsolePageHeader
        title="Security posture"
        description="Current security controls, planned improvements and future roadmap items. Security is a release requirement, not an afterthought."
      />

      <div className="space-y-6">
        <DemoBanner>
          This is a self-assessed security posture summary. It does not
          substitute for an independent security audit.
        </DemoBanner>

        <div className="grid gap-4 sm:grid-cols-3">
          <Card>
            <CardContent className="flex items-center gap-3 p-5">
              <ShieldCheck className="h-8 w-8 text-success" />
              <div>
                <p className="text-2xl font-bold">17</p>
                <p className="text-sm text-muted-foreground">Active controls</p>
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="flex items-center gap-3 p-5">
              <AlertTriangle className="h-8 w-8 text-warning" />
              <div>
                <p className="text-2xl font-bold">7</p>
                <p className="text-sm text-muted-foreground">Planned</p>
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="flex items-center gap-3 p-5">
              <Shield className="h-8 w-8 text-muted-foreground" />
              <div>
                <p className="text-2xl font-bold">6</p>
                <p className="text-sm text-muted-foreground">Future phases</p>
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
              <p className="font-medium text-foreground">Responsible disclosure</p>
              <p className="mt-1 text-muted-foreground">
                To report a security vulnerability, contact the security team
                directly. Do not open a public issue for security-sensitive findings.
                See <code className="text-xs">docs/security/threat-model.md</code> for
                the full STRIDE threat model and residual risk statements.
              </p>
            </div>
          </CardContent>
        </Card>
      </div>
    </>
  );
}
