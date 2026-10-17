/** @type {import('next').NextConfig} */

// Security headers applied to every response. The CSP is intentionally strict;
// 'unsafe-inline' for styles is kept because Tailwind/Next inject inline styles,
// and connect-src allows the API origin.
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
      "img-src 'self' data: blob:",
      "style-src 'self' 'unsafe-inline'",
      "script-src 'self' 'unsafe-inline'",
      "connect-src 'self' " + (process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000"),
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
