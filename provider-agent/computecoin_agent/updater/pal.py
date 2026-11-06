"""Adapter: expose :class:`AgentUpdater` through the PAL ``Updater`` interface (platform/__init__.py, plan section 3.2)."""

from __future__ import annotations

from typing import Optional

from ..platform import Updater
from .core import UPDATED, AgentUpdater
from .version import compare


class AgentPalUpdater(Updater):
    can_self_update = True

    def __init__(self, updater: AgentUpdater) -> None:
        self._u = updater

    def current_version(self) -> str:
        return self._u.current_version

    def check(self) -> Optional[str]:
        manifest, _early = self._u.check()
        if manifest is not None and compare(manifest.version, self._u.current_version) > 0:
            return manifest.version
        return None

    def apply(self, version: str) -> None:
        result = self._u.run_once()
        if result.status != UPDATED or result.to_version != version:
            raise RuntimeError(f"update to {version} did not complete: {result.status} {result.code}")
