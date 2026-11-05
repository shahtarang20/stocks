# Kit: scientific-compute

Runs ONE customer-supplied Python script on numpy, scipy and pandas and collects the files it writes.

Status: wave 3 candidate, **code written, never built or run.** The kit list is subject to OWNER approval (kits/README.md).
Billing hint unit: `cpu_second` (the pricing unit for non-GPU work is a U15 / owner decision). Verification: `exact_deterministic`.

## The script is UNTRUSTED CODE

Anything the customer uploads as `script` runs inside this container. The Kit is only safe because of the hardened runtime
the platform applies (kits/README.md): **no network** (`--network none`), read-only root filesystem, non-root uid 10001,
capabilities dropped, `/tmp` tmpfs `noexec`, only `/out` writable, a wall-clock cap, CPU/memory/file-size/process rlimits.
Do not run this Kit on a host or runtime that does not enforce all of these, and never give it network (L10). The entrypoint adds
defence in depth only: isolated interpreter (`python -I -B`), a cleaned environment, process-group kill on timeout, deletion
of symlinks and special files in the results, and output size and file-count caps. The script can read `/out/input.json`
and everything else the container shows; no secret may ever be placed in the image, the job input or the environment (L7).

## Contract

- Inputs: `script` (required, one UTF-8 `.py`, at most 1 MiB), `data` (optional zip: regular files only, no absolute or `..`
  names, at most 5000 files and 2 GiB unpacked), exposed to the script as `$DATA_DIR`.
- The working directory and `$RESULTS_DIR` are `/out/results`; whatever the script writes there is the output. `stdout` and
  `stderr` (last 64 KiB each) and `determinism.json` go to `/out/logs`.
- Params: `args` (up to 16 strings), `timeout_seconds` (wall clock, 1 to 3600), `max_output_mb`, `max_memory_mb`
  (address-space limit; numerical libraries reserve virtual memory, so keep it generous), `deterministic`, `seed`.
- A script that fails, times out or writes too much is the customer's error: logs are kept, the Kit exits non-zero with
  `script_failed`, `script_timeout` or `output_too_large`.

## Determinism and verification

With `deterministic=true` the Kit pins `PYTHONHASHSEED`, BLAS/OpenMP threads (1), `TZ`, the locale and `SOURCE_DATE_EPOCH`, and
passes `KIT_SEED`; the script must itself use that seed for every random choice and avoid clocks, directory-order and
hash-order dependence. The Kit cannot prove reproducibility. Verification is the `exact_deterministic` plug-in: byte-for-byte
`exact` comparison, applied only when the independent re-run's `logs/determinism.json` says `deterministic: true`; with
`deterministic=false` (the default) the result is `inconclusive`, never a failure of the host. CPU float results can also differ
across CPU models (BLAS kernels, FMA), so a customer asking for `deterministic=true` accepts that a mismatch across hosts is
possible; the owner decides how a failed exact check is treated.

## Licences (record, owner to confirm before publish, L4)

- **numpy** (BSD-3-Clause), **scipy** (BSD-3-Clause), **pandas** (BSD-3-Clause); the wheels bundle OpenBLAS and other libraries
  with their own notices (check each wheel's licence files). The **python** base image: PSF licence plus the OS packages of the
  chosen image. No model weights and no third-party data are shipped.
- Customer scripts and data stay the customer's.

## Pins you must supply

`BASE_IMAGE` (a python image pinned by digest). Defaults fail the build when unpinned. `requirements.txt` versions are NOT yet
checked against PyPI and the base image's Python (L4); freeze with hashes in CI. There is nothing to bake in, so no asset hash.

## Owner decisions

- Whether this Kit is offered at all, given it executes arbitrary customer code (abuse and AUP review, ADR-0006 P3).
- Verification (`exact_deterministic`), sample fraction, price unit and hints, the default and maximum caps in
  `params.schema.json`, and the host list in `smoke/smoke.json`. `resource_defaults` has `gpu_count` 0: the scheduler must
  accept CPU-only Kits before this one can be matched to hosts (U-series check).
- Whether `numpy`, `scipy` and `pandas` are enough, or a larger package set (each addition widens the attack surface).
