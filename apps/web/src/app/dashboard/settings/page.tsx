"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { AlertCircle, CheckCircle2, Loader2 } from "lucide-react";
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
import { api, apiErrorCode, ApiError, clearStoredTokens, CREDITS_LABEL } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

const ACCOUNT_ERRORS: Record<string, string> = {
  current_password_incorrect: "Your current password is not correct.",
  new_password_same_as_current: "Choose a new password that is different from the current one.",
  password_incorrect: "That password is not correct.",
  account_has_active_jobs: "You still have jobs in progress. Wait for them to finish or cancel them first.",
  account_has_held_credits: "Some of your credits are held by a job in progress. Wait for it to finish first.",
  account_has_open_disputes: "You have an open dispute. It must be resolved before the account can be deleted.",
  account_has_pending_payouts: "You have a payout request in progress. Wait for it to settle first.",
  administrator_cannot_self_delete: "Administrator accounts cannot delete themselves. Ask another administrator to remove your admin role first.",
  credits_would_be_forfeited: "Tick the box to confirm you give up your remaining credits.",
};

function accountError(err: unknown): string {
  const code = apiErrorCode(err);
  if (code && ACCOUNT_ERRORS[code]) return ACCOUNT_ERRORS[code];
  if (err instanceof ApiError) {
    if (err.status === 429) return "Too many attempts. Please wait a few minutes and try again.";
    return err.message;
  }
  return "Something went wrong. Please try again.";
}

function ErrorLine({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
    >
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
      <span>{message}</span>
    </div>
  );
}

export default function SettingsPage() {
  const { user, email, accessToken, refreshUser } = useAuth();
  const token = accessToken as string;

  // ---- profile ----
  const [name, setName] = useState("");
  const [nameSaved, setNameSaved] = useState(false);
  useEffect(() => {
    setName(user?.display_name ?? "");
  }, [user?.display_name]);
  const profileMut = useMutation({
    mutationFn: () => api.updateProfile(token, name.trim()),
    onSuccess: async () => {
      await refreshUser();
      setNameSaved(true);
      setTimeout(() => setNameSaved(false), 3000);
    },
  });
  const nameDirty = name.trim() !== (user?.display_name ?? "");

  // ---- password ----
  const [curPw, setCurPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [newPw2, setNewPw2] = useState("");
  const [pwLocalError, setPwLocalError] = useState<string | null>(null);
  const pwMut = useMutation({
    mutationFn: () => api.changePassword(token, curPw, newPw),
    onSuccess: () => {
      // The server ended every session, including this one. Navigate for real
      // (not router.push): clearing the user first makes the auth guard redirect to
      // /login?next=... before our own navigation, which hid the confirmation.
      clearStoredTokens();
      window.location.assign("/login?changed=1");
    },
  });
  function submitPassword(e: React.FormEvent) {
    e.preventDefault();
    setPwLocalError(null);
    if (newPw.length < 8) return setPwLocalError("New password must be at least 8 characters.");
    if (newPw !== newPw2) return setPwLocalError("The new passwords do not match.");
    pwMut.mutate();
  }

  // ---- delete account ----
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [delPw, setDelPw] = useState("");
  const [forfeit, setForfeit] = useState(false);
  const accountQuery = useQuery({
    queryKey: ["billing-account-settings"],
    enabled: Boolean(accessToken) && confirmDelete,
    queryFn: () => api.getBillingAccount(token),
  });
  const available = accountQuery.data?.available ?? "0.00";
  const hasCredits = Number(available) > 0;
  const deleteMut = useMutation({
    mutationFn: () => api.deleteAccount(token, delPw, forfeit),
    onSuccess: () => {
      clearStoredTokens();
      window.location.assign("/");
    },
  });
  function closeDelete() {
    setConfirmDelete(false);
    setDelPw("");
    setForfeit(false);
    deleteMut.reset();
  }

  return (
    <>
      <ConsolePageHeader
        title="Settings"
        description="Manage your account profile, password and session."
      />
      <div className="max-w-2xl space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>Profile</CardTitle>
            <CardDescription>Your email is your sign-in and cannot be changed here.</CardDescription>
          </CardHeader>
          <CardContent>
            <form
              className="space-y-4"
              onSubmit={(e) => {
                e.preventDefault();
                profileMut.mutate();
              }}
            >
              <div className="space-y-1.5">
                <Label htmlFor="s-email">Email</Label>
                <Input id="s-email" type="email" value={email ?? ""} readOnly />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="s-name">Display name</Label>
                <Input
                  id="s-name"
                  placeholder="Not set"
                  maxLength={120}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </div>
              {profileMut.isError ? <ErrorLine message={accountError(profileMut.error)} /> : null}
              <div className="flex items-center gap-3">
                <Button type="submit" disabled={!nameDirty || profileMut.isPending}>
                  {profileMut.isPending ? <Loader2 className="animate-spin" /> : null}
                  Save changes
                </Button>
                {nameSaved ? (
                  <span role="status" className="flex items-center gap-1 text-sm text-success">
                    <CheckCircle2 className="h-4 w-4" /> Saved
                  </span>
                ) : null}
              </div>
            </form>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Password</CardTitle>
            <CardDescription>
              Changing your password signs you out everywhere, including this device.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form className="space-y-4" onSubmit={submitPassword} noValidate>
              <div className="space-y-1.5">
                <Label htmlFor="s-cur-pw">Current password</Label>
                <Input id="s-cur-pw" type="password" autoComplete="current-password" value={curPw} onChange={(e) => setCurPw(e.target.value)} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="s-new-pw">New password</Label>
                <Input id="s-new-pw" type="password" autoComplete="new-password" value={newPw} onChange={(e) => setNewPw(e.target.value)} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="s-new-pw2">Confirm new password</Label>
                <Input id="s-new-pw2" type="password" autoComplete="new-password" value={newPw2} onChange={(e) => setNewPw2(e.target.value)} />
              </div>
              <ErrorLine message={pwLocalError ?? (pwMut.isError ? accountError(pwMut.error) : null)} />
              <Button type="submit" disabled={!curPw || !newPw || pwMut.isPending}>
                {pwMut.isPending ? <Loader2 className="animate-spin" /> : null}
                Change password
              </Button>
            </form>
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
              onClick={async () => {
                await api.logout().catch(() => {});
                clearStoredTokens();
                window.location.assign("/");
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
              Deleting your account removes your email and name, signs you out everywhere and
              revokes your API keys and machines. Job, invoice and ledger records are kept
              without your identity, because they are financial records.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button variant="danger" onClick={() => setConfirmDelete(true)}>
              Delete account
            </Button>
          </CardContent>
        </Card>
      </div>

      <Dialog open={confirmDelete} onOpenChange={(open) => (open ? setConfirmDelete(true) : closeDelete())}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete your account?</DialogTitle>
            <DialogDescription>
              This cannot be undone. You can sign up again with the same email later, but
              nothing from this account carries over.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            {hasCredits ? (
              <label className="flex items-start gap-2 text-sm">
                <input
                  id="s-forfeit"
                  type="checkbox"
                  className="mt-1"
                  checked={forfeit}
                  onChange={(e) => setForfeit(e.target.checked)}
                />
                <span>
                  I give up my remaining <span className="font-mono">{available}</span> {CREDITS_LABEL}.
                </span>
              </label>
            ) : null}
            <div className="space-y-1.5">
              <Label htmlFor="s-del-pw">Enter your password to confirm</Label>
              <Input id="s-del-pw" type="password" autoComplete="current-password" value={delPw} onChange={(e) => setDelPw(e.target.value)} />
            </div>
            {deleteMut.isError ? <ErrorLine message={accountError(deleteMut.error)} /> : null}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeDelete} disabled={deleteMut.isPending}>
              Cancel
            </Button>
            <Button
              variant="danger"
              disabled={!delPw || (hasCredits && !forfeit) || deleteMut.isPending}
              onClick={() => deleteMut.mutate()}
            >
              {deleteMut.isPending ? <Loader2 className="animate-spin" /> : null}
              Delete my account
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
