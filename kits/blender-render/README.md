# Kit: blender-render

Renders a frame range of a customer `.blend` with the Blender command line (Cycles or Eevee; device AUTO, CPU, CUDA, OPTIX,
HIP). Input slot `scene` (mounted at `/in/scene`), output directory `/out/frames` with `frame_0000001.png` style names.
Billing hint unit: `frame`. Verification: `perceptual` (looser thresholds than ai-image because Cycles is a noisy path tracer).

## Licensing note (record, owner to confirm)

- **Blender is free software under the GNU GPL** (version as stated in the licence files shipped in the tarball you pin;
  believed GPL v3 or later for current releases, verify). Cycles is separately licensed (believed Apache-2.0, verify).
- This kit **redistributes an unmodified Blender build inside a container image**. GPL obligations on redistribution:
  ship the licence text (kept in `/opt/blender`), and make the corresponding source available (point to the official
  source archive for the pinned version in the image's published description). We do not modify Blender.
- **Rendered output and customer scenes are not covered by the GPL**; the customer owns them. The customer's `.blend` may
  itself contain scripts: `--disable-autoexec` is always passed and the container has no network.
- Commercial render add-ons and paid assets need the customer's licence and a network allowlist to a licence server;
  that is the licence-server path in ADR-0006 section 14 and is out of scope for this wave-1 kit (network is always off here).
- Add-ons are not installed; scenes that need add-ons or missing external files (textures by absolute path) fail with `render_failed`.
  Customers should pack resources (`File > External Data > Pack`).

## Devices

`AUTO` picks OPTIX, then CUDA, then CPU. A named device that is not usable fails with `device_unavailable` (no silent
CPU fallback). **HIP needs a ROCm base image**; this wave-1 image is CUDA/CPU. A ROCm variant is a later kit version.

## Pins you must supply

`BLENDER_URL` and `BLENDER_SHA256` (official Linux tarball, from the Blender download site; hash recorded by the owner).
`BASE_IMAGE` (CUDA runtime, digest-pinned), `FETCH_IMAGE` (python, digest-pinned). All default to missing/`PENDING` and
fail the build.

## Smoke scene

`smoke/make_scene.py` builds a 3-frame cube scene (`blender -b --python make_scene.py -- scene.blend`).
