"use client";

import { useState } from "react";
import { KeyRound, Plus } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { DemoBanner } from "@/components/demo-banner";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface DemoKey {
  id: string;
  label: string;
  prefix: string;
  created: string;
  lastUsed: string;
}

const DEMO_KEYS: DemoKey[] = [
  { id: "k1", label: "ci-pipeline", prefix: "cc_live_a1b2", created: "2026-09-02", lastUsed: "2026-10-15" },
  { id: "k2", label: "local-dev", prefix: "cc_live_9f8e", created: "2026-08-19", lastUsed: "2026-10-10" },
];

export default function ApiKeysPage() {
  const [revokeTarget, setRevokeTarget] = useState<DemoKey | null>(null);

  return (
    <>
      <ConsolePageHeader
        title="API keys"
        description="Programmatic access to the ComputeCoin API. Key management is a placeholder in Phase 1."
        action={
          <Button disabled>
            <Plus /> Create key
          </Button>
        }
      />
      <div className="space-y-6">
        <DemoBanner>
          these keys are fake, non-functional placeholders. Creating and
          revoking keys is wired as a UI demonstration only.
        </DemoBanner>

        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Label</TableHead>
                <TableHead>Key</TableHead>
                <TableHead>Created</TableHead>
                <TableHead>Last used</TableHead>
                <TableHead className="text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {DEMO_KEYS.map((key) => (
                <TableRow key={key.id}>
                  <TableCell className="font-medium">
                    <span className="flex items-center gap-2">
                      <KeyRound className="h-4 w-4 text-muted-foreground" />
                      {key.label}
                    </span>
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {key.prefix}
                    <span className="text-muted-foreground">••••••••••••</span>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {key.created}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {key.lastUsed}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="text-danger"
                      onClick={() => setRevokeTarget(key)}
                    >
                      Revoke
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>

        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Badge variant="neutral">Security</Badge>
          Real keys will be shown once at creation and stored hashed server-side.
        </div>
      </div>

      <Dialog
        open={Boolean(revokeTarget)}
        onOpenChange={(open) => !open && setRevokeTarget(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Revoke API key?</DialogTitle>
            <DialogDescription>
              Revoking{" "}
              <span className="font-medium text-foreground">
                {revokeTarget?.label}
              </span>{" "}
              would immediately invalidate it. Any integration using it would
              stop working. This demo does not affect real keys.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRevokeTarget(null)}>
              Keep key
            </Button>
            <Button variant="danger" onClick={() => setRevokeTarget(null)}>
              Revoke key
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
