# Kit: whisper-transcribe

Transcribes one audio or video file with faster-whisper (CTranslate2) using Whisper weights baked into the image. Greedy or beam decoding at temperature 0. Licences: see kits/whisper-transcribe/README.md.

Status: wave 2 candidate, **code written, never built or run.** The kit list is subject to OWNER approval (kits/README.md).
Billing hint unit: `audio_second`. Verification: `wer`.

## Licences (record, owner to confirm before publish, L4)

- **Whisper** code and weights (OpenAI): MIT. **faster-whisper**: MIT. **CTranslate2**: MIT. **PyAV** (audio decoding, LGPL, bundles FFmpeg libraries): check the wheel's licence notice and the FFmpeg codecs it ships.
- The converted weights are a derivative of OpenAI's; keep the OpenAI MIT notice in the weights archive.
- Customer audio stays the customer's.

## Pins you must supply

`ASSET_URL` and `ASSET_SHA256` (Whisper model converted to CTranslate2 format (a tar of the model directory, for example large-v3)), `BASE_IMAGE`, `FETCH_IMAGE`. All default to `PENDING` and fail the build.
`requirements.txt` versions are NOT yet checked against PyPI or the base image's torch (L4); freeze with hashes in CI.

## Notes

Input `audio` is one file (any container PyAV can decode; size cap 4 GiB). Output `/out/transcripts/transcript.<fmt>`. Splitting long audio into segments is the planner's job (U4). No checkpoint: a single pass.

## Owner decisions

- Verification: `wer` plug-in (max word error rate 0.10, OWNER DECISION, to be tuned with U5.3 cross-GPU data). Redundancy sampling stays the owner's call.
- Whether this Kit is offered at all, its price hints (`est_gpu_seconds_per_unit` is null until benchmarked, U5.3) and the GPU model list in `smoke/smoke.json`.
