/** @type {import('next').NextConfig} */

// Security headers applied to every response. The CSP is intentionally strict;
// 'unsafe-inline' for styles is kept because Tailwind/Next inject inline styles,
// and connect-src allows the API origin.
const isDev = process.env.NODE_ENV !== "production";
const scriptSrc = isDev
  ? "script-src 'self' 'unsafe-inline' 'unsafe-eval'"
  : "script-src 'self' 'unsafe-inline'";

// The dev server talks to a local API over http and uses a websocket for hot
// reload. Those origins belong in the CSP ONLY in development — shipping them
// in a production build would let the deployed app open connections to
// whatever happens to be listening on the visitor's own machine.
// The interactive-session relay is a WebSocket on the API origin. A CSP host
// source of https://api.example.com does NOT match wss://api.example.com, so
// the ws(s) twin of the API origin must be listed or the browser blocks the
// terminal and web-view connections in production.
const apiOrigin = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000").replace(/\/$/, "");
const apiWsOrigin = apiOrigin.replace(/^http/i, "ws");
const dataPlaneOrigins = (process.env.NEXT_PUBLIC_DATA_PLANE_ORIGIN || "")
  .split(/\s+/)
  .filter(Boolean)
  .map((o) => o.replace(/\/$/, ""));
// Signed output downloads are API-relative, so image and video previews load from the API origin (and, when
// a download is redirected to the object store, from the data-plane origin). 'self' alone blocks them.
const mediaOrigins = [apiOrigin, ...dataPlaneOrigins].join(" ");
const connectSrc = [
  "connect-src 'self'",
  apiOrigin,
  apiWsOrigin,
  // Browser uploads PUT each part straight to the object store with a presigned URL (data plane). That origin is
  // not the API, so without it here the browser blocks every upload. Set at build time, space separated.
  ...dataPlaneOrigins,
  ...(isDev
    ? ["http://localhost:8000", "ws://localhost:3000", "ws://localhost:8000"]
    : []),
].join(" ");

const securityHeaders = [
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  {
    key: "Strict-Transport-Security",
    value: "max-age=63072000; includeSubDomains; preload",
  },
  {
    key: "Content-Security-Policy",
    value: [
      "default-src 'self'",
      "base-uri 'self'",
      "frame-ancestors 'none'",
      "object-src 'none'",
      `img-src 'self' data: blob: ${mediaOrigins}`,
      `media-src 'self' blob: ${mediaOrigins}`,
      "style-src 'self' 'unsafe-inline'",
      scriptSrc,
      connectSrc,
      "font-src 'self' data:",
    ].join("; "),
  },
];

const nextConfig = {
  reactStrictMode: true,
  output: "standalone",
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
