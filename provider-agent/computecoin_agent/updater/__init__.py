"""Signed agent updates with staged rings and automatic rollback (cross-platform plan W6, X10).

``manifest``   signed manifest format, strict validation, Ed25519 verification against pinned keys
``version``    version parsing and ordering
``state``      durable updater state (known-good artifact, in-flight update, failed versions, forced floor)
``gate``       the claim gate: no update while a job runs, no claim below the forced floor
``core``       :class:`AgentUpdater`: fetch, verify, download, install, health-check, roll back
``transport``  HTTP manifest fetch and download (injectable)
``msi``        Windows msiexec install step (injected runner)
``pal``        adapter to the PAL ``Updater`` interface

Nothing is executed on import.
"""
