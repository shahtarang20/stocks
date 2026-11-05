# Kit: 3d-gaussian-splat-train

Trains a 3D Gaussian splat from a photo set (optionally running COLMAP first) with the gsplat library and writes the trained model and validation renders. Licences: see kits/3d-gaussian-splat-train/README.md.

Status: wave 3 candidate, **code written, never built or run.** The kit list is subject to OWNER approval (kits/README.md).
Billing hint unit: `image`. Verification: `psnr_ssim`.

## Licences (record, owner to confirm before publish, L4)

- **Do NOT use the original graphdeco-inria 3D Gaussian Splatting code or its rasteriser:** its licence is non-commercial research use only; a commercial service would need a separate licence from INRIA. This Kit uses **gsplat** (Apache-2.0) instead. Confirm the pinned gsplat release licence and the licences of every dependency it pulls in (L4).
- **COLMAP**: BSD-3-Clause (the base image must contain it, built without the non-free SIFT-GPU/CUDA parts unless their licence is checked). **PyTorch**: BSD-style.
- Photos and the resulting model stay the customer's. Photos of people or private property raise privacy duties: AUP (U16).

## Pins you must supply

`ASSET_URL` and `ASSET_SHA256` (gsplat source (pinned release) with its example trainer (a tar); no model weights), `BASE_IMAGE`, `FETCH_IMAGE`. All default to `PENDING` and fail the build.
`requirements.txt` versions are NOT yet checked against PyPI or the base image's torch (L4); freeze with hashes in CI.

## Notes

Input `dataset`: a `.zip` with `images/*.jpg|png` (at most 500, 40 MP each) and optionally a COLMAP model in `sparse/0/{cameras,images,points3D}.{bin,txt}`. Names are whitelisted and flat inside those two folders; anything else refuses the archive. With `run_colmap=true` the Kit runs COLMAP (feature extraction, matching, mapper) with fixed arguments and no shell; with `false` the zip must carry `sparse/0`. Then it runs gsplat's example trainer (baked in, arguments to VERIFY against the pinned release, L4). Outputs: `/out/model` (trained checkpoint and exported splat) and `/out/preview` (validation renders used for verification). No resume in v1: the trainer's own checkpoints are not hash-committed, so a retry starts over.

## Owner decisions

- Verification: Training is not deterministic across GPU models (CUDA atomics in the rasteriser). Verification compares held-out validation renders by PSNR/SSIM with bounds set from benchmark data (U5.3); the ADR method (re-run of a sample, metric compare) is approximated this way.
- Whether this Kit is offered at all, its price hints (`est_gpu_seconds_per_unit` is null until benchmarked, U5.3) and the GPU model list in `smoke/smoke.json`.
