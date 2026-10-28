# ComputeCoin Provider Agent

A small, separately-installable agent that a GPU provider runs on their own
machine to join the ComputeCoin marketplace. **Phase-1 skeleton**: it detects
hardware, enrolls the machine, and sends signed heartbeats. It does **not** run
customer jobs yet (see [Security model](#security-model)).

Linux-first. Runs fine on a CPU-only host (no GPU required).

## Install

```bash
cd provider-agent
python3 -m venv .venv            # or: python3 -m virtualenv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # dev extras add pytest
# Optional GPU support:
pip install -e ".[gpu]"          # NVIDIA NVML bindings
pip install -e ".[amd]"          # AMD ROCm amdsmi bindings (must match host ROCm version)
```

> The `gpu` and `amd` extras are **optional**. Without them, GPU detection returns an empty
> list and a clear "CPU-only" capability report — the agent never crashes on a
> machine without a GPU. When using AMD GPUs, ensure the installed `amdsmi` package
> version matches your host's installed ROCm runtime version (e.g. ROCm 6.x).

## Usage

```bash
# 1. See what this machine reports (JSON capability report)
computecoin-agent detect

# 2. Enroll the machine with a machine-scoped enrollment token from your
#    provider dashboard. Writes ~/.computecoin/agent.toml with 0600 perms.
computecoin-agent enroll --token tok_xxx

# 3. Send a few signed heartbeats (skeleton loops a few iterations)
computecoin-agent run --iterations 3

# What will/won't run in customer workloads
computecoin-agent security-info
```

Configuration lives at `~/.computecoin/agent.toml` (override the directory with
`COMPUTECOIN_CONFIG_DIR`). Any field can be overridden by an environment
variable, e.g. `COMPUTECOIN_API_URL=https://api.example`. Env vars take
precedence over the file.

## Capability reporting & trust

Every capability the agent reports is **self-reported** and marked `DETECTED`.
The agent **never** emits `VERIFIED` — only a future server-side
benchmark/attestation step may promote a capability to `BENCHMARKED` or
`VERIFIED`. The control plane should treat all agent-reported capabilities as
untrusted until verified.

Heartbeats are signed with a per-machine HMAC-SHA256 secret generated at
enrollment, so the control plane can reject spoofed or tampered health reports.
The enrollment token is **revocable and machine-scoped**: a leaked token cannot
be replayed across the fleet, and revoking it does not require re-keying an
already-enrolled machine (which authenticates with its own signing secret).

## Security model

This agent runs on **semi-trusted, third-party hardware**. The hard rules it is
built around (see `computecoin_agent/security.py`):

- **MUST NOT** execute arbitrary customer shell commands on the host.
- **MUST NOT** mount the host filesystem, the Docker socket, or host
  credentials into any workload.
- **MUST** run workloads (when job execution lands) under least privilege:
  non-root, dropped capabilities, read-only rootfs where possible, restricted
  egress, and resource quotas.

See `docs/architecture/ADR-0003-isolation.md` and
`docs/security/threat-model.md`.

## Not yet implemented (labelled skeleton)

- **Job execution / sandboxing** — none. The agent only does detect / enroll /
  heartbeat.
- **Server exchange on enroll** — the skeleton records the token and mints a
  local signing secret offline; it does not yet POST to `/v1/agents/enroll`.
- **Benchmark-based verification** — capabilities are `DETECTED` only.
- **Stable hardware-bound machine id** — the skeleton uses a random id; a
  production agent would bind it to stable hardware identifiers.
- **Asymmetric signing** — HMAC today; Ed25519 (via `cryptography`) is the
  intended upgrade so the control plane never holds the signing key.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

Tests cover: CPU-only detection (valid report, no GPUs, no crash), capability
report never marks hardware `VERIFIED`, heartbeat signing/verification with a
mocked offline transport, and enrollment writing a `0600` config. NVML and the
network transport are mocked, so the suite runs offline on any host.
