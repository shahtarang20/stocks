"""Runs INSIDE Blender (bpy) after the scene loads: applies the Kit's render settings deterministically.

Usage (from entrypoint.py): blender -b scene.blend --python blender_setup.py ... -- setup.json
Determinism: fixed Cycles seed (not animated per frame), fixed sample count, adaptive sampling off,
denoising only when asked. CPU vs GPU and different GPU models still differ by sampling noise, so
verification is perceptual. API names to verify against the Blender Python API for the pinned version (L4).
"""

import json
import sys

import bpy  # noqa: E402  (only available inside Blender)


def main():
    cfg = json.load(open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8"))
    scene = bpy.context.scene
    scene.render.resolution_percentage = cfg["resolution_percent"]
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA" if scene.render.film_transparent else "RGB"

    if cfg["engine"] == "EEVEE":
        # Blender 4.2 renamed the engine id to BLENDER_EEVEE_NEXT; pick whichever this build offers.
        ids = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
        scene.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in ids else "BLENDER_EEVEE"
        return

    scene.render.engine = "CYCLES"
    cy = scene.cycles
    cy.samples = cfg["samples"]
    cy.seed = cfg["seed"]
    cy.use_animated_seed = False
    cy.use_adaptive_sampling = False
    cy.use_denoising = bool(cfg["denoise"])

    prefs = bpy.context.preferences.addons["cycles"].preferences
    want = cfg["device"]
    if want == "CPU":
        cy.device = "CPU"
        return
    kinds = ["OPTIX", "CUDA", "HIP"] if want == "AUTO" else [want]
    chosen = None
    for kind in kinds:
        try:
            prefs.compute_device_type = kind
            prefs.get_devices()
        except Exception:
            continue
        gpus = [d for d in prefs.devices if d.type == kind]
        if gpus:
            for d in prefs.devices:
                d.use = d.type == kind
            chosen = kind
            break
    if chosen is None:
        if want == "AUTO":
            cy.device = "CPU"
            return
        print("DEVICE_UNAVAILABLE: " + want)
        sys.exit(3)
    cy.device = "GPU"


main()
