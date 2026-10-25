# ComputeCoin QA checklist and results

Generated 2026-10-25 from a live crawl of the running web app plus the browser test suites in `tests/qa/`.

**372 of 372 control-level checks pass.** Each check drives a real browser (or the API directly for permission and race checks) against a stack with seeded customers, providers, machines, jobs in every status, disputes, payouts and refunds.

## 1. Control inventory (what exists on each page)

Counts are visible interactive elements found by crawling the live DOM as the right role. Dialog-only fields appear in section 2.

| Role | Page | Buttons | Fields | Links | Controls |
|---|---|---|---|---|---|
| cust1 | `/dashboard` | 0 | 0 | 14 | — |
| cust1 | `/dashboard/projects` | 4 | 0 | 12 | Refresh, New project, Delete project |
| cust1 | `/dashboard/api-keys` | 3 | 1 | 12 | Refresh, Create key, Revoke · proj-select |
| cust1 | `/dashboard/jobs` | 1 | 0 | 18 | Refresh |
| cust1 | `/dashboard/jobs/new` | 1 | 11 | 12 | Submit job · workload_type, isolation_tier, model, prompt, max_tokens, machine_id, gpu_count, |
| cust1 | `/dashboard/jobs/83a1a044-a1c8-4a3e-82b7-fbe5e0` | 2 | 0 | 12 | Refresh, Download |
| cust1 | `/dashboard/reservations` | 6 | 0 | 12 | Refresh, Release |
| cust1 | `/dashboard/billing` | 2 | 0 | 12 | Buy credits, Refresh |
| cust1 | `/dashboard/invoices` | 1 | 0 | 18 | Refresh |
| cust1 | `/dashboard/invoices/148029c0-950e-4467-b6e8-4a` | 1 | 0 | 13 | Download |
| cust1 | `/dashboard/usage` | 1 | 0 | 15 | Refresh |
| cust1 | `/dashboard/disputes` | 1 | 0 | 14 | Refresh |
| cust1 | `/dashboard/settings` | 4 | 5 | 12 | Save changes, Change password, Log out, Delete account · s-email, s-name, s-cur-pw, s-new-pw, s-new-pw2 |
| cust1 | `/marketplace` | 5 | 6 | 13 | Refresh, Reset filters, Get quote · f-model, f-vram, f-price, f-region, f-workload, f-verif |
| cust1 | `/marketplace/gpus` | 5 | 6 | 13 | Refresh, Reset filters, Get quote · f-model, f-vram, f-price, f-region, f-workload, f-verif |
| prov1 | `/provider/onboarding` | 7 | 1 | 7 | Generate enrollment token, Refresh, Revoke, Copy to clipboard · token-label |
| prov1 | `/provider/dashboard` | 1 | 0 | 7 | Refresh |
| prov1 | `/provider/hardware` | 4 | 0 | 7 | Refresh, Machine actions |
| prov1 | `/provider/earnings` | 1 | 0 | 7 | Refresh |
| prov1 | `/provider/payouts` | 2 | 0 | 7 | Refresh, Request payout |
| admin | `/admin/overview` | 1 | 0 | 22 | Refresh |
| admin | `/admin/users` | 8 | 0 | 14 | Refresh, Reinstate, Suspend |
| admin | `/admin/providers` | 1 | 0 | 14 | Refresh |
| admin | `/admin/jobs` | 1 | 0 | 22 | Refresh |
| admin | `/admin/billing` | 4 | 8 | 14 | Refresh, Post refund, Authorize payout, Resolve dispute · refund-job, refund-invoice, refund-amount, refund-reason, authorize-payout, reso |
| admin | `/admin/disputes` | 1 | 0 | 14 | Refresh |
| admin | `/admin/settlement` | 2 | 3 | 14 | Refresh State, Generate Platform Voucher · Job ID (e.g. 550e8, 400000000000000000 |
| admin | `/admin/security` | 0 | 0 | 14 | — |
| admin | `/admin/telemetry` | 1 | 1 | 14 | Refresh · select-one |
| admin | `/admin/vendors` | 1 | 0 | 21 | Check now |
| admin | `/admin/audit-logs` | 1 | 0 | 14 | Refresh |
| admin | `/admin/configuration` | 0 | 0 | 14 | — |
| public | `/` | 0 | 0 | 15 | — |
| public | `/pricing` | 0 | 0 | 15 | — |
| public | `/developers` | 0 | 0 | 15 | — |
| public | `/providers` | 0 | 0 | 16 | — |
| public | `/security` | 0 | 0 | 15 | — |
| public | `/status` | 1 | 0 | 15 | Refresh |
| public | `/whitepaper` | 0 | 0 | 15 | — |
| public | `/about` | 0 | 0 | 15 | — |
| public | `/legal/terms` | 0 | 0 | 15 | — |
| public | `/legal/privacy` | 0 | 0 | 15 | — |
| public | `/login` | 1 | 2 | 3 | Log in · email, password |
| public | `/register` | 1 | 2 | 2 | Create account · email, password |
| public | `/forgot-password` | 1 | 1 | 2 | Send reset link · email |

Totals across 45 pages: 77 buttons, 47 form fields, 586 link slots.

## 2. Test results by area

| Area | Checks | Pass | Fail |
|---|---|---|---|
| Customer: dashboard, navigation, projects, API keys | 37 | 37 | 0 |
| Customer: job form, job list, job detail | 38 | 38 | 0 |
| Customer: marketplace, quotes, reservations, billing, invoices, usage, disputes, settings | 59 | 59 | 0 |
| Provider: onboarding, dashboard, hardware, earnings, payouts | 55 | 55 | 0 |
| Admin: every admin page | 71 | 71 | 0 |
| Public pages and authentication | 69 | 69 | 0 |
| Mobile layout (390px) on every page | 43 | 43 | 0 |

### Failing checks

None.

### Every check


**Customer: dashboard, navigation, projects, API keys**

- [x] dashboard: loads and greets the user
- [x] dashboard: shows real figures, no demo data left
- [x] dashboard: tiles match the API (credits, job count, total spend)
- [x] dashboard: charts render for a user with jobs
- [x] dashboard: a brand-new user sees a clear 'No jobs yet' state with a way forward
- [x] dashboard: 'Browse GPUs' link works
- [x] sidebar: "Dashboard" navigates to /dashboard
- [x] sidebar: "Projects" navigates to /dashboard/projects
- [x] sidebar: "Jobs" navigates to /dashboard/jobs
- [x] sidebar: "Reservations" navigates to /dashboard/reservations
- [x] sidebar: "Usage" navigates to /dashboard/usage
- [x] sidebar: "Billing" navigates to /dashboard/billing
- [x] sidebar: "Invoices" navigates to /dashboard/invoices
- [x] sidebar: "Disputes" navigates to /dashboard/disputes
- [x] sidebar: "API keys" navigates to /dashboard/api-keys
- [x] sidebar: "Settings" navigates to /dashboard/settings
- [x] theme toggle changes theme
- [x] projects: seeded projects listed
- [x] projects: empty name keeps Create disabled
- [x] projects: Cancel closes the dialog without creating
- [x] projects: whitespace-only name keeps Create disabled
- [x] projects: very long name (300 chars) is handled, no 500
- [x] projects: valid create with description appears in list
- [x] projects: Refresh button reloads list
- [x] projects: Delete -> Cancel keeps project
- [x] projects: Delete -> confirm removes project
- [x] projects: another user cannot delete my project (404)
- [x] api-keys: project dropdown lists my projects
- [x] api-keys: switching project changes the key list
- [x] api-keys: empty key name keeps Generate disabled
- [x] api-keys: create shows secret once, copy button works
- [x] api-keys: raw secret never shown again after closing
- [x] api-keys: new key authenticates the API
- [x] api-keys: 'Last used' updates after use
- [x] api-keys: Revoke -> Cancel keeps key live
- [x] api-keys: Revoke -> confirm kills key and marks it Revoked
- [x] api-keys: another user cannot list/revoke my keys

**Customer: job form, job list, job detail**

- [x] job form: all 11 fields present with sane defaults
- [x] job form: workload dropdown offers text-generation and batch-inference
- [x] job form: isolation dropdown offers 4 tiers
- [x] job form: empty prompt is refused (no job created)
- [x] job form: empty prompt shows a visible error
- [x] job form: negative gpu count refused
- [x] job form: zero estimated minutes refused
- [x] job form: non-numeric max cost refused with message
- [x] job form: nonexistent machine id gives a readable error
- [x] job form: malformed machine id gives a readable error
- [x] job form: unknown model gives readable error (model_not_supported)
- [x] job form: max cost below the estimate is refused with a plain-English message
- [x] job form: unsupported isolation tier is refused in plain English (no raw code)
- [x] job form: offline machine -> job is created and does not run (queued/failed, never running)
- [x] job form: batch-inference workload can actually be submitted from the UI
- [x] job form: valid submit redirects to the new job's page
- [x] job form: double-click submit creates exactly one job
- [x] job form: credits are held on submit (reserved balance rises)
- [x] job form: absurd budget never leaves a stuck hold
- [x] jobs list: shows seeded jobs with all statuses
- [x] jobs list: submissions the API refused are not listed (no phantom failed jobs)
- [x] jobs list: completed/disputed jobs show their real cost
- [x] jobs list: cancelled/failed/reserved jobs read 'not charged'
- [x] jobs list: 'New job' button goes to the form
- [x] jobs list: clicking a job opens its detail page
- [x] jobs list: Refresh works
- [x] jobs list: another user's jobs are not listed
- [x] job detail: completed job shows FSM timeline, logs, cost
- [x] job detail: simulated mode is disclosed honestly
- [x] job detail: artifact Download opens a signed API link that serves the file as an attachment
- [x] job detail: Refresh button works
- [x] job detail: failed job shows its error, not a success
- [x] job detail: cancelled job is terminal, no Cancel/Dispute button
- [x] job detail: reserved job offers Cancel and cancelling releases the hold
- [x] job detail: already-disputed job has no second Dispute button
- [x] job detail: dispute needs a real reason (button disabled for <3 chars)
- [x] job detail: another user's job shows not-found, not data
- [x] job detail: garbage job id is handled gracefully

**Customer: marketplace, quotes, reservations, billing, invoices, usage, disputes, settings**

- [x] gpus: 3 live machines listed, offline machine hidden
- [x] gpus: model filter narrows results
- [x] gpus: Reset filters restores all
- [x] gpus: min VRAM filter excludes small GPUs
- [x] gpus: max price filter
- [x] gpus: region filter
- [x] gpus: workload filter (image-generation matches only the H100)
- [x] gpus: no-match filter shows an empty state, not an error
- [x] gpus: verification dropdown (Detected) keeps results
- [x] gpus: negative VRAM / price are handled without crashing
- [x] gpus: Refresh works
- [x] quote: dialog shows machine, workload, minutes, GPUs, spend ceiling
- [x] quote: Cancel closes without creating a quote
- [x] quote: valid request returns a price breakdown
- [x] quote: price math is right (60 min at 1.50/hr = 1.50 before fee)
- [x] quote: zero minutes refused
- [x] quote: GPU count above machine's GPUs refused with message
- [x] reservations: list renders with status and expiry
- [x] reservations: Release is disabled on non-active rows
- [x] reservations: Release -> Cancel keeps it active
- [x] reservations: Release -> confirm frees the slot
- [x] reservations: Refresh works
- [x] billing: balance, reserved and total are shown and add up
- [x] billing: ledger lists purchases, holds and releases
- [x] billing: buy 25.00 raises the balance by exactly 25.00
- [x] billing: zero is refused
- [x] billing: negative is refused
- [x] billing: text is refused
- [x] billing: 3 decimal places refused (no silent rounding)
- [x] billing: absurdly large amount is capped/refused
- [x] billing: Cancel in the dialog buys nothing
- [x] billing: Refresh on the ledger works
- [x] billing: a brand-new user sees a clean zero state, no errors
- [x] invoices: lists invoices with job, estimated, actual, status
- [x] invoices: clicking an invoice opens its detail
- [x] invoice detail: shows estimated vs actual separately
- [x] invoice detail: Download saves the invoice as a JSON file
- [x] invoice detail: another user's invoice shows not-found
- [x] invoice detail: garbage id handled gracefully
- [x] usage: totals match the API (no more 0.00 with metered jobs)
- [x] usage: per-job duration is shown (60s), not a dash
- [x] usage: Refresh works
- [x] disputes: lists open and resolved disputes
- [x] disputes: dispute row links to its job
- [x] dispute dialog: <3 chars keeps submit disabled, valid reason opens a dispute
- [x] dispute: a second dispute on the same job is blocked with a clear message
- [x] dispute: cannot dispute someone else's job
- [x] settings: email is shown
- [x] settings: no stale 'tokens stored in browser' claim
- [x] settings: display name saves, persists and appears in the greeting
- [x] settings: a 121-character name is cut off by the field, never a 500
- [x] settings: password change validates, then signs out and the new password works
- [x] settings: Delete account -> Cancel keeps the account
- [x] settings: deleting needs the password and, when credits remain, an explicit forfeit tick
- [x] settings: wrong password on delete shows a clear error and keeps the account
- [x] settings: a user with a job in progress cannot delete, with a plain explanation
- [x] settings: confirmed deletion signs out, anonymises the account and frees the email
- [x] settings: a deleted person can sign up again with the same email
- [x] settings: Log out ends the session and protects the dashboard

**Provider: onboarding, dashboard, hardware, earnings, payouts**

- [x] onboarding: registered provider sees step 1 complete and can mint tokens
- [x] onboarding: existing tokens are listed with status
- [x] onboarding: install commands show the server URL
- [x] onboarding: generate token with a label shows the secret once
- [x] onboarding: token appears in the install command
- [x] onboarding: Copy to clipboard control present on the token
- [x] onboarding: after reload the raw token is no longer shown
- [x] onboarding: new token is listed as active with its label
- [x] onboarding: token works once for a real agent enrollment, then shows 'used'
- [x] onboarding: a used token cannot enroll a second machine
- [x] onboarding: Revoke -> Cancel keeps the token active
- [x] onboarding: Revoke -> confirm marks it revoked
- [x] onboarding: a revoked token is refused by the enroll endpoint
- [x] onboarding: Refresh works
- [x] onboarding: blank label is allowed
- [x] onboarding: 200-char label handled without a 500
- [x] onboarding: a normal customer can become a provider (Become a provider)
- [x] dashboard: machine tiles reflect the fleet
- [x] dashboard: earnings tiles match the API
- [x] dashboard: shows recent earnings from the ledger, no demo chart
- [x] dashboard: Refresh works
- [x] hardware: lists the provider's machines with GPU, status and price
- [x] hardware: online vs offline status is shown correctly
- [x] hardware: only this provider's machines are shown
- [x] hardware: Edit dialog opens prefilled with current values
- [x] hardware: negative price is refused
- [x] hardware: non-numeric price is refused
- [x] hardware: max concurrent 0 is refused
- [x] hardware: max concurrent 1000 is refused
- [x] hardware: 65-char region is refused
- [x] hardware: Cancel discards edits
- [x] hardware: valid edit saves price, region, concurrency and shows in the list
- [x] hardware: going online makes the machine appear in the marketplace
- [x] hardware: another provider cannot edit my machine (404)
- [x] hardware: a customer cannot edit provider machines (403)
- [x] hardware: Refresh works
- [x] hardware: Deregister -> Cancel keeps the machine
- [x] hardware: Deregister -> confirm removes it and revokes its credential
- [x] earnings: available and lifetime match the ledger API
- [x] earnings: simulated credits are labelled as such (no fiat claim)
- [x] earnings: Refresh works
- [x] payouts: requested and authorized payouts are listed
- [x] payouts: valid request within earnings succeeds
- [x] payouts: over-available amount is refused with a clear message
- [x] payouts: zero refused
- [x] payouts: negative refused
- [x] payouts: text refused
- [x] payouts: empty refused
- [x] payouts: Cancel requests nothing
- [x] payouts: balance drops by the requested amount (no double-spend)
- [x] payouts: Refresh works
- [x] payouts: another provider cannot see my payouts
- [x] gates: a plain customer is refused provider data by the API
- [x] gates: a provider is refused admin data by the API
- [x] gates: provider console pages never show another provider's data

**Admin: every admin page**

- [x] overview: tiles match /v1/admin/stats
- [x] overview: recent jobs table is populated
- [x] overview: "Manage users" link goes to /admin/users
- [x] overview: "Manage providers" link goes to /admin/providers
- [x] overview: "All jobs" link goes to /admin/jobs
- [x] overview: "Open disputes" link goes to /admin/disputes
- [x] overview: "Audit logs" link goes to /admin/audit-logs
- [x] overview: "Configuration" link goes to /admin/configuration
- [x] overview: Refresh works
- [x] users: every registered user is listed with roles, status, verified
- [x] users: own row is marked and Suspend is disabled for yourself
- [x] users: Suspend -> Cancel leaves the user active
- [x] users: Suspend -> confirm suspends and writes an audit entry
- [x] users: row flips to Reinstate after suspending
- [x] users: a suspended user cannot sign in through the UI
- [x] users: a suspended user's existing API token stops working
- [x] users: Reinstate restores sign-in
- [x] users: API refuses self-suspension
- [x] users: unknown user id is a clean 404
- [x] users: non-admin cannot list or suspend (403)
- [x] users: a deleted account shows as deleted and cannot be reinstated
- [x] users: Refresh works
- [x] providers: every live machine is listed with GPU, region, price
- [x] providers: Refresh works
- [x] jobs: shows jobs from ALL customers with status, mode, isolation and error code
- [x] jobs: row count matches the platform (capped at the page limit)
- [x] jobs: Refresh works
- [x] jobs: a customer is refused the admin job list (403)
- [x] disputes: lists every customer's disputes (open and resolved)
- [x] disputes: customer endpoint cannot be used to list others' disputes
- [x] disputes: Refresh works
- [x] billing: reconciliation shows balanced ledger (debits == credits)
- [x] billing: account table lists customer, platform and provider accounts
- [x] billing/refund: no job or invoice id is refused with a message
- [x] billing/refund: malformed amount is refused
- [x] billing/refund: a 3-decimal amount is refused (no silent rounding)
- [x] billing/refund: reason under 3 chars is refused
- [x] billing/refund: unknown job id gives a readable error, not a crash
- [x] billing/refund: refund larger than the charge is refused (cumulative cap)
- [x] billing/refund: valid refund posts and the ledger stays balanced
- [x] billing/payout: authorizing an unknown payout id fails cleanly
- [x] billing/payout: authorize button stays disabled with an empty id
- [x] billing/payout: authorizing a requested payout settles it
- [x] billing/payout: authorizing the same payout twice is refused
- [x] billing/dispute: resolving an unknown dispute fails cleanly
- [x] billing/dispute: submitting without a resolution shows a validation error and changes nothing
- [x] billing/dispute: resolving an open dispute closes it and records the resolution
- [x] billing/dispute: resolving an already-resolved dispute is refused
- [x] billing: a customer is refused every admin money endpoint (403)
- [x] settlement: loads with the chain disconnected, shows zero escrows, no crash
- [x] settlement: no longer claims the contracts are 'Audited'
- [x] settlement: voucher form with blank fields does not crash
- [x] settlement: Refresh State works
- [x] security: posture page loads with counts and a self-assessment disclaimer
- [x] configuration: read-only page loads and is labelled as defaults
- [x] telemetry: node picker lists every machine (admin no longer 403s)
- [x] telemetry: stat tiles and alert panel render without errors
- [x] telemetry: selecting a node shows its explorer / empty state
- [x] telemetry: Refresh works
- [x] telemetry: a provider/customer cannot read another machine's telemetry (403/404)
- [x] vendors: nine service cards with plan, cost and renewal fields
- [x] vendors: unmeasurable usage is stated honestly, not faked
- [x] vendors: 'Check now' runs without error
- [x] vendors: non-admin refused (403)
- [x] audit logs: records admin actions with actor, action, IP and outcome
- [x] audit logs: newest entries first
- [x] audit logs: Refresh works
- [x] audit logs: a customer cannot read them (403)
- [x] gates: a customer opening /admin/users sees no user data
- [x] gates: a provider opening /admin/users sees no user data
- [x] gates: an anonymous visitor to /admin/overview is sent to login

**Public pages and authentication**

- [x] /: renders with one H1 and a title
- [x] /: no broken images, no dead '#' links
- [x] /: external links open safely (rel noopener)
- [x] /pricing: renders with one H1 and a title
- [x] /pricing: no broken images, no dead '#' links
- [x] /pricing: external links open safely (rel noopener)
- [x] /developers: renders with one H1 and a title
- [x] /developers: no broken images, no dead '#' links
- [x] /developers: external links open safely (rel noopener)
- [x] /providers: renders with one H1 and a title
- [x] /providers: no broken images, no dead '#' links
- [x] /providers: external links open safely (rel noopener)
- [x] /security: renders with one H1 and a title
- [x] /security: no broken images, no dead '#' links
- [x] /security: external links open safely (rel noopener)
- [x] /status: renders with one H1 and a title
- [x] /status: no broken images, no dead '#' links
- [x] /status: external links open safely (rel noopener)
- [x] /whitepaper: renders with one H1 and a title
- [x] /whitepaper: no broken images, no dead '#' links
- [x] /whitepaper: external links open safely (rel noopener)
- [x] /about: renders with one H1 and a title
- [x] /about: no broken images, no dead '#' links
- [x] /about: external links open safely (rel noopener)
- [x] /legal/terms: renders with one H1 and a title
- [x] /legal/terms: no broken images, no dead '#' links
- [x] /legal/terms: external links open safely (rel noopener)
- [x] /legal/privacy: renders with one H1 and a title
- [x] /legal/privacy: no broken images, no dead '#' links
- [x] /legal/privacy: external links open safely (rel noopener)
- [x] /marketplace: renders with one H1 and a title
- [x] /marketplace: no broken images, no dead '#' links
- [x] /marketplace: external links open safely (rel noopener)
- [x] every internal link on public pages resolves (16 unique)
- [x] unknown route shows the 404 page with a way home
- [x] header: logo goes home, theme toggle present, Log in / Sign up reachable
- [x] header: Log in link navigates to /login
- [x] home: primary call-to-action leads somewhere real
- [x] status page: states what is demo vs live (no fake uptime)
- [x] pricing: nothing claims real fiat prices/guaranteed returns
- [x] whitepaper/token pages: no price, market cap or guaranteed-appreciation claims
- [x] legal pages exist with real content (not lorem ipsum)
- [x] marketplace: /marketplace forwards to the live listing (no demo rows)
- [x] marketplace: an anonymous visitor can browse live machines
- [x] marketplace: asking for a quote while signed out prompts sign-in instead of failing silently
- [x] status page: shows live API, database and cache checks (no placeholder)
- [x] no JavaScript errors on any public page
- [x] login: empty form shows field errors and sends nothing
- [x] login: malformed email shows a validation error
- [x] login: wrong password gives a generic error
- [x] login: unknown email gives the SAME message (no account enumeration)
- [x] login: brute force is rate-limited with a readable message
- [x] login: ?email= prefills the field
- [x] login: a ?password= in the URL is never used and is stripped
- [x] login: successful sign-in honours ?next= (returns to the requested page)
- [x] login: ?next= pointing off-site is ignored (no open redirect)
- [x] login: 'Sign up' and 'Forgot password?' links work
- [x] register: short password is refused client-side
- [x] register: invalid email refused
- [x] register: already-registered email gets a generic conflict (no enumeration)
- [x] register: valid details create an account and land on the dashboard
- [x] register: email is stored lower-cased and trimmed (no duplicate by case)
- [x] register: password is never echoed or stored in plain text
- [x] forgot-password: unknown email gets the same neutral confirmation
- [x] forgot-password: malformed email is refused
- [x] reset-password: bad token gives a readable error
- [x] verify-email: bad token gives a readable error
- [x] security headers: CSP, frame, nosniff, referrer on web responses
- [x] API security headers: nosniff on artifact downloads and no server banner leakage

**Mobile layout (390px) on every page**

- [x] public /: no horizontal page scroll at 390px
- [x] public /pricing: no horizontal page scroll at 390px
- [x] public /developers: no horizontal page scroll at 390px
- [x] public /providers: no horizontal page scroll at 390px
- [x] public /security: no horizontal page scroll at 390px
- [x] public /status: no horizontal page scroll at 390px
- [x] public /whitepaper: no horizontal page scroll at 390px
- [x] public /about: no horizontal page scroll at 390px
- [x] public /legal/terms: no horizontal page scroll at 390px
- [x] public /marketplace: no horizontal page scroll at 390px
- [x] public /login: no horizontal page scroll at 390px
- [x] public /register: no horizontal page scroll at 390px
- [x] public /forgot-password: no horizontal page scroll at 390px
- [x] cust1 /dashboard: no horizontal page scroll at 390px
- [x] cust1 /dashboard/projects: no horizontal page scroll at 390px
- [x] cust1 /dashboard/api-keys: no horizontal page scroll at 390px
- [x] cust1 /dashboard/jobs: no horizontal page scroll at 390px
- [x] cust1 /dashboard/jobs/new: no horizontal page scroll at 390px
- [x] cust1 /dashboard/jobs/83a1a044-a1c8-4a3e-82b7-…: no horizontal page scroll at 390px
- [x] cust1 /dashboard/reservations: no horizontal page scroll at 390px
- [x] cust1 /dashboard/billing: no horizontal page scroll at 390px
- [x] cust1 /dashboard/invoices: no horizontal page scroll at 390px
- [x] cust1 /dashboard/usage: no horizontal page scroll at 390px
- [x] cust1 /dashboard/disputes: no horizontal page scroll at 390px
- [x] cust1 /dashboard/settings: no horizontal page scroll at 390px
- [x] cust1 /marketplace/gpus: no horizontal page scroll at 390px
- [x] prov1 /provider/onboarding: no horizontal page scroll at 390px
- [x] prov1 /provider/dashboard: no horizontal page scroll at 390px
- [x] prov1 /provider/hardware: no horizontal page scroll at 390px
- [x] prov1 /provider/earnings: no horizontal page scroll at 390px
- [x] prov1 /provider/payouts: no horizontal page scroll at 390px
- [x] admin /admin/overview: no horizontal page scroll at 390px
- [x] admin /admin/users: no horizontal page scroll at 390px
- [x] admin /admin/providers: no horizontal page scroll at 390px
- [x] admin /admin/jobs: no horizontal page scroll at 390px
- [x] admin /admin/billing: no horizontal page scroll at 390px
- [x] admin /admin/disputes: no horizontal page scroll at 390px
- [x] admin /admin/settlement: no horizontal page scroll at 390px
- [x] admin /admin/security: no horizontal page scroll at 390px
- [x] admin /admin/telemetry: no horizontal page scroll at 390px
- [x] admin /admin/vendors: no horizontal page scroll at 390px
- [x] admin /admin/audit-logs: no horizontal page scroll at 390px
- [x] admin /admin/configuration: no horizontal page scroll at 390px
