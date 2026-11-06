"""Version parsing and ordering for update decisions (semantic-version subset: MAJOR.MINOR.PATCH[-pre][+build])."""

from __future__ import annotations

import re

_VERSION_RE = re.compile(
    r"^(\d{1,6})\.(\d{1,6})\.(\d{1,6})(?:-([0-9A-Za-z][0-9A-Za-z.\-]{0,31}))?(?:\+[0-9A-Za-z][0-9A-Za-z.\-]{0,31})?$"
)


def is_valid_version(value: object) -> bool:
    return isinstance(value, str) and _VERSION_RE.match(value) is not None


def parse_version(value: str) -> tuple:
    """A sortable key. A pre-release (1.2.3-rc1) sorts BELOW its release (1.2.3); build metadata is ignored."""
    m = _VERSION_RE.match(value) if isinstance(value, str) else None
    if m is None:
        raise ValueError(f"invalid version {value!r}")
    major, minor, patch, pre = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
    if pre is None:
        return (major, minor, patch, 1, ())
    parts = tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split("."))
    return (major, minor, patch, 0, parts)


def compare(a: str, b: str) -> int:
    ka, kb = parse_version(a), parse_version(b)
    return (ka > kb) - (ka < kb)
