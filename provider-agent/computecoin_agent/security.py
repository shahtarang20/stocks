"""Security model for the ComputeCoin provider agent.

This module is documentation-as-code: it holds no logic, only the non-negotiable
rules the agent is built around. They are repeated in docs/security/threat-model.md
and docs/architecture/ADR-0003-isolation.md.

TRUST MODEL
-----------
* The provider's machine is SEMI-TRUSTED hardware owned by a third party. The
  control plane does not trust the machine's self-reported capabilities; they are
  marked DETECTED (self-reported) and only a future, server-side benchmark /
  attestation step may mark them VERIFIED. This skeleton NEVER emits VERIFIED.
* The agent authenticates to the API with a machine-scoped keypair derived at
  enrollment. The enrollment token is revocable and scoped to a single machine,
  so a leaked token cannot be replayed across the fleet.
* Heartbeats are signed (HMAC-SHA256 over a canonical payload) so the control
  plane can reject spoofed or tampered health reports.

WHAT THIS AGENT MUST NOT DO (enforced by the Executor contract, not by omission)
--------------------------------------------------------------------------------
* MUST NOT execute arbitrary customer shell commands on the host. Customer
  workloads run ONLY through an approved Executor
  (``computecoin_agent.executor``); there is no shell-out-to-customer path.
* MUST NOT mount the host filesystem, the Docker socket, or host credentials
  into any workload, and MUST NOT run ``--privileged``.
* MUST run containerized workloads under least privilege: pinned image by
  digest, ``--network none`` by default, non-root ``--user``, ``--cap-drop ALL``,
  ``--read-only`` rootfs + a dedicated writable tmpfs/output dir, ``--pids-limit``,
  ``--memory``/``--cpus`` quotas, ``--gpus`` only for GPU jobs, and a hard timeout.

JOB EXECUTION (Phase 4) -- what is and is NOT isolated
------------------------------------------------------
Job execution, artifact upload, and lease/heartbeat handling ARE implemented
(see ``executor.py`` / ``jobrunner.py``). Two executors exist:

* ``SimulatedExecutor`` (default; ``image_digest`` null / no Docker+GPU): an
  in-process DETERMINISTIC transform with NO network and NO filesystem escape.
  Every output is stamped ``execution_mode="simulated"`` / ``simulated=true`` and
  is NEVER presented as real inference.
* ``DockerExecutor`` (only with a pinned ``image_digest`` AND Docker available):
  a hardened container per the rules above. A hardware job with no usable Docker
  raises ``HardwareUnavailableError`` -- it is NEVER silently simulated.

ISOLATION HONESTY: an ordinary hardened container is defence-in-depth, NOT a
proven sandbox escape boundary (see ADR-0003). What is NOT guaranteed: kernel-
level isolation against a container-escape 0-day, or protection of the host from
a privileged-escalation bug in the container runtime itself. VERIFIED (vs
DETECTED) hardware attestation remains server-side and is out of agent scope.
"""

# Human-readable constants other modules / the CLI can surface.
JOB_EXECUTION_IMPLEMENTED = True

FORBIDDEN_IN_WORKLOADS = (
    "host filesystem mounts",
    "docker socket passthrough",
    "host credential injection",
    "privileged containers",
    "unrestricted network egress",
)
