"""Enrolment on Windows without a plain-text credential at rest (W2.3, X7, L7).

``enroll_remote`` (unchanged, shared) persists the returned ``machine_secret`` into ``agent.toml``. On Windows that file
lives in the ACL-restricted ``%ProgramData%\\ComputeCoin`` directory, and this module moves the secret into DPAPI
(``secrets_dpapi``) and rewrites the file WITHOUT it. If the store fails AFTER the server accepted the (single-use)
token, the file is deliberately NOT deleted (that would destroy the only copy of the credential): the error says so and
``computecoin-agent service migrate --from <file>`` finishes the job once the store works. Known limit: between ``enroll_remote`` writing the file and the scrub, the secret is on disk inside the
SYSTEM/Administrators-only directory. Closing that window needs a ``persist`` hook in ``enrollment.enroll_remote``
(shared file, not changed by W2).

``load_protected_config`` is the reverse for the service: it loads the non-secret toml and fills the secrets from the
store, so ``machine_secret`` exists only in this process's memory.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, Optional

from .paths import Layout, ensure_layout, install_acl_platform
from .secrets_dpapi import SECRET_CONFIG_KEYS, DpapiSecretStore, SecretStoreError, migrate_plain_config


def _default_store(layout: Layout) -> DpapiSecretStore:
    from .paths import restrict_path

    return DpapiSecretStore(layout.secrets_dir, prepare_directory=restrict_path)


def load_protected_config(layout: Layout, store: Any) -> Any:
    """``AgentConfig`` from ``layout.config_file`` with the secret fields taken from ``store`` (never from disk)."""
    from ..config import AgentConfig

    cfg = AgentConfig.load(layout.config_file)
    for key in SECRET_CONFIG_KEYS:
        value = store.load(key)
        if value:
            setattr(cfg, key, value)
    return cfg


def default_runtime_gate(layout: Layout, *, provisioner_factory: Optional[Callable[[Any], Any]] = None) -> Any:
    """The enrolment gate (W3): the managed runtime must be healthy and pass the GPU smoke test BEFORE the server is
    contacted. Reads ``runtime-settings.json`` and the signed ``runtime-manifest.json`` from the ProgramData layout and
    calls ``RuntimeProvisioner.assert_ready_for_enrolment``. Raises ``RuntimeNotReady`` (with the reasons) when the
    runtime is not ready, or when it was never provisioned; returns the ``GateResult`` otherwise."""
    from .runtime.provision import RuntimeNotReady, RuntimeProvisioner, RuntimeSettings

    settings_file, manifest_file = layout.runtime_settings_file, layout.runtime_manifest_file
    if not settings_file.is_file() or not manifest_file.is_file():
        raise RuntimeNotReady(
            f"the managed runtime is not set up: {settings_file.name} and {manifest_file.name} were not found in "
            f"{layout.root}. Run the runtime provisioner (provision.ps1 -Action provision) first, then enrol"
        )
    try:
        settings = RuntimeSettings.from_file(str(settings_file))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeNotReady(f"the runtime settings could not be read: {type(exc).__name__}") from None
    prov = provisioner_factory(settings) if provisioner_factory else RuntimeProvisioner(settings)
    return prov.assert_ready_for_enrolment(manifest_file.read_bytes())


def enroll_protected(
    server: str,
    token: str,
    report: Any,
    *,
    layout: Optional[Layout] = None,
    store: Any = None,
    enroll_fn: Optional[Callable[..., Any]] = None,
    acl_runner: Any = None,
    runtime_gate: Optional[Callable[[Layout], Any]] = None,
) -> Any:
    """Enrol against the backend and keep the credential only in the secret store.

    Returns the enrolment result with ``machine_secret`` blanked. Raises ``EnrollmentError`` exactly as
    ``enroll_remote`` does (nothing is stored on a rejected token), or :class:`SecretStoreError` if the credential
    cannot be protected (the credential then remains in the ACL-restricted ``agent.toml`` until migrated).

    BEFORE anything is written or the server is contacted, ``runtime_gate(layout)`` must pass (default:
    :func:`default_runtime_gate`, i.e. ``RuntimeProvisioner.assert_ready_for_enrolment``); otherwise
    ``RuntimeNotReady`` is raised and the single-use enrolment token is not spent. Tests inject their own gate.
    This is the Windows path only: the Linux enrolment (``enrollment.enroll_remote``) is unchanged.
    """
    from ..enrollment import enroll_remote

    layout = layout or Layout.default()
    (runtime_gate or default_runtime_gate)(layout)
    store = store or _default_store(layout)
    ensure_layout(layout, acl_runner)
    install_acl_platform(layout, acl_runner)  # AgentConfig.save -> Paths.restrict_dir applies the ACL
    fn = enroll_fn or enroll_remote
    result = fn(server, token, report, config_path=layout.config_file)
    try:
        migrate_plain_config(layout.config_file, store)
    except SecretStoreError as exc:
        raise SecretStoreError(
            f"{exc}. The credential is still in {layout.config_file.name} inside the restricted directory; "
            "run `computecoin-agent service migrate` once the secret store works"
        ) from None
    return dataclasses.replace(result, machine_secret="")


def migrate_legacy_credential(
    layout: Layout, store: Any, legacy_config: "str | Any"
) -> Any:
    """Move a pre-existing plain ``~/.computecoin/agent.toml`` into ProgramData + the store, then securely delete it.

    The non-secret part of the old file is written to ``layout.config_file`` (only if that file does not exist yet, so
    a newer config is never overwritten); the old file is overwritten and removed only after the secrets round-trip.
    """
    from pathlib import Path

    legacy = Path(legacy_config)
    ensure_layout(layout)
    dest = layout.config_file if not layout.config_file.exists() else layout.root / "agent.migrated.toml"
    return migrate_plain_config(legacy, store, dest=dest)
