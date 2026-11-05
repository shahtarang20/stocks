# Kit: batch-embeddings

Computes sentence embeddings for a list of texts with one baked encoder model (transformers, mean pooling) and writes a NumPy matrix and the id list. Licences: see kits/batch-embeddings/README.md.

Status: wave 2 candidate, **code written, never built or run.** The kit list is subject to OWNER approval (kits/README.md).
Billing hint unit: `text`. Verification: `cosine`.

## Licences (record, owner to confirm before publish, L4)

- **The encoder model's licence is the owner's decision** (many are Apache-2.0 or MIT; some are research-only). Record the exact model, revision and licence here before publishing.
- **transformers**: Apache-2.0. **PyTorch**: BSD-style. **NumPy**: BSD-3-Clause.
- Customer texts are never logged.

## Pins you must supply

`ASSET_URL` and `ASSET_SHA256` (one encoder model (a tar of a Hugging Face model directory) and tokenizer), `BASE_IMAGE`, `FETCH_IMAGE`. All default to `PENDING` and fail the build.
`requirements.txt` versions are NOT yet checked against PyPI or the base image's torch (L4); freeze with hashes in CI.

## Notes

Input `texts`: UTF-8 JSONL with `{"id": ..., "text": ...}` per line, or plain text with one text per line (ids become the line numbers). Caps: 1,000,000 texts, 1 GiB, 1 MiB per text. Output `/out/embeddings/embeddings.npy` (float32, rows in input order, fixed batch order, no length sorting) and `ids.json`. Checkpoint: a chunk file of 2048 rows is committed at a time; resume reuses committed chunks.

## Owner decisions

- Verification: `cosine` plug-in (per-vector minimum cosine 0.99, ordered ids must match, OWNER DECISION, to be tuned with U5.3 cross-GPU data). Redundancy sampling stays the owner's call.
- Whether this Kit is offered at all, its price hints (`est_gpu_seconds_per_unit` is null until benchmarked, U5.3) and the GPU model list in `smoke/smoke.json`.
