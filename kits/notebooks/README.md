# Kit: notebooks

JupyterLab with a GPU PyTorch stack for **U10 interactive sessions** (`docs/architecture/U10-interactive-sessions-design.md`).
Licences: see below.

Status: wave 2 candidate, **code written, never built or run.** The kit list is subject to OWNER approval (kits/README.md).
Billing: sessions are billed per GPU-second by the session service (U10), not through batch job settlement.

## Session contract (matches `provider-agent/computecoin_agent/session_agent.py`)

The manifest carries a `session` block (the key is added to `services/kits.py` validation by a separate change; until that lands
the registry refuses it as an unknown field, and `scripts/kit_ci.py` must allow it):

```json
"session": {"mode": "tcp", "tcp_port": 8888, "command": ["jupyter", "lab", "--ip=127.0.0.1", "--port=8888", "..."]}
```

| Item | Value |
|---|---|
| Start | `docker run -d` of the image with **no command override**: the image `ENTRYPOINT` (`/app/entrypoint.py`) execs JupyterLab and must stay running |
| Attach | `docker exec -i <container> nc 127.0.0.1 8888` (mode `tcp`); `nc` is a loopback-only Python shim at `/usr/local/bin/nc` (`nc_shim.py`) |
| Bind | `127.0.0.1:8888` only; the container publishes no port and runs with `--network none` |
| Auth | token and password **disabled**: the only way in is the end-to-end encrypted session channel, which the owner opens with a single-use connect token. Anyone on the channel is the session owner |
| Workspace | `/tmp/workspace` on the `/tmp` tmpfs (rootfs is read-only). Lost when the session ends |
| GPU | the session request asks for GPUs; the agent passes them through. Torch comes from the base image |
| `command` | the same Jupyter argv as the entrypoint (used by `pty` attach; `tests/test_kits_notebooks.py` checks they match) |

Known limit (from U10 section 7): a browser cannot yet open Jupyter through the channel. The HTTP-over-channel proxy page is NOT
built; today the byte stream reaches the port but a client must speak HTTP/WebSocket over it. Until that exists this Kit is not
usable from the dashboard.

Security note: a notebook is arbitrary code execution inside the sandbox by design. The hardening comes from the container
(read-only rootfs, no caps, non-root, no network, GPU only), not from Jupyter.

## Licences (record, owner to confirm before publish, L4)

- **JupyterLab, jupyter-server, IPython**: BSD-3-Clause. **PyTorch**: BSD-3-Clause. **NumPy, pandas, matplotlib**: BSD-style / PSF-based.
- **CUDA base image**: NVIDIA's terms for the image you pin. Check every extension JupyterLab bundles before adding more.

## Pins you must supply

`BASE_IMAGE` (CUDA + PyTorch, python3 on PATH, digest-pinned). It defaults to nothing and an unpinned value fails the build.
`requirements.txt` versions are NOT checked against PyPI or the base image (L4); freeze with hashes in CI.

## Not verified (needs a GPU host)

- Option names (`--IdentityProvider.token=`, `--PasswordIdentityProvider.hashed_password=`, `--ServerApp.root_dir`) for jupyter-server 2.x of the pinned JupyterLab.
- That Jupyter writes only under `/tmp` with a read-only rootfs, and accepts requests whose `Host` header is not `127.0.0.1` (a relay proxy may need `--ServerApp.allow_remote_access` or host allow-listing).
- That `nc` shim half-close semantics work with the agent's bridge.

## OWNER DECISIONS

- Whether interactive notebooks are offered at all, price, idle and maximum duration defaults.
- The extra Python packages shipped (kept minimal here), and whether terminals and extension installs are allowed.
- The GPU model list in `smoke/smoke.json`, and a session-aware smoke runner.
- Data safety: a session on a stranger's host sees everything typed into it; privacy tiers are refused for sessions (U10).
