"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, RefreshCw, ShieldAlert, UserCheck, UserX, Users } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, RelativeTime, TableSkeleton } from "@/components/console/bits";
import { useToast } from "@/components/console/toast";
import { capitalize } from "@/lib/format";
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
import { api, ApiError, type AdminUser } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

export default function AdminUsersPage() {
  const { accessToken, email: currentEmail } = useAuth();
  const token = accessToken as string;
  const queryClient = useQueryClient();
  const toast = useToast();

  const [actionTarget, setActionTarget] = useState<{ user: AdminUser; action: "suspend" | "reinstate" } | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ["admin", "users"],
    enabled: Boolean(accessToken),
    queryFn: () => api.listAdminUsers(token),
    refetchInterval: 30000,
  });

  const suspendMutation = useMutation({
    mutationFn: (userId: string) => api.suspendUser(token, userId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "users"] });
      toast.success("Account suspended.");
      setActionTarget(null);
    },
    onError: (err) => {
      setErrorMessage(err instanceof ApiError ? err.message : "We could not suspend this user. Please try again, or check the audit log.");
    },
  });

  const reinstateMutation = useMutation({
    mutationFn: (userId: string) => api.reinstateUser(token, userId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "users"] });
      toast.success("Account reinstated.");
      setActionTarget(null);
    },
    onError: (err) => {
      setErrorMessage(err instanceof ApiError ? err.message : "We could not reinstate this user. Please try again, or check the audit log.");
    },
  });

  const users = query.data ?? [];

  const isPendingAction = suspendMutation.isPending || reinstateMutation.isPending;

  function handleConfirmAction() {
    if (!actionTarget) return;
    setErrorMessage(null);
    if (actionTarget.action === "suspend") {
      suspendMutation.mutate(actionTarget.user.user_id);
    } else {
      reinstateMutation.mutate(actionTarget.user.user_id);
    }
  }

  return (
    <>
      <ConsolePageHeader
        title="Users"
        description="See everyone who has an account. Suspend an account to stop it from using ComputeCoin, or reinstate it to give access back. Every action is saved in the audit log."
        action={
          <Button variant="outline" onClick={() => query.refetch()} disabled={query.isFetching}>
            <RefreshCw className={query.isFetching ? "animate-spin" : undefined} />
            Refresh
          </Button>
        }
      />

      <div className="space-y-6">
        {query.isLoading ? (
          <TableSkeleton rows={5} label="Loading users" />
        ) : query.isError ? (
          <ErrorState
            title="We could not load the users"
            message={query.error instanceof ApiError ? query.error.message : undefined}
            onRetry={() => query.refetch()}
            retrying={query.isFetching}
          />
        ) : users.length === 0 ? (
          <EmptyState icon={Users} title="No users yet">People show up here after they create an account.</EmptyState>
        ) : (
          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Email</TableHead>
                  <TableHead className="hidden md:table-cell">Roles</TableHead>
                  <TableHead>Account</TableHead>
                  <TableHead className="hidden sm:table-cell">Email confirmed</TableHead>
                  <TableHead className="hidden md:table-cell">Joined</TableHead>
                  <TableHead className="text-right">What to do</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {users.map((user) => {
                  const isSelf = user.email === currentEmail;
                  return (
                    <TableRow key={user.user_id}>
                      <TableCell className="max-w-[12rem] break-all font-medium sm:max-w-none">
                        {user.email}
                        {isSelf && (
                          <span className="ml-2 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-semibold text-primary">
                            You
                          </span>
                        )}
                        <span className="mt-0.5 block break-normal text-xs font-normal capitalize text-muted-foreground md:hidden">
                          {user.roles.map((r) => (r === "provider" ? "host" : r.replace("_", " "))).join(", ")}
                        </span>
                      </TableCell>
                      <TableCell className="hidden md:table-cell">
                        <div className="flex flex-wrap gap-1">
                          {user.roles.length > 0 ? (
                            user.roles.map((r) => (
                              <Badge key={r} variant={r === "administrator" ? "warning" : "neutral"} className="capitalize text-xs">
                                {r === "provider" ? "host" : r.replace("_", " ")}
                              </Badge>
                            ))
                          ) : (
                            <span className="text-xs text-muted-foreground">—</span>
                          )}
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge variant={user.status === "active" ? "success" : "danger"}>
                          {capitalize(user.status)}
                        </Badge>
                      </TableCell>
                      <TableCell className="hidden text-sm text-muted-foreground sm:table-cell">
                        {user.email_verified ? (
                          <span className="inline-flex items-center gap-1 text-success">
                            <CheckCircle2 className="h-3.5 w-3.5" /> Yes
                          </span>
                        ) : (
                          "No"
                        )}
                      </TableCell>
                      <TableCell className="hidden text-sm text-muted-foreground md:table-cell">
                        <RelativeTime iso={user.created_at} />
                      </TableCell>
                      <TableCell className="text-right">
                        {user.status === "deleted" ? (
                          <span className="text-xs text-muted-foreground">Deleted</span>
                        ) : user.status === "active" ? (
                          <Button
                            variant="outline"
                            size="sm"
                            disabled={isSelf}
                            onClick={() => {
                              setErrorMessage(null);
                              setActionTarget({ user, action: "suspend" });
                            }}
                            className="text-danger hover:bg-danger/10 hover:text-danger"
                          >
                            <UserX className="h-3.5 w-3.5 mr-1" />
                            Suspend account
                          </Button>
                        ) : (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => {
                              setErrorMessage(null);
                              setActionTarget({ user, action: "reinstate" });
                            }}
                            className="text-success hover:bg-success/10 hover:text-success"
                          >
                            <UserCheck className="h-3.5 w-3.5 mr-1" />
                            Reinstate account
                          </Button>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </Card>
        )}
      </div>

      {/* Confirmation Dialog */}
      <Dialog open={Boolean(actionTarget)} onOpenChange={(open) => !open && setActionTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <ShieldAlert className="h-5 w-5 text-warning" />
              {actionTarget?.action === "suspend" ? "Suspend this account?" : "Reinstate this account?"}
            </DialogTitle>
            <DialogDescription>
              {actionTarget?.action === "suspend"
                ? `${actionTarget.user.email} will lose access right away and will not be able to send or run jobs. You can reinstate the account later.`
                : `${actionTarget?.user.email} will get full access again.`}
            </DialogDescription>
          </DialogHeader>

          {errorMessage && (
            <div role="alert" className="rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
              {errorMessage}
            </div>
          )}

          <DialogFooter className="gap-2 sm:gap-0">
            <Button variant="outline" onClick={() => setActionTarget(null)} disabled={isPendingAction}>
              Cancel
            </Button>
            <Button
              variant={actionTarget?.action === "suspend" ? "default" : "outline"}
              className={actionTarget?.action === "suspend" ? "bg-danger hover:bg-danger/90 text-white" : "border-success text-success hover:bg-success/10"}
              onClick={handleConfirmAction}
              disabled={isPendingAction}
            >
              {isPendingAction
                ? "Processing…"
                : actionTarget?.action === "suspend"
                ? "Suspend account"
                : "Reinstate account"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
