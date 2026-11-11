"""Agent configuration and local credential storage.

Config lives at ``~/.computecoin/agent.toml`` and may be overridden by
environment variables prefixed ``COMPUTECOIN_`` (e.g. ``COMPUTECOIN_API_URL``).

The config file holds the machine-scoped enrollment token and the machine's
HMAC signing secret, so it is written with 0600 permissions and its parent dir
with 0700. See the trust model in ``computecoin_agent.security``.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

import tomli_w
from pydantic import field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from .platform import get_platform


# ``load`` points the class-level TOML source at the file for the duration of one construction; concurrent loads (the
# supervisor, the executor and the heartbeat thread all call it) must not see each other's setting.
_LOAD_LOCK = threading.RLock()


def default_config_dir() -> Path:
    return get_platform().paths.config_dir()


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
    # Optional GPU vendor override ("nvidia" | "amd") when a host has both vendors (D2)
    gpu_vendor: Optional[str] = None

    @field_validator("gpu_vendor", mode="before")
    @classmethod
    def _check_gpu_vendor(cls, value: Any) -> Optional[str]:
        """Refuse a typo ("AMD ", "intel") instead of silently running CPU-only or as NVIDIA."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        vendor = str(value).strip().lower()
        if vendor not in ("nvidia", "amd"):
            raise ValueError("gpu_vendor must be 'nvidia' or 'amd'")
        return vendor
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
    # ---- Confidential tier (P4): CPU TEE + NVIDIA CC GPU evidence, see computecoin_agent.tee.
    tee_cpu: Optional[str] = None  # "tdx" | "sev_snp"
    nvidia_cc: bool = False  # GPU is in confidential-computing mode and its evidence is sent
    nvidia_attest_cmd: list[str] = []  # NVIDIA evidence tool command line
    tee_snp_product: Optional[str] = None  # AMD KDS product: Milan | Genoa | Turin
    tee_cache_dir: Optional[str] = None  # cached VCEK/ASK or Intel collateral files

    @field_validator("tee_cpu", mode="before")
    @classmethod
    def _check_tee_cpu(cls, value: Any) -> Optional[str]:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        flavor = str(value).strip().lower()
        if flavor not in ("tdx", "sev_snp"):
            raise ValueError("tee_cpu must be 'tdx' or 'sev_snp'")
        return flavor

    # ---- B6 GPU thermal capping (degrees C). At the cap: stop claiming, report "throttled";
    # below resume: claim again; at the hard stop: stop the running container (thermal_shutdown).
    gpu_temp_cap_c: float = 85.0
    gpu_temp_resume_c: float = 80.0
    gpu_temp_hard_stop_c: float = 95.0
    # Optional board power limit in watts, applied only when the agent is permitted to (never fails enrolment).
    gpu_power_limit_w: Optional[float] = None
    # ---- B5 egress proxy image, pinned by digest (repo/name@sha256:...). Unset = jobs with a
    # network_policy are refused on this host (no network is never silently widened).
    egress_proxy_image: Optional[str] = None
    # ---- U3 agent runtime v2 (OWNER DECISIONS: defaults are conservative, see U3 design note).
    # Per-kit read-only host cache of models/assets, content-addressed, LRU-evicted. 0 disables it.
    cache_dir: Optional[str] = None  # default ~/.computecoin/cache
    cache_max_bytes: int = 20 * 1024**3
    work_dir: Optional[str] = None  # staged inputs, checkpoints, outputs (default: system temp)
    checkpoint_min_interval_seconds: int = 60
    checkpoint_max_bytes: int = 10 * 1024**3
    # ---- U18 private Kit jobs: RAM-only (tmpfs) directory for decrypted inputs and outputs. Unset = /dev/shm, then
    # /run/user/<uid>; a directory that is not tmpfs/ramfs is refused (no fallback to disk).
    private_ram_dir: Optional[str] = None
    # ---- U14 Windows VM tier (OWNER go/no-go, default off). A VM claim is run only when this is true AND the host
    # advertises vfio_gpu (vm_runner.advertise()). Needs `vm_work_dir` space for the image and its overlay.
    windows_vm_enabled: bool = False
    # ---- CPU-only Kits (resource_defaults.gpu_count 0). Operator opt-in: when true the heartbeat health block carries
    # ``cpu_capable: true`` so the platform may place a CPU-only Kit job here. Such a job runs without --gpus.
    cpu_only_kits_enabled: bool = False
    # ---- AMD GPUs in the Sealed tiers (A6, OWNER DECISION on D5). Second lock: the platform must allow it (server setting
    # AMD_SEALED_ENABLED and a current verdict naming a Sealed image) AND this host must opt in. Default false: an AMD GPU
    # job in any tier other than `container` is refused. Confidential, gVisor and microVM stay refused on AMD either way.
    amd_sealed_enabled: bool = False
    # ---- U13 multi-node (OWNER DECISION, see ADR-0003 addendum). Name of the internal docker network the operator
    # created with scripts/setup-multinode-net.sh. Unset = a multi-node rank is refused on this host (no network).
    multinode_network: Optional[str] = None
    # Operator ATTESTATION that the group network is non-internal AND confined by host firewall rules installed by
    # scripts/setup-multinode-net.sh (checked with its --verify). Default false: multi-node ranks are refused.
    multinode_network_firewalled: bool = False
    # Benchmark responder + prober (netbench.py). Off by default; needs the machine to have a datacenter_group.
    netbench_enabled: bool = False
    # This host's private cluster address (literal IP). The benchmark responder binds ONLY here.
    cluster_addr: Optional[str] = None
    # ---- What kind of machine this is, shown to customers and usable as a placement filter. "dedicated" = it does nothing
    # but run ComputeCoin jobs (a server); "shared" = a person also uses it (a laptop, a gaming PC), so a job may slow down
    # or be moved if the owner needs the GPU. Unset = not declared (customers see "unspecified", and a customer who asks
    # for a dedicated host will not be placed here). A Windows host always reports "shared", whatever is set here.
    # This is SELF-DECLARED: say "dedicated" only if it is true.
    host_kind: Optional[str] = None

    # ---- W5 resource governance (X13: the host yields to its owner). Defaults are the CONSERVATIVE ones.
    # None = on for Windows hosts, off elsewhere (a Linux desktop opts in with true). See windows/governance/policy.py.
    governance_enabled: Optional[bool] = None
    governance_headless: bool = False  # no desktop session (server): activity signals are not required to be known
    governance_yield_to_owner: bool = True  # foreground GPU app / recent input stop new claims (full-screen always does)
    governance_idle_threshold_seconds: int = 300
    governance_resume_clear_seconds: int = 60  # hysteresis: calm this long before claiming again
    governance_allow_on_battery: bool = False
    governance_vram_reserve_mb: int = 1024  # GPU memory kept free for the owner
    # Allowed hours; empty = no limit. Each: {days = ["mon", ...] | "daily" | "weekdays", start = "22:00", end = "07:00",
    # timezone = "Europe/Berlin"}. A window may cross midnight. A typo is refused when the config loads.
    governance_schedule: list[dict[str, Any]] = []
    governance_ignored_processes: list[str] = []  # extra process names that never count as "the owner is using the GPU"
    governance_stop_now_hold_minutes: int = 0  # 0 = a "stop now" lasts until the owner resumes
    governance_preempt_on_fullscreen: bool = False  # True: stop a RUNNING job when a full-screen app appears
    # ---- W6 updates (signed manifests, rings, rollback; see computecoin_agent/updater/).
    update_enabled: bool = True
    update_ring: str = "general"  # the ring this host ASKS for; the server decides what it may have (never a wider ring)
    update_check_interval_seconds: int = 6 * 3600

    @field_validator("host_kind")
    @classmethod
    def _check_host_kind(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        kind = str(value).strip().lower()
        if kind not in ("dedicated", "shared"):
            raise ValueError("host_kind must be 'dedicated' or 'shared'")
        return kind

    @field_validator("governance_schedule")
    @classmethod
    def _check_governance_schedule(cls, value: list) -> list:
        from .windows.governance.policy import parse_schedule

        parse_schedule(value)  # raises ValueError on a malformed window, unknown zone or day
        return value

    @field_validator("governance_idle_threshold_seconds", "governance_resume_clear_seconds", "governance_vram_reserve_mb",
                     "governance_stop_now_hold_minutes", "update_check_interval_seconds")
    @classmethod
    def _governance_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be >= 0")
        return value

    @field_validator("update_ring", mode="before")
    @classmethod
    def _check_update_ring(cls, value: Any) -> str:
        ring = str(value).strip().lower() if value is not None else "general"
        if ring not in ("canary", "early", "general"):
            raise ValueError("update_ring must be 'canary', 'early' or 'general'")
        return ring

    @field_validator("cache_max_bytes", "checkpoint_max_bytes")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be >= 0")
        return value

    @field_validator("gpu_temp_hard_stop_c")
    @classmethod
    def _check_thermal_order(cls, value: float, info) -> float:
        cap = info.data.get("gpu_temp_cap_c")
        resume = info.data.get("gpu_temp_resume_c")
        if resume is not None and cap is not None and not resume < cap:
            raise ValueError("gpu_temp_resume_c must be below gpu_temp_cap_c")
        if cap is not None and not cap < value:
            raise ValueError("gpu_temp_cap_c must be below gpu_temp_hard_stop_c")
        return value

    @field_validator("egress_proxy_image")
    @classmethod
    def _check_proxy_image(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        if "@sha256:" not in value:
            raise ValueError("egress_proxy_image must be pinned by @sha256: digest")
        return value.strip()

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
        with _LOAD_LOCK:
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
        plat = get_platform()
        plat.paths.restrict_dir(path.parent)  # 0700 on Linux

        data = self.model_dump(exclude_none=True)
        # Create with restrictive perms, then write (0600 file on Linux; an OS secret store on Windows, X7).
        plat.secrets.write_file(path, tomli_w.dumps(data).encode("utf-8"))
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
