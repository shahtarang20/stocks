# ADR-0005: Proof-of-Privacy, protecting customer work from the host

Status: **partly built (2026-10-26).** Built and tested: ccpp-v1 crypto (X25519 / Ed25519 /
ChaCha20-Poly1305), customer-side encryption in the Python SDK, guardian-sealed Data Key shares
with log-before-release, Ed25519 verdicts, RFC 6962 transparency log, Privacy Receipts and
`ccverify`, canary jobs, the ComputeStakeVault contract (unaudited), tier routing.
**Not built:** real TPM / TDX / SEV-SNP / NVIDIA attestation (verdicts are issued only in dev/test),
the external guardian service, the Sealed OS image, wiring key release and receipts into the job
runner, on-chain anchoring, and syncing DB stake with the vault. Owner approval is still needed
for the decisions in section 12.
Date: 2026-10-26. Supersedes the "Residual risk: provider-side confidentiality" note in
[ADR-0003](ADR-0003-isolation.md).

## 1. The problem, stated plainly

A ComputeCoin job runs on a stranger's computer. Today ([ADR-0003](ADR-0003-isolation.md)) the job is locked
in a container so it cannot hurt the host, but **the host can read the job**: its input, model, output, RAM and
GPU memory. The host is root on the machine; a container does not stop root.

Every community-GPU marketplace we reviewed has the same limit (Vast.ai marketplace hosts, RunPod Community
Cloud, Salad consumer nodes). They answer it in one of three ways: **trust** (only vetted datacentres, contracts,
certifications), **anti-tamper** (a VM plus snoop detection on consumer PCs), or **confidential-computing
hardware** (Phala, io.net, Akash: only on H100-class servers). Each covers one slice of the market.

## 2. The goal

> Any customer can run private work on any ComputeCoin host and receive **cryptographic proof** of what
> protected it, from a choice of tiers whose guarantees are stated exactly, with no tier claiming more than
> it delivers.

Three properties make this different from what we found offered today:

1. **Private work on ordinary gaming PCs**, not only H100 servers, through a locked, remotely attested host
   OS ("Sealed"), backed by money at stake.
2. **No single party can unlock the data**, ComputeCoin included: keys are released by independent guardians
   only to a machine that has just proven its state.
3. **Every job returns a Privacy Receipt** that the customer can check with an open-source tool, against a
   public transparency log anchored on-chain, without trusting ComputeCoin.

We call the whole system **Proof-of-Privacy**.

## 3. The physics we cannot change (and do not claim to)

- A GPU must see data in the clear to compute on it. Computing on encrypted data (fully homomorphic
  encryption) is roughly 10,000 to 1,000,000 times slower: not usable for AI today.
- On hardware without memory encryption, someone with physical access and lab equipment (RAM freezing,
  bus probes) can in principle read memory. Software cannot prevent that; only hardware memory encryption can.

So the design does two things: it makes **software** access impossible and **provable** everywhere, and it adds
**hardware** memory encryption wherever the hardware has it, with money at stake to deter the rest.

## 4. The tiers

| Tier | Runs on | Who can read the job | Claim we are allowed to make |
|---|---|---|---|
| **Open** | Any host (today's container) | The host | "Not for private data." |
| **Sealed** | Any modern PC with TPM 2.0 and an NVIDIA GPU | Nobody via software. A physical lab attack on RAM or GPU memory remains possible | "No one, including the host, can access your job through software. Any change to the host system is detected before your data is released. The host has COMPUTE at stake that pays you if a leak is proven." |
| **Sealed Pro** | Sealed, plus CPU memory encryption on (AMD SME/Memory Guard or Intel TME), a KYC-verified host and a larger stake | Nobody via software; RAM is hardware-encrypted, so cold-boot and DRAM bus probing fail. GPU memory is still unencrypted on consumer cards (a much harder physical attack) | As Sealed, plus "System memory is hardware-encrypted." |
| **Confidential** | NVIDIA H100, H200 or B200 in confidential-computing mode, with Intel TDX or AMD SEV-SNP | Nobody, not the host and not ComputeCoin; CPU and GPU memory are hardware-encrypted | "Hardware-encrypted end to end, attested by Intel or AMD and by NVIDIA." |

Rules:

- The customer picks a minimum tier. The scheduler **never** places a job below it (the existing "refuse, never
  downgrade" rule in ADR-0003 extends to these tiers).
- A customer **data policy** (classification, allowed regions, allowed host types) is checked at submit time.
- The **value at risk** on a Sealed host is capped by its stake: a job whose declared sensitivity exceeds the
  host's stake is not placed there.

## 5. Architecture

```
 CUSTOMER                         COMPUTECOIN CONTROL PLANE                         HOST MACHINE
 ────────                         ─────────────────────────                         ────────────
 SDK / web / CLI                  API + Scheduler + Policy engine                    Sealed OS (signed, read-only,
 ① HPKE-encrypt inputs with a     (sees only ciphertext + metadata)                   no login) or Confidential VM
   one-time Data Key (DK)                     │                                      ┌──────────────────────────┐
 ② split DK into shares for     ③ place job on a host of the required tier ──────▶ │ Agent + GPU driver + job │
   k-of-n Key Guardians                       │                                      │                          │
                                  Attestation Verifier ◀── ④ fresh evidence ──────── │ TPM quote (Sealed) or    │
                                  - genuine TPM / TDX / SNP / NVIDIA CC              │ TDX/SNP + NVIDIA report  │
                                  - measurements == published image                  │ (Confidential)           │
                                  - fresh nonce, policy checks                       │                          │
                                              │ signed verdict                       │                          │
                                  Key Guardians (k-of-n, independent)                │                          │
                                  ComputeCoin · independent partner · customer(opt.) │                          │
                                  each re-checks the verdict, releases its share ──▶ │ ⑤ DK rebuilt in RAM only │
                                  wrapped to the host's in-RAM ephemeral key         │ ⑥ decrypt → GPU → result │
                                              │                                      │ ⑦ HPKE-encrypt result to │
                                  Transparency Log (append-only, Merkle)  ◀── ⑧ ──── │   the customer's key     │
                                  image hashes · attestations · key releases ·       │ ⑧ wipe RAM, VRAM, scratch│
                                  wipe receipts; root hash anchored on BSC           └──────────────────────────┘
 ⑨ Privacy Receipt verified
   offline with the open-source   Stake Vault (COMPUTE) · Canary Service · Insurance Pool
   `ccverify` tool                slashing on proven leak or tamper evidence
```

### 5.1 Customer side

- The SDK, web app and CLI encrypt inputs **before upload** using HPKE (RFC 9180) with a fresh per-job Data Key.
  The platform stores and moves only ciphertext.
- The customer holds a long-term key pair. Results come back encrypted to it, so only the customer can read them.
- **Bring your own guardian**: an enterprise customer can run one Key Guardian themselves, so no release
  happens without their own system agreeing.

### 5.2 The Sealed host OS (consumer PCs)

The host boots a dedicated, read-only ComputeCoin OS from a USB stick or a second disk while hosting, the way
mining rigs boot a mining OS. Their normal Windows install is untouched.

| Lock | Purpose |
|---|---|
| Unified kernel image; every boot stage measured into the TPM (PCRs) | Any change to firmware, bootloader, kernel or OS is visible in the attestation |
| Root filesystem with dm-verity (hash-verified, read-only) | No modified binary can run |
| Secure Boot with our key where the host can enroll it (optional; see below) | Refuses unsigned code at boot, on top of measurement |
| No SSH, no console login, no shell, no root password; keyboard and virtual consoles disabled | Nobody can get into the running system |
| Kernel lockdown (confidentiality mode): no `/dev/mem`, kprobes, unsigned modules or hibernation | No reading memory through the kernel |
| IOMMU on and verified; Thunderbolt and external-PCIe DMA restricted | No memory theft through plugged-in devices |
| No swap; scratch storage encrypted with a key held only in RAM | Nothing readable on disk after power-off |
| GPU profiling and debug off; GPU memory cleared before and after each job | No reading another job's leftovers in VRAM |
| Bare metal only: refuses to run inside a hypervisor (checked and attested) | No spying from a virtual machine underneath |
| Local monitor shows only a locked status screen; interactive sessions stream end-to-end encrypted to the customer | Nothing private is ever drawn on the host's screen |
| Runtime measurement (Linux IMA) streamed to the verifier during long jobs | Detects changes after boot |
| Reproducible build; the image hash is published in the transparency log | Anyone can rebuild the image from source and get the same hash |

Measured boot does not require the host to enroll our Secure Boot key: a modified boot chain produces different
measurements, and no key is released to it. Secure Boot is an extra lock where the host can enable it. Shipping
with Secure Boot on by default needs a Microsoft-signed shim (the public shim-review process) or host-side key
enrollment.

### 5.3 The Confidential VM (H100 / H200 / B200 hosts)

- The job runs in an Intel TDX or AMD SEV-SNP confidential VM with the GPU in NVIDIA confidential-computing mode.
- Evidence is composite: the CPU TEE report (checked against Intel or AMD certificate chains) plus the NVIDIA
  GPU report (checked with NVIDIA's attestation service or local verifier).
- The VM image is the same reproducible, measured image as Sealed, so one build pipeline serves both tiers.

### 5.4 Attestation Verifier

- **Sealed:** checks that the TPM is genuine (endorsement-key certificate from the TPM vendor), binds the
  attestation key to it (`MakeCredential` / `ActivateCredential`), verifies a quote over the boot PCRs with a
  fresh nonce, and compares against the reference values published for the image. It also checks the IMA log
  and the reported security state (IOMMU, lockdown, memory encryption, bare metal).
- **Confidential:** checks the TDX or SNP report and the NVIDIA report, both bound to the same nonce.
- Output: a signed, short-lived verdict naming the machine, the image hash, the tier reached and an ephemeral
  public key generated inside the machine's RAM. Built on an existing open-source TPM attestation framework
  (Keylime or equivalent) rather than written from scratch.

### 5.5 Key Guardians (threshold key release)

- The Data Key is split into n shares (Shamir); any k reconstruct it. Default **2-of-3**: ComputeCoin, an
  independent partner (for example an auditing firm or a separate cloud account), and optionally the customer.
- Each guardian independently re-verifies the verdict and the job's policy, then releases its share **wrapped to
  the machine's ephemeral key**, so only that RAM can open it.
- Every release is written to the transparency log **before** the share is sent. An unlogged release is
  impossible by construction, so a secret release cannot happen silently.

### 5.6 Transparency Log and Privacy Receipt

- Append-only Merkle log (the same design as Certificate Transparency or Sigstore Rekor) of approved image hashes,
  attestation verdicts, key releases and wipe confirmations. Its root hash is anchored on BNB Smart Chain at a
  fixed interval.
- Each job returns a **Privacy Receipt**: tier reached, machine identity, image hash, attestation evidence,
  log inclusion proofs, and the wipe confirmation signed inside the sealed environment.
- `ccverify` (open source) checks a receipt offline: signatures, certificate chains, log inclusion and the
  on-chain anchor. A customer, auditor or regulator can verify a job without trusting ComputeCoin.

### 5.7 Economic security (real utility for COMPUTE)

| Mechanism | How |
|---|---|
| **Stake Vault** | Sealed hosts bond COMPUTE. Sealed Pro needs a larger bond. A job's value at risk is capped by the bond |
| **Canary jobs** | The platform sends indistinguishable "private" jobs seeded with unique traceable markers (fake credentials, watermarked text and data). Any marker seen outside, such as a fake credential used or a watermark found, is proof of a leak |
| **Tamper evidence** | Failed or inconsistent attestation, attempts to boot a modified image, or a VM detected under the OS are recorded with the evidence |
| **Slashing** | Proven leak or tamper: stake slashed, the affected customer compensated first, host banned. Decided by published rules with an appeal window, executed by contract |
| **Insurance pool** | A share of Sealed and Confidential fees covers claims beyond a host's stake |

### 5.8 Integrity (that the work was really done)

Kept from today: shadow re-runs and known-answer challenges. Added: randomized algebraic spot checks on matrix
multiplications (Freivalds' algorithm), which verify a GPU's arithmetic far more cheaply than recomputing it.

## 6. Research track: Sandwich execution (experimental, not sold until proven)

For large models on consumer hardware: the sensitive first and last stages (input embedding and output head)
run in a Confidential VM; the bulk middle layers run on Sealed hosts that receive only hidden states, transformed
by a secret orthogonal rotation absorbed into the weights (the "computational invariance" used by model-compression
research). Data never appears in readable form on consumer hardware.

Honest limit: this is **obfuscation, not encryption**. If the base model's weights are public, the rotation can
be estimated (orthogonal Procrustes), so the protection weakens. It ships only after an external cryptographic
review, and never under a tier name with a guarantee attached.

## 7. Threat model

| Attack | Open | Sealed | Sealed Pro | Confidential |
|---|---|---|---|---|
| Host runs a tool, shell or debugger to read the job | ❌ | ✅ blocked | ✅ | ✅ |
| Host modifies the OS, driver or agent | ❌ | ✅ detected, no key | ✅ | ✅ |
| Host runs our OS inside a spying VM | ❌ | ✅ detected | ✅ | ✅ |
| DMA through Thunderbolt or PCIe devices | ❌ | ✅ IOMMU | ✅ | ✅ |
| Cold-boot or DRAM bus probe | ❌ | ⚠️ possible (lab attack); deterred by stake and canaries | ✅ RAM encrypted | ✅ |
| Physical probing of GPU VRAM | ❌ | ⚠️ possible, very hard | ⚠️ possible, very hard | ✅ encrypted |
| A ComputeCoin insider reads data | ⚠️ | ✅ k-of-n guardians plus log | ✅ | ✅ |
| Secret key release without a record | ⚠️ | ✅ log-before-release | ✅ | ✅ |
| Firmware or TPM vulnerabilities (for example TPM-Fail, faulTPM) | n/a | ⚠️ patch, revoke affected models through reference values | ⚠️ | ⚠️ vendor TCB recovery |
| Side channels (timing, power) | ❌ | ⚠️ partly | ⚠️ partly | ⚠️ partly |
| Customer's own output leaks data (model memorisation) | Out of scope for all tiers | | | |

✅ prevented · ⚠️ residual, mitigated · ❌ not protected

## 8. Marketing guardrails

May say: "Proof-of-Privacy", "verifiable privacy receipt for every job", "no one can access your job through
the host's software" (Sealed), "hardware-encrypted end to end" (Confidential), "ComputeCoin cannot read your data"
(with k-of-n guardians).

Must not say: "unhackable", "military-grade", "fully encrypted" for Sealed, or "nobody can ever access your data"
for any tier below Confidential. Every claim must map to a row in section 7.

## 9. What exists today and what is reused

| Piece | Today | Plan |
|---|---|---|
| Isolation tiers and "never downgrade" | Built (agent and scheduler) | Extend with Sealed, Sealed Pro and Confidential |
| `attestation.py` | Software placeholder | Replace with real TPM, TDX, SNP and NVIDIA evidence |
| Signed agent messages, machine identity | Built | Bind to the TPM attestation key |
| Signed short-lived artifact grants | Built | Carry ciphertext only |
| Shadow re-runs, challenges, reputation | Built | Add canary jobs and Freivalds checks |
| COMPUTE token, timelock, vesting | Built, unaudited | New Stake Vault contract (**new audit required**) |
| Ledger and settlement | Built | Unchanged; slashing payouts go through the existing ledger rules |

## 10. Build plan

Effort is an estimate, not measured.

| Phase | Deliverable | Effort | Depends on |
|---|---|---|---|
| **0** (launch) | Paid private work only on contracted trusted hosts; Terms forbid inspection; Open-tier warning in the UI | Days | — |
| **1** Core | Customer-side HPKE encryption (SDK, web, CLI); Attestation Verifier service; tier and policy routing; encrypted results | 5–7 weeks | — |
| **2** Guardians and log | k-of-n Key Guardians; transparency log with on-chain anchor; Privacy Receipt; `ccverify` | 4–6 weeks | 1 |
| **3** Confidential | TDX/SNP plus NVIDIA CC attestation; confidential VM image; tested on rented confidential H100 VMs | 3–5 weeks | 1 |
| **4** Sealed OS | Reproducible measured image, lockdown, no login, IOMMU and bare-metal checks, IMA, USB installer, host onboarding and remote update | 8–12 weeks | 1 |
| **5** Sealed Pro | Memory-encryption detection and attestation; KYC flow; larger stake tier | 2–3 weeks | 4, 6 |
| **6** Economics | Stake Vault contract, slashing rules, canary service, insurance pool | 5–7 weeks + external audit | 2 |
| **7** Assurance | External penetration test of the Sealed OS and guardians; cryptography review; public bug bounty | 4–6 weeks, then ongoing | 2, 4 |
| **R** Research | Sandwich execution prototype and review | Parallel, no ship date | 3, 4 |

Phases 3 and 4 run in parallel after phase 1. Realistic total: **6–9 months with a team of 4–5**: a Linux
boot-security engineer, a cryptography and backend engineer, a GPU and driver engineer, a smart-contract engineer,
and security review support.

Budget items (amounts to be quoted): engineers; rented confidential H100 time; a hardware lab (several consumer
PCs with Intel and AMD fTPMs and different GPUs); an external penetration test; a cryptography review; a second
smart-contract audit; a bug bounty pool; a Microsoft shim review if Secure Boot ships on by default.

## 11. Proof it works (acceptance)

- A red team with root-equivalent physical access to a Sealed host, but no lab hardware, cannot obtain a canary
  job's plaintext. A modified image never receives a key.
- Every key release in a test month appears in the transparency log, and `ccverify` validates 100% of receipts.
- Confidential jobs verify against Intel or AMD and NVIDIA roots on rented hardware.
- Slashing executes end to end on testnet from a planted, detected canary leak.
- Each claim in section 8 has a passing test or test report behind it.

## 12. Decisions for the owner

1. Approve the four tiers and the marketing guardrails.
2. Who the independent Key Guardian is (a partner firm, a separate cloud account, or an open set of operators).
3. Stake sizes and the value-at-risk formula for Sealed and Sealed Pro.
4. Whether the Stake Vault uses COMPUTE (it changes the contracts, so a new audit is needed).
5. Budget and hiring for the team in section 10.
6. Phase 0 at launch: trusted hosts only for paid private work.
