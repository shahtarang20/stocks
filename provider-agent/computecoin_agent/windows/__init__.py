"""Windows host support for the ComputeCoin agent (cross-platform plan W2..W7).

Everything here runs on the WINDOWS side (the native supervisor); the Linux runner inside the managed WSL2 distro is
the unchanged agent (X1, X2). Importing any module of this package must work on Linux (tests) and without pywin32:
every Windows API or third-party call is made lazily, through an injectable module object, so it can be faked.

W2 (this part): ``paths`` (ProgramData layout and ACLs), ``secrets_dpapi`` (DPAPI / Credential Manager), ``logging_setup``,
``service`` (Windows Service plus console fallback), ``supervisor`` (heartbeat, telemetry, thermal, availability,
runner lifecycle), ``enrol`` (enrolment that never leaves a plain-text credential) and ``cli_commands``
(``computecoin-agent service ...``). W4: the control channel itself lives in ``computecoin_agent/control_channel.py``.
Other sub-packages (``runtime``, ``governance``) belong to W3 and W5.

NOTHING HERE HAS RUN ON WINDOWS. Every Windows API, pywin32 and ``wsl.exe`` assumption is marked ``TODO(verify-docs)``
unless the comment names the documentation page it was checked against (X11).
"""
