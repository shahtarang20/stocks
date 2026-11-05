# Kit: llm-finetune-lora

Trains a LoRA adapter on one baked base model from a JSONL dataset with peft and PyTorch. Runs on one GPU, or on several GPUs of one host (and several nodes when U13 multi-node is enabled) through torchrun. Licences: see kits/llm-finetune-lora/README.md.

Status: wave 2 candidate, **code written, never built or run.** The kit list is subject to OWNER approval (kits/README.md).
Billing hint unit: `training_example`. Verification: `loss_curve`.

## Licences (record, owner to confirm before publish, L4)

- **Base model licence is the owner's decision** and decides whether customers may use the adapter commercially (Llama community licence, Apache-2.0, Gemma terms, ...). Record the exact model, revision and licence here before publishing and name it in the Kit description.
- **transformers**, **peft**: Apache-2.0. **PyTorch**: BSD-style. **safetensors**: Apache-2.0.
- The adapter belongs to the customer; the base model's licence terms still bind its use.
- Customer datasets are never logged; metrics hold only aggregate numbers.

## Pins you must supply

`ASSET_URL` and `ASSET_SHA256` (the base model (a tar of a Hugging Face model directory, safetensors) and its tokenizer), `BASE_IMAGE`, `FETCH_IMAGE`. All default to `PENDING` and fail the build.
`requirements.txt` versions are NOT yet checked against PyPI or the base image's torch (L4); freeze with hashes in CI.

## Notes

Dataset `dataset`: JSONL, one object per line, either `{"text": ...}` or `{"prompt": ..., "completion": ...}` (caps: 200000 rows, 512 MiB, 64 KiB per row).

Multi-GPU (U13): the platform gives the container N GPUs of ONE host (and `COMPUTECOIN_GPU_COUNT=N`); the entrypoint re-launches itself with `torchrun --standalone --nproc_per_node=N`. With the multi-node rendezvous variables (`NNODES` > 1, `MASTER_ADDR`, ...) it uses them instead. The effective batch is fixed by `global_batch_size`: gradient accumulation is derived as `global_batch_size / (micro_batch_size * world_size)` and the job is refused (`invalid_param`) unless that is a whole number, so results do not depend on the GPU count by design. Seeds are `seed + rank`. A checkpoint (adapter, optimizer state, epoch) is committed at every epoch end by rank 0 and restored on `COMPUTECOIN_RESUME=1`; resuming with a different world size is refused.

## Owner decisions

- Verification: `loss_curve` plug-in (final loss within 0.25 relative, OWNER DECISION, to be tuned with U5.3 data). `sample_fraction` is 0.0: redundancy stays off until the owner enables it.
- Whether this Kit is offered at all, its price hints (`est_gpu_seconds_per_unit` is null until benchmarked, U5.3) and the GPU model list in `smoke/smoke.json`.
