# Kit: ffmpeg-transcode

Re-encodes input slot `source` to `/out/result.mp4` (H.264 or H.265, quality via `crf`, `preset` fast|medium|slow,
optional `height`, `audio` aac|none, optional window `start_seconds` + `duration_seconds`). `encoder=auto` uses NVENC when a
real probe encode succeeds on the host GPU and falls back to the CPU encoder. Billing hint unit: `video_minute`.
Verification: `psnr_ssim` against a re-encode of a sampled window.

## ffmpeg licence build choice: OWNER DECISION

ffmpeg is LGPL 2.1+ by default; enabling libx264 / libx265 makes the whole binary **GPL**. NVENC itself
(`h264_nvenc`, `hevc_nvenc`) is usable in an LGPL build (headers are MIT-licensed; the encoder library comes from the
host's NVIDIA driver, not from this image).

| Build | CPU fallback | Obligations when redistributed in the image |
|---|---|---|
| **GPL build** (default assumed in the Dockerfile comments) | yes (libx264, libx265) | ship licence texts, offer corresponding source for ffmpeg and the GPL libraries |
| **LGPL build** | no (NVENC only); GPUs without NVENC fail with `encoder_unavailable` | ship the licence text, allow relinking (a static binary needs care; a dynamic one is simpler) |

Recommendation (not a decision): start with the GPL build, because several datacentre GPUs have no NVENC (for example
A100/H100) and a kit that cannot encode there would be useless, and publish the source offer with the image. Note also
that H.264 and H.265 encoders may carry **patent licensing** questions in some jurisdictions; that is a legal review item,
not a code one. The Dockerfile copies the licence files found in the tarball to `/opt/ffmpeg`.

Pins you must supply: `FFMPEG_URL` and `FFMPEG_SHA256` (static build tarball containing `bin/ffmpeg` and `bin/ffprobe`
and licence files; layout is an assumption to verify for the tarball you choose), `BASE_IMAGE`, `FETCH_IMAGE`.

## Safety

Only the `file` and `pipe` protocols are allowed (`-protocol_whitelist file,pipe`), so a crafted playlist cannot read other
files or open sockets. Metadata is stripped. Network is off anyway.

## Determinism and resume

NVENC and x264 give different bitstreams, and different GPU generations differ too. Verification compares quality
(PSNR/SSIM) and structure (duration, frame count, streams), never bytes. No checkpoint: a retry re-encodes; long videos should be
split into windows (U4 planner), which is also what the verification reference re-run uses.
