# U18 design note: Privacy across modes (private fan-out and private streaming)

Status: draft for owner approval (L8). Implements WP-U18 (U18.1 private fan-out receipts, U18.2 private session
stream) on top of ADR-0005 (receipts), U4 (fan-out) and B2 (streaming chat). Nothing in the ledger, pricing,
billing, settlement or `contracts/` changes (L3). Everything here is OFF by default (L1):
`PRIVACY_FANOUT_ENABLED=false`, `PRIVACY_STREAMING_ENABLED=false`, and both also need `PRIVACY_ENABLED` (privacy_active).
No database migration: the job-level receipt and the per-part key material live in the existing JSON columns
`fanout_groups.plan` and `fanout_groups.merge_result`.

## 1. Private fan-out

Rule: the platform never sees plaintext. So the platform cannot split a private input. The CUSTOMER splits and
encrypts client-side; the platform only plans, schedules, verifies and orders ciphertext.

Submit (`POST /v1/jobs/v2/fanout`, tier `sealed` | `sealed_pro` | `confidential`, flag on): the body gains
`private_parts`, a list with exactly one entry per planned part:

```json
{ "encrypted_input": {ccpp-v1 data envelope}, "dk_shares": {guardian: {"x": n, "envelope": sealed}},
  "result_public_key": "<hex x25519>" }
```

Each part is a private job with its OWN key set (one job, one key set, R5): its own Data Key (so its own shares and
input envelope) and its own result key. Refused (422, field path `private_parts[i]`): wrong count, a malformed
envelope or share (`validate_sealed_shares`), a reused result key, a reused input envelope or share set across parts.
Also refused for a private fan-out: `split.kind = bytes` (needs plaintext sizes), any Kit `inputs` (data-plane files
are plaintext; inputs travel inside `encrypted_input`), `network` (already refused), flag off
or privacy not active (both `privacy_tier_not_supported_for_fanout`, 422, fail closed). The slice patch (`range`/`items`) is
still recorded per part so the customer and the part's job know the slice; for `items` the customer's encrypted
input already holds the slice, so the planner only needs the count of parts.

Child run: an ordinary private child job (`isolation_tier` = the parent's, `encrypted_input`, `dk_shares`,
`data_policy.result_public_key` of that part) scheduled, attested, key-released, executed, wiped and receipted by the
existing private-job code. A retry re-runs the same part with the same key set (a new job id, so a new host key
release, a new output AAD, a new receipt). Straggler copies are DISABLED for private groups: a second host would
receive a second key release for the same Data Key, which widens exposure for a speed gain.

Merge (`merge_private`): for each part, in index order, the winner job must be COMPLETED with a sealed
`encrypted_output`, a 64-hex `output_commitment`, and a stored privacy receipt that is `verified` and passes
`verify_privacy_receipt_offline`. The merge only verifies and orders. It outputs
`{"fanout": {"merge": "ordered_ciphertext", "outputs": {"sealed": [ {index, job_id, encrypted_output,
output_commitment, receipt_hash} ... ]}, "manifest_sha256", "job_receipt"}}`. It never opens an envelope (it cannot).
Open (customer): each envelope opens with the part's result private key and AAD `ccpp-v1|job-output|<part job id>`.

Job-level receipt: see `docs/specs/ccpp-v1.md` section 6g (`ccpp-fanout-v1`). Summary: part receipt hash =
SHA-256 of the canonical JSON of the part receipt's offline-verifiable body; leaf i =
`hash_leaf("FANOUT_PART_RECEIPT", {index, job_id, receipt_hash})`; root = RFC 6962 Merkle root over the ordered
leaves; the verifier key signs the canonical JSON of the receipt. Offline verification recomputes every leaf, the
root and the signature, and (when the part receipts are supplied) verifies each part receipt and checks its hash.
Retrieval: `GET /v1/fanout/{parent_job_id}/privacy-receipt` (owner only), parts via `GET /v1/privacy/receipts/{job_id}`.

Limits: the job receipt is signed by the platform verifier key and is not itself in the transparency log in this
slice (each part receipt is). Anchoring is the part receipts' anchoring. A partial group never gets a job receipt.
Ciphertext sizes and part counts are visible to the platform (metadata), as for any private job.

## 2. Private streaming (end-to-end sealed chat stream)

No U10 session note exists in the repo, so this note defines the scheme (the same X25519 + AEAD primitives as
ccpp-v1, no new crypto). Per chunk, the agent seals to the customer's `result_public_key` with the existing
`seal()` (fresh ephemeral X25519 key per chunk, HKDF-SHA256, ChaCha20-Poly1305), with AAD
`ccpp-v1|stream|<job_id>|<seq>|<final 0|1>`. Binding job id, sequence number and the final flag makes reorder,
replay across jobs/positions and silent truncation detectable by the customer. Per-chunk ephemeral keys were chosen
over a single session key because it reuses audited code unchanged and each chunk is independently forward-safe;
the cost (one X25519 per chunk) is small next to token generation.

Customer request (`POST /v1/chat/completions`, `stream: true`, private tier, both flags on): extra fields
`encrypted_input` (the real messages, a data envelope), `dk_shares`, `result_public_key`. `messages` must be the single
placeholder `[{"role":"user","content":"sealed"}]` (the schema requires one message; plaintext messages are refused).
Simulated models are refused (`private_requires_hardware_model`): no host exists to encrypt. With a flag off the
old error `streaming_not_supported_for_private` is returned unchanged.

Relay: the agent POSTs `{"seq", "text": "", "sealed": <envelope>, "final"}`. The API relay checks only structure
(`is_sealed_envelope`), ordering (same rules as plaintext), and size (`PRIVACY_STREAM_MAX_CHARS`), stores and forwards
the envelope JSON. It never holds a key. It rejects plaintext `text` for a private job and a `sealed` chunk for a
non-private job or for a stream the customer did not open sealed. SSE events to the customer:
`{"object":"chat.completion.chunk.sealed","id","job_id","seq","final","sealed":{...}}`, then `[DONE]`. There is no
fallback to the final text (the platform has none). If the job ends without a final sealed chunk, the stream ends
with error `sealed_stream_incomplete`; the sealed final result is still on the job (`encrypted_output`).

Agent: the claim carries `stream_sealed: true` only when the job asked for it and the flag is on. The JobMonitor
runs inside the private run (progress reporting off). The private run's output dir is on RAM-only storage (tmpfs, verified
from /proc/mounts; with none, streaming is switched off, never a plaintext file) and `stream.ndjson` there is a named
pipe, read in-process: no plaintext stream is written to disk. Each piece is padded to `PRIVACY_STREAM_PAD_BYTES`
(default 256, 0 = off; `u32 BE length || text || zeros`, ccpp-v1.md 6g), sealed and posted; the relay refuses a chunk whose
ciphertext is not block aligned. Agent and API must use the same value, and the customer's reader must strip the frame
(the Python SDK, TypeScript SDK and web client now do: spec ccpp-v1.md 6g, reader rules).

Private Kit part on the agent (`provider-agent/computecoin_agent/private_kit.py`): a private fan-out part's claim carries
a minimal `v2` block `{"private": true, "kit", "outputs": [{name, path, required}]}` and nothing else (no inputs, assets,
egress, checkpoint, URL). After attestation, key release and the in-RAM decrypt, the agent stages the decrypted
`{"inputs": {name: {"b64"|"text"}}, "params": {...}}` into a tmpfs work dir, mounts the files read-only at `/in/<name>` in the
Kit's hardened container with no network, reads each declared output from `/out`, seals it to the part's `result_public_key`
(AAD `ccpp-v1|job-output-file|<job>|<name>`), uploads only the ciphertext through the data plane (the output upload routes
accept a private fan-out part for this) and zeroes and removes the work dir. The part's `encrypted_output` lists the
sealed files. No tmpfs = the job is refused before any key is released.

What this does and does not give: relay (API, load balancer, logs) and any host process outside the attested
environment see only ciphertext. The host operator is protected only as far as the tier's attestation protects the
agent process and the tmpfs holding `stream.ndjson` (Sealed/Confidential). Timing leaks to the relay; chunk sizes are
hidden to within the padding block. U18.2 still needs the lab test.

## Tests (written, not run in this session)
`apps/api/tests/test_u18_privacy_modes.py`: job receipt root/signature, tamper cases, part-hash check, private
refusals (flag off, bytes split, reused keys, count), ordered ciphertext merge, sealed relay accept/reject,
AAD binding, agent-side sealing round trip.
