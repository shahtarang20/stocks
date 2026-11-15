#!/usr/bin/env bash
set -e

echo "========================================================"
echo " 🚀 ComputeCoin Production API Entrypoint"
echo "========================================================"
# Exported so the app can see how many workers it really runs (container rental refuses an in-memory relay when there is more than one).
export WEB_CONCURRENCY="${WEB_CONCURRENCY:-2}"
echo "Environment:  ${ENVIRONMENT:-production}"
echo "Port:         ${PORT:-8000}"
echo "Concurrency:  ${WEB_CONCURRENCY:-2} workers"
echo "========================================================"

# Run database migrations automatically before starting server
echo "Applying database schema migrations..."
alembic upgrade head
echo "✅ Database schema is up to date."

# Start uvicorn server
echo "Starting production uvicorn server..."
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --log-level "${LOG_LEVEL:-info}" \
  --proxy-headers \
  --forwarded-allow-ips "*"
