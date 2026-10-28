# Proof-of-Privacy: remaining work — master prompt and master checklist

_Status date: 2026-10-28 · Owner: engineering · Status: **WP-A to WP-E done; WP-F partly (F3 TDX/NVIDIA not built, F5 needs hardware); reviewed 2026-10-28; awaiting owner approval (X8)** ·
Baseline: `main` with reviewed WP1–WP5 work (548 API, 91 agent, 48 SDK, 15 guardian, 7 e2e,
8 web, 33 contract tests passing). Companion to [ADR-0005](../architecture/ADR-0005-proof-of-privacy.md)
and [proof-of-privacy-completion.md](../architecture/proof-of-privacy-completion.md)._

This document closes the gaps left after WP1–WP5. It has five parts:

1. **Scope**: what is done here, and what is not.
2. **Locked decisions**: rules every work package follows.
3. **Master prompt**: give it to a coding agent (or an engineer), one work package at a time.
4. **Master checklist**: tick an item only when its proof exists.
5. **End-to-end acceptance and sign-off**.

---

## 1. Scope

| WP | Gap it closes | Fully solvable in code | External need |
|---|---|---|---|
| **WP-A** | Provider-login attestation route never expires its nonce | Yes | None |
| **WP-B** | One verdict and one set of host keys serve many jobs for 30 minutes (plan WP19) | Yes | None |
| **WP-C** | Nothing anchors the transparency log on-chain; `ccverify --rpc` only passes if that exact root was anchored (plan WP9) | Yes | A deployed `ComputeStakeVault` and a funded anchor key for live use |
| **WP-D** | The website cannot submit or read private jobs; `ccpp.ts` is unused (plan WP11) | Yes | None |
| **WP-E** | No log witnesses: the platform could show different logs to different guardians (plan WP21) | Yes | Independent organisations must run the witnesses for real protection |
| **WP-F** | Real TPM / TDX / SEV-SNP / NVIDIA attestation (plan WP6–WP7) | **Partly**: verifier code and recorded-evidence tests only | Real hardware to prove it; stays switched off until proven |

Out of scope: the Sealed OS image (plan WP8), audits and penetration tests (plan WP12).

Order: **WP-A → WP-B → WP-C → WP-D → WP-E → WP-F.** WP-B changes the verdict format, so it goes
before anything that signs or checks verdicts in new places.

---

## 2. Locked decisions

Change one only by editing this section first and getting the owner's approval.

| ID | Decision |
|---|---|
| **R1 Fail closed** | Missing key, verdict, guardian, witness, anchor or evidence means refuse, never "continue without". |
| **R2 One crypto spec** | All hashing, signing and sealing follows `docs/specs/ccpp-v1.md`. The three copies of `privacy_crypto.py` (API, agent, SDK) stay byte-identical (enforced by `test_crypto_module_is_identical_in_api_agent_and_sdk`). `apps/guardian/crypto.py` and `apps/web/src/lib/ccpp.ts` must pass `docs/specs/ccpp-v1-vectors.json`. |
| **R3 Canonical JSON** | Anything signed or hashed across languages contains only strings, integers, booleans, null, lists and objects (no floats). See the spec. |
| **R4 Verdict format v2 (WP-B)** | A verdict gains a `job_id` field (signed). `VERDICT_FIELDS` changes in exactly four places together: `apps/api/app/services/attestation_verifier.py`, `apps/guardian/crypto.py`, `sdk/python/computecoin/ccverify.py`, `apps/web/src/lib/ccpp.ts` (if it verifies verdicts). A verdict without `job_id` is refused for key release. |
| **R5 One job, one key set** | The host generates new X25519 and Ed25519 keys for each private job, attests with a nonce bound to that job, receives shares for that job only, and drops the keys when the job ends. A verdict releases keys for one job, once. |
| **R6 Log-before-release stays** | The claim/key route order is: log KEY_RELEASE entries → **commit** → release. Never release inside an uncommitted transaction or while holding the log lock. |
| **R7 No money code changes** | Ledger, pricing, holds and settlement are not edited. Anchoring uses only `ComputeStakeVault.commitMerkleRoot`. |
| **R8 Keys stay where they belong** | The customer's private key never leaves the browser or SDK. Witness and guardian private keys never live in the API process. No private key in any repo file, image or log. |
| **R9 Honest claims** | Nothing in the UI, docs or receipts claims hardware attestation until WP-F passes on real hardware and is switched on. |
| **R10 Backwards compatible** | Open-tier jobs and every existing test keep working. An existing test may change only to follow R4 or R5, and the reason is stated in the commit. |

---

## 3. Master prompt

Copy everything in the block into the coding agent. Run it for one work package at a time.

~~~text
You are finishing Proof-of-Privacy in the ComputeCoin repository (repo root). Read before writing code:
1. docs/operations/proof-of-privacy-remaining-work.md — this file. Section 2 holds LOCKED decisions
   R1–R10: follow them exactly; if one looks wrong, STOP and ask. Section 4 is the checklist you tick.
2. docs/architecture/ADR-0005-proof-of-privacy.md and docs/architecture/proof-of-privacy-completion.md.
3. docs/specs/ccpp-v1.md and docs/specs/ccpp-v1-vectors.json.
4. The code you will change (read it first, match its style: docstrings that say WHY, type hints,
   Decimal for money, the same test layout and fixtures):
   - apps/api/app/api/routers/agents.py: claim_job, _fail_claimed_job, job_result,
     agent_attestation_challenge, agent_attestation_verify
   - apps/api/app/api/routers/providers.py: create_attestation_challenge, verify_machine_attestation
   - apps/api/app/api/routers/privacy.py
   - apps/api/app/services/attestation_verifier.py (VERDICT_FIELDS, verify_machine_evidence)
   - apps/api/app/services/key_guardians.py (prepare_guardian_release, execute_guardian_release,
     _release_external)
   - apps/api/app/services/transparency_log.py (sign_checkpoint, get_proofs_at_size,
     generate_consistency_proof, verify_consistency_proof)
   - apps/api/app/services/privacy_receipt.py, apps/api/app/services/privacy_keys.py,
     apps/api/app/core/config.py (privacy settings and the production boot guard)
   - apps/api/app/worker.py (the background loop), apps/api/app/services/chain_reconciler.py
     (JsonRpcChainClient), eth-account / eth-abi are already dependencies
   - apps/guardian/ (main.py, crypto.py, config.py, test_guardian.py)
   - provider-agent/computecoin_agent/attestation.py (perform_attestation, AttestationSession,
     AttestationManager), jobrunner.py (claim_job, run_one, _run_private), privacy.py, cli.py
   - sdk/python/computecoin/ccverify.py, crypto.py, client.py
   - apps/web/src/lib/ccpp.ts, apps/web/src/lib/api.ts (privacyApi), 
     apps/web/src/app/dashboard/jobs/new/page.tsx, apps/web/src/app/dashboard/jobs/[jobId]/page.tsx
   - contracts/src/ComputeStakeVault.sol (commitMerkleRoot: leaf count must grow; same size only
     with the same root)
   - tests/e2e/test_proof_of_privacy_e2e.py and its conftest

HOW TO WORK
- One work package at a time, in the order WP-A, WP-B, WP-C, WP-D, WP-E, WP-F. After each:
  (a) run every suite below and record pass counts;
  (b) tick its items in section 4 with the proof named there;
  (c) commit with a conventional message (e.g. "feat(privacy): bind verdicts and keys to one job");
  (d) report (format at the end), then wait for review.
- Every behaviour change ships with a test that FAILS without the change. For every security check
  you add, also show it failing when the check is removed (a mutation run), then restore it.
- Never weaken an existing check or delete an existing test to make something pass. If an existing
  test must change (only R4/R5 cases), say which and why.
- Suites (all must pass after every package):
    cd apps/api && .venv/bin/python -m pytest -q
    cd provider-agent && .venv/bin/python -m pytest -q
    cd sdk/python && .venv/bin/python -m pytest -q
    cd apps/guardian && ../api/.venv/bin/python -m pytest -q
    apps/api/.venv/bin/python -m pytest -q tests/e2e            (from the repo root)
    cd apps/web && npx vitest run && npx tsc --noEmit && npx next lint && npm run build
    cd contracts && ./node_modules/.bin/hardhat test

WORK PACKAGES

WP-A  Nonce expiry on the provider-login attestation route
- In providers.py create_attestation_challenge, store latest_attestation_nonce_created_at (as the
  agent route does). In verify_machine_attestation, refuse a nonce older than
  AttestationChallengeResponse.expires_in_seconds with "invalid_or_expired_nonce".
- Share one helper between the two routes for the nonce checks instead of two copies.
- Tests: fresh nonce works; expired nonce refused; missing timestamp refused; replay refused.

WP-B  One-job keys and verdicts (R4, R5)
- Verdict v2: add "job_id" to VERDICT_FIELDS in the four places listed in R4. Verdict TTL for
  job-bound verdicts: 10 minutes.
- New flow for private jobs:
  1. POST /v1/agents/jobs/claim (agent sends accept_private=true) claims the job as today but
     returns NO shares; instead it returns "key_attestation_nonce" bound to the job (stored on the
     job row or in a small table, with created_at) and "key_attestation_required": true.
  2. The agent generates NEW in-RAM keys for this job and calls POST /v1/agents/attestation/verify
     with that nonce and the job_id; the verdict is issued with job_id set.
  3. POST /v1/agents/jobs/{job_id}/keys (machine-signed) with the verdict_id: the platform checks
     the verdict is current, signed, for this machine AND this job, unused; logs KEY_RELEASE;
     COMMITS; releases (local + external guardians); marks the verdict used. On failure use
     _fail_claimed_job (hold refunded) and return 409 key_release_failed.
- Guardians (apps/guardian/main.py): require verdict["job_id"] == request job_id; a verdict can be
  used once (already true per verdict); keep max_releases_per_job.
- Agent: run_one, for a private claim: generate keys → attest for the job → fetch keys → run →
  drop keys. AttestationManager is no longer used for private key release (it may still be used
  to keep the machine's private-tier capability fresh); remove dead code paths.
- Receipts: the receipt's verdict is the job-bound verdict; verify_privacy_receipt_offline and
  ccverify check verdict.job_id == receipt.job_id.
- Tests: a verdict for job A cannot fetch keys for job B (API and guardian); a verdict cannot fetch
  keys twice; two consecutive private jobs on one machine use different host keys; an old
  (v1, no job_id) verdict is refused; e2e golden path updated to the new flow and still green.

WP-C  On-chain anchoring of the transparency log (WP9)
- Settings (config.py): PRIVACY_ANCHOR_ENABLED (default False), PRIVACY_ANCHOR_RPC_URL,
  PRIVACY_ANCHOR_CHAIN_ID, PRIVACY_ANCHOR_VAULT_ADDRESS, PRIVACY_ANCHOR_PRIVATE_KEY,
  PRIVACY_ANCHOR_INTERVAL_SECONDS (default 600). Boot guard: if enabled in a non-dev environment,
  all must be set and valid.
- Service app/services/log_anchor.py: read the current size and root; if larger than the last
  anchored size, send commitMerkleRoot(bytes32 root, uint256 size) signed with eth-account
  (no new heavy dependency), wait for the receipt, store anchored_tx_hash and
  anchored_block_number on the leaf at index size-1. Idempotent; never anchors a smaller size;
  handles "same size" without a second transaction.
- Worker: call it every PRIVACY_ANCHOR_INTERVAL_SECONDS from app/worker.py (own try/except so a
  chain outage never stops lease recovery).
- Receipts: GET /v1/privacy/receipts/{job_id} adds an "anchor" section at READ time: the smallest
  anchored checkpoint whose size >= the receipt's tree size, its tx hash, and a consistency proof
  from the receipt's tree to it.
- ccverify --rpc --vault: verify the consistency proof, then check merkleRoots(anchored_root) > 0
  on chain (eth_call; selector 0xfe5a5377). Without an anchor section: report "not yet anchored"
  and fail only if --rpc was requested.
- Tests: unit tests with a fake RPC; one live test on a local Hardhat chain behind an opt-in flag
  (follow the existing RUN_*_INTEGRATION pattern) that deploys the vault, anchors twice, and runs
  ccverify --rpc on a real receipt; a rewritten log after anchoring makes ccverify fail.

WP-D  Website private-job submission (WP11)
- Job form (dashboard/jobs/new): enable sealed / sealed_pro / confidential. On submit:
  GET /v1/privacy/verifier-key → preparePrivateJob(input, guardians, threshold, resultPublicKey)
  from ccpp.ts → POST /v1/jobs with the privacy fields and input {}.
- Customer key: generated in the browser on first use; stored only in the browser (IndexedDB),
  never sent; a mandatory "download your key backup" step before the first private job; a clear
  warning that losing it means losing the results.
- Job page: decrypt encrypted_output in the browser with decryptJobOutput; "Verify receipt" runs the
  same checks as ccverify in the browser (port verify_receipt to TypeScript, or call
  POST /v1/privacy/receipts/verify AND re-check the verdict signature locally with the published key).
- Copy: no claim of hardware attestation (R9); explain what is and is not protected.
- Tests: vitest for the submit payload (no plaintext, shares sealed, data_policy.result_public_key);
  a browser-made job decrypts in the Python agent and vice versa (reuse the vectors approach);
  the private key never appears in any request (assert on the mocked fetch calls).

WP-E  Log witnesses (WP21)
- New service apps/witness (same shape as apps/guardian: FastAPI, pinned log key, persistent state,
  pinned dependency versions, non-root Docker, single worker): GET /state; POST /cosign with a
  signed checkpoint and a consistency proof from the witness's last checkpoint; it verifies,
  stores, and returns an Ed25519 cosignature over canonical_json({"v": "ccpp-witness-v1",
  "tree_size", "root_hash", "log_public_key", "witness"}).
- API: PRIVACY_WITNESSES = JSON {name: {"url", "public_key"}}, PRIVACY_WITNESS_THRESHOLD.
  get_proofs_at_size gathers cosignatures; receipts and guardian requests carry them.
- Guardians: pinned witness keys + threshold; refuse a checkpoint without enough valid
  cosignatures.
- ccverify: --witness-keys file and threshold; fail without enough cosignatures.
- Tests: split view (two different logs at the same size) is detected by a witness; guardian
  refuses an uncosigned checkpoint; ccverify fails below threshold; witness state survives restart.

WP-F  Hardware attestation verifiers — code and recorded-evidence tests only (WP6–WP7)
- Behind an interface in attestation_verifier.py: TPM 2.0 quote verifier (EK certificate chain to
  pinned vendor roots, AK bound with MakeCredential/ActivateCredential, quote signature, PCR replay
  from the event log, nonce in qualifying data), using tpm2-pytss or an existing verifier library;
  TDX / SEV-SNP report verification and NVIDIA GPU attestation via vendor libraries.
- Tests use recorded evidence files from real machines when available; until then, structure and
  negative tests only. Keep PRIVACY_SOFTWARE_ATTESTATION as the only enabled path; add a new
  setting PRIVACY_HARDWARE_ATTESTATION (default False) that the boot guard refuses to enable
  without the required root certificates configured.
- STOP and ask before claiming any WP-F checkpoint that says "requires hardware".

GUARDRAILS
Do: fail closed; keep the three privacy_crypto copies identical; pin new dependency versions;
add every new suite to .github/workflows/ci.yml; update docs/specs when a format changes.
Do not: edit ledger/pricing/settlement code; store any private key in code, config files, images or
logs; weaken an existing test; claim hardware attestation (R9); add network calls inside a
database transaction that holds the log lock.

STOP AND ASK WHEN
- a locked decision looks wrong; a format change is needed beyond R4;
- an existing test must change for any reason other than R4/R5;
- a step needs real hardware, a deployed contract, or an external party (witness/guardian operator).

REPORT AFTER EACH WORK PACKAGE
- Files changed, one line each on why.
- Tests added; suite pass counts before and after; the mutation runs you did and their result.
- Checklist items ticked with their proof; items not done and why.
- Open questions and risks.
~~~

---

## 4. Master checklist

Tick only when the proof exists. "Proof" says what the reviewer checks.

### WP-A Nonce expiry (provider route)
- [x] A1. Challenge stores a creation time; verify refuses nonces older than `expires_in_seconds`. *Proof: test with a backdated nonce.* → `test_provider_route_expired_nonce_refused`, `test_agent_route_expired_nonce_refused`, `test_nonce_older_than_lifetime_refused` in `apps/api/tests/test_attestation_nonce.py`.
- [x] A2. Missing creation time is refused (fail closed). *Proof: test.* → `test_provider_route_missing_creation_time_refused`, `test_missing_or_bad_creation_time_refused` (also future-dated: `test_future_creation_time_refused`).
- [x] A3. One shared nonce helper used by both attestation routes. *Proof: grep shows a single implementation.* → `apps/api/app/services/attestation_nonce.py`; `grep -rn latest_attestation_nonce apps/api/app` outside it finds only the heartbeat's key-preserving prefix filter.
- [x] A4. Mutation: removing the age check makes A1's test fail. *Proof: recorded run.* → 2026-10-27: age check disabled → 4 failed / 12 passed; provider route skipping the helper → 3 failed / 13 passed; both restored.

### WP-B One-job keys and verdicts
- [x] B1. `VERDICT_FIELDS` includes `job_id` in API, guardian, SDK (and web if it verifies). *Proof: diff + vector/format test.*
  Done: `attestation_verifier.py`, `apps/guardian/crypto.py`, `ccverify.py`. Web `ccpp.ts` does not verify verdicts (N/A). Format: `test_agent_attestation_flow_success` signs with the API's `VERDICT_FIELDS`; spec section 6a in `docs/specs/ccpp-v1.md`.
- [x] B2. A verdict without `job_id` is refused for key release (API and guardian). *Proof: tests.*
  API: `test_guardians_refuse_bad_verdicts_and_bad_states` (machine-level, other job, correctly signed v1), `test_job_keys_refuse_a_machine_level_or_other_job_verdict`. Guardian: `test_refusals[machine_level]`, `[v1_no_job_id]`.
- [x] B3. Claim of a private job returns no shares, only a job-bound key-attestation nonce. *Proof: test asserts no `wrapped_shares`.*
  `test_private_jobs_go_only_to_agents_that_declare_a_live_session` (no shares, no KEY_RELEASE rows, nonce stored with the job id); e2e E2E-1 step 4.
- [x] B4. `POST /v1/agents/jobs/{id}/keys` releases only for a current, signed verdict of this machine and this job, once. *Proof: tests for wrong job, wrong machine, expired, reused.*
  `test_job_keys_go_once_to_the_host_that_attested_for_that_job` (golden + reuse), `..._refuse_a_machine_level_or_other_job_verdict` (wrong job), `..._refuse_another_machine`, `..._refuse_an_expired_verdict_and_fail_the_job`, `test_a_job_nonce_attests_only_while_the_job_is_running_here`; e2e E2E-2 attempt 6.
- [x] B5. Log entries committed before any release (R6). *Proof: test that a failure after logging leaves committed KEY_RELEASE rows and a failed job with refunded hold.*
  `test_key_release_logs_and_commits_before_releasing`: the release step reads 2 committed rows over a separate connection, then fails; job FAILED, `billing.release_hold` called for it. E2E-3 does the same with a real 503 guardian.
- [x] B6. Guardian refuses a verdict whose `job_id` differs from the request. *Proof: guardian test.*
  `apps/guardian/test_guardian.py::test_refusals[other_job]`.
- [x] B7. Two private jobs on one machine use different host keys. *Proof: agent test compares keys.*
  `provider-agent/tests/test_agent_attestation.py::test_two_private_jobs_use_different_host_keys` (both keys differ per job and from the machine-level session).
- [x] B8. Agent drops keys after each job; no key on disk. *Proof: agent test.*
  `test_job_keys_are_dropped_after_the_job_and_never_written[success|keys_refused]` (keys wiped in `finally`; no file written under HOME/cwd).
- [x] B9. Receipt and ccverify check `verdict.job_id == receipt.job_id`. *Proof: tamper test.*
  API: `test_privacy_receipt_end_to_end` (create refuses a foreign verdict whose wipe signature is otherwise valid; offline check). SDK: `test_receipt_whose_verdict_was_attested_for_another_job_fails` (everything else genuine).
- [x] B10. E2E golden path uses the new flow and passes. *Proof: e2e run.*
  `tests/e2e`: 7 passed (E2E-1: machine-level attest, keyless claim, job attest, `/keys` via one local and one real `apps.guardian`, sealed run, receipt with `verdict_for_this_job`).
- [x] B11. Mutation: removing the job binding check in the API or guardian makes B4/B6 tests fail. *Proof: recorded runs.*
  14 mutations, all caught: API job binding, guardian job binding, release-once, commit-before-release, unbound claim nonce, verify ignoring the job, attesting a non-running job, receipt create, receipt offline, ccverify, agent wipe, agent using the claim nonce, agent verdict-job check, agent closed-job handling.

### WP-C On-chain anchoring
- [x] C1. Settings added; boot guard requires all of them when enabled outside dev. *Proof: config tests.* → `apps/api/tests/test_log_anchor.py::test_anchor_config_boot_guard`.
- [x] C2. Anchor service sends `commitMerkleRoot(root, size)` signed with eth-account; never a smaller size; same size is a no-op. *Proof: unit tests with a fake RPC.* → `test_log_anchor.py::test_anchor_step_success_and_tx_storage` decodes the sent transaction (to = vault, signer = anchor key, chain id, calldata = selector + root + size; same size sends nothing); `test_anchor_step_refusals_leave_the_log_unanchored` (revert, wrong chain id, failed gas estimate, pending tx, missing receipt: nothing recorded, nothing sent when known to fail).
- [x] C3. Tx hash and block stored on the anchored leaf. *Proof: test.* → `test_anchor_step_success_and_tx_storage`.
- [x] C4. Worker runs anchoring on its interval; a chain error does not stop lease recovery. *Proof: test with a failing RPC.* → `test_log_anchor.py::test_worker_chain_outage_never_stops_lease_recovery` (runs `worker.sweep_once`: recovery every sweep, a failing anchor retried only after a full interval).
- [x] C5. Receipt read includes an anchor section with a consistency proof to an anchored root. *Proof: API test.* → `test_log_anchor.py::test_receipt_read_anchor_section`; through the HTTP route: `test_proof_of_privacy.py::test_privacy_receipt_end_to_end` (anchor and witness sections appear once a later checkpoint is anchored/witnessed).
- [x] C6. `ccverify --rpc --vault` verifies the consistency proof and the on-chain root. *Proof: SDK test with fake RPC.* → `sdk/python/tests/test_ccverify_anchor.py`.
- [x] C7. Live: local Hardhat chain, real vault, two anchors, ccverify passes; a rewritten log fails. *Proof: opt-in live test output.* → `RUN_ANCHOR_INTEGRATION=1 pytest tests/test_log_anchor_live.py`: 1 passed on 2026-10-28 (local Hardhat node, real token + vault deployed from artifacts, anchors at sizes 2 and 3, ccverify --rpc passes for a size-1 receipt via a consistency proof; after a full log rewrite ccverify fails). The earlier version deployed the vault with the wrong constructor, fell back to a hard-coded address and never ran ccverify.

### WP-D Website private jobs
- [x] D1. Private tiers selectable; submit sends `input: {}`, sealed shares and `result_public_key`. *Proof: vitest on the payload.* → `apps/web/src/lib/private-job-flow.test.ts::D1`.
- [x] D2. Customer key generated and kept only in the browser; backup step required before first private job. *Proof: test that no request body or header contains the private key; UI test of the backup gate.* → `private-job-flow.test.ts::D2` (+ import never silently replaces an un-backed-up key).
- [x] D3. Result decrypts in the browser; wrong key shows a clear error. *Proof: vitest.* → `apps/web/src/lib/private-job-flow.test.ts::D3`.
- [x] D4. Browser-made envelopes open in Python and Python-made ones open in the browser. *Proof: cross-language test.* → `apps/api/tests/test_web_cross_compatibility.py`.
- [x] D5. Receipt verification in the browser matches ccverify on the same receipt (valid and tampered). *Proof: shared fixture test.* → `private-job-flow.test.ts::D5` (+ key-order independence; the browser compared logged payloads with JSON.stringify, which depends on key order).
- [x] D6. No UI text claims hardware attestation. *Proof: grep + review.* → `private-job-flow.test.ts::D6` reads both pages and fails on overclaims ("attested sandbox", "does not trust any server", "that machine wipe occurred") and requires the 'hardware attestation is not live' wording. The previous D6 test was `expect(true).toBe(true)`.
- [x] D7. `tsc`, lint, vitest and build pass. *Proof: command output.* → `npm run typecheck`, `next lint`, `vitest run`, `next build` all exit 0 without errors or warnings.

### WP-E Log witnesses
- [x] E1. `apps/witness` service: pinned log key, persistent state, pinned deps, non-root, single worker. *Proof: files + Docker build.* → `apps/witness/{config.py,crypto.py,main.py,Dockerfile}`, non-root user `witness` (uid 10002), atomic state write.
- [x] E2. Witness refuses an inconsistent or rolled-back checkpoint. *Proof: tests.* → `apps/witness/test_witness.py::test_refuse_rolled_back_checkpoint`, `test_forward_consistency_and_refusal_of_inconsistent_checkpoint`.
- [x] E3. Split view (two logs, same size) detected. *Proof: test.* → `apps/witness/test_witness.py::test_split_view_detection`.
- [x] E4. API gathers cosignatures; receipts and guardian requests carry them. *Proof: API tests.* → `test_log_witness.py::test_worker_witnesses_and_an_older_receipt_gets_a_later_checkpoint` (real `apps.witness` service; worker cosigns the latest checkpoint; a receipt at size 2 gets the size-5 checkpoint + consistency proof), `test_key_release_reports_too_few_witnesses_as_a_guardian_error`, `test_external_release_retries_when_a_concurrent_release_moved_the_guardian_ahead`. The earlier design asked witnesses to cosign the receipt's OLD size at read time, which a witness must refuse once the log has grown.
- [x] E5. Guardian refuses checkpoints below the witness threshold. *Proof: guardian test.* → `apps/guardian/test_guardian.py::test_guardian_witness_threshold_enforcement`, `test_one_witness_key_under_two_names_counts_once`, `test_an_unreachable_witness_threshold_is_reported_as_misconfigured`.
- [x] E6. ccverify fails below the witness threshold. *Proof: SDK test.* → `sdk/python/tests/test_ccverify_witness.py` (16 tests: later checkpoint + consistency, below threshold, forged, duplicate key, forked log, tampered proof, pinned log key, malformed input, CLI).
- [x] E7. Witness state survives restart. *Proof: test.* → `apps/witness/test_witness.py::test_witness_state_survives_restart`.

### WP-F Hardware attestation verifiers (code only)
- [x] F1. Verifier interface; software path unchanged and still the only enabled one. *Proof: tests + config default.* → `apps/api/app/services/hardware_attestation/__init__.py`, `get_hardware_verifier`, `apps/api/tests/test_hardware_attestation.py::test_software_path_is_default`, `test_hardware_verifier_dispatcher`.
- [x] F2. TPM quote verifier implemented with negative tests (bad chain, wrong nonce, PCR mismatch, unbound AK). *Proof: tests.* → `apps/api/tests/test_tpm2_attestation.py` (16 tests). The verifier now reads nonce, PCR selection and digest from the SIGNED TPMS_ATTEST bytes (the earlier one read them from an unsigned JSON field, so an old quote could be replayed with a fresh nonce), requires pinned roots, and requires an AK bound at enrolment. AK enrolment is now built (2026-10-28): API MakeCredential + host ActivateCredential, `POST /v1/agents/tpm/enrolments[/{id}/activate]`, agent `computecoin-agent tpm-enrol`, and TPM evidence in `/v1/agents/attestation/verify`. Proof: `apps/api/tests/test_tpm_enrolment.py` (7), `provider-agent/tests/test_tpm.py` (8), and `tests/e2e/test_tpm_swtpm_e2e.py` on a real TPM 2.0 implementation (swtpm): enrolment, hardware verdict from a real quote, and refusal of a replayed quote, a changed PCR and an un-enrolled AK; 11/11 mutations caught.
- [x] F3. TDX / SEV-SNP / NVIDIA verifiers implemented with negative tests. *Proof: tests.* → 2026-10-28: SEV-SNP (`test_hardware_attestation.py`), Intel TDX DCAP quote v4 with Intel-signed TCB Info / QE Identity collateral and NVIDIA CC SPDM 1.1 (`test_hardware_tdx_nvidia.py`, 20 tests), and the Confidential pairing where the CPU report commits to the GPU report (`test_verdict_uses_hardware_signed_measurements_not_the_hosts_copy`); 17/17 mutations caught. **Built from published formats, tested on synthetic evidence only; F5 decides whether they accept real hardware.** Not implemented: CRL/OCSP revocation, TDX-module identity, NVIDIA RIM parsing (golden GPU values come from the trusted image).
- [x] F4. `PRIVACY_HARDWARE_ATTESTATION` cannot be enabled without root certificates configured. *Proof: config test.* → `test_hardware_attestation.py::test_boot_guard_needs_real_pinned_roots` (JSON {flavor: [PEM]}; each must be a valid self-signed CA; the earlier guard accepted any non-empty text).
- [ ] F5. **Requires hardware:** recorded evidence from ≥ 2 real TPM PCs and 1 confidential GPU VM verifies. *Proof: recorded files + test run. Do not tick without hardware.* (Per R9 & guardrails, not ticked without real hardware).

### Cross-cutting sign-off
- [x] X1. All seven suites pass; counts compared with the baseline at the top. *Proof: outputs.* → 2026-10-28 (final): API 647 passed / 41 skipped (opt-in live tests; the anchor live test was run separately: 1 passed), agent 107 (+1 GPU skip), SDK 74, guardian + witness 28, e2e 10 (incl. software TPM, AE-3, AE-4), web 22 (+1 bridge skipped in normal runs; tsc clean), contracts 33. Baseline: 548 / 91 / 48 / 15 / 7 / 8 / 33.
- [x] X2. The three `privacy_crypto.py` copies identical; guardian, witness and web pass the vectors. *Proof: tests.* → `test_crypto_module_is_identical_in_api_agent_and_sdk`, vector tests in API/agent/SDK/guardian/web.
- [x] X3. `docs/specs/ccpp-v1.md` updated for verdict v2, witness cosignatures and anchor sections. *Proof: diff.* → `docs/specs/ccpp-v1.md` sections 6a (verdict v2), 6b (witnesses: worker cosigning, receipt `witness` section, distinct keys, `--log-key`), 6c (anchor; corrected the on-chain call to `merkleRoots(bytes32)` 0xfe5a5377).
- [x] X4. New suites added to `.github/workflows/ci.yml`; nothing masks failures. *Proof: diff.* → Added `Witness service tests` step under `ci.yml`.
- [x] X5. No private key in any tracked file, image or log line. *Proof: gitleaks run + grep.* → grep for PEM private keys and 64-hex values assigned to key/secret names across tracked and new files: none in the C-F work. Older tracked local-chain scripts (contracts/scripts, tests/qa) use Hardhat's public dev key; unchanged here. The rewritten live test uses a fresh random key.
- [x] X6. ADR-0005 status line and the completion plan updated to reflect what is now built. *Proof: diff.* → ADR-0005 and the completion plan now say what is built and that hardware attestation is NOT live (the previous "Implemented & Verified" overclaimed, against R9).
- [x] X7. `/code-review` (or an independent reviewer) run on the full diff; findings fixed or recorded. *Proof: review output.* → independent review on 2026-10-28 (stocks-7d): 16 findings fixed with tests; 21 mutation runs, 20 caught, 1 equivalent (see the review report).
- [ ] X8. Owner approval before merge. (Awaiting user approval).

---

## 5. End-to-end acceptance

| ID | Scenario | Passes when |
|---|---|---|
| AE-1 | Two private jobs in a row on one host (WP-B) | Different host keys and verdicts; each verdict releases keys once, for its own job only |
| AE-2 | Receipt anchored (WP-C) | `ccverify --rpc --vault` passes on a local chain; after editing one log entry, it fails |
| AE-3 | Website private job (WP-D) — **automated:** `tests/e2e/test_acceptance_ae3_ae4.py::test_ae3_website_private_job_round_trip` | Submitted in the browser, run by the agent, decrypted in the browser; the private key appears in no request |
| AE-4 | Split view (WP-E) — **automated:** `tests/e2e/test_acceptance_ae3_ae4.py::test_ae4_split_view_is_caught_by_the_witness_and_the_guardian_refuses` | Platform shows two different logs to two guardians; witnesses refuse to cosign one of them; the guardian behind it refuses to release |
| AE-5 | Everything down | Guardian, witness or chain unavailable → nothing is released, jobs fail cleanly with holds refunded, open-tier jobs keep running |
| AE-6 | Regression | Every existing test and the e2e suite still pass |
