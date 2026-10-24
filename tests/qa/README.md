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

Needs the stack up (Postgres, API, web app; Redis/S3/SMTP optional) and a Chrome.
The seed assumes a **fresh database**, an API with `LEASE_TTL_SECONDS=3600` and
`ONLINE_WINDOW_SECONDS=86400` (so seeded machines stay listed and claimed jobs stay
running), and `EMAIL_PROVIDER` set so the dev API returns verification tokens.

```bash
npm i playwright-core                 # once, in this folder
export QA_WEB=http://localhost:3000 QA_API=http://localhost:8010 \
       QA_PG=postgresql://user@localhost:5432/computecoin \
       QA_CHROME=/usr/bin/google-chrome
python seed.py                        # writes out/seed.json
node crawl.mjs                        # warms every route + writes out/inventory.json
for s in c1 c2 c3 p1 a1 pub mobile; do node $s.mjs; done
python fuzz.py; python fuzz_get.py    # needs the provider-agent venv or any python with httpx
python gen_checklist.py               # rewrites docs/QA_CHECKLIST.md
```

Run `seed.py` with the provider-agent virtualenv (it imports `computecoin_agent`).
The web dev server compiles routes on first visit, so run `crawl.mjs` first to warm
them; otherwise early checks can time out and look like failures.
`out/` is ignored by git.
