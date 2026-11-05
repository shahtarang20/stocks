# Kit: video-upscale-frames

Upscales an ordered zip of numbered video frames 2x or 4x with Real-ESRGAN weights through spandrel and writes numbered PNG frames plus a manifest. Meant to sit between ffmpeg-transcode (extract frames) and a second ffmpeg-transcode step (encode) in a pipeline. Licences: see kits/video-upscale-frames/README.md.

Status: wave 2 candidate, **code written, never built or run.** The kit list is subject to OWNER approval (kits/README.md).
Billing hint unit: `frame`. Verification: `perceptual`.

## Licences (record, owner to confirm before publish, L4)

- Same stack and licences as the wave-1 `upscale` Kit: **Real-ESRGAN** code and released weights believed BSD-3-Clause (keep the LICENSE text in the weights archive), **spandrel** MIT, **PyTorch** BSD-style, **Pillow** MIT-CMU. Verify against the pinned release pages (L4).
- Video content stays the customer's; no watermark. Check that customers have the rights to enhance what they upload (AUP, U16).

## Pins you must supply

`ASSET_URL` and `ASSET_SHA256` (Real-ESRGAN weights RealESRGAN_x4plus.pth and RealESRGAN_x4plus_anime_6B.pth (a tar)), `BASE_IMAGE`, `FETCH_IMAGE`. All default to `PENDING` and fail the build.
`requirements.txt` versions are NOT yet checked against PyPI or the base image's torch (L4); freeze with hashes in CI.

## Notes

Input `frames`: a `.zip` of PNG/JPEG frames named by number only (`1.png`, `0001.jpg`), flat, at most 20000 frames, contiguous numbering required. Output `/out/frames/frame_0000001.png ...` in the same order and `/out/manifest.json` (frame count, scale, fps passed through). The Kit does not decode or encode video. The code duplicates the tiling logic of `upscale` on purpose: Kits share only `_common/kitrt.py`.

## Owner decisions

- Verification: Per-frame fp32 inference differs slightly across GPU models; no temporal smoothing, so flicker is the source's. Defaults pending U5.3 benchmark data.
- Whether this Kit is offered at all, its price hints (`est_gpu_seconds_per_unit` is null until benchmarked, U5.3) and the GPU model list in `smoke/smoke.json`.
