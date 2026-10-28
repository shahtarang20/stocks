# ADR-0006: Universal Workloads — run (almost) anything on ComputeCoin GPUs

Status: **proposed, not built.** Date: 2026-10-27. Builds on [ADR-0003](ADR-0003-isolation.md)
(isolation) and [ADR-0005](ADR-0005-proof-of-privacy.md) (privacy tiers). Owner decisions in section 21.

---

## 1. The goal

Today a customer can run two kinds of job: `text-generation` and `batch-inference`
(`SUPPORTED_WORKLOADS` in `apps/api/app/services/scheduler.py`). Everything else is refused.

The goal: **a customer is never boxed in.** Rendering an image, rendering a film, transcoding video,
generating images or video with AI, fine-tuning a model, serving a model, running a notebook, running
science code, or running their own container — all on the same platform, priced the same way,
with the same safety, privacy and proof.

The design does this with **one universal job model** and three ways to use it:

| Way | For whom | Example |
|---|---|---|
| **Workload Kits** | Most users: pick a task, fill a form | "Render my Blender animation", "Generate 500 product images" |
| **Bring Your Own Container (BYOC)** | Developers with their own software | "Run my CUDA program", "Run my ComfyUI workflow" |
| **Sessions and Services** | Interactive and always-on work | Jupyter on a 4090, a private image-generation API endpoint |

---

## 2. What "anything" honestly means

| Can run | Cannot run (or not yet) |
|---|---|
| Any **Linux** program that runs in a container on an **NVIDIA** GPU (CUDA, OptiX, NVENC) or on CPU | **Windows-only** software (3ds Max, some renderers) until the optional Windows VM tier (WP-U14) |
| Batch jobs, many-part jobs (frames, shards), multi-step pipelines, interactive sessions, long-running endpoints | **macOS / Apple Metal** software |
| Single-host multi-GPU jobs | **Multi-node training across the internet** (too slow); only between hosts in the same datacenter |
| Commercial software when the **customer brings the licence** | Software whose licence forbids running on third-party hardware |
| Anything legal under the Acceptable Use Policy | Illegal or abusive work (section 15); real-time cloud gaming (latency) |

AMD GPUs follow the separate [AMD plan](../operations/amd-gpu-support-plan.md).

---

## 3. What exists today (verified in code, 2026-10-27)

| Capability | Today | Gap for this ADR |
|---|---|---|
| Job types | `text-generation`, `batch-inference` only | Any workload |
| Execution | Docker runner with pinned image digest, `--network none`, read-only, non-root, limits; **never run on a real GPU yet** | Validate on hardware first (prerequisite) |
| Input data | Datasets via signed upload, max **5 GiB** | Larger data, direct-to-storage, customer buckets |
| Output data | Artifacts via one-time pass, max **25 MiB** per file | GBs of frames and video |
| Many jobs at once | Batches (grouping with bulk discount) | Automatic splitting and merging (frames, shards) |
| Training | Training runs and per-step checkpoints (routes exist) | Real execution, resume on another host |
| Agent | Polls every 5 s over outbound HTTPS, signed requests | Input staging, caching, egress proxy, previews, sessions |
| Verification | Exact-match shadow re-runs, known-answer challenges | Per-workload methods (visual, statistical) |
| Isolation tiers | container, gVisor, microVM (+ privacy tiers from ADR-0005) | Defaults per workload source |

**Prerequisite for everything below:** real GPU execution validated end to end
([gpu-validation-runbook](../operations/gpu-validation-runbook.md)).

---

## 4. Locked principles

| ID | Principle |
|---|---|
| P1 | **One job model** for every workload. Kits, BYOC, sessions and services are all the same Job Spec v2 underneath; one scheduler, one ledger, one verification framework. |
| P2 | **Pinned and inspectable**: every container runs by digest; Kits are signed and scanned; BYOC images are scanned before scheduling. |
| P3 | **Untrusted by default**: BYOC runs in gVisor or microVM by default; no host mounts, no Docker socket, network off unless an allowlist is declared. |
| P4 | **Data moves directly** between customer, storage and host with short-lived, scoped links; the API never relays large files. |
| P5 | **Pay for what runs**: GPU-seconds remain the billing unit; per-frame / per-image / per-minute prices are a display of metered seconds, not a second pricing system. |
| P6 | **Every workload can be verified somehow**; the method is chosen per Kit and shown to the customer. |
| P7 | **Privacy tiers apply to every mode** (ADR-0005); no mode is "open only" by design. |
| P8 | **Fail safe**: a part that fails is retried elsewhere; finished parts are never redone or billed twice. |
| P9 | **The host stays outbound-only**: no open ports on the host, ever, including for interactive sessions. |
| P10 | **Backwards compatible**: v1 jobs (`text-generation`, `batch-inference`) keep working unchanged. |

---

## 5. Architecture overview

```
 ┌──────────────────────────────── INTERFACES ─────────────────────────────────┐
 │ Web (forms generated from Kit schemas, previews, galleries)  ·  CLI `cc`     │
 │ Python/TS SDKs  ·  REST API  ·  OpenAI-compatible endpoints (LLM, images)    │
 └───────────────────────────────────────┬─────────────────────────────────────┘
 ┌───────────────────────────── CONTROL PLANE (cloud) ─────────────────────────┐
 │ Job API v2 ── Kit Registry ── Planner (split / DAG) ── Scheduler v2          │
 │ Policy & AUP ── Pricing & quotes ── Verification engine ── Privacy (ADR-0005)│
 │ Ledger / holds / settlement (unchanged) ── Reputation & stake                │
 └───────────────┬───────────────────────────────────────────┬─────────────────┘
 ┌───────────────▼──────────── DATA PLANE ──────────┐   ┌─────▼──── SESSION RELAY ───┐
 │ Content-addressed object store (SHA-256 chunks)  │   │ Outbound tunnels from hosts │
 │ Presigned multipart upload/download (TB scale)   │   │ End-to-end encrypted streams│
 │ Customer buckets (S3/GCS/R2, scoped temp creds)  │   │ (Jupyter, VS Code, desktop) │
 │ Host cache (images, datasets by digest)          │   └─────────────┬───────────────┘
 └───────────────┬──────────────────────────────────┘                 │
 ┌───────────────▼──────────── EXECUTION PLANE (hosts) ───────────────▼────────────┐
 │ Agent v2: claim · stage inputs (read-only) · run · collect outputs · checkpoint │
 │           cache · egress proxy (allowlist, logged) · previews · tunnel          │
 │ Runtimes: container │ gVisor │ microVM │ Sealed OS │ Confidential VM            │
 │ GPUs: NVIDIA (CUDA/OptiX/NVENC) · multi-GPU on one host                         │
 └──────────────────────────────────────────────────────────────────────────────────┘
```

---

## 6. Job Spec v2 (the universal unit)

```yaml
version: 2
kit: blender-render@4.2          # or omit and give `image` for BYOC
image: registry.example/blender@sha256:...   # always a digest
command: ["render", "--scene", "/in/scene.blend", "--frames", "{frames}"]
mode: batch                      # batch | pipeline | session | service
inputs:
  - name: scene
    source: dataset:ds_123        # or bucket://customer-s3/path, or artifact:job_x/out
    mount: /in                    # read-only
outputs:
  - name: frames
    path: /out/frames/*.png       # collected after the run
    stream_previews: true
resources:
  gpu: { count: 1, min_vram_gb: 16, min_compute_capability: "8.0", nvenc: false }
  cpu: 8
  ram_gb: 32
  disk_gb: 200
parallelism:                     # optional fan-out
  split: { kind: range, field: frames, from: 1, to: 500, chunk: 25 }
  merge: { kit: ffmpeg-encode@6, args: ["--fps", "24", "--codec", "h264"] }
network: { egress: none }        # or { allow: ["license.vendor.com:5053"] }
checkpoint: { path: /ckpt, every_seconds: 600 }
retries: { max: 3, on: [host_lost, timeout] }
privacy: { tier: open }          # open | sealed | sealed_pro | confidential (ADR-0005)
verification: { method: kit_default }   # exact | perceptual | statistical | redundancy | none
budget: { max_cost: "120.00" }
deadline: 2026-11-01T18:00:00Z
```

Rules: Kits fill most fields and expose only their own inputs; BYOC gives `image` and `command`.
Every field is validated against a JSON Schema; unknown fields are refused.

---

## 7. Execution modes

| Mode | What it is | How it works | Example |
|---|---|---|---|
| **Batch** | One run, inputs in, outputs out | Today's flow plus input staging and large outputs | Upscale one video |
| **Fan-out / merge** | One job split into many parts on many hosts | Planner splits by range, items or shards; parts run in parallel; merge step combines; only failed parts re-run | 500-frame animation on 20 hosts, then encode |
| **Pipeline (DAG)** | Steps that feed each other | Outputs of one step become inputs of the next by artifact reference; steps may use different Kits and hardware | Render → denoise → upscale → encode |
| **Interactive session** | A live environment | Agent opens an outbound tunnel to the Session Relay; customer connects in the browser; stream encrypted end to end so the relay cannot read it; billed per second while open | Jupyter, VS Code, Blender desktop over WebRTC |
| **Service** | Always-on endpoint | Long-running container behind a ComputeCoin URL; health checks; scale across hosts; billed per second or per request | Private Stable Diffusion API, vLLM endpoint |
| **Multi-GPU** | One job, several GPUs on one host | Gang allocation of N GPUs on one machine; NCCL inside the host | Fine-tune a 70B model with LoRA on 4×4090 |
| **Multi-node** | One job across hosts | Only hosts in the same datacenter with fast interconnect, flagged by the provider and benchmarked | Large training runs |

---

## 8. Workload Kits

A Kit is a signed, versioned bundle: container digest, input schema (drives the web form and validation),
output schema, resource defaults, splitter and merger, verification method, price hints from
benchmarks, and a smoke test. Kits are published in the Kit Registry; versions are immutable.

| Kit (wave) | Category | Splits by | Verification method |
|---|---|---|---|
| AI image generation (diffusers / ComfyUI) — W1 | Generative | prompt items | Same seed + model → re-run sample, compare perceptually |
| Blender render (Cycles / EEVEE) — W1 | 3D | frame ranges, tiles | Re-render random frames on another host; perceptual difference under threshold |
| Video transcode / encode (ffmpeg, NVENC) — W1 | Video | time segments | Duration and stream checks; PSNR/SSIM against a sampled re-encode |
| Image / video upscaling (Real-ESRGAN class) — W1 | Enhancement | frames, tiles | Sampled re-run, perceptual compare |
| AI video generation — W2 | Generative | clips | Sampled re-run, perceptual compare |
| LLM serving (vLLM, OpenAI-compatible) — W2 | Inference service | requests | Known-answer prompts, token-level checks at temperature 0 |
| Fine-tuning / LoRA — W2 | Training | none / data shards | Loss curve sanity, evaluation of a held-out sample, checkpoint hashes |
| Speech (Whisper ASR, TTS) — W2 | Audio | audio segments | Known-answer clips, word error rate bound |
| Photogrammetry / Gaussian splatting / NeRF — W3 | 3D reconstruction | none | Re-run of a sample, metric compare |
| Scientific (molecular dynamics, docking, simulation) — W3 | Science | ensembles, seeds | Redundant run of a sample, energy/statistics bounds |
| Notebook (Jupyter / VS Code) — W2 | Session | — | Not applicable (customer-driven) |
| **BYOC** (any container) | Anything | customer-defined | Redundancy sampling if customer opts in; canaries |

---

## 9. Data plane

| Need | Design |
|---|---|
| Large uploads (scenes, datasets, video) | Presigned **multipart** uploads straight to object storage, resumable, per-part checksums; no 25 MiB / 5 GiB ceilings (limits become per-account quotas) |
| Integrity and dedupe | **Content addressing**: every file and chunk named by SHA-256; identical inputs stored and transferred once |
| Fast repeat jobs | **Host cache** of images and datasets keyed by digest; scheduler prefers hosts that already hold them |
| Customer-owned storage | **Bring your own bucket** (S3, GCS, R2): scoped, short-lived credentials per job; data never copied to ComputeCoin unless asked |
| Large outputs | Hosts upload outputs directly via presigned multipart; previews (thumbnails, low-res frames) streamed during the run |
| Retention | Lifecycle rules per account; crypto-shredding for private tiers (ADR-0005 / WP24) |
| Bandwidth cost | Egress metered and shown in quotes; host upload speed measured and used by the scheduler |

---

## 10. Scheduler v2

- **Capability matching**: GPU model, VRAM, compute capability, driver/CUDA version, NVENC/NVDEC sessions, CPU, RAM, free disk, upload/download bandwidth, region, privacy tier, isolation runtime, Kit compatibility (smoke test passed on that host).
- **Data locality**: prefer hosts with the image and inputs already cached.
- **Gang scheduling** for multi-GPU; **bin packing** of small jobs.
- **Priority, deadlines and fair share**; **spot vs reserved** capacity (spot can be preempted with checkpoint).
- **Reliability-weighted** choice using the existing reputation scores.
- **Speculative execution** for slow parts of a fan-out (first finisher wins, the other is cancelled and not billed beyond its fair share — policy in pricing).

---

## 11. Reliability

| Failure | Behaviour |
|---|---|
| Host disappears | Lease expires (exists); part requeued on another host; checkpoint restored if declared |
| Part of a fan-out fails | Only that part re-runs; finished parts kept |
| Merge step fails | Retried from stored part outputs; no re-render |
| Slow host | Speculative copy on another host after a threshold |
| Preemption (spot) | Checkpoint, release, resume elsewhere |
| Bad input (corrupt scene) | Fails fast on the first part with the Kit's validator message; remaining parts not started |

---

## 12. Verification engine

Pluggable methods chosen by the Kit (or the customer for BYOC):

| Method | When | How |
|---|---|---|
| Exact | Deterministic workloads | Byte or hash compare of a re-run (exists today) |
| Perceptual | Renders, generated images, upscales | Re-run a random sample elsewhere; SSIM / perceptual hash under a threshold set per Kit |
| Signal metrics | Video encodes | PSNR / SSIM against a sampled reference encode; duration and stream checks |
| Statistical | Training, simulations | Loss and metric bounds; held-out evaluation |
| Redundancy sampling | BYOC | Customer-chosen fraction re-run on a second host; outputs compared by the customer's rule |
| Known answer | LLM, speech | Planted prompts / clips with known results |
| Canaries | All private work | ADR-0005 |

Failures feed reputation and stake (ADR-0005) and open a dispute with the evidence attached.

---

## 13. Security model per workload source

| Source | Default isolation | Network | Extra controls |
|---|---|---|---|
| Kit | container (hardened) | none | Signed Kit, scanned image, smoke-tested on the host |
| BYOC | **gVisor or microVM** | none, or declared allowlist through the agent's logged egress proxy | Image scan (malware, miners), resource caps, runtime monitoring, size limits |
| Session | microVM | allowlist (package mirrors, git) | Idle timeout, per-second billing, recorded audit events (not content) |
| Service | microVM | inbound only through the ComputeCoin gateway | Rate limits, authentication on the endpoint |
| Any + privacy tier | Sealed / Confidential (ADR-0005) | none | Encrypted inputs/outputs, attestation, receipts |

Secrets (API keys, licence keys) are delivered sealed to the attested host (ADR-0005 crypto), never as plain environment variables in the job record.

---

## 14. Licences for commercial software

Customers bring their own licences. Licence servers are reached only through the declared egress allowlist;
node-locked licences are not supported on third-party hosts. Kits for commercial software are published
only with the vendor's permission.

---

## 15. Acceptable use, abuse and legal

| Area | Control |
|---|---|
| Acceptable Use Policy | Published; accepted by customers and hosts; enforced by policy engine at submit |
| Prohibited content | Generative Kits run content-safety checks where the law requires; reported content handled with a takedown process; legal review per jurisdiction before launch |
| Crypto mining and resource abuse | Forbidden unless explicitly offered; detected by image scan and runtime GPU-pattern monitoring; account suspension |
| Attacks from jobs | Network off by default; egress allowlist logged; abuse reports trace to the job |
| Sanctions and export controls | Customer and host screening where required |
| Host protection | Isolation defaults (section 13); hosts can choose which Kits and modes they accept |

---

## 16. Pricing

- Billing stays **per GPU-second** with the existing ledger, holds and settlement (P5).
- Quotes show the customer's unit: per image, per frame, per minute of video, per 1M tokens — estimated from Kit benchmarks on the chosen GPU class, with the cap held up front.
- Storage, egress and session idle time are shown separately.
- Spot (preemptible) capacity at a discount; reserved capacity at a premium.

---

## 17. Developer and customer experience

| Interface | Capability |
|---|---|
| Web | Kit catalogue; forms generated from Kit schemas; drag-and-drop upload; live progress and previews; gallery and download |
| CLI `cc` | `cc run kit blender-render scene.blend --frames 1-500`, `cc run image my/app@sha256:... -- cmd`, `cc session jupyter --gpu 4090`, logs, cancel |
| SDK | Same as CLI from Python / TypeScript; job spec builder; private-tier helpers (ADR-0005) |
| OpenAI-compatible endpoints | `/v1/chat/completions`, `/v1/images/generations` on Services |
| Local dry run | Run the same Kit container locally on a small sample before paying |

---

## 18. Privacy across all modes

| Mode | Private-tier approach |
|---|---|
| Batch / fan-out / pipeline | Every part attested; inputs encrypted; outputs sealed to the customer; one receipt per part plus a job-level receipt |
| Session | Stream encrypted end to end from the sealed environment to the browser; relay sees only ciphertext |
| Service | Runs in Confidential tier; requests encrypted to the attested endpoint key |
| BYOC | Allowed on private tiers; image hash recorded in the receipt |

---

## 19. Work packages and checkpoints

Effort is an estimate for one experienced engineer unless stated. Each checkpoint lists the proof.

### WP-U0 — Prerequisite: real GPU execution (1–2 weeks)

| # | Checkpoint | Proof |
|---|---|---|
| U0.1 | A real CUDA container runs on a real NVIDIA host through the agent, `execution_mode: hardware` | gpu-validation-runbook record |
| U0.2 | Billing on metered seconds matches wall-clock within 1% | Ledger vs log comparison |

### WP-U1 — Job Spec v2 and Kit Registry (3–4 weeks)

- JSON Schema for Job Spec v2; v1 jobs mapped to v2 internally; Kit model (immutable versions, signature, schemas, defaults, splitter, verifier, benchmarks); admin publishing flow; validation at submit.

| # | Checkpoint | Proof |
|---|---|---|
| U1.1 | Existing v1 jobs pass unchanged | Full existing test suite green |
| U1.2 | Invalid specs refused with field-level errors; unknown fields refused | Tests |
| U1.3 | A Kit cannot be changed after publish; a new version is a new entry | Test |
| U1.4 | Kit signature checked before use | Test with a tampered Kit |

### WP-U2 — Data plane (3–4 weeks)

- Presigned multipart uploads/downloads; content addressing; per-account quotas instead of fixed ceilings; bring-your-own-bucket with scoped temporary credentials; lifecycle rules.

| # | Checkpoint | Proof |
|---|---|---|
| U2.1 | 50 GB upload resumes after a dropped connection | Test |
| U2.2 | Corrupted part detected and re-sent | Test |
| U2.3 | Same file uploaded twice is stored once | Storage count |
| U2.4 | BYO-bucket credentials only work for that job's paths and expire | Test |

### WP-U3 — Agent runtime v2 (4–5 weeks)

- Input staging (download, verify digest, mount read-only), output collection by glob with direct multipart upload, image/data cache (LRU by digest, disk quota), egress proxy with allowlist and logs, checkpoint sync, preview streaming, progress events.

| # | Checkpoint | Proof |
|---|---|---|
| U3.1 | Inputs are read-only inside the job | Test writes fail |
| U3.2 | A cached image or dataset is not downloaded again | Network counter |
| U3.3 | Egress outside the allowlist is blocked and logged | Test |
| U3.4 | Killing the agent mid-job, then restarting, resumes from checkpoint on another host | E2E-U8 |

### WP-U4 — Fan-out planner and merge (3 weeks)

| # | Checkpoint | Proof |
|---|---|---|
| U4.1 | 500 frames split into parts, run on several hosts, merged in order | E2E-U1 |
| U4.2 | One failed part re-runs alone; others not re-billed | Ledger check |
| U4.3 | Straggler copy starts after threshold; only one result kept | Test |

### WP-U5 — Kits wave 1: AI image, Blender, ffmpeg, upscaling (4–6 weeks)

| # | Checkpoint | Proof |
|---|---|---|
| U5.1 | Each Kit passes its smoke test on ≥ 3 GPU models | Kit CI report |
| U5.2 | Web form generated from each Kit schema; bad input rejected before scheduling | UI tests |
| U5.3 | Benchmarks recorded per GPU class; quotes within ±20% of actual | Benchmark report |

### WP-U6 — Verification engine plug-ins (3 weeks)

| # | Checkpoint | Proof |
|---|---|---|
| U6.1 | Perceptual check passes honest re-renders, fails a deliberately altered frame | Test set |
| U6.2 | PSNR/SSIM check fails a truncated or low-bitrate encode | Test set |
| U6.3 | Verification method and result shown on the job page | UI test |

### WP-U7 — Scheduler v2 (3–4 weeks)

| # | Checkpoint | Proof |
|---|---|---|
| U7.1 | A job needing 16 GB VRAM never lands on a 12 GB card | Test |
| U7.2 | Hosts with cached inputs preferred | Test |
| U7.3 | 4-GPU job gets 4 GPUs on one host or waits | Test |
| U7.4 | Existing scheduler tests unchanged and green | Suite |

### WP-U8 — BYOC (3–4 weeks)

| # | Checkpoint | Proof |
|---|---|---|
| U8.1 | Customer image by digest runs in gVisor/microVM by default | Test |
| U8.2 | Image with a known miner or malware signature is refused before scheduling | Test image set |
| U8.3 | Container escape attempts from a standard test suite fail | Security test report |

### WP-U9 — Pipelines (DAG) (2–3 weeks)

| # | Checkpoint | Proof |
|---|---|---|
| U9.1 | Render → upscale → encode runs as one pipeline with artifact hand-off | E2E-U5 |
| U9.2 | Failure in step 2 re-runs from step 2, not from the start | Test |

### WP-U10 — Interactive sessions (4–5 weeks)

- Session Relay (outbound tunnels from agents), browser access to Jupyter / VS Code / desktop streaming, end-to-end encryption, idle timeout, per-second billing.

| # | Checkpoint | Proof |
|---|---|---|
| U10.1 | Session works with the host behind home NAT, no open ports | E2E-U6 |
| U10.2 | Relay cannot read session content | Packet capture shows only ciphertext |
| U10.3 | Idle session stops and billing stops | Test |

### WP-U11 — Services / endpoints (4 weeks)

| # | Checkpoint | Proof |
|---|---|---|
| U11.1 | Endpoint serves requests, scales to a second host under load | Load test |
| U11.2 | Host failure → traffic moves to another host within 60 s | Chaos test |
| U11.3 | Per-request and per-second billing reconcile | Ledger test |

### WP-U12 — Kits wave 2 and 3 (6–10 weeks, rolling)

AI video, LLM serving, fine-tuning/LoRA, speech, notebooks, photogrammetry/splatting, scientific. Same checkpoints as U5 per Kit.

### WP-U13 — Multi-GPU and datacenter multi-node (3–5 weeks)

| # | Checkpoint | Proof |
|---|---|---|
| U13.1 | 4-GPU fine-tune completes on one host | Run record |
| U13.2 | Multi-node only offered on hosts flagged and benchmarked as same-datacenter | Scheduler test |

### WP-U14 — Windows VM tier (optional, 6–8 weeks)

For Windows-only creative software, in a VM with GPU passthrough on hosts that opt in; licences customer-supplied.

| # | Checkpoint | Proof |
|---|---|---|
| U14.1 | A Windows renderer job completes in an isolated VM | Run record |
| U14.2 | Host cannot access the VM except through the agent | Security review |

### WP-U15 — Pricing units and quotes (2 weeks)

| # | Checkpoint | Proof |
|---|---|---|
| U15.1 | Quote per frame / image / minute derived from GPU-seconds; held cap never exceeded | Ledger test |
| U15.2 | Egress and storage shown separately | UI test |

### WP-U16 — Acceptable use, abuse and content safety (3–4 weeks plus legal review)

| # | Checkpoint | Proof |
|---|---|---|
| U16.1 | AUP accepted at sign-up for customers and hosts | UI + DB record |
| U16.2 | Mining pattern detected and job stopped | Test |
| U16.3 | Content-safety and takedown process signed off by counsel | Legal sign-off |

### WP-U17 — Web experience (4–6 weeks)

Kit catalogue, generated forms, uploads, previews, galleries, pipeline builder, session launcher.

| # | Checkpoint | Proof |
|---|---|---|
| U17.1 | A non-technical user renders a Blender animation without docs | Usability test (5 users) |
| U17.2 | Works on mobile for monitoring and download | QA run |

### WP-U18 — Privacy across all modes (with ADR-0005 work)

| # | Checkpoint | Proof |
|---|---|---|
| U18.1 | Private fan-out render: every part has a receipt; job-level receipt verifies | ccverify |
| U18.2 | Private session stream unreadable by relay and host | Lab test (Sealed/Confidential) |

### WP-U19 — Observability (2 weeks)

| # | Checkpoint | Proof |
|---|---|---|
| U19.1 | Per-job GPU utilisation, progress, cost so far visible live | UI test |
| U19.2 | Platform dashboards for queue depth, part failures, host health | Dashboard review |

---

## 20. End-to-end acceptance scenarios

| ID | Scenario | Passes when |
|---|---|---|
| E2E-U1 | 500-frame Blender animation, 20 hosts, merged to MP4 | Correct frame order; finished faster than one host by ≥ 10×; billed once per frame-range |
| E2E-U2 | 1,000 AI images from a prompt list | All delivered; sampled re-runs pass perceptual check |
| E2E-U3 | 2-hour 4K video transcoded with NVENC | Duration exact; PSNR above threshold; segments joined seamlessly |
| E2E-U4 | Customer's own container (BYOC) with a 30 GB dataset from their S3 bucket | Runs isolated; data never stored by ComputeCoin; outputs back to their bucket |
| E2E-U5 | Pipeline: render → denoise → upscale → encode | Each step's output feeds the next; partial failure resumes mid-pipeline |
| E2E-U6 | Jupyter session on a 4090 at home | Opens in browser; host has no open ports; billing stops when idle |
| E2E-U7 | Private image-generation endpoint | Serves requests; survives a host failure; per-request billing correct |
| E2E-U8 | Host unplugged mid-render | Part resumes elsewhere from checkpoint; no frame lost or billed twice |
| E2E-U9 | Cheating host returns blank frames | Verification flags it; frames re-rendered; reputation and stake penalised |
| E2E-U10 | Crypto miner hidden in a BYOC image | Refused at scan or stopped at runtime; account flagged |
| E2E-U11 | Private (Sealed/Confidential) fan-out render | Every part attested; receipts verify; host never sees plaintext (ADR-0005 gates) |
| E2E-U12 | All existing v1 tests and journeys | Unchanged and green |

---

## 21. Readiness gates

| Gate | Requires | Then you may |
|---|---|---|
| **G-U0** | WP-U0 | Run real GPU work at all |
| **G-U1 Creative beta** | WP-U1–U7, U15, U16 (non-legal parts), U19; E2E-U1, U2, U3, U8, U9, U12 | Beta for images, renders, video with Open tier |
| **G-U2 Developers** | G-U1 + WP-U8, U9; E2E-U4, U5, U10 | Open BYOC and pipelines |
| **G-U3 Sessions & services** | G-U2 + WP-U10, U11; E2E-U6, U7 | Offer notebooks and endpoints |
| **G-U4 Everything private** | Respective ADR-0005 gates + WP-U18; E2E-U11 | Sell private tiers for all modes |
| **G-U5 Public launch** | G-U3 + U16 legal sign-off + U17 + penetration test of BYOC and relay | General availability |

---

## 22. Timeline (estimate)

```
Months:            1        2        3        4        5        6        7        8
WP-U0 GPU valid.  ██
WP-U1 spec/kits    ████████
WP-U2 data plane      ███████
WP-U3 agent v2         █████████
WP-U4 fan-out                ██████
WP-U5 kits W1               ██████████
WP-U6 verification                ██████
WP-U7 scheduler v2          ████████
WP-U15 pricing                        ███
WP-U16 AUP/abuse                    ████████ (+ legal)
WP-U19 observability                    ███
                  ─────────── G-U1 creative beta ≈ month 4 ───────────
WP-U8 BYOC                                 ████████
WP-U9 pipelines                                 █████
WP-U10 sessions                                    █████████
WP-U11 services                                       ████████
WP-U12 kits W2/3                                   ███████████████
WP-U13 multi-GPU                                         ██████
WP-U17 web UX              ████████████████████████████████
WP-U14 Windows (optional)                                     ████████
```

About **4 months to a creative beta (images, renders, video)** and **7–8 months to the full scope**
with 4–6 engineers (backend, agent/runtime, frontend, GPU/media specialist, security). Estimates, not
measurements; they assume WP-U0 succeeds without major rework.

---

## 23. Owner decisions

1. Which Kits launch first (recommended: AI image generation, Blender render, video transcode, upscaling).
2. Allow BYOC at beta, or only after G-U2?
3. Content policy for generative Kits and the jurisdictions to serve (needs counsel).
4. Is crypto mining allowed as a workload (and priced) or banned?
5. Spot / reserved capacity and pricing policy.
6. Build the Windows VM tier (WP-U14) or not.
7. Storage retention defaults and who pays egress.
8. Team and budget for 4–6 engineers and a media/GPU specialist.

---

## 24. Honest limits

- NVIDIA and Linux first; Windows-only and macOS software need extra work (WP-U14) or are out of scope.
- Internet-wide multi-node training is too slow to be useful; multi-node is datacenter-only.
- Render and AI outputs are rarely byte-identical across GPUs, so verification for them is statistical
  (sampled and perceptual), not absolute.
- Interactive sessions depend on the host's upload bandwidth; quality varies by host.
- "Anything" always means anything **legal and containerisable** under the Acceptable Use Policy.
