"""Configuration for the independent log witness service (ADR-0005 WP21).

Read from the environment; nothing has a usable default because a witness missing
any of these must refuse to cosign (fail closed).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class WitnessConfig:
    name: str = field(default_factory=lambda: os.getenv("WITNESS_NAME", ""))
    # This witness's Ed25519 private seed (hex). Used to cosign valid checkpoints.
    private_key: str = field(default_factory=lambda: os.getenv("WITNESS_PRIVATE_KEY", ""))
    # Pinned Ed25519 public key of the platform's transparency log.
    log_public_key: str = field(default_factory=lambda: os.getenv("LOG_PUBLIC_KEY", "").lower())
    # Where the witness persists its largest seen checkpoint tree size and root hash.
    state_path: str = field(default_factory=lambda: os.getenv("WITNESS_STATE_PATH", "/var/lib/witness/state.json"))

    def missing(self) -> list[str]:
        out = []
        if not self.name:
            out.append("WITNESS_NAME")
        for attr, env in (("private_key", "WITNESS_PRIVATE_KEY"), ("log_public_key", "LOG_PUBLIC_KEY")):
            value = getattr(self, attr)
            try:
                if len(bytes.fromhex(value)) != 32:
                    out.append(env)
            except ValueError:
                out.append(env)
        return out
