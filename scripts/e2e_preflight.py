#!/usr/bin/env python3
"""E2E safety preflight: refuse to continue unless the API would talk ONLY to local, throwaway services.

Why this exists: the API (and Alembic, the worker, scripts/seed_dev.py) read ``apps/api/.env`` automatically. On a
developer laptop that file can hold REAL credentials (a hosted database, a hosted Redis, an email key). Running a
migration, a downgrade, the seed script or the worker with those values would change or delete real data or send
real email. This script loads the settings exactly as the API would and fails if any of them point off this machine.

It prints only the kind and host of each target, never a password, key or full URL.

Usage (from the repository root, with the API virtualenv):
    apps/api/.venv/bin/python scripts/e2e_preflight.py
Exit codes: 0 = safe, 1 = unsafe settings found, 2 = settings could not be loaded.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "apps" / "api"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}
SAFE_ENVIRONMENTS = {"test", "testing", "development", "dev", "local"}


def _host(url: str) -> str:
    if not url:
        return ""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return "<unparseable>"


def _scheme(url: str) -> str:
    return url.split(":", 1)[0].lower() if url else ""


def main() -> int:
    os.chdir(API_DIR)  # pydantic reads ".env" relative to the working directory, as the API does
    sys.path.insert(0, str(API_DIR))
    try:
        from app.core.config import settings
    except Exception as exc:  # noqa: BLE001 - report, never traceback with values
        print(f"PREFLIGHT ERROR: could not load settings ({type(exc).__name__}). Fix the environment first.")
        return 2

    problems: list[str] = []
    rows: list[tuple[str, str, str]] = []

    def check(name: str, ok: bool, shown: str, why: str) -> None:
        rows.append(("ok" if ok else "UNSAFE", name, shown))
        if not ok:
            problems.append(f"{name}: {why}")

    db = str(getattr(settings, "DATABASE_URL", "") or "")
    db_scheme = _scheme(db)
    if db_scheme.startswith("sqlite"):
        check("DATABASE_URL", True, "sqlite (file or memory)", "")
    else:
        h = _host(db)
        check("DATABASE_URL", h in LOCAL_HOSTS and bool(db), f"{db_scheme} host={h or '<none>'}",
              "the database is not on this machine; use SQLite or the local compose Postgres")

    redis = str(getattr(settings, "REDIS_URL", "") or "")
    check("REDIS_URL", _host(redis) in LOCAL_HOSTS, f"{_scheme(redis) or '<none>'} host={_host(redis) or '<none>'}",
          "Redis is not on this machine")

    env = str(getattr(settings, "ENVIRONMENT", "") or "").lower()
    check("ENVIRONMENT", env in SAFE_ENVIRONMENTS, env or "<empty>", "ENVIRONMENT must be test, development, dev or local")

    provider = str(getattr(settings, "EMAIL_PROVIDER", "") or "").lower()
    check("EMAIL_PROVIDER", provider == "console", provider or "<empty>", "must be 'console' so no real email is sent")
    check("RESEND_API_KEY", not getattr(settings, "RESEND_API_KEY", ""), "set" if getattr(settings, "RESEND_API_KEY", "") else "empty",
          "an email key is set; clear it")

    check("STRIPE_SECRET_KEY", not getattr(settings, "STRIPE_SECRET_KEY", ""),
          "set" if getattr(settings, "STRIPE_SECRET_KEY", "") else "empty", "a payment key is set; clear it for local runs")
    check("SENTRY_DSN", not getattr(settings, "SENTRY_DSN", ""), "set" if getattr(settings, "SENTRY_DSN", "") else "empty",
          "error reports would leave this machine")

    for name in ("DATA_PLANE_ENDPOINT", "OBJECT_STORAGE_ENDPOINT", "CHAIN_RPC_URL", "STAKE_SYNC_RPC_URL",
                 "PRIVACY_ANCHOR_RPC_URL"):
        value = str(getattr(settings, name, "") or "")
        check(name, _host(value) in LOCAL_HOSTS, f"host={_host(value) or '<none>'}", "points off this machine")

    check("WITHDRAWALS_ENABLED", not getattr(settings, "WITHDRAWALS_ENABLED", False),
          str(getattr(settings, "WITHDRAWALS_ENABLED", False)), "withdrawals must stay off unless a local chain is used")

    width = max(len(r[1]) for r in rows)
    for status, name, shown in rows:
        print(f"[{status:6}] {name:<{width}}  {shown}")
    if problems:
        print("\nPREFLIGHT FAILED: do NOT run migrations, the API, the worker or any seed script.")
        for p in problems:
            print(f"  - {p}")
        print("\nFix: move apps/api/.env aside (rename it to .env.disabled) and export local values; see the checklist, section 3.")
        return 1
    print("\nPREFLIGHT OK: every target is local and throwaway.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
