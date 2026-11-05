# Kit: ai-video

Text-to-video (image-to-video when an image-conditioned model is baked) with a fixed seed per prompt (`seed + index`).
Frames are encoded to `.mp4` with a reproducible ffmpeg libx264 command. Default model: CogVideoX-2B.

Status: wave 3 candidate, **code written, never built or run.** The kit list is subject to OWNER approval (kits/README.md).
Billing hint unit: `video_second`. Verification: `psnr_ssim`.

## Model choice is an OWNER DECISION

CogVideoX-2B was picked as the default because its weights are Apache-2.0 and it fits a 24 GB card. Alternatives of the same
class (for example Lightricks LTX-Video) have different licences (OpenRAIL-style use restrictions on some releases); the
owner chooses the model, reads its licence and records it here before publish. Changing the model means changing `MODEL_REPO`,
`MODEL_REVISION`, `EXPECTED_MANIFEST_SHA256` and the pipeline classes in `entrypoint.py`.

## Licences (record, owner to confirm before publish, L4)

- **CogVideoX-2B** weights (THUDM / Zhipu AI): Apache-2.0 per the model card (verify the card at the pinned revision). The larger
  **CogVideoX-5B** and the **5B-I2V** image-to-video weights use the separate CogVideoX licence (not Apache-2.0); read it before
  use. The default text-to-video build has no image-to-video capability: `MODEL_KIND=t2v` makes the entrypoint refuse an `image`
  input; an image-to-video build needs `MODEL_KIND=i2v` plus a licensed image-conditioned repo.
- **diffusers, transformers, accelerate**: Apache-2.0. **imageio-ffmpeg**: BSD-2-Clause wrapper; the bundled **ffmpeg** binary is
  built with libx264, so it is GPL-2.0-or-later. Shipping it in an image is distribution of GPL software: owner/legal to
  confirm the obligations (source offer, notices) or replace the binary with an LGPL build and another encoder.
- Customer prompts and images stay the customer's. Generated output licence terms follow the model card.

## Pins you must supply

`MODEL_REVISION` (commit), `EXPECTED_MANIFEST_SHA256` (SHA-256 of the sorted per-file listing), optional `MODEL_REPO` and
`MODEL_KIND` (`t2v` or `i2v`), `BASE_IMAGE`, `FETCH_IMAGE`. All default to `PENDING` or fail the build when unpinned.
`requirements.txt` versions are NOT yet checked against PyPI or the base image's torch (L4); freeze with hashes in CI.

## Notes

Output `/out/videos/video_<index>.mp4` per prompt. Checkpoint (COMMIT marker) after each finished video; on resume only
committed videos whose SHA-256 still matches are reused. Video size, frame count (CogVideoX needs 8k+1 frames), steps and
guidance are bounded in `params.schema.json`. In image-to-video mode one `image` input conditions every prompt of the job.
No network at run time; weights are hash-checked at start.

## Owner decisions

- The model and its licence (above).
- Verification: `psnr_ssim` with loose placeholders (mean PSNR 22 dB, worst frame 16 dB, mean SSIM 0.70, equal frame count).
  Diffusion video differs across GPU models by far more than a re-encode, so these must be tuned with U5.3 cross-GPU data;
  `perceptual` is an alternative for single-frame spot checks. Redundancy sampling stays off until the owner decides.
- Whether ffmpeg with libx264 (GPL) may ship, price hints (`est_gpu_seconds_per_unit` is null until benchmarked) and the GPU
  model list in `smoke/smoke.json`.
