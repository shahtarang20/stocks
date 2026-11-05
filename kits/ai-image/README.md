# Kit: ai-image

Text-to-image with diffusers. Manifest `kit.json`, params `params.schema.json`, container `Dockerfile` + `entrypoint.py`,
smoke spec `smoke/smoke.json`. Billing hint unit: `image`. Verification: `perceptual`.

## Model and licence: OWNER DECISION (made provisionally, record your final choice here)

Chosen provisionally: **FLUX.1-schnell** (`black-forest-labs/FLUX.1-schnell`), published under **Apache-2.0** per its model
card. That is the most permissive licence among current Stable-Diffusion-class models, and it allows commercial use and
redistribution inside an image without use-based restrictions. Its text encoders (T5-XXL, CLIP-L) are separate components
with their own licences (believed Apache-2.0 and MIT). **All of this must be re-read on the model card and in the
repository's LICENSE files at the pinned revision before the first publish (L4); the Dockerfile copies `LICENSE*` files
into `/opt/model` so the image carries them.**

| Option | Licence (as understood, verify) | Why not chosen |
|---|---|---|
| FLUX.1-schnell (chosen) | Apache-2.0 | needs about 24 GB VRAM (bf16 with CPU offload) |
| FLUX.1-dev | non-commercial | commercial use not allowed |
| SDXL base 1.0 | CreativeML OpenRAIL++-M | permitted, but with use-based restrictions that must be passed on; less permissive |
| SD 1.5 / 2.1 | CreativeML OpenRAIL-M | same, and original repository availability is uncertain |
| SDXL-Turbo / SD3 | non-commercial / community licence | revenue limits or non-commercial |

If the owner prefers a smaller card requirement (12-16 GB), SDXL is the fallback; then change `MODEL_REPO`, the pipeline class
in `entrypoint.py` (`StableDiffusionXLPipeline`), `resource_defaults.min_vram_gb`, and this table.

Before the first build the owner records: `MODEL_REVISION` (exact commit) and `EXPECTED_MANIFEST_SHA256` (hash of the
per-file SHA-256 listing, computed once on a trusted machine). Both default to `PENDING` and the build fails until set.
Weights are baked into the image (about 33 GB); the manifest has no asset field, so the `/cache` mount is unused by this kit.

## Content safety and abuse

ADR-0006 section 15 requires content-safety handling for generative Kits where the law requires it. **Not implemented in this
kit**: a safety checker choice and the jurisdiction list are an owner and legal decision. Do not publish to the public
catalogue before that decision.

## Determinism

Image `i` uses seed `seed + i` on a CPU generator (identical noise on every GPU), fixed steps, guidance 0. Results across GPU
models and driver versions differ in float rounding, so the verification method is `perceptual`, never `exact`.

## Thresholds

`kit.json` `verification.thresholds` is empty, meaning the conservative defaults in
`apps/api/app/services/verify_plugins/perceptual.py`. OWNER DECISION: tune from honest cross-GPU runs (U5.3).
`est_gpu_seconds_per_unit` is `null` until benchmarks exist; no number is invented.
