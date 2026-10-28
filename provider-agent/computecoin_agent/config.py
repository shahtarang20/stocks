"""Agent configuration and local credential storage.

Config lives at ``~/.computecoin/agent.toml`` and may be overridden by
environment variables prefixed ``COMPUTECOIN_`` (e.g. ``COMPUTECOIN_API_URL``).

The config file holds the machine-scoped enrollment token and the machine's
HMAC signing secret, so it is written with 0600 permissions and its parent dir
with 0700. See the trust model in ``computecoin_agent.security``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional

import tomli_w
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


def default_config_dir() -> Path:
    return Path(os.environ.get("COMPUTECOIN_CONFIG_DIR", str(Path.home() / ".computecoin")))


def default_config_path() -> Path:
    return default_config_dir() / "agent.toml"


class AgentConfig(BaseSettings):
    """Agent configuration.

    Values resolve from (lowest -> highest precedence): defaults, the toml file
    loaded via :meth:`load`, then ``COMPUTECOIN_*`` environment variables.
    """

    model_config = SettingsConfigDict(
        env_prefix="COMPUTECOIN_",
        extra="ignore",
        toml_file=None,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence (first wins): explicit init args > env vars > toml file >
        # defaults. This is what makes COMPUTECOIN_* env vars OVERRIDE the file.
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    api_url: str = "https://api.computecoin.example"
    # Set once the machine is enrolled.
    machine_id: Optional[str] = None
    provider_id: Optional[str] = None
    enrollment_token: Optional[str] = None
    # HMAC signing secret for heartbeats (hex). Generated locally in the Phase 1
    # offline path; kept for backward compatibility.
    signing_secret: Optional[str] = None
    # Machine credential returned by the backend at enrollment (``msk_...``). The
    # agent signs every agent-endpoint request (heartbeats) with it per the
    # Phase 2 contract. Returned once; we store it 0600 and never transmit it in
    # cleartext -- only HMAC signatures derived from it leave the host.
    machine_secret: Optional[str] = None
    heartbeat_interval_seconds: int = 30
    online_window_seconds: int = 90
    # ---- TPM 2.0 (hardware attestation, WP-F). Unset = no TPM evidence is sent.
    # tpm2-tools TCTI, e.g. "device:/dev/tpmrm0" (real host) or "swtpm:host=127.0.0.1,port=2321".
    tpm_tcti: Optional[str] = None
    # PEM file of the EK certificate when the TPM has none in NV (and its issuing CA chain).
    tpm_ek_cert_path: Optional[str] = None
    tpm_ek_intermediate_paths: list[str] = []
    # PCRs the quote covers (SHA-256 bank); must include every PCR the trusted image pins.
    tpm_pcrs: list[int] = [0, 1, 2, 3, 4, 5, 6, 7]
    # Optional unpacked tpm2-tools tree (<root>/usr/bin, <root>/usr/lib/...) instead of the system one.
    tpm_tools_root: Optional[str] = None

    def tpm(self):
        """A ``Tpm2`` for the configured TPM, or None when no TPM is configured."""
        if not self.tpm_tcti:
            return None
        from .tpm import Tpm2

        return Tpm2(self.tpm_tcti, tools_root=self.tpm_tools_root, ek_cert_path=self.tpm_ek_cert_path,
                    ek_intermediate_paths=self.tpm_ek_intermediate_paths, pcrs=self.tpm_pcrs)

    # ---- persistence -------------------------------------------------------- #
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AgentConfig":
        """Load config from the toml file (if present), with env overrides.

        Env vars take precedence over file values (see
        ``settings_customise_sources``).
        """
        path = path or default_config_path()
        # Point the TOML source at the requested file for this construction.
        cls.model_config["toml_file"] = str(path) if path.exists() else None
        try:
            return cls()
        finally:
            cls.model_config["toml_file"] = None

    def save(self, path: Optional[Path] = None) -> Path:
        """Write config to toml with 0600 perms (0700 on the parent dir)."""
        path = path or default_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, stat.S_IRWXU)  # 0700
        except OSError:
            pass

        data = self.model_dump(exclude_none=True)
        # Write atomically-ish: create with restrictive perms, then write.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(tomli_w.dumps(data).encode("utf-8"))
        finally:
            # Ensure perms even if umask interfered on some platforms.
            try:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
            except OSError:
                pass
        return path

    def is_enrolled(self) -> bool:
        # Enrolled either via the Phase 2 backend path (machine_secret) or the
        # Phase 1 offline path (signing_secret).
        return bool(self.machine_id and (self.machine_secret or self.signing_secret))

    def has_machine_credential(self) -> bool:
        """True iff a backend-issued machine credential is present (Phase 2)."""
        return bool(self.machine_id and self.machine_secret)

    @classmethod
    def load_machine_credential(
        cls, path: Optional[Path] = None
    ) -> tuple[Optional[str], Optional[str]]:
        """Return ``(machine_id, machine_secret)`` from the stored config."""
        cfg = cls.load(path)
        return cfg.machine_id, cfg.machine_secret
