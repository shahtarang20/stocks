"""Verification plug-ins keyed by the kit manifest ``verification.method`` (U6, ADR-0006 section 12).

    outcome = run_kit_verification(manifest, primary_files, reference_files)

Fail closed on configuration, never on the provider's account: an unknown method or invalid
thresholds is ``inconclusive`` (our problem), not ``failed`` (provider's output is bad).
Heavy dependencies (Pillow, numpy, scikit-image, imagehash) are imported lazily inside the plug-ins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .audio_spectral import AudioSpectralPlugin
from .base import (FAILED, INCONCLUSIVE, PASSED, ThresholdError, VerifyOutcome, VerifyPlugin,
                   choose_sample_indices, resolve_thresholds)
from .cosine import CosinePlugin
from .exact import ExactHashPlugin
from .exact_deterministic import ExactDeterministicPlugin
from .loss_curve import LossCurvePlugin
from .perceptual import PerceptualPlugin
from .psnr_ssim import PsnrSsimPlugin
from .wer import WerPlugin

_REGISTRY: dict[str, VerifyPlugin] = {}


def register(plugin: VerifyPlugin) -> None:
    if plugin.method in _REGISTRY:
        raise ValueError(f"verification method {plugin.method!r} already registered")
    _REGISTRY[plugin.method] = plugin


def get_plugin(method: str) -> VerifyPlugin | None:
    return _REGISTRY.get(method)


def methods() -> list[str]:
    return sorted(_REGISTRY)


for _p in (ExactHashPlugin(), PerceptualPlugin(), PsnrSsimPlugin(), WerPlugin(), CosinePlugin(), LossCurvePlugin(),
           AudioSpectralPlugin(), ExactDeterministicPlugin()):
    register(_p)


def run_kit_verification(manifest: dict[str, Any], primary: list[Path], reference: list[Path]) -> VerifyOutcome:
    """Run the plug-in named by ``manifest['verification']['method']`` with the manifest's thresholds."""
    section = manifest.get("verification") if isinstance(manifest, dict) else None
    method = section.get("method") if isinstance(section, dict) else None
    plugin = _REGISTRY.get(method) if isinstance(method, str) else None
    if plugin is None:
        return VerifyOutcome(method=str(method), status=INCONCLUSIVE, reason="unknown_verification_method")
    try:
        merged = resolve_thresholds(plugin.default_thresholds, plugin.threshold_bounds, section.get("thresholds"))
    except ThresholdError as exc:
        return VerifyOutcome(method=plugin.method, status=INCONCLUSIVE, reason=f"invalid_thresholds:{exc}")
    return plugin.verify(list(primary), list(reference), merged)
