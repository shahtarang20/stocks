# ComputeCoin Web (Phase 1)

The frontend shell for **ComputeCoin**, a decentralized AI GPU compute
marketplace. This is a Phase 1 developer preview: the marketing surface and the
account/console shell are real, while live provisioning, scheduling, and billing
are stubbed and **clearly labelled as demo** wherever placeholder data appears.

> **Honesty constraints enforced in the UI**
> - All synthetic data is labelled "DEMO MODE" and is never presented as live
>   infrastructure, real benchmarks, real utilization, or real token metrics.
> - No token price, market cap, or trading volume is displayed anywhere.
> - Marketing copy frames decentralized compute as *measured, not promised* — it
>   does not claim it is always cheaper, faster, or safer.

## Stack

- **Next.js 15** (App Router) + **React 19** + **TypeScript** (strict)
- **Tailwind CSS 3** with CSS-variable design tokens (dark + light)
- Local shadcn-style UI primitives built on **Radix** (Button, Card, Input,
  Table, Badge, Dialog, Dropdown, Label, Skeleton)
- **TanStack Query** for server state
- **React Hook Form** + **Zod** for forms
- **Recharts** for dashboard charts
- **next-themes** for class-based dark/light theming
- **lucide-react** icons

All dependency versions are pinned in `package.json`.

## Prerequisites

- Node.js 22+
- npm 10+

## Setup

```bash
cd apps/web
cp .env.example .env.local   # optional; defaults are used if omitted
npm install
```

### Environment

| Variable                   | Default                 | Description                          |
| -------------------------- | ----------------------- | ------------------------------------ |
| `NEXT_PUBLIC_API_BASE_URL` | `http://localhost:8000` | Base URL of the FastAPI backend.     |

## Develop

```bash
npm run dev        # http://localhost:3000
```

## Quality gates

```bash
npm run typecheck  # tsc --noEmit (strict)
npm run lint       # next lint
npm run build      # production build (next build)
```

## Build & run (production)

```bash
npm run build
npm run start      # serves on port 3000
```

## Docker

Multi-stage build on `node:22-alpine`, runs as a non-root user, uses Next.js
standalone output.

```bash
docker build -t computecoin-web .
docker run -p 3000:3000 \
  -e NEXT_PUBLIC_API_BASE_URL=http://host.docker.internal:8000 \
  computecoin-web
```

## Backend API contract (Phase 1)

The typed client lives in [`src/lib/api.ts`](src/lib/api.ts):

| Method | Path             | Body                    | Returns                        |
| ------ | ---------------- | ----------------------- | ------------------------------ |
| POST   | `/auth/register` | `{email, password}`     | registered user                |
| POST   | `/auth/login`    | `{email, password}`     | `{access_token, refresh_token}`|
| POST   | `/auth/refresh`  | `{refresh_token}`       | `{access_token, refresh_token}`|
| GET    | `/projects`      | — (Bearer access token) | `Project[]`                    |

## Routes

**Public:** `/`, `/marketplace`, `/marketplace/gpus`, `/pricing`, `/developers`,
`/providers`, `/security`, `/status`, `/about`, `/legal/terms`, `/legal/privacy`

**Auth:** `/login`, `/register`

**Console (client-guarded):** `/dashboard`, `/dashboard/projects`,
`/dashboard/jobs`, `/dashboard/usage`, `/dashboard/billing`,
`/dashboard/api-keys`, `/dashboard/settings`

**Provider:** `/provider/onboarding`, `/provider/dashboard`,
`/provider/hardware`, `/provider/earnings`

## ⚠️ Security notes (Phase 1, dev-only)

- **Token storage:** auth tokens are kept in a React context and persisted to
  `localStorage` (see [`src/lib/auth-context.tsx`](src/lib/auth-context.tsx)).
  This is **development-only** and is marked with a `TODO(security)`. Tokens
  **must** move to secure, `httpOnly`, `SameSite` cookies before production.
- **Route guards are UX only.** The client-side `AuthGuard` improves the
  experience but is **not** a security boundary. Real authorization is enforced
  server-side by the FastAPI backend on every request.

## What is stubbed / not yet connected

- Marketplace listings, dashboard metrics/charts, jobs, usage, billing, API
  keys, provider hardware/earnings — all use **synthetic demo data** (see
  [`src/lib/demo-data.ts`](src/lib/demo-data.ts)) and are labelled DEMO MODE.
- Only **auth** (`register`/`login`/`refresh`) and **`GET /projects`** are wired
  to the live backend. The projects page renders live data when authenticated
  and shows loading / empty / error states otherwise.
- Job submission, project creation, key creation/revocation, payments, and
  provider onboarding actions are disabled placeholders.
