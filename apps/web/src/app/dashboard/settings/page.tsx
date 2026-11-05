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
import { useToast } from "@/components/console/toast";

const ACCOUNT_ERRORS: Record<string, string> = {
  current_password_incorrect: "Your current password is not correct.",
  new_password_same_as_current: "Your new password must be different from your current one. Choose another.",
  password_incorrect: "That password is not right. Check it and try again.",
  account_has_active_jobs: "You still have jobs running. Wait for them to finish, or cancel them, then try again.",
  account_has_held_credits: "A running job is holding some of your credits. Wait for it to finish, then try again.",
  account_has_open_disputes: "You have a dispute that is still open. Wait for our answer, then try again.",
  account_has_pending_payouts: "You have a payout request that is not finished. Wait for it to finish, then try again.",
  administrator_cannot_self_delete: "An administrator account cannot delete itself. Ask another administrator to remove your admin role first.",
  credits_would_be_forfeited: "Tick the box to confirm you give up your remaining credits, then try again.",
};

function accountError(err: unknown): string {
  const code = apiErrorCode(err);
  if (code && ACCOUNT_ERRORS[code]) return ACCOUNT_ERRORS[code];
  if (err instanceof ApiError) {
    if (err.status === 429) return "Too many tries. Wait a few minutes, then try again.";
    return err.message;
  }
  return "Something went wrong. Check your internet connection and try again.";
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
  const toast = useToast();

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
      toast.success("Your name was saved.");
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
    if (newPw.length < 8) return setPwLocalError("That password is too short. Use at least 8 characters.");
    if (newPw !== newPw2) return setPwLocalError("The two new passwords are different. Type the same password in both boxes.");
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
        description="Change your name or password, log out, or delete your account."
      />
      <div className="max-w-2xl space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>Your details</CardTitle>
            <CardDescription>Your email is how you log in. You cannot change it here.</CardDescription>
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
                <Label htmlFor="s-name">Your name (shown in the app)</Label>
                <Input
                  id="s-name"
                  placeholder="Not set yet"
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
              When you change your password, you are logged out on every device, including this one. Then log in again with the new password.
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
                <Input
                  id="s-new-pw"
                  type="password"
                  autoComplete="new-password"
                  aria-describedby="s-new-pw-help"
                  value={newPw}
                  onChange={(e) => setNewPw(e.target.value)}
                />
                <p id="s-new-pw-help" className="text-xs text-muted-foreground">Use at least 8 characters.</p>
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
            <CardTitle>Log out</CardTitle>
            <CardDescription>
              Log out to end your visit on this device. We do not keep your password in this browser.
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
              Delete your account <Badge variant="danger">Cannot be undone</Badge>
            </CardTitle>
            <CardDescription>
              Deleting your account removes your email and name, logs you out everywhere, and turns
              off your API keys and any computers you added as a host. We keep your job, invoice and
              payment records, but without your name or email, because they are financial records.
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
              You cannot undo this. You can sign up again with the same email later, but
              nothing from this account will come with you.
            </DialogDescription>
          </DialogHeader>
          <form
            id="delete-account-form"
            className="space-y-4"
            onSubmit={(e) => {
              e.preventDefault();
              if (delPw && !(hasCredits && !forfeit) && !deleteMut.isPending) deleteMut.mutate();
            }}
          >
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
                  I give up my remaining <span className="font-mono">{available}</span> credits. They are {CREDITS_LABEL.toLowerCase()}.
                </span>
              </label>
            ) : null}
            <div className="space-y-1.5">
              <Label htmlFor="s-del-pw">Type your password to confirm</Label>
              <Input id="s-del-pw" type="password" autoComplete="current-password" value={delPw} onChange={(e) => setDelPw(e.target.value)} />
            </div>
            {deleteMut.isError ? <ErrorLine message={accountError(deleteMut.error)} /> : null}
          </form>
          <DialogFooter>
            <Button variant="outline" onClick={closeDelete} disabled={deleteMut.isPending}>
              Cancel
            </Button>
            <Button
              type="submit"
              form="delete-account-form"
              variant="danger"
              disabled={!delPw || (hasCredits && !forfeit) || deleteMut.isPending}
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
