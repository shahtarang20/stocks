"use client";

import { useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ConsolePageHeader } from "@/components/console/console-page";
import { Button } from "@/components/ui/button";
import { sessionsApi } from "@/lib/sessions";
import { SessionChannel, T_CLOSE, T_HTTP, T_READY, fromHex, toHex } from "@/lib/session-crypto";
import { SessionHttpProxy } from "@/lib/session-http";

/**
 * Web view of a tcp-mode session (Jupyter, a desktop in the browser). The remote app is shown in an iframe; every
 * request it makes is intercepted by /session-sw.js and carried over the end-to-end encrypted relay to the host.
 * See docs/architecture/U10-interactive-sessions-design.md section 9.
 *
 * SECURITY: the content comes from the (untrusted) host. It must NOT run on the dashboard origin, where it could act as
 * the signed-in user. Set NEXT_PUBLIC_SESSION_PROXY_ORIGIN to a separate origin that serves this same app; the
 * dashboard then only launches it, passing the one-time token and key in the URL fragment. Same-origin mode exists
 * for local development only (NEXT_PUBLIC_SESSION_PROXY_ALLOW_SAME_ORIGIN=1) and is refused otherwise.
 */
const PROXY_ORIGIN = (process.env.NEXT_PUBLIC_SESSION_PROXY_ORIGIN ?? "").replace(/\/$/, "");
const DEV_SAME_ORIGIN = process.env.NEXT_PUBLIC_SESSION_PROXY_ALLOW_SAME_ORIGIN === "1";
const MAX_REQ_BODY = 8 * 1024 * 1024;

interface Launch { priv: string; token: string; hostPub: string }

function readHash(): Launch | null {
  const p = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const priv = p.get("k"), token = p.get("t"), hostPub = p.get("h");
  return priv && token && hostPub && /^[0-9a-f]{64}$/.test(priv) && /^[0-9a-f]{64}$/.test(hostPub) ? { priv, token, hostPub } : null;
}

export default function SessionWebPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const frame = useRef<HTMLIFrameElement>(null);
  const [state, setState] = useState("starting");
  const [src, setSrc] = useState<string | null>(null);
  const [mode, setMode] = useState<"loading" | "launcher" | "proxy" | "refused">("loading");
  const info = useQuery({ queryKey: ["session", sessionId], queryFn: () => sessionsApi.get(sessionId), refetchInterval: 3000, enabled: mode !== "proxy" });

  useEffect(() => {
    const isolated = !!PROXY_ORIGIN && window.location.origin === PROXY_ORIGIN;
    if (isolated) setMode(readHash() ? "proxy" : "refused");
    else if (PROXY_ORIGIN) setMode("launcher");
    else if (DEV_SAME_ORIGIN) setMode("proxy");
    else setMode("refused");
  }, []);

  useEffect(() => {
    if (mode !== "proxy") return;
    let cancelled = false;
    let ws: WebSocket | null = null;
    let proxy: SessionHttpProxy | null = null;
    (async () => {
      const { x25519 } = await import("@noble/curves/ed25519.js");
      let launch = readHash();
      if (launch) history.replaceState(null, "", window.location.pathname); // keep the key out of history
      else {
        const privHex = (() => { try { return sessionStorage.getItem(`ccsess:${sessionId}`); } catch { return null; } })();
        if (!privHex) { setState("This tab cannot open the session. The key is kept only in the tab where you started it."); return; }
        const tok = await sessionsApi.connectToken(sessionId);
        if (!tok.host_pubkey) { setState("The session has no host computer yet. Wait a moment and try again."); return; }
        launch = { priv: privHex, token: tok.token, hostPub: tok.host_pubkey };
      }
      if (!("serviceWorker" in navigator)) { setState("This browser cannot show the web view. Try a recent version of Chrome, Edge, Firefox or Safari."); return; }
      const priv = fromHex(launch.priv);
      let chan: SessionChannel | null = null;
      const send = (payload: Uint8Array) => { if (chan && ws?.readyState === 1) ws.send(chan.seal(T_HTTP, payload)); };
      proxy = new SessionHttpProxy(send);
      (window as unknown as { __ccProxy: SessionHttpProxy }).__ccProxy = proxy; // used by the iframe's WebSocket shim

      navigator.serviceWorker.onmessage = (ev: MessageEvent) => {
        const d = ev.data;
        const port = ev.ports?.[0];
        if (!d || d.type !== "cc-http" || !port || !proxy) return;
        const body = d.body ? new Uint8Array(d.body as ArrayBuffer) : undefined;
        if (body && body.length > MAX_REQ_BODY) { port.postMessage({ t: "err", code: "request_too_large" }); return; }
        const cancel = proxy.request({ method: d.method, path: d.path, headers: d.headers, body }, {
          head: (status, headers) => port.postMessage({ t: "head", status, headers }),
          body: (chunk) => { const ab = chunk.buffer.slice(chunk.byteOffset, chunk.byteOffset + chunk.byteLength) as ArrayBuffer; port.postMessage({ t: "body", data: ab }, [ab]); },
          end: () => port.postMessage({ t: "end" }),
          error: (code) => port.postMessage({ t: "err", code }),
        });
        port.onmessage = (m) => { if (m.data?.t === "cancel") cancel(); };
      };

      ws = new WebSocket(sessionsApi.relayUrl(sessionId, launch.token));
      ws.binaryType = "arraybuffer";
      ws.onopen = () => ws!.send(JSON.stringify({ type: "hello", epk: toHex(x25519.getPublicKey(priv)) }));
      ws.onmessage = async (ev) => {
        if (typeof ev.data === "string") {
          const m = JSON.parse(ev.data);
          if (m.type === "hello_ack") chan = new SessionChannel(sessionId, priv, launch!.hostPub, m.epk);
          return;
        }
        if (!chan) return;
        try {
          const { kind, payload } = chan.open(new Uint8Array(ev.data as ArrayBuffer));
          if (kind === T_READY) {
            setState("connected (locked end to end)");
            await navigator.serviceWorker.register("/session-sw.js", { scope: "/dashboard/sessions/" });
            await navigator.serviceWorker.ready;
            if (!cancelled) setSrc(`/dashboard/sessions/${sessionId}/proxy/`);
          } else if (kind === T_HTTP) proxy?.onFrame(payload);
          else if (kind === T_CLOSE) setState("the program in the session has ended");
        } catch { setState("the connection was closed because a message failed its safety check"); ws!.close(); }
      };
      ws.onclose = () => { proxy?.failAll(); if (!cancelled) setState((s) => (s.startsWith("connected") ? "disconnected. Open the web view again from your Sessions page" : s)); };
    })().catch(() => setState("could not connect. Reload the page to try again"));
    return () => { cancelled = true; ws?.close(); proxy?.failAll(); delete (window as unknown as { __ccProxy?: unknown }).__ccProxy; };
  }, [mode, sessionId]);

  const launch = async () => {
    const privHex = (() => { try { return sessionStorage.getItem(`ccsess:${sessionId}`); } catch { return null; } })();
    if (!privHex) { setState("This tab cannot open the session. The key is kept only in the tab where you started it."); return; }
    const tok = await sessionsApi.connectToken(sessionId);
    if (!tok.host_pubkey) { setState("The session has no host computer yet. Wait a moment and try again."); return; }
    const frag = new URLSearchParams({ k: privHex, t: tok.token, h: tok.host_pubkey }).toString();
    window.open(`${PROXY_ORIGIN}/dashboard/sessions/${sessionId}/web#${frag}`, "_blank", "noopener");
  };

  const ready = info.data?.status === "active" && !!info.data.host_pubkey;
  return (
    <>
      <ConsolePageHeader
        title="Session web view"
        description={mode === "proxy" ? state : mode === "launcher" ? `${info.data?.status ?? "loading"} - ${state}` : mode === "refused" ? "This web view is not available right now." : "loading"}
        action={mode === "launcher" ? <Button disabled={!ready || info.data?.mode !== "tcp"} onClick={() => void launch()}>Open web view</Button> : undefined}
      />
      {mode === "refused" && (
        <p className="text-sm text-muted-foreground">
          The web view must open on its own separate web address, so pages from a host&apos;s computer can never run next to your account. It is not set up on this site yet. Open your session from the Sessions page, or ask the site owner to turn it on.
        </p>
      )}
      {mode === "proxy" && src && (
        <iframe ref={frame} src={src} title="Session web view" className="h-[75vh] w-full rounded-md border bg-white"
          sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-downloads allow-modals" />
      )}
    </>
  );
}
