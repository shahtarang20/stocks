# Browser QA suites

Control-level checks that drive a real Chrome against a running stack, plus two
fuzzers that hit every API endpoint with hostile input.

| File | What it covers |
|---|---|
| `seed.py` | Seeds users in every role, 4 machines (real agent enrollment), jobs in every status, disputes, payouts, a refund |
| `crawl.mjs` | Logs in as each role and records every real control on every page (`inventory.json`) |
| `c1.mjs` `c2.mjs` `c3.mjs` | Customer: projects, API keys, job form (valid + invalid), job list/detail, marketplace filters, quotes, reservations, billing, invoices, usage, disputes, settings |
| `p1.mjs` | Provider: onboarding tokens, hardware edit/deregister, earnings, payouts |
| `a1.mjs` | Admin: every admin page, suspend/reinstate, refunds, payout authorisation, dispute resolution |
| `pub.mjs` | Public pages, link integrity, auth forms, rate limiting, open-redirect guard |
| `mobile.mjs` | No horizontal scroll at 390px on every page |
| `fuzz.py` / `fuzz_get.py` | ~15,000 hostile requests across every endpoint; any 5xx is a bug |
| `gen_checklist.py` | Builds `docs/QA_CHECKLIST.md` from the crawl and the results |

## Running

`stack.sh` builds and runs a complete, private local stack (Postgres, Redis, an
S3-compatible server, an SMTP sink, the API and the web app) as ordinary user
processes on localhost. Nothing is installed system-wide and no other database or
service on the machine is touched. Needs: Postgres 16 binaries, Node, Python 3,
a Chrome, `curl`, `make`/`gcc` (Redis is built from source), the API venv
(`apps/api/.venv`) and the agent venv (`provider-agent/.venv`).

```bash
cd tests/qa
./stack.sh setup            # once: builds Redis, installs moto/aiosmtpd/playwright-core into $QA_HOME
(cd ../../apps/web && npm run build)      # the suites run against the production build
./stack.sh up fresh         # fresh database + migrations, then starts everything
./stack.sh seed             # users in every role, machines, jobs in every status, disputes, payouts
./stack.sh suites           # warms every route, then runs every suite (output in $QA_DIR/<suite>.out)
./stack.sh run c3           # or one suite
python fuzz.py; python fuzz_get.py        # hostile-input fuzzers (QA_API selects the API)
python gen_checklist.py     # rewrites docs/QA_CHECKLIST.md from $QA_DIR
./stack.sh down
```

`QA_HOME` (default `/tmp/computecoin-qa`) holds the databases, logs and results, so it
can be deleted at any time. The seed assumes a **fresh database**, which `up fresh`
provides. The API is started with `LEASE_TTL_SECONDS=3600` and
`ONLINE_WINDOW_SECONDS=86400` so seeded machines stay listed and claimed jobs stay
running.

Run the suites against the **production build** (`WEB_MODE=start`, the default): the
dev server compiles each route on first visit, which makes early checks time out and
look like failures. Use `WEB_MODE=dev ./stack.sh up` only while iterating.
