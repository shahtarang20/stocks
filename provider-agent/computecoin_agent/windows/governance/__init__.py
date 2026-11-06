"""Resource governance for hosts that share a machine with their owner (cross-platform plan W5, X13).

``policy``       pure engine: signals in, availability decision out (testable on Linux)
``snapshot``     the session helper's report format and the probe that reads it
``probes``       Windows probes through ctypes (imports safely on any OS; WRITTEN, NEVER RUN ON WINDOWS)
``integration``  ``GovernedGuard``: plugs the engine into the existing ``thermal_guard`` slot

Nothing is executed on import.
"""

from .policy import (  # noqa: F401
    Decision,
    GovernanceEngine,
    GovernancePolicy,
    ProbeUnsupported,
    ScheduleWindow,
    Signals,
    State,
    collect_signals,
    parse_schedule,
)
