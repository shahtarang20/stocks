"""Supervisor logging: rotating file plus the Windows Event Log, with secrets redacted (W2, L7).

* file: ``<root>\\logs\\supervisor.log``, ``RotatingFileHandler`` (5 MiB x 5 by default);
* Event Log: stdlib ``logging.handlers.NTEventLogHandler`` (needs pywin32; skipped with a note when unavailable);
  only WARNING and above go there so the Application log is not flooded;
* every record passes :class:`RedactingFilter`, which masks ``msk_`` machine secrets, ``enr_`` enrolment tokens and any
  long hex string (control tokens, signatures) before a handler sees it.

TODO(verify-docs): ``NTEventLogHandler(appname, dllname=None, logtype='Application')`` is a stdlib class whose
Windows-only behaviour (needs ``win32evtlogutil``; without a registered message DLL entries show a generic
"description cannot be found" prefix) was not checked on a real machine.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path
from typing import Any, Optional

APP_NAME = "ComputeCoinAgent"
_PATTERNS = (
    re.compile(r"\bmsk_[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\benr_[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
)


def redact(text: str) -> str:
    for pat in _PATTERNS:
        text = pat.sub("[redacted]", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not lose the record
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info:  # tracebacks may quote values
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def configure_logging(
    logs_dir: "str | Path",
    *,
    level: int = logging.INFO,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
    event_log: bool = True,
    console: bool = False,
    logger_name: str = "computecoin_agent",
    nt_handler_factory: Any = None,
) -> logging.Logger:
    """Attach the handlers to the ``computecoin_agent`` logger (idempotent) and return it."""
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    for h in list(logger.handlers):
        if getattr(h, "_cc_supervisor", False):
            logger.removeHandler(h)
            h.close()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    redactor = RedactingFilter()

    Path(logs_dir).mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            Path(logs_dir) / "supervisor.log", maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
    ]
    if console:
        handlers.append(logging.StreamHandler())
    if event_log:
        factory = nt_handler_factory or logging.handlers.NTEventLogHandler
        try:
            nt = factory(APP_NAME)
            nt.setLevel(logging.WARNING)
            handlers.append(nt)
        except Exception as exc:  # noqa: BLE001 - not on Windows / pywin32 missing / no rights
            logger.warning("Windows Event Log handler unavailable: %s", type(exc).__name__)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(redactor)
        h._cc_supervisor = True  # type: ignore[attr-defined]
        logger.addHandler(h)
    return logger
