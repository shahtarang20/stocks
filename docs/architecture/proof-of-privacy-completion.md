# Proof-of-Privacy: completion plan and end-to-end readiness checkpoints

_Status date: 2026-10-28 · Companion to [ADR-0005](ADR-0005-proof-of-privacy.md) ·
Status: **WP-A to WP-E built and tested; WP-F code only (TPM 2.0 and SEV-SNP verify synthetic
evidence, TDX and NVIDIA refuse everything); no hardware attestation is live (see ADR-0005 status).**_

ADR-0005 describes the architecture. Commit `7c195f4` built and tested its software building blocks.
This document lists **everything still missing** to run a real private job end to end, in the order
to build it, with a checkpoint (and the proof a reviewer checks) for every step, and the end-to-end
test suite that decides whether we are ready.

---

## 1. Where we are (baseline)

| ADR-0005 component | Built and tested in `7c195f4` | Missing |
|---|---|---|
| Crypto (ccpp-v1) | X25519 sealing, Ed25519, ChaCha20-Poly1305, Shamir; identical in API, agent, SDK | Published test vectors; browser implementation |
| Customer encryption | Python SDK `prepare_private_job`, `decrypt_job_output` | SDK client methods that call the API; website encryption |
| Tier routing | Tiers, stake, value at risk, region policy, never-downgrade | — |
| Attestation verifier | Image/PCR match, security locks, stake, Ed25519 verdicts, logged | Real TPM quote and TEE report verification; agent-side attestation |
| Key Guardians | k-of-n sealed shares, log-before-release, re-seal to host | External guardian service; release on job claim |
| Sealed execution (agent) | `execute_sealed_job`, wipe receipt | Called by the job runner; Sealed OS image |
| Transparency log, receipts | RFC 6962 log, receipts, `ccverify` | Receipt created on job completion; on-chain anchoring; ccverify anchor check |
| Economics | ComputeStakeVault (unaudited), canaries, leak reports | Vault ↔ DB stake sync; slash execution from the platform; canary scheduling and leak watching |
| Integrity for private jobs | Freivalds service | Output commitment so shadow re-runs work on encrypted output |

**The single biggest gap:** nothing connects these parts to the real job flow. Today a private job
is accepted and scheduled, but no host ever receives its key, runs it sealed, or returns a receipt.

---

## 2. Target end-to-end flow (what "done" means)

```
 CUSTOMER (SDK / web)            API + WORKER                       GUARDIANS                     HOST (Sealed OS / CVM)
 ────────────────────            ────────────                       ─────────                     ──────────────────────
 A1 GET /v1/privacy/verifier-key
 A2 encrypt input, seal shares
 A3 POST /v1/jobs  ───────────▶  B1 validate (ciphertext only),
                                    schedule on attested host
                                                                                                 H1 boot measured image
                                                                                                 H2 gen X25519 + Ed25519 in RAM
                                 B2 /v1/agents/attestation/* ◀──────────────────────────────────  H3 TPM quote / TDX|SNP + GPU report
                                    verify evidence → signed verdict
                                    (logged)                                                    
                                 B3 /v1/agents/jobs/claim  ◀────────────────────────────────────  H4 claim
                                    for each guardian:
                                      log KEY_RELEASE  ─────────────▶ G1 verify verdict + log proof
                                                                      G2 open own share, re-seal
                                                       ◀───────────── to host key (job-bound)
                                    claim response: encrypted_input,
                                    wrapped shares, result key  ─────────────────────────────────▶ H5 rebuild DK in RAM, decrypt,
                                                                                                    run, seal result to customer,
                                                                                                    wipe, sign wipe receipt,
                                                                                                    output commitment
                                 B4 /v1/agents/jobs/{id}/result ◀────────────────────────────────  H6 encrypted_output + wipe + commit
                                    store ciphertext, create receipt,
                                    shadow check by commitment
                                 B5 worker: anchor log root on-chain
 A4 GET job → decrypt result
 A5 GET receipt → ccverify (+ anchor)
```

---

## 3. Locked decisions (change only with owner approval)

| ID | Decision |
|---|---|
| L1 | **Fail closed everywhere.** Missing evidence, key, guardian, anchor or verdict means "refuse", never "continue without". |
| L2 | The platform must never hold enough key material to decrypt alone in production (boot guard already enforces < k local guardian keys). |
| L3 | Agent attestation uses **machine-signed** routes under `/v1/agents/attestation/*` (the existing `/provider/machines/{id}/attestation/*` routes need a user login the agent does not have). |
| L4 | Private outputs are never seen by the platform. Integrity uses an **output commitment** = `HMAC-SHA256(HKDF(DK, "ccpp-v1|output-commit"), canonical_output)`; both a primary and a shadow host can compute it, the platform can compare it, and nobody without DK can brute-force it. |
| L5 | One crypto spec (ccpp-v1). Every implementation (Python ×3, TypeScript) must pass the same published test vectors. |
| L6 | No change to ledger, pricing or settlement code. Slashing money moves only through ComputeStakeVault. |
| L7 | Marketing claims follow ADR-0005 section 8; a tier is sold only after its readiness gate (section 7) is passed. |
| L8 | **No single point of trust.** No one key, server, company or person can, alone: issue a verdict, release a key, ship a release, or rewrite the log. Every such action needs k-of-n independent parties (section 4B). |
| L9 | **Post-quantum by default** for anything that protects customer data at rest or in transit: hybrid classical + post-quantum, never post-quantum alone (if either half holds, the data is safe). |
| L10 | **One job, one set of keys.** Host keys and verdicts are bound to a single job and destroyed when it ends. |

---

## 4. Work packages and checkpoints

Effort figures are estimates for one experienced engineer, not measurements. Each checkpoint lists the
**proof** a reviewer must see before ticking it.

### WP1 — Crypto test vectors and browser implementation (3–5 days)

- Publish `docs/specs/ccpp-v1.md` and `docs/specs/ccpp-v1-vectors.json` (fixed keys, nonces, AAD → expected ciphertexts, signatures, Shamir splits).
- TypeScript implementation `apps/web/src/lib/ccpp.ts` using audited `@noble/curves` (X25519, Ed25519) and `@noble/ciphers` (ChaCha20-Poly1305) and `@noble/hashes` (HKDF), pinned versions.

| # | Checkpoint | Proof |
|---|---|---|
| 1.1 | Spec and vectors published | Files reviewed |
| 1.2 | API, agent and SDK pass every vector | New pytest in each package |
| 1.3 | TypeScript passes every vector, and opens envelopes made by Python (and vice versa) | `vitest` run |
| 1.4 | A deliberate one-byte change in any vector makes every implementation fail | Mutation test output |

### WP2 — Agent-side attestation (5–7 days)

- API: `POST /v1/agents/attestation/challenge` and `POST /v1/agents/attestation/verify`, HMAC machine-signed like the job routes, reusing `verify_machine_evidence`. Store the latest verdict and its log leaf index per machine (`machine_attestations.details`).
- Agent: `computecoin-agent attest` and an attestation loop inside `start`: generate keys in RAM, attest, re-attest at 2/3 of the verdict TTL, drop keys on exit. Keys never written to disk.

| # | Checkpoint | Proof |
|---|---|---|
| 2.1 | Agent obtains a verdict with no user login | Integration test with the real agent against the API |
| 2.2 | Private keys exist only in process memory | Test asserts no key bytes in the config dir; code review |
| 2.3 | Expired verdict → agent re-attests before claiming private work | Test with a short TTL |
| 2.4 | Replayed nonce, wrong machine signature, banned machine → refused | Tests |

### WP3 — Private job execution wired end to end (7–10 days)

- `POST /v1/agents/jobs/claim`: for a private job, load the machine's current verdict, call `release_guardian_shares` (local guardians) and the external guardian client (WP4), and return `encrypted_input`, `wrapped_shares`, `result_public_key`, `verdict_id`. Never return `input` or `dk_shares`.
- Agent `run_one`: if the claim is private, run `execute_sealed_job` (the existing executor inside it), compute the output commitment (L4), post `encrypted_output`, `wipe_receipt`, `output_commitment`; no plaintext output, no plaintext logs (log lines are dropped for private jobs).
- `POST /v1/agents/jobs/{id}/result`: for private jobs skip `verify_output(job.input, …)`; store `encrypted_output`; create the receipt (`create_privacy_receipt_for_job`, verdict leaf from WP2); billing unchanged (metered seconds).
- Failure paths: release refused → job requeued to another eligible machine or failed with a clear code; host failure → wipe still signed; no partial plaintext stored anywhere.

| # | Checkpoint | Proof |
|---|---|---|
| 3.1 | A private job runs from submit to decrypted result with the real agent | E2E-1 (section 5) |
| 3.2 | DB, logs, artifacts and API responses never contain the plaintext input or output | E2E-1 scans every table, log line and artifact for a marker string |
| 3.3 | Receipt exists, `job.privacy_receipt_id` set, `ccverify` passes | E2E-1 |
| 3.4 | Release refused (no verdict, expired, banned, wrong tier) → job not run, clear error, hold released | E2E-3 |
| 3.5 | Billing identical to an open job of the same runtime | Ledger comparison test |

### WP4 — External guardian service (8–12 days)

- New deployable `apps/guardian` (small FastAPI service, its own key, its own database or none). Endpoint `POST /release`: input = signed verdict, job id, the guardian's sealed share, and the platform's KEY_RELEASE log entry with an inclusion proof and a signed tree head.
- The guardian independently verifies: verdict signature (verifier public key pinned in its config), verdict current, image hash in **its own** copy of trusted images (pulled from the log and verified), the KEY_RELEASE entry is in the log, rate limits per job. Then opens its share and re-seals it to the host key.
- Platform client with timeouts and retries; customer-run guardian option documented (Docker image).

| # | Checkpoint | Proof |
|---|---|---|
| 4.1 | Production config: platform holds 1 key, partner guardian holds 1, k = 2; private job completes | E2E-1 run with the guardian in a separate container |
| 4.2 | Guardian refuses: forged verdict, unknown image, missing log entry, replay | Guardian test suite |
| 4.3 | With the guardian down, nothing is released and the job fails closed | E2E-3 |
| 4.4 | A platform database dump plus the platform's key cannot decrypt any job | Test: attempt reconstruction with only platform-side material |

### WP5 — Integrity for private jobs (3–4 days)

- Agent computes the output commitment (L4). Shadow re-runs for private jobs go to another attested host of the same tier; the API compares commitments only.
- Freivalds checks scheduled by the worker on sampled matrix workloads (when real inference exists).

| # | Checkpoint | Proof |
|---|---|---|
| 5.1 | Honest primary and shadow → commitments equal → passes | Test |
| 5.2 | Cheating primary (altered output) → mismatch → flagged | Test |
| 5.3 | The platform cannot learn a low-entropy output from the commitment | Test: brute force over 10⁶ candidates without DK fails |

### WP6 — Hardware attestation: Sealed tier on TPM 2.0 (3–5 weeks)

- Enrollment: read the TPM EK certificate; verify its chain against pinned TPM vendor roots (Intel PTT, AMD fTPM, Infineon, Nuvoton, STMicro); create an attestation key and bind it to the EK with `MakeCredential` / `ActivateCredential`.
- Attestation: `tpm2_quote` over the boot PCRs with the server nonce as qualifying data; send quote, signature, PCR values and the measured-boot event log; server verifies signature with the bound AK, replays the event log to the PCRs, and matches PCRs to a trusted image.
- Use an existing verifier (Keylime libraries or `tpm2-pytss`) rather than new crypto. Revocation list for TPM models with known flaws (TPM-Fail, faulTPM).
- Then switch `verify_machine_evidence` from "software attestation flag" to "verified TPM quote" for Sealed / Sealed Pro.

| # | Checkpoint | Proof |
|---|---|---|
| 6.1 | Genuine Intel and AMD consumer PCs pass | Lab run on ≥ 2 real PCs, recorded |
| 6.2 | A software TPM (swtpm) or a TPM without a vendor EK chain is refused | Test |
| 6.3 | Changing any boot component (kernel, initrd, cmdline) → PCR mismatch → no verdict | Lab run |
| 6.4 | Replay of an old quote (old nonce) refused | Test |
| 6.5 | `PRIVACY_SOFTWARE_ATTESTATION` no longer needed for Sealed in staging | Config diff + E2E-1 on hardware |

### WP7 — Hardware attestation: Confidential tier (3–4 weeks)

- Intel TDX quote verification (Intel DCAP libraries or Intel Trust Authority), AMD SEV-SNP report verification (AMD KDS VCEK chain), NVIDIA H100/H200/B200 GPU attestation (NVIDIA nvtrust local verifier or NRAS), all bound to the same nonce; measured CVM image registered as a trusted image.
- Test on rented confidential GPU VMs (Azure and Google Cloud offer them).

| # | Checkpoint | Proof |
|---|---|---|
| 7.1 | Real TDX or SNP + NVIDIA CC-mode VM gets a Confidential verdict | Recorded run on a rented VM |
| 7.2 | GPU not in CC mode, or CPU TEE debug mode on → refused | Test with captured reports |
| 7.3 | Report bound to another nonce → refused | Test |

### WP8 — Sealed OS image (6–10 weeks, parallel with WP6)

- Minimal Linux built with `mkosi` as a unified kernel image: dm-verity root, kernel lockdown (confidentiality), no login/SSH/console, IOMMU on, no swap, RAM-only encrypted scratch, NVIDIA driver signed into the image, agent baked in, IMA policy, bare-metal check, locked status screen.
- Reproducible build; reference PCRs computed at build time (`systemd-measure`); release pipeline registers the image hash in the transparency log (`POST /v1/privacy/trusted-images`).
- USB installer and update channel (new image = new trusted entry; old image revoked after a grace period).

| # | Checkpoint | Proof |
|---|---|---|
| 8.1 | Two independent builds produce the same image hash | CI job |
| 8.2 | No interactive access: SSH, console, keyboard shortcuts, serial all refused | Lab checklist |
| 8.3 | `/dev/mem`, kprobes, unsigned modules, hibernation blocked | Lab checklist |
| 8.4 | Red team with physical access, no lab hardware, cannot read a canary job | External test report |

### WP9 — Transparency log anchoring (3–4 days)

- Worker task every N minutes: `ComputeStakeVault.commitMerkleRoot(root, size)` from the anchor-signer key; store tx hash and block on the leaf; alert if anchoring falls behind.
- `ccverify --rpc <url> --vault <address>`: checks the receipt's root (or a later root with a consistency proof) is anchored.
- Add RFC 6962 consistency proofs (`/v1/privacy/transparency-log/consistency?from=&to=`).

| # | Checkpoint | Proof |
|---|---|---|
| 9.1 | Root anchored on BSC testnet within N minutes of a receipt | E2E-1 on testnet |
| 9.2 | `ccverify` fails if the log was rewritten after anchoring | Test: rewrite a leaf, run ccverify |
| 9.3 | Consistency proof between any two anchored sizes verifies | Test |

### WP10 — Stake and slashing, end to end (5–7 days)

- Chain reconciler reads `StakeDeposited`, `UnbondingRequested`, `MachineSlashed`, `SlashProposed/Cancelled` and updates `stake_bonded` and bans; the admin "record stake" endpoint becomes read-only reconciliation.
- Machine id on-chain = `keccak256(machine UUID bytes)`; registration via the owner multisig.
- Canary scheduler (random, per machine, rate-limited) and leak watchers (public paste/code search for canary markers; honeytoken API keys that alert on use) → `report_canary_leak` → `proposeSlash` from the slasher key.

| # | Checkpoint | Proof |
|---|---|---|
| 10.1 | Deposit on testnet → DB stake updated → machine eligible | E2E-4 |
| 10.2 | Planted canary leak → ban → proposeSlash → delay → executeSlash → insurance pool paid | E2E-4 on testnet |
| 10.3 | Appeal → owner cancels → stake unfrozen | E2E-4 |

### WP11 — Customer experience (4–6 days)

- SDK: `client.submit_private_job(...)`, `client.get_private_result(job_id, private_key)`, `client.get_receipt(job_id)`, `client.verify_receipt(...)`.
- Website: private tiers enabled using `ccpp.ts` (WP1); the customer's key stays in the browser (download/backup flow, never uploaded); receipt card with in-browser verification.

| # | Checkpoint | Proof |
|---|---|---|
| 11.1 | A private job from the website completes and the result decrypts in the browser | E2E-1 (browser variant) |
| 11.2 | The private key never leaves the browser | Network capture during the flow |

### WP12 — Assurance and launch (4–8 weeks, overlapping)

| # | Checkpoint | Proof |
|---|---|---|
| 12.1 | External audit of ComputeStakeVault; all findings fixed and re-checked | Audit report |
| 12.2 | Cryptography review of ccpp-v1, guardians, receipts | Review report |
| 12.3 | Penetration test of API, agent, guardian, Sealed OS | Test report |
| 12.4 | Public bug bounty live | Programme page |
| 12.5 | Marketing text matches the passed gates only | Sign-off against ADR-0005 section 8 |

---

## 4B. Security hardening (beyond ADR-0005)

These measures go beyond the base design. Each one closes a specific attack; the "Stops" column says
which. Effort is an estimate. Together they are the basis of the selling line *"post-quantum, no
single point of trust, verifiable by the customer"*, which may be used only after gate G2 or G3.

| WP | Measure | Stops |
|---|---|---|
| WP13 | Post-quantum hybrid crypto | Data recorded today and decrypted by a future quantum computer |
| WP14 | Verifier quorum (k-of-n verdict signatures) | One stolen or corrupted verifier key issuing fake "safe host" verdicts |
| WP15 | All platform keys in HSMs | A server breach stealing signing or guardian keys |
| WP16 | Guardians inside TEEs, across clouds and jurisdictions | A guardian operator or its cloud provider reading shares |
| WP17 | Customer approval per release (optional) | Any key release the customer did not expect |
| WP18 | Fresh VM per job, no shared cores | Data left over between jobs; cross-job side channels |
| WP19 | One-job keys and verdicts | A stolen key or verdict reused for other jobs |
| WP20 | Continuous attestation during the job | Tampering after the job started |
| WP21 | Independent log witnesses | The platform showing different logs to different customers |
| WP22 | Reproducible builds, provenance, k-of-n signed releases | A malicious insider or hacked build server shipping a backdoor |
| WP23 | Mutual TLS with pinned keys; no network for private jobs | Interception; a job sending data out |
| WP24 | Crypto-shredding at end of retention | Old encrypted data leaking years later |
| WP25 | Formal verification of protocol and contract | Design flaws that tests do not find |
| WP26 | Continuous fuzzing and invariant testing | Edge-case bugs |
| WP27 | Anomaly detection on key releases | Large-scale abuse while it is happening |

### WP13 — Post-quantum hybrid crypto, ccpp-v2 (2–3 weeks; after WP1)

- Sealing: X25519 **and** ML-KEM-768 (FIPS 203); both shared secrets go into HKDF, so breaking one is not enough. Signatures on verdicts, receipts and wipe receipts: Ed25519 **and** ML-DSA-65 (FIPS 204); a verifier requires both.
- Libraries: the Python `cryptography` package once it ships ML-KEM / ML-DSA, otherwise `liboqs-python`; `@noble/post-quantum` in the browser. ccpp-v1 stays readable during migration; new jobs use v2.

| # | Checkpoint | Proof |
|---|---|---|
| 13.1 | ccpp-v2 spec and test vectors published; all four implementations pass | Vector tests |
| 13.2 | Breaking only the classical half (simulated by giving the attacker the X25519 secret) does not open an envelope | Test |
| 13.3 | A receipt with a valid Ed25519 but missing or bad ML-DSA signature is rejected by ccverify | Test |
| 13.4 | Size and speed measured and acceptable (envelope overhead, job submit time) | Benchmark report |

### WP14 — Verifier quorum (2–3 weeks; after WP2)

- A verdict is valid only with **2-of-3** signatures from independent verifiers: ComputeCoin, a partner verifier, and an external attestation service where available (for TEEs, Intel Trust Authority or the cloud provider's verifier). Each runs the full evidence check itself.
- Verdict format carries a list of signatures; `ccverify`, the guardians and the API require the quorum. FROST threshold Ed25519 (RFC 9591) is an option if one combined signature is preferred.

| # | Checkpoint | Proof |
|---|---|---|
| 14.1 | One verifier alone cannot produce an accepted verdict | Test with one valid signature |
| 14.2 | A verifier that disagrees blocks the verdict | Test: partner verifier rejects an image the platform accepts |
| 14.3 | Verifier keys listed in the transparency log; rotation logged | Log entries |

### WP15 — Keys in HSMs (1–2 weeks plus hardware cost)

- Verifier, guardian, anchor-signer and slasher keys generated and used inside HSMs (cloud HSM or YubiHSM 2 class devices); keys are non-exportable. Signing and unsealing run through the HSM API.

| # | Checkpoint | Proof |
|---|---|---|
| 15.1 | No private key exists in any config file, env var, image or backup | Repository and image scan, config review |
| 15.2 | Root access to the API server does not reveal a key | Pen-test item |
| 15.3 | HSM unavailable → releases and verdicts stop (fail closed) | E2E-3 variant |

### WP16 — Guardians inside TEEs, spread out (3–4 weeks; after WP4)

- The guardian service runs in a confidential VM (AMD SEV-SNP or Intel TDX); its own attestation is published, so anyone can check that the running guardian is the published open-source build. Guardians run on different clouds, owned by different organisations, in different jurisdictions.

| # | Checkpoint | Proof |
|---|---|---|
| 16.1 | Guardian attestation published and checkable by a customer tool | ccverify `--guardians` check |
| 16.2 | The cloud operator of a guardian cannot read its key (key sealed to the TEE) | Design review + pen-test item |
| 16.3 | No two guardians share a cloud account, organisation or country | Deployment record |

### WP17 — Customer approval per release (2 weeks; after WP4)

- Optional for enterprise customers: their own guardian asks for approval (app push or API webhook) showing job, host, tier and image before releasing its share. Auto-approve rules allowed (for example "Confidential tier, these regions").

| # | Checkpoint | Proof |
|---|---|---|
| 17.1 | Without approval the job does not run and nothing is released | E2E-1 variant |
| 17.2 | Approval request shows exactly what the verdict says | UI test against a verdict |

### WP18 — Fresh VM per job, no shared cores (3–4 weeks; inside WP8)

- Each private job runs in a new micro-VM inside the Sealed OS, destroyed after the job; host RAM pages scrubbed; GPU reset between jobs; SMT (hyper-threading) off on Sealed hosts; one private tenant per GPU.

| # | Checkpoint | Proof |
|---|---|---|
| 18.1 | A second job cannot find any byte of the previous job's data in RAM, disk or GPU memory | Lab test with marker data |
| 18.2 | SMT off and one tenant per GPU are part of the attested state | Verdict field + test |

### WP19 — One-job keys and verdicts (1 week; with WP3)

- The attestation nonce includes the job id; the verdict names the job; host keys are generated per job and erased after it; guardians refuse a verdict for another job.

| # | Checkpoint | Proof |
|---|---|---|
| 19.1 | A verdict for job A cannot release keys for job B | Test |
| 19.2 | Host keys differ for every job | Test across two jobs |

### WP20 — Continuous attestation (2 weeks; after WP6/WP7)

- Fresh quote every few minutes during a private job plus the runtime measurement log (IMA); any change, missed quote or new kernel module kills the job, revokes its keys, and writes TAMPER_EVIDENCE.

| # | Checkpoint | Proof |
|---|---|---|
| 20.1 | Loading a module mid-job stops the job within one interval | Lab test |
| 20.2 | A host that stops sending quotes is stopped and flagged | Test |

### WP21 — Independent log witnesses (2 weeks; after WP9)

- External witnesses co-sign each log checkpoint (the C2SP tlog-witness / cosignature approach used by public transparency logs). Receipts carry the witness cosignatures; ccverify requires a quorum of them.

| # | Checkpoint | Proof |
|---|---|---|
| 21.1 | Showing two different logs to two customers is detected | Split-view test |
| 21.2 | ccverify rejects a checkpoint without enough witness cosignatures | Test |

### WP22 — Reproducible builds, provenance, multi-signed releases (3–4 weeks)

- Agent, guardian, API and Sealed OS builds are reproducible; SLSA Build Level 3 provenance; artifacts signed with Sigstore; updates distributed with The Update Framework (TUF) so a release needs k-of-n release keys; every release hash goes into the transparency log.

| # | Checkpoint | Proof |
|---|---|---|
| 22.1 | Two independent builders produce identical artifacts | CI job |
| 22.2 | A release signed by only one key is refused by the updater | Test |
| 22.3 | Every running binary's hash is in the log | Attestation + log check |

### WP23 — Mutual TLS, pinned keys, no network for private jobs (1–2 weeks)

- mTLS with pinned keys between agent, API and guardians; the private job VM has no network interface at all (inputs and outputs pass through the agent only).

| # | Checkpoint | Proof |
|---|---|---|
| 23.1 | A man-in-the-middle with a valid public CA certificate is rejected | Test |
| 23.2 | A private job trying to open any socket fails | Lab test |

### WP24 — Crypto-shredding (1 week)

- Stored encrypted inputs and outputs are deleted after the retention period, and the guardians destroy their per-job sealed shares, so even a later backup cannot be decrypted. Logged as a SHRED entry.

| # | Checkpoint | Proof |
|---|---|---|
| 24.1 | After retention, no combination of platform and guardian material decrypts the job | Test |

### WP25 — Formal verification (4–6 weeks with a specialist)

- Key-release protocol (customer, guardians, verifier quorum, host) modelled in ProVerif or Tamarin with the secrecy and authentication properties stated in ADR-0005; ComputeStakeVault invariants proven with Certora or Halmos (token conservation, no stuck tokens, slash only after delay).

| # | Checkpoint | Proof |
|---|---|---|
| 25.1 | Data Key secrecy proven against a host-controlled network and a single corrupted guardian | Model + tool output |
| 25.2 | Vault invariants proven | Prover report |

### WP26 — Continuous fuzzing and invariant testing (1–2 weeks, then ongoing)

- Echidna or Foundry invariant tests on the vault; property-based tests (Hypothesis) on envelope parsing, Merkle proofs and policy checks; fuzzers in CI and nightly.

| # | Checkpoint | Proof |
|---|---|---|
| 26.1 | Fuzzers run nightly with no open crash | CI dashboard |
| 26.2 | Every past bug from the 2026-10-27 review has a regression property | Test list |

### WP27 — Anomaly detection on key releases (1–2 weeks)

- Alert and automatic pause when releases spike, come from unusual regions or images, or cluster on few hosts; every pause logged.

| # | Checkpoint | Proof |
|---|---|---|
| 27.1 | A burst of releases above threshold pauses releases and alerts | Test |
| 27.2 | False-positive rate measured on staging traffic | Report |

### What hardening cannot change (state this to customers)

- Gaming PCs without memory encryption remain open to physical lab attacks on RAM; only Sealed Pro (with memory encryption) and Confidential close that.
- Flaws in Intel, AMD, NVIDIA or TPM hardware happen; the defence is fast revocation of affected models, not prevention.
- A customer's own model can leak data in its answers; infrastructure cannot prevent that.
- "World's best" cannot be self-declared; it is earned through audits, a public bug bounty and time in production.

---

## 5. End-to-end verification suite (the readiness test)

Automated in `tests/e2e/proof_of_privacy_e2e.py`, runnable against a local stack (software attestation)
and against staging with real hardware. Every scenario writes a pass/fail line; readiness requires all.

| ID | Scenario | Passes when |
|---|---|---|
| **E2E-1 Golden path** | Customer submits a private job with the SDK → host attests → claims → guardians release (one local, one external) → sealed run → encrypted result → receipt → anchoring | Customer decrypts the correct result; `ccverify --rpc` passes; marker string absent from every DB table, log, artifact and API response; billed correctly |
| **E2E-2 Host is hostile** | Host tries: claim without attestation; replay an old verdict; boot a modified image; run in a VM; read the share with the public key; reuse a share on another job | Every attempt refused; no key material released; events in the transparency log |
| **E2E-3 Something is down** | Guardian down; verdict expires mid-queue; host crashes mid-job; anchoring delayed | Nothing decrypts anywhere it should not; job requeued or failed with a clear code; holds released; wipe receipt still signed where the host survived |
| **E2E-4 Economics** | Stake deposit; canary leak; slash propose/execute; appeal/cancel | Balances and bans match on chain and in DB; insurance pool paid; no tokens stuck |
| **E2E-5 Platform is hostile** | Using only the platform's DB and keys, try to decrypt a completed job; try to issue a receipt without a log entry; try to rewrite the log after anchoring | All fail; ccverify detects the rewrite |
| **E2E-6 Integrity** | Shadow re-run with honest and with cheating primary | Honest passes, cheater flagged, platform never sees output |
| **E2E-7 Compatibility** | Envelopes made in the browser open in the agent and vice versa | All vectors pass in every implementation |
| **E2E-8 No single point of trust** | One at a time: compromise one verifier, one guardian, one release key, one witness, the platform's root account | No single compromise produces a verdict, a key release, an accepted release or an undetected log rewrite |
| **E2E-9 Post-quantum** | Give the attacker every classical private key involved in a finished job | Data still cannot be decrypted; receipts without the post-quantum signature are rejected |
| **E2E-10 Job isolation** | Run job A with marker data, then job B on the same host trying to find it | No marker found in RAM, disk, GPU memory or network |
| **E2E-11 Runtime tamper** | Change the host mid-job (load a module, stop quoting) | Job stopped, keys revoked, tamper logged within one interval |

---

## 6. Order and timeline (estimate)

```
Weeks:        1    2    3    4    5    6    7    8    9   10   11   12   13   14   15   16
WP1 vectors  ███
WP2 agent att     ████
WP3 wiring           ███████
WP4 guardian          ████████
WP5 integrity                 ███
WP9 anchoring                    ███
WP10 economics                      █████
WP11 customer                          █████
WP6 TPM              ██████████████████
WP7 TEE                         ██████████████
WP8 Sealed OS          ████████████████████████████████
WP12 assurance                                       ███████████████████████
```

Hardening (section 4B) runs alongside, attached to the package it builds on:

```
Weeks:        1    2    3    4    5    6    7    8    9   10   11   12   13   14   15   16   17   18   19   20
WP13 PQ          ██████                                 (after WP1)
WP19 1-job keys           ██                            (with WP3)
WP23 mTLS                   ███
WP15 HSM                      ███
WP14 quorum                     ██████                  (after WP2)
WP16 TEE guard                      ████████            (after WP4)
WP17 approval                               ████
WP21 witnesses                                 ████     (after WP9)
WP22 builds          ████████
WP24 shredding                                     ██
WP27 anomaly                                         ███
WP26 fuzzing        ███ (then nightly)
WP25 formal                         ████████████
WP18 fresh VM                          ████████         (inside WP8)
WP20 live att.                                              ████   (after WP6/WP7)
```

With hardening the plan is about **5–6 months for 4–5 engineers** (adding a cryptography engineer and
formal-verification support), plus assurance at the end.

Software path (WP1–5, 9–11) ≈ 8–10 weeks for 2 engineers. Hardware path (WP6–8) ≈ 10–14 weeks in
parallel and needs a boot-security engineer and a GPU/TEE engineer. Assurance adds 4–8 weeks at the end.

---

## 7. Readiness gates

| Gate | Requires | Then you may |
|---|---|---|
| **G0 Dev complete** | WP1–5, WP9–11, WP19, WP23, WP26 checkpoints; E2E-1…7 pass with software attestation on the local stack | Demo end to end internally; no customer claims |
| **G1 Testnet / private beta** | G0 + WP13, WP15, WP22 + E2E suite on staging with anchoring and staking on BSC testnet + external guardian running separately | Invite beta customers with **test** data only; clearly labelled "beta, attestation simulated" |
| **G2 Confidential tier live** | G1 + WP7 + WP14, WP16, WP21, WP24, WP27 + WP12.2–12.3 + E2E-8, E2E-9 | Sell **Confidential** with the ADR-0005 claims and "post-quantum, no single point of trust" |
| **G3 Sealed tier live** | G2's hardening + WP6 + WP8 + WP18 + WP20 + red-team report (8.4) + WP12 complete for the Sealed path + E2E-10, E2E-11 | Sell **Sealed / Sealed Pro** with the ADR-0005 claims |
| **G4 Mainnet economics** | G3 + vault audit (12.1) + legal review + multisig owner | Real COMPUTE staking and slashing on mainnet |

---

## 8. Decisions needed from the owner

1. Who runs the external guardian (partner firm, separate cloud account, or customer-run only)?
2. Which tier ships first: Confidential (faster, rented hardware) or Sealed (our differentiator, longer)?
3. Budget for: rented confidential GPU VMs, a TPM test lab (≥ 4 PCs, Intel and AMD), external audit, crypto review, penetration test, bug bounty.
4. Hiring: boot-security engineer (WP6, WP8) and GPU/TEE engineer (WP7).
5. Canary rate and the leak-watch sources to monitor.
6. Who the partner verifier is for the 2-of-3 verifier quorum (WP14), and who runs the log witnesses (WP21).
7. HSM choice and budget (cloud HSM or own devices) for WP15.
8. Which enterprise customers get per-release approval (WP17) first.
9. Budget for a formal-verification specialist (WP25) and a cryptography engineer (WP13).
