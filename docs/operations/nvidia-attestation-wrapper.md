# NVIDIA GPU attestation: the operator wrapper contract

_Status: **documented contract, unverified against a real NVIDIA tool run.** Written 2026-11-06 for the confidential
tier (`nvidia_cc`). Everything marked `TODO(verify-docs)` must be checked on a Confidential Computing capable GPU
(Hopper or newer) with the installed NVIDIA attestation SDK before `nvidia_cc` is turned on anywhere._

## 1. The gap this closes

| Piece | What it needs | Where |
|---|---|---|
| Platform verifier | **Raw evidence**: an SPDM 1.1 `GET_MEASUREMENTS` request, its response, and the device certificate chain. It verifies the chain against pinned NVIDIA roots, checks revocation, and compares the signed measurements with signed RIMs. | `apps/api/app/services/hardware_attestation/nvidia.py` |
| Agent collector | Runs `nvidia_attest_cmd` and relays that evidence. | `provider-agent/computecoin_agent/tee.py` (`NvidiaGpuCollector`) |
| NVIDIA's documented `attest` command | Prints an attestation **result** (an Entity Attestation Token or Result: a signed JWT that says the GPU passed). A result holds **no** SPDM request, response or certificate chain. | https://docs.nvidia.com/attestation/nv-attestation-sdk-cpp/latest/overview.html (last checked 2026-11-03 by the earlier confidential-tier work; NOT re-read when this document was written, so re-check it) |

A result cannot be turned into evidence, and a host's own "passed" verdict proves nothing (the host is the party being
checked; L1). So the agent **refuses** a result with a clear error and accepts only output that contains evidence.

## 2. Wrapper contract

`nvidia_attest_cmd` in the agent config is the command prefix. The agent runs:

```
<nvidia_attest_cmd...> --gpu-index <N> --nonce <64 hex characters>
```

`<nonce>` is `SHA-256(challenge)` in hex. The wrapper must:

1. Ask the GPU for a fresh attestation report **bound to that exact nonce** (never reuse a report).
2. Print one JSON document on stdout, and nothing else, in **one** of the two forms below.
3. Exit 0 on success, non-zero with a one-line reason on stderr on failure.
4. Finish within 120 seconds.
5. Never print or log a private key.

### Form A: our contract (preferred)

```json
{
  "request":           "<hex of the 37-byte SPDM GET_MEASUREMENTS request>",
  "response":          "<hex of the SPDM MEASUREMENTS response>",
  "certificate_chain": ["-----BEGIN CERTIFICATE-----\n...", "..."],
  "crls":              ["<optional>"],
  "ocsp_responses":    ["<optional>"],
  "rims":              ["<optional signed RIM documents>"]
}
```

The chain is leaf first. The optional collateral is relayed untouched; the API verifies each item by signature against
its pinned roots.

### Form B: NVIDIA evidence list (accepted, adapted)

```json
[
  {
    "arch":        "HOPPER",
    "nonce":       "<optional, hex; must equal the challenge nonce if present>",
    "evidence":    "<base64: SPDM request (37 bytes) followed by the SPDM response>",
    "certificate": "<base64 of the PEM certificate chain, or the PEM text itself>"
  }
]
```

This is the shape NVIDIA's evidence-collection path is documented to produce. `TODO(verify-docs)`:

* whether the `evidence` blob really is `request(37) || response` (the adapter only accepts the split when the 37-byte
  prefix is a signed `GET_MEASUREMENTS` carrying this nonce, so a wrong layout fails closed);
* the field names `evidence`, `certificate`, `arch`, `nonce` and whether entry order equals GPU index (the adapter
  uses the entry at `--gpu-index`, and refuses if there are fewer entries);
* the exact name of the collect-evidence command and its JSON flag.

### Refused: an attestation result

These are rejected with `NVIDIA tool returned an attestation RESULT ... no raw SPDM evidence`, and the confidential
tier does not start:

* a detached EAT array such as `[["JWT","eyJ..."],{"LOCAL_GPU_CLAIMS":["JWT","eyJ..."]}]`;
* a bare JWT on stdout;
* a JSON document with result claims (`eat_nonce`, `x-nvidia-overall-att-result`, `submods`, `ear.status`, ...).

If your only tool prints results, you need a small program built on the attestation SDK's evidence-collection API (or
NVML's confidential-compute attestation calls) that prints Form A or Form B instead. Do not "convert" a result: nothing
in it can be verified by the platform.

## 3. What the agent checks before it sends anything

In `tee.py` after the adapter runs (all fail closed with `TeeError`):

* the request is 37 bytes, SPDM 1.1 (`0x11`), code `GET_MEASUREMENTS` (`0xE0`), with the signature-requested bit set;
* the request carries exactly `SHA-256(challenge)` (a stale or replayed report is refused);
* the response is an SPDM 1.1 `MEASUREMENTS` (`0x60`) of at least 8 bytes;
* the certificate chain is present and made of PEM certificates (at most 8);
* tool output is at most 4 MiB and at most 64 evidence entries.

The agent never verifies a signature and never trusts a claim; the platform does that server side.

## 4. Example wrapper outline (not a tested program)

```text
parse --gpu-index N and --nonce HEX            # reject anything else
report = <SDK or NVML call: confidential-compute attestation report for GPU N, nonce=HEX>   # TODO(verify-docs)
chain  = <SDK or NVML call: device certificate chain for GPU N>                              # TODO(verify-docs)
print JSON { request: hex(report[:37]), response: hex(report[37:]), certificate_chain: split_pem(chain) }
```

The `report[:37]` split mirrors the adapter's assumption and is itself `TODO(verify-docs)`. Verify on hardware with the
checks below.

## 5. Verification checklist for a real host (hardware)

- [ ] Run the wrapper with a known nonce; confirm bytes 4..36 of `request` equal the nonce.
- [ ] Confirm `request[0:2] == 11 E0` and `response[0:2] == 11 60`.
- [ ] Confirm the chain's leaf subject is the GPU and the chain reaches the pinned NVIDIA root.
- [ ] Run `NvidiaGpuCollector.collect` against the wrapper and send the result through
      `POST /v1/agents/attestation/verify`; expect a verdict, not `nvidia_rim_missing`.
- [ ] Record the SDK version, command line, and the date here and in
      [external-interfaces-checked.md](external-interfaces-checked.md).

## 6. Code map

| What | File |
|---|---|
| Adapter and parser (documented forms, fail closed) | `provider-agent/computecoin_agent/nvidia_eat.py` |
| Collector that calls it | `provider-agent/computecoin_agent/tee.py` |
| Tests and authored fixtures | `provider-agent/tests/test_nvidia_eat.py`, `provider-agent/tests/test_tee.py` |
| Server verifier | `apps/api/app/services/hardware_attestation/nvidia.py` |

Windows hosts do not offer the confidential tier (cross-platform plan X5), so this wrapper is Linux-only.
