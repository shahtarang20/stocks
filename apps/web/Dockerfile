# syntax=docker/dockerfile:1

# ---- Dependencies ----
FROM node:22-alpine AS deps
WORKDIR /app
# libc6-compat helps some native deps (e.g. sharp) on Alpine.
RUN apk add --no-cache libc6-compat
COPY package.json package-lock.json* ./
RUN npm ci

# ---- Builder ----
FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
# NEXT_PUBLIC_* values are inlined into the browser bundle (and the CSP in next.config.mjs is built from them)
# at BUILD time. Setting them only as container environment variables at runtime has no effect, so the image
# would call http://localhost:8000. Pass them with `--build-arg` / compose `build.args`.
ARG NEXT_PUBLIC_API_BASE_URL=https://api.computecoin.ai
ARG NEXT_PUBLIC_SITE_URL
ARG NEXT_PUBLIC_DATA_PLANE_ORIGIN
ARG NEXT_PUBLIC_AUP_VERSION
ARG NEXT_PUBLIC_PRIVACY_GUARDIAN_PIN
ARG NEXT_PUBLIC_PRIVACY_REQUIRE_GUARDIAN_PIN
ARG NEXT_PUBLIC_SESSION_PROXY_ORIGIN
ARG NEXT_PUBLIC_SESSION_PROXY_ALLOW_SAME_ORIGIN
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL \
    NEXT_PUBLIC_SITE_URL=$NEXT_PUBLIC_SITE_URL \
    NEXT_PUBLIC_DATA_PLANE_ORIGIN=$NEXT_PUBLIC_DATA_PLANE_ORIGIN \
    NEXT_PUBLIC_AUP_VERSION=$NEXT_PUBLIC_AUP_VERSION \
    NEXT_PUBLIC_PRIVACY_GUARDIAN_PIN=$NEXT_PUBLIC_PRIVACY_GUARDIAN_PIN \
    NEXT_PUBLIC_PRIVACY_REQUIRE_GUARDIAN_PIN=$NEXT_PUBLIC_PRIVACY_REQUIRE_GUARDIAN_PIN \
    NEXT_PUBLIC_SESSION_PROXY_ORIGIN=$NEXT_PUBLIC_SESSION_PROXY_ORIGIN \
    NEXT_PUBLIC_SESSION_PROXY_ALLOW_SAME_ORIGIN=$NEXT_PUBLIC_SESSION_PROXY_ALLOW_SAME_ORIGIN
RUN npm run build

# ---- Runner ----
FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

# Run as a non-root user.
RUN addgroup --system --gid 1001 nodejs \
  && adduser --system --uid 1001 nextjs

# Leverage Next.js standalone output (see next.config.mjs: output: "standalone").
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
