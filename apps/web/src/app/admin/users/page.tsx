"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, CheckCircle2, RefreshCw, ShieldAlert, UserCheck, UserX, Users } from "lucide-react";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
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
import { formatRelativeTime } from "@/lib/utils";

export default function AdminUsersPage() {
  const { accessToken, email: currentEmail } = useAuth();
  const token = accessToken as string;
  const queryClient = useQueryClient();

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
      setActionTarget(null);
    },
    onError: (err) => {
      setErrorMessage(err instanceof ApiError ? err.message : "Failed to suspend user");
    },
  });

  const reinstateMutation = useMutation({
    mutationFn: (userId: string) => api.reinstateUser(token, userId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "users"] });
      setActionTarget(null);
    },
    onError: (err) => {
      setErrorMessage(err instanceof ApiError ? err.message : "Failed to reinstate user");
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
        title="User management"
        description="View and manage platform users live from GET /v1/admin/users. Administrators can suspend and reinstate accounts with immutable audit logging."
        action={
          <Button variant="outline" onClick={() => query.refetch()} disabled={query.isFetching}>
            <RefreshCw className={query.isFetching ? "animate-spin" : undefined} />
            Refresh
          </Button>
        }
      />

      <div className="space-y-6">
        {query.isLoading ? (
          <Card>
            <CardContent className="space-y-3 p-6">
              {Array.from({ length: 5 }).map((_, i) => (
                <Skeleton key={i} className="h-8 w-full" />
              ))}
            </CardContent>
          </Card>
        ) : query.isError ? (
          <Card className="border-danger/40">
            <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
              <AlertCircle className="h-8 w-8 text-danger" />
              <p className="text-sm text-muted-foreground">
                {query.error instanceof ApiError ? query.error.message : "Failed to load platform users"}
              </p>
              <Button variant="outline" onClick={() => query.refetch()}>
                <RefreshCw /> Try again
              </Button>
            </CardContent>
          </Card>
        ) : users.length === 0 ? (
          <Card className="border-dashed">
            <CardContent className="flex flex-col items-center gap-3 p-12 text-center">
              <Users className="h-8 w-8 text-muted-foreground" />
              <h3 className="font-semibold">No registered users found</h3>
            </CardContent>
          </Card>
        ) : (
          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Email</TableHead>
                  <TableHead>Roles</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Verified</TableHead>
                  <TableHead>Joined</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {users.map((user) => {
                  const isSelf = user.email === currentEmail;
                  return (
                    <TableRow key={user.user_id}>
                      <TableCell className="font-medium">
                        {user.email}
                        {isSelf && (
                          <span className="ml-2 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-semibold text-primary">
                            You
                          </span>
                        )}
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-wrap gap-1">
                          {user.roles.length > 0 ? (
                            user.roles.map((r) => (
                              <Badge key={r} variant={r === "administrator" ? "warning" : "neutral"} className="capitalize text-xs">
                                {r.replace("_", " ")}
                              </Badge>
                            ))
                          ) : (
                            <span className="text-xs text-muted-foreground">—</span>
                          )}
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge variant={user.status === "active" ? "success" : "danger"}>
                          {user.status}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">
                        {user.email_verified ? (
                          <span className="inline-flex items-center gap-1 text-success">
                            <CheckCircle2 className="h-3.5 w-3.5" /> Yes
                          </span>
                        ) : (
                          "No"
                        )}
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">
                        {formatRelativeTime(user.created_at)}
                      </TableCell>
                      <TableCell className="text-right">
                        {user.status === "deleted" ? (
                          <span className="text-xs text-muted-foreground">Deleted</span>
                        ) : user.status === "active" ? (
                          <Button
                            variant="outline"
                            size="sm"
                            disabled={isSelf}
                            onClick={() => setActionTarget({ user, action: "suspend" })}
                            className="text-danger hover:bg-danger/10 hover:text-danger"
                          >
                            <UserX className="h-3.5 w-3.5 mr-1" />
                            Suspend
                          </Button>
                        ) : (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => setActionTarget({ user, action: "reinstate" })}
                            className="text-success hover:bg-success/10 hover:text-success"
                          >
                            <UserCheck className="h-3.5 w-3.5 mr-1" />
                            Reinstate
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
              {actionTarget?.action === "suspend" ? "Suspend user account" : "Reinstate user account"}
            </DialogTitle>
            <DialogDescription>
              {actionTarget?.action === "suspend"
                ? `Are you sure you want to suspend ${actionTarget.user.email}? They will immediately lose access to API endpoints and job execution.`
                : `Are you sure you want to reinstate ${actionTarget?.user.email}? Their full platform access will be restored.`}
            </DialogDescription>
          </DialogHeader>

          {errorMessage && (
            <div className="rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
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
                ? "Confirm suspension"
                : "Confirm reinstatement"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
