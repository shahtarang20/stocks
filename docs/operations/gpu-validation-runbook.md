# GPU Validation Runbook (Workstream B)

> **Audience:** Infrastructure Engineers, Node Operators, and QA.  
> **Purpose:** Step-by-step instructions for provisioning a real NVIDIA GPU host, enrolling it with the ComputeCoin control plane, and executing an authenticated hardware text-generation job through `DockerExecutor` under ADR-0003 container hardening.

---

## 1. Overview & Architectural Contract

ComputeCoin guarantees honest workload reporting. Unlike CPU dev environments where `SimulatedExecutor` returns synthetic deterministic completions, a hardware job runs in an isolated container on physical GPU hardware.

### Hard Invariants
1. **Never Silently Downgrade**: If a hardware job (`execution_mode: "hardware"`) lands on an agent without Docker or without the NVIDIA Container Toolkit, the executor immediately raises `HardwareUnavailableError` and reports the failure to the control plane. It is **never** downgraded to simulation.
2. **Pinned Image Digest**: Only immutable digests (`image@sha256:...`) are allowed; mutable tags (such as `:latest`) are rejected.
3. **Defense-in-Depth Container Hardening (ADR-0003)**:
   - `--network none` (zero egress by default; blocks LAN and cloud metadata `169.254.169.254`).
   - `--read-only` root filesystem.
   - `--cap-drop ALL` (all Linux capabilities dropped).
   - `--security-opt no-new-privileges`.
   - `--user 65534:65534` (`nobody:nogroup` non-root execution).
   - `--pids-limit 128` (fork bomb mitigation).
   - Dedicated writable `--tmpfs /tmp:rw,noexec,nosuid,size=64m`.
   - Per-job isolated output bind mount `/out` for `input.json` and `output.json`.
   - Hard wall-clock timeout wrapper (`timeout --signal=KILL <seconds>`).
   - Dedicated GPU access via `--gpus <count>`.
4. **Metered Actual Duration Billing**: Completed jobs are billed based on metered runtime (`metrics.duration_seconds`), and unused hold balance is released back to the customer's ledger account.

---

## 2. Infrastructure & Hardware Prerequisites

### Supported Platforms
- **Cloud Providers**: RunPod (Secure Cloud / Community Cloud), Lambda Labs, AWS EC2 (`g5.xlarge`, `g4dn.xlarge`), GCP (`g2-standard-4`), Azure (`NCasT4_v3`).
- **Bare Metal**: Any x86_64 server with modern NVIDIA GPUs.

### Recommended Host Specifications
| Component | Minimum Specification | Recommended Specification |
|---|---|---|
| **OS** | Ubuntu 22.04 LTS (x86_64) | Ubuntu 22.04 LTS (x86_64) |
| **GPU** | 1x NVIDIA GPU (>= 8 GB VRAM, Turing or newer: T4, RTX 3080/4090, A10G, L4, A100) | 1x NVIDIA A10G or L4 (24 GB VRAM) |
| **NVIDIA Driver** | >= 535.86.10 | >= 550.54.14 |
| **CUDA Version** | >= 12.2 | >= 12.4 |
| **CPU / RAM** | 4 vCPU, 16 GB RAM | 8 vCPU, 32 GB RAM |
| **Storage** | 50 GB NVMe SSD | 100 GB NVMe SSD |

---

## 3. Host System Setup & Preparation

Run all commands on the GPU host as `root` (or with `sudo`).

### 3.1 Install NVIDIA Drivers & Kernel Headers
```bash
sudo apt-get update && sudo apt-get install -y linux-headers-$(uname -r) build-essential
sudo apt-get install -y nvidia-driver-535 nvidia-utils-535

# Verify driver initialization
nvidia-smi
```
Expected output:
```text
+-----------------------------------------------------------------------------------------+
| NVIDIA-SMI 535.183.01             Driver Version: 535.183.01     CUDA Version: 12.2     |
|-----------------------------------------+------------------------+----------------------+
| GPU  Name                 Persistence-M | Bus-Id          Disp.A | Volatile Uncorr. ECC |
| Fan  Temp   Perf          Pwr:Usage/Cap |           Memory-Usage | GPU-Util  Compute M. |
|                                         |                        |               MIG M. |
|=========================================+========================+======================|
|   0  NVIDIA A10G                    On  | 00000000:00:1E.0   Off |                    0 |
| N/A   32C    P8              17W / 150W |      0MiB / 23028MiB |      0%      Default |
+-----------------------------------------+------------------------+----------------------+
```

### 3.2 Install Docker Engine
```bash
# Set up Docker official repository
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
sudo systemctl enable --now docker
```

### 3.3 Install NVIDIA Container Toolkit
```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
  && curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit

# Configure Docker daemon to use the NVIDIA runtime
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### 3.4 Verify Docker GPU Access (Smoke Test)
Execute a containerized `nvidia-smi` through Docker:
```bash
docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi
```
> [!IMPORTANT]
> If this command fails or cannot find the GPU, do **not** start the ComputeCoin agent. Review the NVIDIA Container Toolkit logs via `journalctl -u docker -e`.

---

## 4. Provider Machine Enrollment

### 4.1 Generate Enrollment Token
In the ComputeCoin dashboard (or via API):
```bash
curl -X POST https://api.computecoin.network/v1/providers/enrollment-tokens \
  -H "Authorization: Bearer <PROVIDER_JWT>" \
  -H "Content-Type: application/json" \
  -d '{"name": "production-gpu-node-01"}'
```
Response:
```json
{
  "token": "et_live_9b4e18f2a719c8d0...",
  "expires_at": "2026-10-18T18:00:00Z"
}
```

### 4.2 Enroll the Node
On the GPU host, install `computecoin-agent` and run:
```bash
computecoin-agent enroll \
  --server https://api.computecoin.network \
  --token et_live_9b4e18f2a719c8d0...
```
The agent automatically:
- Probes CPU model, RAM, disk, and GPU hardware via NVML (`pynvml`).
- Probes isolation tiers (`container`, and optionally `runsc` / `kata-qemu`).
- Generates a local cryptographic key pair (`msk_...`).
- Persists credentials securely to `~/.computecoin/credentials.json` (`0600` permissions).

---

## 5. Starting the Provider Agent Worker

Launch the worker loop in the background or under `systemd`:

```bash
computecoin-agent work --server https://api.computecoin.network
```

### Production systemd Unit Example
`/etc/systemd/system/computecoin-agent.service`:
```ini
[Unit]
Description=ComputeCoin Provider Agent
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/computecoin-agent work --server https://api.computecoin.network
Restart=always
RestartSec=5s
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```
Start and enable the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now computecoin-agent
sudo journalctl -u computecoin-agent -f
```

---

## 6. End-to-End Hardware Job Validation

### 6.1 Submit a Hardware Text-Generation Job
Submit a job using the registered pinned model `tinyllama-1.1b-chat-v1`:

```bash
curl -X POST https://api.computecoin.network/v1/jobs \
  -H "Authorization: Bearer <CUSTOMER_JWT>" \
  -H "Content-Type: application/json" \
  -d '{
    "workload_type": "text-generation",
    "model": "tinyllama-1.1b-chat-v1",
    "input": {
      "prompt": "ComputeCoin architecture guarantees honest workload reporting because:",
      "max_tokens": 64
    },
    "gpu_count": 1,
    "estimated_minutes": 5,
    "max_cost": "2.00",
    "max_runtime_seconds": 180
  }'
```
Response:
```json
{
  "job_id": "9d82e1c3-4f2a-4b91-89ce-123456789abc",
  "status": "reserved",
  "execution_mode": "hardware",
  "match_explanation": {
    "chosen_machine_id": "...",
    "score": 0.88
  }
}
```

### 6.2 Monitor Agent Execution Logs
On the GPU host, observe `journalctl -u computecoin-agent`:
```text
[hardware] claimed job 9d82e1c3-4f2a-4b91-89ce-123456789abc (tinyllama-1.1b-chat-v1)
[hardware] materializing input.json in /tmp/ccoin-job-xyz/input.json
[hardware] invoking docker run with pinned digest ghcr.io/computecoin/model-runners/tinyllama-v1@sha256:4a382...
[hardware] container completed in 3.41s, exit code 0
[hardware] validated output.json schema: valid=True, tokens=48
[hardware] uploading output artifact (412 bytes)
[hardware] submitting receipt: status=completed, execution_mode=hardware, duration=3.41s
```

### 6.3 Verification Checklist
Verify via `GET /v1/jobs/9d82e1c3-4f2a-4b91-89ce-123456789abc`:

- [x] **Job Status**: `status: "completed"`.
- [x] **Honesty Stamp**: `execution_mode: "hardware"`.
- [x] **Verification**: `verification: {"valid": true}`.
- [x] **Artifacts**: Download artifact via `/download` endpoint; verify valid JSON containing generated text.
- [x] **Metered Billing**: Check `job.final_cost` and `/billing/invoices`; customer is charged for ~3.41 seconds (or minimum billable seconds), **not** the 5 estimated minutes.

---

## 7. Troubleshooting & Diagnostics

| Symptom | Probable Cause | Remediation |
|---|---|---|
| `HardwareUnavailableError: GPU job requires the NVIDIA container toolkit` | `nvidia-container-toolkit` not installed or Docker daemon not configured with `nvidia` runtime. | Run `nvidia-ctk runtime configure --runtime=docker` and `systemctl restart docker`. Verify with `docker run --rm --gpus all nvidia/cuda:... nvidia-smi`. |
| `container_nonzero_exit: docker run exited 137` | Out of Memory (OOM) killed by kernel or cgroup memory quota. | Verify GPU VRAM and system memory limits. Increase `memory_mb` in job spec or assign a machine with higher VRAM. |
| `permission denied` reading `/out/input.json` inside container | Output directory created with restricted umask. | The agent automatically applies `chmod 0777` to the per-job temporary output directory and `chmod 0666` to `input.json` to allow the unprivileged `nobody:nogroup` container user to read input and write output. Verify host filesystem is not mounted `noexec` or `nosuid`. |
| `missing_output: container exited 0 but produced no valid output` | Model runner crashed silently or failed to write `/out/output.json`. | Inspect container logs via `journalctl -u computecoin-agent` or check container standard error. |
