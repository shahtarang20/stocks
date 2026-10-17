# ComputeCoin — Production Cloud Deployment Runbook (Workstream E)

> **Target Architecture:** Modern, serverless-native production stack:
> - **Frontend (Web):** [Vercel](https://vercel.com) (Next.js 15 Standalone)
> - **Backend (API):** [Railway](https://railway.app) (FastAPI + Uvicorn with auto-migrations)
> - **Background Worker:** [Railway](https://railway.app) (Lease Sweeper & Scheduler Worker)
> - **Database:** [Supabase](https://supabase.com) (Managed PostgreSQL 16 + daily backups)
> - **Object Storage:** [Supabase Storage / AWS S3](https://supabase.com) (Artifact uploads)
> - **Cache / Rate Limiting:** [Upstash](https://upstash.com) (Serverless Redis with TLS)
> - **Transactional Email:** [Resend](https://resend.com) (SPF/DKIM verified sender)
> - **Error Monitoring:** [Sentry](https://sentry.io) (APM + Exception alerts)
> - **Payments:** [Stripe](https://stripe.com) (Subscriptions + Usage billing)

---

## 1. Managed Infrastructure Setup

Before deploying the application code, set up the managed cloud services:

### 1.1 Supabase (PostgreSQL & Object Storage)
1. Log in to [https://supabase.com](https://supabase.com) and click **"New Project"**.
2. Select your closest AWS region (e.g. `us-east-1` or `ap-south-1`).
3. Set a strong database password (store in password manager).
4. Go to **Project Settings $\rightarrow$ Database $\rightarrow$ Connection string**:
   - Copy the **Connection pooling (Transaction mode, port 6543)** or Direct connection URI:
     ```
     postgresql+asyncpg://postgres.[PROJECT_REF]:[PASSWORD]@aws-0-[REGION].pooler.supabase.com:6543/postgres
     ```
5. Go to **Storage $\rightarrow$ Create bucket**:
   - Bucket name: `computecoin-artifacts`
   - Privacy: **Private**
6. Enable automated daily point-in-time recovery (PITR) backups under **Database $\rightarrow$ Backups**.

### 1.2 Upstash (Serverless Redis)
1. Log in to [https://upstash.com](https://upstash.com) and click **"Create Database"**.
2. Select the same cloud region as your Supabase database.
3. Enable **TLS (Encryption in Transit)**.
4. Copy the `rediss://` connection URL:
   ```
   rediss://default:[PASSWORD]@[ENDPOINT].upstash.io:6379
   ```

### 1.3 Resend (Transactional Email)
1. Log in to [https://resend.com](https://resend.com) and click **"Add Domain"**.
2. Enter your custom domain (e.g. `computecoin.ai` or `mail.computecoin.ai`).
3. Add the provided DNS records (MX, SPF, and DKIM TXT records) to your DNS registrar (Cloudflare / Namecheap).
4. Under **API Keys**, create a new restricted key with sending permission.
5. Store `RESEND_API_KEY=re_...`.

### 1.4 Sentry (Error Monitoring & Performance)
1. Log in to [https://sentry.io](https://sentry.io) and create a project for `computecoin-api`.
2. Select platform: **FastAPI / Python**.
3. Copy the client DSN:
   ```
   https://[PUBLIC_KEY]@[ORGANIZATION].ingest.sentry.io/[PROJECT_ID]
   ```

---

## 2. Deploying Backend API & Worker to Railway

Railway runs both the HTTP REST API and the dedicated background worker process:

### 2.1 Service 1: API Service (`api`)
1. In [https://railway.app](https://railway.app), create a **New Project $\rightarrow$ Deploy from GitHub repo**.
2. Select your repository: `shahtarang20/stocks`.
3. In **Settings**:
   - **Root Directory:** `apps/api`
   - **Build:** Automatically detects [`apps/api/Dockerfile`](../../apps/api/Dockerfile)
   - **Healthcheck Path:** `/health`
   - **Restart Policy:** `On Failure` (Max retries: 5)
4. In **Variables**, add all production environment variables (see Reference Table below).
5. Railway deploys the image. The entrypoint script automatically executes:
   ```bash
   alembic upgrade head
   ```
   before starting uvicorn workers.

### 2.2 Service 2: Background Worker (`worker`)
1. In the same Railway project, click **"+ New $\rightarrow$ GitHub Repo"**.
2. Select the same repository: `shahtarang20/stocks`.
3. In **Settings**:
   - **Root Directory:** `apps/api`
   - **Start Command Override:** `python -m app.worker`
4. In **Variables**, share the same database and Redis connection variables.

### 2.3 Railway Environment Variables Reference

| Variable | Description | Example / Production Setting |
| :--- | :--- | :--- |
| `ENVIRONMENT` | Deployment environment | `production` |
| `DATABASE_URL` | Supabase AsyncPG connection string | `postgresql+asyncpg://postgres:...@...pooler.supabase.com:6543/postgres` |
| `REDIS_URL` | Upstash Redis TLS URL | `rediss://default:...@...upstash.io:6379` |
| `AUTH_SECRET` | 64+ char random hex string | Generate: `openssl rand -hex 32` |
| `SESSION_SECRET` | 64+ char random hex string | Generate: `openssl rand -hex 32` |
| `FRONTEND_URL` | Public production frontend URL | `https://computecoin.ai` |
| `COOKIE_SECURE` | Enforce Secure cookie attribute | `true` |
| `COOKIE_SAMESITE` | Cookie SameSite attribute | `lax` |
| `EMAIL_PROVIDER` | Transactional email provider | `resend` |
| `RESEND_API_KEY` | Resend API key | `re_...` |
| `EMAIL_FROM` | Sender address | `ComputeCoin <noreply@computecoin.ai>` |
| `SENTRY_DSN` | Sentry project DSN | `https://...@sentry.io/...` |
| `STRIPE_SECRET_KEY` | Stripe live secret key | `sk_live_...` |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret | `whsec_...` |
| `PAYMENT_PROVIDER` | Payment provider engine | `stripe` |
| `SETTLEMENT_ENABLED` | Enable on-chain settlement layer | `true` |
| `SETTLEMENT_CHAIN_ID` | Sepolia testnet chain ID | `11155111` |
| `SETTLEMENT_CONTRACT_ADDRESS` | Deployed ComputeSettlement address | `0x...` |
| `COMPUTE_TOKEN_ADDRESS` | Deployed ComputeCoinToken address | `0x...` |
| `PLATFORM_SIGNER_PRIVATE_KEY` | Dedicated platform signer private key | `0x...` |
| `PLATFORM_TREASURY_ADDRESS` | Protocol fee treasury address | `0x...` |

---

## 3. Deploying Frontend Web Application to Vercel

Vercel provides edge CDN caching, image optimization, and global delivery for the Next.js frontend:

1. Log in to [https://vercel.com](https://vercel.com) and click **"Add New $\rightarrow$ Project"**.
2. Import the `shahtarang20/stocks` repository.
3. In **Project Configuration**:
   - **Framework Preset:** `Next.js`
   - **Root Directory:** `apps/web`
4. In **Environment Variables**:
   - `NEXT_PUBLIC_API_BASE_URL`: Your Railway production API URL (e.g., `https://api.computecoin.ai`).
5. Click **"Deploy"**.
6. Under **Project Settings $\rightarrow$ Domains**, add your custom production domain (e.g., `computecoin.ai`).

---

## 4. Staging Verification & Smoke Testing

Once staging or production is stood up, run the automated verification test suite:

### 4.1 Running the Automated Smoke Test
```bash
./scripts/smoke-test.sh https://api.yourdomain.com
```

**Verifies:**
- `/health` returns status `200 OK`, `db: "up"`, `redis: "up"`, `sentry: "enabled"`.
- Public catalog endpoints `/v1/marketplace/gpus` and `/v1/settlement/contracts`.
- User registration and login flows.
- HttpOnly cookie issuance (`access_token`, `refresh_token`) and double-submit `csrf_token`.
- Authenticated profile `/auth/me` and `/v1/billing/balance`.

### 4.2 Running the Concurrency Load Test
```bash
python scripts/staging-load-test.py https://api.yourdomain.com --concurrency 25 --requests 250
```

**Validates:**
- Sub-100ms p95 response latencies.
- 0% HTTP 5xx error rates under concurrent load.
- Stable connection pool utilization.

---

## 5. Production Go-Live Checklist

Complete every check before launching public user traffic:

- [ ] **Security Boot Guard Verification**:
  - `ENVIRONMENT=production` set on all backend services.
  - Custom `AUTH_SECRET` and `SESSION_SECRET` generated via `openssl rand -hex 32`.
  - Default development keys rejected at boot.
- [ ] **Cookie Security**:
  - `cookie_secure=True` verified in browser inspector (cookies marked `Secure; HttpOnly; SameSite=Lax`).
  - Double-submit CSRF protection active on all state-changing endpoints.
- [ ] **Email Delivery**:
  - Resend domain DNS verification (SPF, DKIM) marked verified.
  - Test registration email delivered to inbox (no dev tokens in API JSON responses).
- [ ] **Database & Backup**:
  - All Alembic migrations applied through `0010_stripe_billing_phase5.py`.
  - Supabase daily automated backups active.
- [ ] **Stripe Webhook**:
  - Production webhook endpoint configured in Stripe Dashboard: `https://api.yourdomain.com/v1/billing/webhooks/stripe`.
  - Events subscribed: `checkout.session.completed`, `charge.refunded`.
  - Signature verification verified with test event.
- [ ] **Monitoring & Alerts**:
  - Sentry receiving events; test exception created and logged.
  - Email/Slack alert channel configured in Sentry.
- [ ] **Smart Contract Audit Gate**:
  - Token and settlement contracts deployed to **Sepolia Testnet** (`SETTLEMENT_CHAIN_ID=11155111`).
  - Mainnet token launch remains strictly blocked until external security audit (₹8L+) and legal clearance are achieved.

---

## 6. Disaster Recovery & Rollback Procedures

### Application Rollback
- **Railway:** In Railway Dashboard, go to **Deployments $\rightarrow$ select previous working commit $\rightarrow$ Redeploy**.
- **Vercel:** In Vercel Dashboard, go to **Deployments $\rightarrow$ select previous working deployment $\rightarrow$ Instant Rollback**.

### Database Migration Rollback
If a schema migration must be rolled back:
```bash
# In Railway API service terminal:
alembic downgrade -1
```

### Database Point-In-Time Restore (PITR)
1. Go to **Supabase Dashboard $\rightarrow$ Database $\rightarrow$ Backups**.
2. Select the desired restore timestamp.
3. Click **"Restore to new project"** or **"Restore in-place"**.
