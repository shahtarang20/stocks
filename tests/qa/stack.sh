#!/bin/bash
# Local QA stack: private Postgres, Redis, S3 (moto), SMTP sink, the API and the web app.
# Everything runs as plain user processes on localhost, nothing is installed system-wide,
# and nothing touches any other database or service on the machine.
#
#   ./stack.sh setup        one-off: build Redis, install moto/aiosmtpd/playwright into $QA_HOME
#   ./stack.sh up [fresh]   start everything (fresh = recreate the database + migrate + flush Redis)
#   ./stack.sh seed         seed users, machines, jobs, disputes, payouts (needs a fresh DB)
#   ./stack.sh run <suite>  run a browser suite (c1 c2 c3 p1 a1 pub mobile crawl)
#   ./stack.sh suites       warm routes, then run every browser suite (output in $QA_DIR/<suite>.out)
#   ./stack.sh down         stop everything
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
QA_HOME="${QA_HOME:-${TMPDIR:-/tmp}/computecoin-qa}"; INFRA="$QA_HOME/infra"
PGBIN="${PGBIN:-/usr/lib/postgresql/16/bin}"
PGPORT=55432; REDISPORT=56379; S3PORT=59000; SMTPPORT=51025; APIPORT=8010; WEBPORT=3000
export QA_DIR="$QA_HOME/out" QA_PG="postgresql://cc@localhost:$PGPORT/computecoin"
mkdir -p "$QA_HOME" "$QA_DIR"

listening() { ss -ltn | grep -q ":$1 "; }
kill_port() { ss -ltnp | grep ":$1 " | sed 's/.*pid=\([0-9]*\).*/\1/' | xargs -r kill; }

setup() {
  mkdir -p "$INFRA"
  if [ ! -x "$INFRA/redis-src/src/redis-server" ]; then
    mkdir -p "$INFRA/redis-src"
    curl -fsSL -o "$INFRA/redis.tgz" https://download.redis.io/redis-stable.tar.gz
    tar xzf "$INFRA/redis.tgz" -C "$INFRA/redis-src" --strip-components=1
    # The optional modules fail to build offline-free; redis-server itself builds fine.
    (cd "$INFRA/redis-src" && make -j4 >"$INFRA/redis-build.log" 2>&1; true)
  fi
  python3 -m pip install -q --break-system-packages --target "$INFRA/motolib" "moto[server]"
  python3 -m pip install -q --break-system-packages --target "$INFRA/smtplib_dir" aiosmtpd
  (cd "$HERE" && npm i --no-audit --no-fund >/dev/null 2>&1)
  [ -d "$QA_HOME/pg" ] || "$PGBIN/initdb" -D "$QA_HOME/pg" -U cc --auth=trust >/dev/null
  echo "setup done in $QA_HOME"
}

up() {
  "$PGBIN/pg_ctl" -D "$QA_HOME/pg" status >/dev/null 2>&1 || "$PGBIN/pg_ctl" -D "$QA_HOME/pg" -o "-p $PGPORT -k $QA_HOME" -l "$QA_HOME/pg.log" start >/dev/null
  sleep 2
  if [ "${1:-}" = "fresh" ]; then
    kill_port $APIPORT; sleep 1
    psql "postgresql://cc@localhost:$PGPORT/postgres" -qc "drop database if exists computecoin" -c "create database computecoin" 2>&1 | grep -v NOTICE
    psql "postgresql://cc@localhost:$PGPORT/postgres" -qc "drop database if exists pgtest" -c "create database pgtest" 2>&1 | grep -v NOTICE
    (cd "$REPO/apps/api" && DATABASE_URL="postgresql+asyncpg://cc@localhost:$PGPORT/computecoin" .venv/bin/alembic upgrade head 2>&1 | tail -1 | cut -c1-70)
    [ -x "$INFRA/redis-src/src/redis-cli" ] && listening $REDISPORT && "$INFRA/redis-src/src/redis-cli" -p $REDISPORT flushall >/dev/null
    : > "$QA_HOME/mail.jsonl"
  fi
  listening $REDISPORT || (setsid "$INFRA/redis-src/src/redis-server" --port $REDISPORT --bind 127.0.0.1 --dir "$INFRA" --save "" >"$QA_HOME/redis.log" 2>&1 </dev/null &)
  listening $S3PORT || (PYTHONPATH="$INFRA/motolib" setsid python3 -m moto.server -H 127.0.0.1 -p $S3PORT >"$QA_HOME/moto.log" 2>&1 </dev/null &)
  listening $SMTPPORT || (PYTHONPATH="$INFRA/smtplib_dir" setsid python3 "$HERE/smtp_sink.py" "$QA_HOME/mail.jsonl" $SMTPPORT >"$QA_HOME/sink.log" 2>&1 </dev/null &)
  sleep 3
  if ! listening $APIPORT; then
    (cd "$REPO/apps/api"
     export DATABASE_URL="postgresql+asyncpg://cc@localhost:$PGPORT/computecoin" ENVIRONMENT=development FRONTEND_URL="http://localhost:$WEBPORT" \
       REDIS_URL="redis://127.0.0.1:$REDISPORT/0" RATE_LIMIT_REQUIRE_REDIS=true LEASE_TTL_SECONDS=3600 ONLINE_WINDOW_SECONDS=86400 \
       OBJECT_STORAGE_ENDPOINT="http://127.0.0.1:$S3PORT" OBJECT_STORAGE_BUCKET=computecoin-artifacts OBJECT_STORAGE_ACCESS_KEY=test OBJECT_STORAGE_SECRET_KEY=test \
       EMAIL_PROVIDER=smtp SMTP_HOST=127.0.0.1 SMTP_PORT=$SMTPPORT SMTP_TLS=false
     setsid .venv/bin/python -m uvicorn app.main:app --port $APIPORT >"$QA_HOME/api.log" 2>&1 </dev/null &)
  fi
  if ! listening $WEBPORT; then
    # The web build reads NEXT_PUBLIC_API_BASE_URL (apps/web/.env.local may pin another port).
    (cd "$REPO/apps/web" && NEXT_PUBLIC_API_BASE_URL="http://localhost:$APIPORT" setsid npx next "${WEB_MODE:-start}" -p $WEBPORT >"$QA_HOME/web.log" 2>&1 </dev/null &)
  fi
  sleep 10
  curl -s "localhost:$APIPORT/health" | head -c 110; echo; curl -s -o /dev/null -w "web %{http_code}\n" "localhost:$WEBPORT/login"
}

seed() { (cd "$REPO" && QA_API="http://localhost:$APIPORT" QA_OUT="$QA_DIR/seed.json" provider-agent/.venv/bin/python "$HERE/seed.py"); }
run()  { (cd "$HERE" && QA_WEB="http://localhost:$WEBPORT" QA_API="http://localhost:$APIPORT" node "$1.mjs"); }
suites() { for s in crawl c1 c2 c3 p1 a1 pub mobile; do run $s > "$QA_DIR/$s.out" 2>&1; done; echo done > "$QA_DIR/suites.done"; }
down() {
  for p in $WEBPORT $APIPORT $REDISPORT $S3PORT $SMTPPORT; do kill_port $p; done
  "$PGBIN/pg_ctl" -D "$QA_HOME/pg" stop -m fast 2>&1 | tail -1
}

cmd="${1:-}"; shift || true
case "$cmd" in setup) setup;; up) up "$@";; seed) seed;; suites) rm -f "$QA_DIR/suites.done"; suites;; run) run "$@";; down) down;; *) sed -n 2,11p "$0";; esac
