# ComputeCoin API — Phase 1

FastAPI backend foundation for the ComputeCoin decentralized GPU compute
marketplace. Provides auth (register/verify/login/refresh/logout/password-reset),
RBAC, projects + API keys with account isolation, audit logging, rate limiting
and a health check.

- App object: `app.main:app`
- Python: 3.12
- DB: SQLAlchemy 2.0 async (asyncpg in prod, aiosqlite for tests)
- Migrations: Alembic (`apps/api/alembic`)

## Layout

```
app/
  core/        config, security (Argon2id, JWT), rate limiting
  db/          declarative Base, async engine/session
  models/      SQLAlchemy models (single Base)
  schemas/     Pydantic request/response models
  api/routers/ auth, projects, admin, health
  services/    auth (session rotation), audit logging
  deps.py      get_current_user, require_roles(...)
  main.py      FastAPI app factory
alembic/       env.py + versions/0001_initial.py
tests/         pytest + httpx ASGITransport (offline SQLite)
```

## Setup

```bash
cd apps/api
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Configuration

All settings have safe defaults so the app boots with no env. Override via
environment variables (or a `.env` file):

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./computecoin.db` | async DB URL (use `postgresql+asyncpg://...` in prod) |
| `REDIS_URL` | `redis://localhost:6379/0` | rate-limit backend (falls back to in-process) |
| `AUTH_SECRET` | dev value | HS256 JWT signing key |
| `SESSION_SECRET` | dev value | session secret |
| `ACCESS_TOKEN_TTL_SECONDS` | `900` | access JWT lifetime |
| `REFRESH_TOKEN_TTL_SECONDS` | `1209600` | refresh token lifetime |
| `ENVIRONMENT` | `development` | `production`/`development`/`test` |
| `LOG_LEVEL` | `INFO` | logging level |
| `FRONTEND_URL` | `http://localhost:3000` | CORS origin |

In `development`/`test`, the email-verification and password-reset tokens are
returned in the API responses (no real email is sent). In `production` they are
not exposed.

## Migrate

```bash
cd apps/api
.venv/bin/alembic upgrade head
# reversibility
.venv/bin/alembic downgrade base
.venv/bin/alembic upgrade head
```

The migration reads `DATABASE_URL`. For Postgres:

```bash
DATABASE_URL=postgresql+asyncpg://user:pass@host/db .venv/bin/alembic upgrade head
```

## Run

```bash
cd apps/api
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Docs at `http://localhost:8000/docs`, health at `/health`.

## Test

```bash
cd apps/api
.venv/bin/pytest -q
```

Tests run fully offline against in-memory SQLite — no live Postgres or Redis
required (the rate limiter falls back to an in-process limiter).

## Docker

```bash
cd apps/api
docker build -t computecoin-api .
docker run -p 8000:8000 -e DATABASE_URL=postgresql+asyncpg://... computecoin-api
```

## Security notes

- Passwords hashed with Argon2id (`argon2-cffi`); never stored or logged in plaintext.
- Opaque refresh/verification/reset tokens and API keys are stored only as SHA-256 hashes.
- Refresh tokens rotate on every use; replay of a revoked token revokes the whole chain.
- Login/register/password-reset are rate limited (429 on exceed).
- Audit log rows are written for login success/failure, password reset, and API key create/revoke.
```
