"use client";

import "@xterm/xterm/css/xterm.css";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ArrowLeft, Loader2 } from "lucide-react";
import { useToast } from "@/components/console/toast";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { sessionsApi } from "@/lib/sessions";
import { SessionChannel, T_CLOSE, T_DATA, T_READY, T_RESIZE, fromHex } from "@/lib/session-crypto";

/** Minimal terminal over the end-to-end encrypted relay. xterm.js is loaded in the browser only. */
export default function SessionTerminalPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const qc = useQueryClient();
  const toast = useToast();
  const [confirmStop, setConfirmStop] = useState(false);
  const [stopping, setStopping] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const [state, setState] = useState("waiting for a host's computer");
  const info = useQuery({ queryKey: ["session", sessionId], queryFn: () => sessionsApi.get(sessionId), refetchInterval: (q) => (q.state.status === "error" ? false : 3000), retry: false });
  const live = info.data && ["pending", "starting", "active"].includes(info.data.status);
  const ready = info.data?.status === "active" && !!info.data.host_pubkey;

  useEffect(() => {
    if (!ready || !box.current) return;
    let cancelled = false;
    let ws: WebSocket | null = null;
    let term: import("@xterm/xterm").Terminal | null = null;
    (async () => {
      const [{ Terminal }, { FitAddon }, { x25519 }] = await Promise.all([
        import("@xterm/xterm"), import("@xterm/addon-fit"), import("@noble/curves/ed25519.js"),
      ]);
      const privHex = (() => { try { return sessionStorage.getItem(`ccsess:${sessionId}`); } catch { return null; } })();
      if (!privHex) { setState("This tab cannot open the session. The key is kept only in the browser tab where you started it. Go back to Sessions and start a new one."); return; }
      const priv = fromHex(privHex);
      const tok = await sessionsApi.connectToken(sessionId);
      if (cancelled || !tok.host_pubkey) return;
      term = new Terminal({ cursorBlink: true, fontSize: 14 });
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(box.current!);
      fit.fit();
      const enc = new TextEncoder();
      let chan: SessionChannel | null = null;
      ws = new WebSocket(sessionsApi.relayUrl(sessionId, tok.token));
      ws.binaryType = "arraybuffer";
      ws.onopen = () => ws!.send(JSON.stringify({ type: "hello", epk: Array.from(x25519.getPublicKey(priv), (b) => b.toString(16).padStart(2, "0")).join("") }));
      ws.onmessage = (ev) => {
        if (typeof ev.data === "string") {
          const m = JSON.parse(ev.data);
          if (m.type === "hello_ack") chan = new SessionChannel(sessionId, priv, tok.host_pubkey!, m.epk);
          return;
        }
        if (!chan) return;
        try {
          const { kind, payload } = chan.open(new Uint8Array(ev.data as ArrayBuffer));
          if (kind === T_READY) {
            setState("connected (locked end to end)");
            ws!.send(chan.seal(T_RESIZE, enc.encode(JSON.stringify({ rows: term!.rows, cols: term!.cols }))));
          } else if (kind === T_DATA) term!.write(payload);
          else if (kind === T_CLOSE) setState("the program in the session has ended");
        } catch { setState("the connection was closed because a message failed its safety check. Start a new session if this keeps happening"); ws!.close(); }
      };
      ws.onclose = () => { if (!cancelled) setState((s) => (s.startsWith("connected") ? "disconnected. Reload the page to reconnect" : s)); };
      term.onData((d) => { if (chan && ws?.readyState === 1) ws.send(chan.seal(T_DATA, enc.encode(d))); });
      term.onResize(({ rows, cols }) => { if (chan && ws?.readyState === 1) ws.send(chan.seal(T_RESIZE, enc.encode(JSON.stringify({ rows, cols })))); });
    })().catch(() => setState("could not connect. Reload the page to try again"));
    return () => { cancelled = true; ws?.close(); term?.dispose(); };
  }, [ready, sessionId]);

  async function stop() {
    setStopping(true);
    try {
      await sessionsApi.stop(sessionId);
      toast.success("Session stopped. You are no longer being charged for it.");
      qc.invalidateQueries({ queryKey: ["session", sessionId] });
      setConfirmStop(false);
    } catch {
      toast.error("We could not stop the session. Press Stop session to try again.");
    } finally {
      setStopping(false);
    }
  }

  const status = info.data?.status;
  return (
    <>
      <ConsolePageHeader
        title="Session"
        description="A command window (terminal) on a host's computer. What you type is locked between your browser and the computer. You only pay while the session is running."
        action={
          <div className="flex flex-wrap gap-2">
            <Button asChild variant="outline">
              <Link href="/dashboard/sessions">
                <ArrowLeft /> Back
              </Link>
            </Button>
            {live ? (
              <Button variant="danger" onClick={() => setConfirmStop(true)}>
                Stop session
              </Button>
            ) : null}
          </div>
        }
      />
      {status ? (
        <div role="status" className="mb-3 flex flex-wrap items-center gap-2 text-sm">
          <Badge variant={status === "active" ? "success" : status === "failed" ? "danger" : "neutral"}>
            {status.charAt(0).toUpperCase() + status.slice(1)}
            {info.data?.stop_reason ? ` (${info.data.stop_reason.replace(/_/g, " ")})` : ""}
          </Badge>
          <span className="text-muted-foreground">{state}</span>
        </div>
      ) : null}
      {info.isError ? (
        <div role="alert" className="mb-3 rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
          {info.error instanceof Error && /not.?found|404/i.test(info.error.message)
            ? "This session does not exist, or it belongs to a different account. Go back to Sessions to see yours."
            : "We could not load this session. Reload the page to try again."}
        </div>
      ) : null}
      <div ref={box} hidden={info.isError} className="h-[28rem] w-full rounded-md border bg-black p-2" aria-label="Terminal" />

      <Dialog open={confirmStop} onOpenChange={(open) => { if (!open && !stopping) setConfirmStop(false); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Stop this session?</DialogTitle>
            <DialogDescription>
              The program in the terminal is ended and you stop being charged. Anything you did not save to
              your uploads is lost.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmStop(false)} disabled={stopping}>
              Keep it running
            </Button>
            <Button variant="danger" onClick={() => void stop()} disabled={stopping}>
              {stopping ? <Loader2 className="animate-spin" /> : null}
              Stop session
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
