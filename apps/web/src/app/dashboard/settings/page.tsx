"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useAuth } from "@/lib/auth-context";

export default function SettingsPage() {
  const { email, logout } = useAuth();
  const router = useRouter();
  const [confirmDelete, setConfirmDelete] = useState(false);

  return (
    <>
      <ConsolePageHeader
        title="Settings"
        description="Manage your account profile and session."
      />
      <div className="max-w-2xl space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>Profile</CardTitle>
            <CardDescription>
              Profile editing is a placeholder in Phase 1.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="s-email">Email</Label>
              <Input id="s-email" type="email" value={email ?? ""} readOnly />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="s-name">Display name</Label>
              <Input id="s-name" placeholder="Not set" disabled />
            </div>
            <Button disabled>Save changes</Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Session</CardTitle>
            <CardDescription>
              Log out to end your session on this device. Your session is held
              in secure cookies and in memory, never in local storage.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button
              variant="outline"
              onClick={() => {
                logout();
                router.push("/");
              }}
            >
              Log out
            </Button>
          </CardContent>
        </Card>

        <Card className="border-danger/40">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              Danger zone <Badge variant="danger">Irreversible</Badge>
            </CardTitle>
            <CardDescription>
              Deleting your account removes your preview data. This demo button
              does not perform a real deletion.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button variant="danger" onClick={() => setConfirmDelete(true)}>
              Delete account
            </Button>
          </CardContent>
        </Card>
      </div>

      <Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete your account?</DialogTitle>
            <DialogDescription>
              This would permanently remove your account and associated data.
              In this Phase 1 demo, no data is actually deleted.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmDelete(false)}>
              Cancel
            </Button>
            <Button variant="danger" onClick={() => setConfirmDelete(false)}>
              Delete account
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
