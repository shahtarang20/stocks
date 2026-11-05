/* U10 session HTTP proxy service worker (docs/architecture/U10-interactive-sessions-design.md section 9).
 * Registered by /dashboard/sessions/<id>/web with scope /dashboard/sessions/. It is STATELESS (a browser may stop and
 * restart a worker at any time): for every proxied request it finds the owning page and hands it a fresh MessageChannel
 * port; the page owns the end-to-end encrypted session channel and answers on that port:
 *   page -> sw: {t:"head", status, headers:[[k,v]]}  {t:"body", data:ArrayBuffer}  {t:"end"}  {t:"err", code}
 * Requests it does not recognise are left to the network untouched. */
"use strict";

const PROXY_RE = /^\/dashboard\/sessions\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\/proxy(\/.*)?$/i;
const WEB_RE = /^\/dashboard\/sessions\/([0-9a-f-]{36})\/web\/?$/i;
const MAX_REQ_BODY = 8 * 1024 * 1024;
const MAX_HTML = 8 * 1024 * 1024;
const HEAD_TIMEOUT_MS = 70000;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

function prefix(sid) { return `/dashboard/sessions/${sid}/proxy/`; }

function shimSource(sid) {
  const P = JSON.stringify(prefix(sid));
  return `(function(){var parent_=window.parent;var API=parent_&&parent_.__ccProxy;if(!API||window.__ccShim)return;window.__ccShim=1;
var Native=window.WebSocket,PFX=${P};
function toPath(u){var x=new URL(u,location.href);if(x.host!==location.host)return null;var p=x.pathname;
 if(p.indexOf(PFX)===0)p="/"+p.slice(PFX.length);return p+x.search;}
function CC(url,protocols){var path=toPath(String(url));if(path===null)return new Native(url,protocols);
 var self_=this,t=new EventTarget();this.__t=t;this.url=String(url);this.readyState=0;this.binaryType="blob";this.protocol="";this.extensions="";this.bufferedAmount=0;
 var hs=["onopen","onmessage","onclose","onerror"];hs.forEach(function(n){self_[n]=null;t.addEventListener(n.slice(2),function(e){if(typeof self_[n]==="function")self_[n].call(self_,e);});});
 var protos=protocols===undefined?[]:Array.isArray(protocols)?protocols.map(String):[String(protocols)];
 var h=API.openWebSocket(path,protos,{
  open:function(p){self_.readyState=1;self_.protocol=p||"";t.dispatchEvent(new Event("open"));},
  message:function(d){var v=d;if(typeof d!=="string"&&self_.binaryType==="blob")v=new Blob([d]);t.dispatchEvent(new MessageEvent("message",{data:v}));},
  error:function(){t.dispatchEvent(new Event("error"));},
  close:function(c,r){self_.readyState=3;t.dispatchEvent(new CloseEvent("close",{code:c,reason:r,wasClean:c===1000}));}});
 this.send=function(d){if(self_.readyState!==1)throw new DOMException("not open","InvalidStateError");
  if(typeof d==="string")h.sendText(d);else if(d instanceof Blob){d.arrayBuffer().then(function(b){h.sendBinary(b);});}else h.sendBinary(d);};
 this.close=function(c,r){if(self_.readyState>=2)return;self_.readyState=2;h.close(c||1000,r||"");};
 this.addEventListener=function(a,b,c){t.addEventListener(a,b,c);};this.removeEventListener=function(a,b,c){t.removeEventListener(a,b,c);};this.dispatchEvent=function(e){return t.dispatchEvent(e);};}
CC.CONNECTING=0;CC.OPEN=1;CC.CLOSING=2;CC.CLOSED=3;window.WebSocket=CC;})();`;
}

function errorPage(status, text) {
  return new Response(`<!doctype html><meta charset="utf-8"><title>${status}</title><p style="font:14px system-ui;padding:2rem">${text}</p>`,
    { status, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
}

async function ownerClient(sid) {
  const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const c of all) {
    const m = WEB_RE.exec(new URL(c.url).pathname);
    if (m && m[1].toLowerCase() === sid.toLowerCase()) return c;
  }
  return null;
}

async function sidFromClient(clientId) {
  if (!clientId) return null;
  const c = await self.clients.get(clientId);
  if (!c) return null;
  const m = PROXY_RE.exec(new URL(c.url).pathname);
  return m ? m[1] : null;
}

function handle(request, sid, path) {
  return (async () => {
    const owner = await ownerClient(sid);
    if (!owner) return errorPage(503, "The session web page is not open. Open it from the dashboard and keep it open.");
    let body = null;
    if (request.method !== "GET" && request.method !== "HEAD") {
      body = await request.arrayBuffer();
      if (body.byteLength > MAX_REQ_BODY) return errorPage(413, "Request body too large for the session proxy.");
    }
    const headers = [];
    request.headers.forEach((v, k) => headers.push([k, v]));
    const ch = new MessageChannel();
    const resp = await new Promise((resolve) => {
      let controller = null, settled = false, isHtml = false, html = [], htmlLen = 0, headInfo = null;
      const timer = setTimeout(() => { if (!settled) { settled = true; resolve(errorPage(504, "The session did not answer in time.")); } }, HEAD_TIMEOUT_MS);
      const done = () => { clearTimeout(timer); ch.port1.close(); };
      ch.port1.onmessage = (ev) => {
        const m = ev.data || {};
        if (m.t === "head") {
          clearTimeout(timer);
          const h = new Headers();
          for (const [k, v] of m.headers || []) {
            if (k === "set-cookie") continue; // cookies are kept by the page, not the browser
            if (k === "location" && v.startsWith("/") && !v.startsWith("//") && !v.startsWith(prefix(sid))) { h.append(k, prefix(sid) + v.slice(1)); continue; }
            try { h.append(k, v); } catch (_) { /* skip an invalid header */ }
          }
          isHtml = /^text\/html/i.test(h.get("content-type") || "");
          const noBody = request.method === "HEAD" || [101, 204, 205, 304].includes(m.status) || m.status < 200 || m.status > 599;
          headInfo = { status: m.status < 200 || m.status > 599 ? 502 : m.status, headers: h, noBody };
          if (noBody) { settled = true; done(); resolve(new Response(null, { status: headInfo.status, headers: h })); return; }
          if (isHtml) return; // buffer, inject the WebSocket shim at the end
          const stream = new ReadableStream({
            start(c) { controller = c; },
            cancel() { ch.port1.postMessage({ t: "cancel" }); done(); },
          });
          settled = true;
          resolve(new Response(stream, { status: headInfo.status, headers: h }));
        } else if (m.t === "body") {
          const chunk = new Uint8Array(m.data);
          if (isHtml) { htmlLen += chunk.length; if (htmlLen > MAX_HTML) { settled = true; done(); resolve(errorPage(502, "HTML response too large for the session proxy.")); return; } html.push(chunk); }
          else if (controller) controller.enqueue(chunk);
        } else if (m.t === "end") {
          if (isHtml && !settled) {
            let text = new TextDecoder().decode(concat(html));
            const tag = `<script src="${prefix(sid)}__cc/ws.js"></script>`;
            text = /<head[^>]*>/i.test(text) ? text.replace(/<head[^>]*>/i, (x) => x + tag) : tag + text;
            headInfo.headers.delete("content-length");
            settled = true; done();
            resolve(new Response(text, { status: headInfo.status, headers: headInfo.headers }));
            return;
          }
          if (controller) controller.close();
          done();
        } else if (m.t === "err") {
          if (!settled) { settled = true; done(); resolve(errorPage(502, "The session could not complete the request (" + String(m.code || "error").replace(/[^a-z0-9_ -]/gi, "") + ").")); }
          else { if (controller) controller.error(new Error("session proxy error")); done(); }
        }
      };
      owner.postMessage({ type: "cc-http", method: request.method, path, headers, body }, body ? [ch.port2, body] : [ch.port2]);
    });
    return resp;
  })();
}

function concat(parts) {
  let n = 0; for (const p of parts) n += p.length;
  const out = new Uint8Array(n); let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  const m = PROXY_RE.exec(url.pathname);
  if (m) {
    const sid = m[1];
    const rest = m[2] || "/";
    if (rest === "/__cc/ws.js") {
      event.respondWith(new Response(shimSource(sid), { headers: { "content-type": "text/javascript; charset=utf-8", "cache-control": "no-store" } }));
      return;
    }
    event.respondWith(handle(request, sid, rest + url.search));
    return;
  }
  // A root-absolute request ("/static/x") made by a page that lives under a proxy prefix is proxied too.
  if (event.clientId) {
    event.respondWith((async () => {
      const sid = await sidFromClient(event.clientId);
      if (!sid) return fetch(request);
      return handle(request, sid, url.pathname + url.search);
    })());
  }
});
