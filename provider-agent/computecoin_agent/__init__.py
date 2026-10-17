"""ComputeCoin provider agent.

A small, separately-installable agent that a GPU provider runs on their own
machine. In this Phase-1 skeleton it can:

  * detect local hardware (CPU / RAM / disk / GPU) and emit a typed capability
    report,
  * enroll the machine to a provider account using a revocable, machine-scoped
    enrollment token, and
  * send signed heartbeats to the ComputeCoin API.

It deliberately does NOT execute customer jobs yet. See the security notes in
``computecoin_agent.security``.
"""

__version__ = "0.1.0"
