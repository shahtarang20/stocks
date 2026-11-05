# Kit: llm-serve

Serves one baked open-weights LLM with vLLM behind an OpenAI-compatible HTTP server, meant to run as a **U11 Service replica**
(`docs/architecture/U11-services-design.md`). Licences: see below.

Status: wave 2 candidate, **code written, never built or run.** The kit list is subject to OWNER approval (kits/README.md).
Billing hint unit: `gpu_second` (replica jobs are metered per GPU-second, U11). Verification: none (see OWNER DECISIONS).

## Service contract (matches `provider-agent/computecoin_agent/service_agent.py`)

| Item | Value | Where it comes from |
|---|---|---|
| Port | `8000` | `svc.port` (the Service record's `port`; agent default 8000). Create the Service with `port: 8000` |
| Health path | `/health` | `svc.health_path` (agent default `/health`). vLLM answers 200 on `GET /health` once the engine is up |
| Bind address | `0.0.0.0` inside the container | The agent reaches the replica at `http://<container ip>:8000` on an `--internal` docker network. A server bound to `127.0.0.1` would be unreachable. The container publishes no port and the network has no egress, so only the agent can connect. (The brief asked for loopback; the agent's design makes that impossible, so this is the safe equivalent.) |
| Start | the image `ENTRYPOINT` with no arguments | `DockerServiceContainer.start` runs `docker run -d ... <image>` and nothing else |
| Environment and params | none | The agent passes no env, no `input.json`, no `/in`, no `/cache`. `param_schema` is empty and every setting is a constant in `entrypoint.py` |
| Process user | `65532:65532` | The agent overrides `USER 10001` with `--user 65532:65532`; all files are world-readable and caches live on the `/tmp` tmpfs |
| Filesystem | read-only rootfs, `/tmp` tmpfs | Hugging Face, vLLM, Triton and inductor caches are redirected to `/tmp` |
| Customer API | `kind=inference`: `POST /v1/services/{id}/v1/chat/completions` (non-streaming) | U11. Other paths go through `POST /v1/services/{id}/invoke?path=/...` (`GET`/`POST` only). Streaming is not supported by U11 |
| Served model name | `model` | Customers send `"model": "model"` |

Fixed engine settings (constants, change = new `version`): `--max-model-len 8192`, `--max-num-seqs 64`,
`--gpu-memory-utilization 0.90`, `--dtype float16`, `--seed 0`, `--enforce-eager`, no `--trust-remote-code`, no API key
(access control is the platform's). Weights are hash-checked at start (`kitrt.verify_manifest`); a mismatch exits 2.

Model weights are **baked** into the image (the service agent mounts no `/cache`, so the manifest declares no `assets`).
Retries may reach a replica twice (U11): the server is stateless, so this is safe.

## Licences (record, owner to confirm before publish, L4)

- **vLLM**: Apache-2.0. **PyTorch**: BSD-3-Clause. **CUDA base image**: NVIDIA's licence terms for the image you pin.
- **Model weights: OWNER DECISION.** The licence follows the model you choose and may restrict use (for example Llama community
  licence, Gemma terms, or an open licence such as Apache-2.0 / MIT). Record model name, revision, licence text and any
  acceptable-use or attribution duty here before publishing. Nothing is chosen in this repository.
- Customer prompts and outputs stay the customer's; the kit logs no request bodies (`--disable-log-requests`).

## Pins you must supply

`BASE_IMAGE` and `FETCH_IMAGE` (digest-pinned), `ASSET_URL` and `ASSET_SHA256` (a tar of one model directory, extracting to
`model/config.json`, tokenizer and safetensors files; no symlinks). All default to `PENDING` or empty and fail the build.
`requirements.txt` versions are NOT checked against PyPI or the base image's torch (L4); freeze with hashes in CI.

## Not verified (needs a GPU host)

- vLLM flag names for the pinned version, and that `GET /health` exists there.
- That vLLM runs with `--read-only` and a `/tmp` tmpfs that may be mounted `noexec` (Triton may need to load compiled code; if so the Service agent's tmpfs flags must change, which is outside this Kit).
- That `/dev/shm` (docker default 64 MB) is enough for a single GPU.
- Start time against the replica start and health limits; large models may need a longer window than the platform default.

## OWNER DECISIONS

- Whether LLM serving is offered, which model(s) and their licence, price hints (`est_gpu_seconds_per_unit` is null until benchmarked).
- Verification: none today (`exact` with `sample_fraction` 0 is a placeholder because the offline gate requires a method). A canary-prompt check is a possible later design.
- The GPU model list in `smoke/smoke.json`, and a service-aware smoke runner (the batch runner cannot test a server).
- Privacy tiers are refused for Services (U11); this Kit must not be marketed as private.
