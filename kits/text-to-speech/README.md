# Kit: text-to-speech

Synthesises each input text to a 24 kHz mono 16-bit WAV with the open Kokoro-82M model using weights baked into the image.

Status: wave 3 candidate, **code written, never built or run.** The kit list is subject to OWNER approval (kits/README.md).
Billing hint unit: `audio_second`. Verification: `audio_spectral` (new plug-in, `apps/api/app/services/verify_plugins/audio_spectral.py`).

## Model choice is an OWNER DECISION

Kokoro-82M was picked for its small size and permissive licence. Other open TTS models (Piper, Parler-TTS, others) have
different licences and quality; the owner chooses, reads the licence at the pinned revision and records it here.

## Licences (record, owner to confirm before publish, L4)

- **Kokoro-82M** weights (hexgrad): Apache-2.0 per the model card (verify at the pinned revision). **kokoro** package: Apache-2.0.
- **misaki** (phonemiser, Apache-2.0) may use **espeak-ng** (GPL-3.0) as a fallback for out-of-dictionary words; the base image
  must then ship espeak-ng, which is GPL software distributed inside the image. Owner/legal to confirm, or test whether the
  English dictionary path alone is acceptable. Voice files are part of the weights archive; check each voice's licence note.
- Keep the licence files inside the weights archive. Customer text stays the customer's.

## Pins you must supply

`ASSET_URL` and `ASSET_SHA256` (a tar with `config.json`, `kokoro.pth` and `voices/<voice>.pt` for every voice named in
`params.schema.json`), `BASE_IMAGE`, `FETCH_IMAGE`. All default to `PENDING` and fail the build. File names inside the archive
must match `entrypoint.py` (`config.json`, `kokoro.pth`, `voices/`). `requirements.txt` versions are NOT yet checked (L4).

## Notes

Params: `texts` (1 to 64 strings, 2000 characters each), `voice` (one of five voices, language derived from the voice prefix),
`speed` (0.5 to 2.0). Output `/out/audio/speech_<index>.wav`. Checkpoint after each text. No network at run time; a missing
CUDA device fails loudly (no CPU fallback). No seed is needed: inference is deterministic up to GPU float differences.

## Owner decisions

- The model and its licence (above), the voice list, and whether espeak-ng may ship.
- Verification: `audio_spectral` with `min_cosine` 0.90 and `max_duration_diff_s` 0.25, conservative placeholders to be tuned with
  U5.3 cross-GPU data. Redundancy sampling stays off until the owner decides.
- Price hints (`est_gpu_seconds_per_unit` is null until benchmarked) and the GPU model list in `smoke/smoke.json`.
