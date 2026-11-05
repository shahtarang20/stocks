# BYOC template (U8, bring your own container)

Not a Kit: there is no `kit.json`, no signing and no registry entry, so `scripts/kit_ci.py` ignores this directory. It shows a
customer how to build an image that the platform will accept for `POST /v1/jobs/byoc`
(`docs/architecture/U8-byoc-design.md`). Status: **written, never built or run.**

## Checklist

1. **Pin by digest.** Pin your base image (`FROM ...@sha256:...`) and submit your own image as
   `<registry-host>/<path>@sha256:<64 hex>`. A tag, or an image with no explicit registry host, is refused.
2. **Registry on the allowlist.** The platform accepts only registries in its `BYOC_REGISTRY_ALLOWLIST`; an empty list refuses all BYOC.
   Registry pull credentials are not supported: the image must be pullable by the provider (public or reachable).
3. **Non-root.** `USER 10001` (or any non-root). Never rely on being root; the host runs with all capabilities dropped and `no-new-privileges`.
4. **Read-only rootfs.** The root filesystem is read-only. Writable places: `/out` (results) and `/tmp` (tmpfs, `noexec`: do not
   execute or `dlopen` files you write there). Set `HOME=/tmp`. Bake everything else into the image.
5. **No network by default.** The container runs with `--network none`. Nothing can be downloaded at run time; bake weights and
   dependencies in. Egress works only if the platform enables `BYOC_ALLOW_EGRESS` and your spec lists exact `host:port` entries (max 8).
6. **Exec-form commands.** `ENTRYPOINT ["python3", "/app/run.py"]` (a JSON array). The spec's `command` replaces the entrypoint
   (`--entrypoint command[0]`), `args` follow it. Both are an argv list, never run through a shell: max 32 items, 512 characters
   each, 8192 in total, no NUL. Leave `command` empty to use the image entrypoint.
7. **No special privileges.** There is no field for privileged mode, host mounts, devices, capabilities, host namespaces or the Docker
   socket, and unknown fields are refused. Isolation is `gvisor` (default) or `microvm`; there is no plain container. Hosts without
   the runtime never receive your job (it stays queued rather than downgrade).
8. **Environment.** At most 32 variables, uppercase names. Allowed: `TZ`, `LANG`, `LC_ALL`, `PYTHONUNBUFFERED`, `OMP_NUM_THREADS`,
   `NUMEXPR_NUM_THREADS`, `MKL_NUM_THREADS`, and names starting `APP_` or `CUSTOM_` (the platform may add more). `COMPUTECOIN_`,
   `LD_`, `DOCKER_` and proxy names are reserved. The spec is stored in plain text: **never put secrets in `env`.**
9. **Image scan.** Every image is scanned (Trivy by default). A CRITICAL vulnerability count above the platform limit, or a known miner or malware
   tool name, is refused. Keep the image small and updated. Denylisted digests are refused at submit and at claim.
10. **GPU.** Billing is per GPU-second; a BYOC job holds at least 1 GPU. Only NVIDIA is supported for BYOC in v1.
11. **Privacy tiers** (sealed, confidential) are refused for BYOC.

## The `/in` and `/out` contract

| Path | Meaning |
|---|---|
| `/in/<input name>` | Read-only. One entry per spec `inputs[]` item (`source` is `dataset:<id>` or `artifact:<job>/<name>`). `COMPUTECOIN_INPUTS_DIR` names it |
| `/out/<path>` | Writable. Each spec `outputs[]` item `{name, path}` is collected from `/out/<path>`. `path` is relative: no leading `/`, no `..`, no `//`, no backslash. Write regular files; no symlinks |
| `/tmp` | tmpfs, `noexec`, small. Scratch only; lost at the end |
| exit code | `0` = success. Non-zero fails the job. Stdout and stderr are logs, not results |

Everything outside `/out` disappears when the job ends.

## Validate your spec locally

```
python scripts/check_byoc_image_spec.py kits/byoc-template/example-spec.json
python scripts/check_byoc_image_spec.py my-spec.json --dockerfile Dockerfile --registry registry.example.com/acme
```

The script runs the server's own strict schema (`apps/api/app/schemas/byoc.py`) plus the environment-name rules of
`apps/api/app/services/byoc.py`. It needs `pydantic` (the API's environment) and checks only the shape: the server still enforces the registry
allowlist, platform resource caps, the scanner and the denylist, which it cannot know. `--dockerfile` adds a few heuristic checks of your Dockerfile
(base pinned, `USER`, exec-form entrypoint). A pass is not an acceptance.
