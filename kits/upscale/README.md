# Kit: upscale

Upscales input slot `source` (one PNG/JPEG/WEBP, or a `.zip` of up to 500 such files, flat names) 2x or 4x into
`/out/upscaled/<name>_x<scale>.png`. Billing hint unit: `image`. Verification: `perceptual`.

## Licences (record, owner to confirm before publish, L4)

- **Real-ESRGAN** code and the released `RealESRGAN_x4plus` / `RealESRGAN_x4plus_anime_6B` weights: believed
  **BSD-3-Clause** (xinntao/Real-ESRGAN repository). Verify on the pinned release page; BSD-3 requires keeping the
  copyright notice and licence text, so copy the repository LICENSE into the image (add it to the fetch stage once the
  URL is pinned).
- **spandrel** (the loader library): believed MIT. **PyTorch**: BSD-style. **Pillow**: MIT-CMU (HPND).
- The architecture code inside spandrel for RRDBNet is its own re-implementation; check that the weights' licence does not
  add restrictions beyond the above (some community upscaler weights are non-commercial; those are NOT included).
- Customer images stay the customer's; the kit adds no watermark.

## Pins you must supply

`X4PLUS_URL`, `X4PLUS_SHA256`, `X4PLUS_ANIME_URL`, `X4PLUS_ANIME_SHA256` (release asset URLs and recorded hashes; all
default to `PENDING` and fail the build), `BASE_IMAGE`, `FETCH_IMAGE`. Weights are baked in; `/cache` is unused.

## Notes

fp32 inference; tiling with 16 px context padding to avoid seams (`tile=0` disables tiling, needs more VRAM).
Scale 2 resizes the native 4x result down with Lanczos. Video upscaling is composed from this kit and
ffmpeg-transcode in a pipeline (U9); the frame-upscaling step is the separate `video-upscale-frames` kit (written, never built or run).
