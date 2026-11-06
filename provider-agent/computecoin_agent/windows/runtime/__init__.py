"""Managed WSL2 runtime distro for Windows hosts (cross-platform plan W3, W7, W8; decisions X1, X2, X6, X10, X11).

Everything here runs on the WINDOWS side and drives ``wsl.exe`` with argv lists (never shell strings). The Linux code
that runs inside the distro is the unchanged runner (X1). Nothing in this package is executed on import: every
side effect goes through an injectable callable so tests use fakes. Written, never run on Windows.

Modules
-------
* ``manifest``           signed ``runtime-manifest.json``: Ed25519 verification and strict schema
* ``wsl``                ``wsl.exe`` argv builders, the injectable runner type, output decoding
* ``config_files``       ``/etc/wsl.conf`` rendering, per-distro limits, non-clobbering ``.wslconfig`` merge
* ``provision``          the idempotent provisioner (provision, repair, reset, remove, health, enrolment gate) and CLI
* ``health``             health checks and the GPU smoke test
* ``gpu``                W7: native NVML probe, power limit, multi-GPU device sets, ``--gpus device=`` check spec
* ``network``            W8: egress proxy provisioning step and verification
* ``requirements_check`` the pre-install requirements checker (installer and website)
* ``distro_paths``       constants for paths INSIDE the Linux distro (not host paths; see X8 note in that module)

Every Windows, WSL, NVIDIA and Docker interface assumption is marked ``TODO(verify-docs)`` (X11): none of it has been
checked against current vendor documentation or run on a Windows machine.
"""
