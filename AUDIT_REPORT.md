# ComputeCoin — Security & Correctness Audit Report

_End-to-end audit, fixes, and verification. Fixes are applied to the working tree
but **not committed** (awaiting review)._

## Test baseline → after fixes
| Suite | Before | After |
|---|---|---|
| `apps/api` pytest | 128 | **141** (+13 proving tests) |
| `provider-agent` | 60 | 60 |
| `sdk/python` | 27 | 27 |
| `sdk/typescript` | 20 | 20 |
| `contracts` (hardhat) | 6 | **11** (+5 settleJob/pause) |
| `apps/web` build | green (next 15.1.6) | **green (next 15.5.28)** |

All green. Two pre-existing Phase-10 tests were **corrected** (they asserted the
vulnerable behavior — a non-admin issuing vouchers/confirming) rather than weakened.

---

## Findings & fix status

### CRITICAL — all fixed
| ID | Issue | File | Fix | Proving test |
|---|---|---|---|---|
| C1 | Settlement endpoints had **no authorization** — any user could mint a platform-signed payout voucher / confirm / refund / read platform-wide data | `api/routers/settlement.py` | voucher/confirm/refund/record/transactions/reconciliation → `require_roles("administrator")`; escrow reads → owner-or-admin (404 to others); escrow create → job-owner check | `test_settlement_privileged_endpoints_require_admin`, `test_escrow_read_is_owner_scoped`, `test_create_escrow_requires_job_ownership` |
| C2 | **Over-refund / money minting** — refund capped only vs original capture, not prior refunds; provider/platform accounts not floored | `services/billing.py`, `services/ledger.py` | cumulative refund cap; `provider_earnings` added to no-negative set | `test_refund_cumulative_cap` |
| C3 | Insecure default `AUTH_SECRET`/`SESSION_SECRET`/signer usable in prod (forge admin JWTs/vouchers), no boot guard | `core/config.py` | `@model_validator` refuses boot in non-dev `ENVIRONMENT` with dev defaults | `test_config_guard.py` (4) |

### HIGH — all fixed
| ID | Issue | Fix | Proof |
|---|---|---|---|
| H1 | Settlement `current_user.role` AttributeError (500) + wrong role string | fixed to `roles`/`require_roles` | settlement authz test |
| H2 | IDOR: read any job's escrow by id | owner-or-admin, 404 to strangers | `test_escrow_read_is_owner_scoped` |
| H3 | SETTLED escrow could also be refunded (double-spend) | `confirm_refund_tx` 409s on SETTLED | `test_confirm_refund_rejects_settled_escrow` |
| H4 | Payout TOCTOU → double payout | `provider_earnings` no-negative + payout catches `InsufficientFunds`→409 | ledger guard + payout tests |
| H5 | No background lease sweeper (stuck jobs/held credits) | recovery sweeper in app lifespan (`run_recovery_cycle`) | — |
| H6 | RESERVED-but-unclaimed jobs never recovered | `recover_stuck_reserved` requeues/fails + releases hold | `test_stuck_reserved_job_is_recovered` |
| H7 | Unsynchronized recovery double-processes | recovery selects `with_for_update(skip_locked=True)` | — |
| H8 | `next@15.1.6` critical CVEs (middleware auth bypass, SSRF, RCE) | bump → `next@15.5.28` | `npm audit` + build |

### MEDIUM — fixed
- Cross-tenant telemetry injection → `record_job_telemetry` drops points for jobs not owned by the machine (`test_job_telemetry_cross_tenant_injection_dropped`).
- Unbounded inference input → `max_length` on chat content/messages; per-string bound on embeddings (`test_oversized_prompt_rejected`).
- Unbounded request body → `MAX_REQUEST_BYTES` middleware (413 before buffering).
- Voucher re-issuance → refused once a voucher exists (`test_voucher_cannot_be_reissued`).
- Seed script → refuses non-dev `ENVIRONMENT` without `--force`.
- Web security headers → CSP/HSTS/X-Frame-Options/nosniff/Referrer-Policy in `next.config.mjs`.
- No-provider path → releases the hold (credit-leak fix).
- Telemetry queries → row cap (`_MAX_QUERY_ROWS`).
- Contracts → **pause no longer freezes refunds** (`refundJob` not `whenNotPaused`); `settleJob` guard/signature/pause tests added.

### LOW — fixed
Debug `print()` → `logger.debug`; `/docs` disabled in prod; artifact downloads get `nosniff` + `attachment`; telemetry validation errors no longer echo internals.

---

## Remaining risks / needs a product decision
1. ~~**Web tokens in `localStorage`** (access+refresh).~~ **RESOLVED** — the backend now sets httpOnly/Secure/SameSite cookies with CSRF double-submit, and `apps/web/src/lib/api.ts` no longer writes any token to `localStorage` (in-memory only; CSRF read from its cookie). Verified: `grep` shows zero token localStorage I/O; web build green.
2. **On-chain ↔ off-chain ledger parity** is reported from an internal flag, not a real chain comparison; treated as **parallel rails**. If they must be one balance, reconciliation needs to cross-check on-chain amounts.
3. **On-chain jobId squatting** — anyone can `lockBudget` for a known jobId on-chain (griefing). The API path is now owner-gated; a full contract fix keys escrows by `(customer, jobId)` and needs a redeploy + backend change.
4. **`settleJob` happy-path** (valid signed voucher → payout math) is covered on the signing side by the Python crypto test and on-chain by guard/pause tests; a full signed on-chain run needs an ethers/JS test (hardhat 3) — recommended follow-up.
5. **postcss** (build-time) advisory remains; its only fix is `next@16` (major) — deferred.
6. Set `ENVIRONMENT=production` and strong secrets in deploy (the new boot guard enforces this).

## Production deployment checklist
- [ ] `ENVIRONMENT=production`, strong `AUTH_SECRET`/`SESSION_SECRET`, real `PLATFORM_SIGNER_PRIVATE_KEY` (boot guard enforces).
- [ ] Run against **Postgres** (not SQLite) so row-locks are real; run the recovery sweeper (`SWEEP_INTERVAL_SECONDS`).
- [ ] Put a body-size/‎rate limit at the proxy too; serve behind TLS (HSTS is set).
- [ ] Do **not** run `scripts/seed_dev.py` against prod.
- [x] Move web auth to httpOnly cookies (risk #1) — **done**; no tokens in JS-accessible storage.
