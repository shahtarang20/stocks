"""Session snapshots: how the supervisor learns what only the user's desktop session can see.

A Windows Service runs in session 0. ``GetLastInputInfo`` (idle time), ``GetForegroundWindow`` and
``SHQueryUserNotificationState`` (full-screen) describe the INTERACTIVE session, so the service cannot call them
itself. A small helper process in the user's session (``python -m computecoin_agent.windows.governance.probes
--snapshot-json``) calls them and reports one JSON object per interval; the supervisor feeds those objects to
:class:`SnapshotProbe`, which implements the PAL ``ActivityProbe`` and the owner-GPU probe for the governance engine.

Fail closed (L1): a snapshot older than ``max_age_seconds`` is an error, not "the owner is away". The one exception is
when the supervisor knows no user is logged in (``no_session()`` is True): then nobody is using the machine.

Schema (version 1), all keys required, ``null`` = unknown::

    {"v": 1, "taken_at": <unix seconds float>, "fullscreen": bool|null, "idle_seconds": float|null,
     "foreground_gpu_app": str|null, "free_vram_mb": int|null}

AUTHENTICATED CHANNEL (W11 7.5). The helper writes one SIGNED file instead of an open stream: line 1 is the snapshot JSON,
line 2 is ``HMAC-SHA256(key, "cc-snapshot-v1|" || line1)`` in lower-case hex. ``key`` is a per-install random 256-bit value
kept in the secret store (``SNAPSHOT_KEY_NAME``); the helper reads its copy from a key file inside the ``helper``
directory, which is restricted to SYSTEM, Administrators and the helper's own account (``windows/governance/helper.py``).
:class:`SignedSnapshotSource` checks, in this order and failing closed on each: size cap, strict ASCII layout, MAC
(constant time), strict decode, and that ``taken_at`` never goes backwards; :class:`SnapshotProbe` then applies the
freshness window. Anything that fails raises :class:`SnapshotError`, which the governance collector records as a probe
error, and a probe error BLOCKS claims. Residual risk, stated plainly: code running as the helper's own account can
read the key and forge a snapshot (it could equally just use the computer); the MAC stops every other local account and
any process that cannot read the helper directory.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ...platform import ActivityProbe, ActivitySnapshot
from .policy import ProbeUnsupported

SCHEMA_VERSION = 1
SNAPSHOT_KEY_NAME = "snapshot_key"  # secret-store name of the per-install HMAC key (64 hex chars)
SNAPSHOT_MAC_DOMAIN = b"cc-snapshot-v1|"
MAX_SNAPSHOT_FILE_BYTES = 4096  # a real snapshot is about 200 bytes; refuse anything bigger before parsing
_KEY_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_MAC_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_AWAY_IDLE_SECONDS = 10.0**9  # "no one has touched anything for a very long time"


class SnapshotError(RuntimeError):
    """The helper's report is missing, stale or malformed."""


def encode_snapshot(
    *,
    taken_at: float,
    fullscreen: Optional[bool],
    idle_seconds: Optional[float],
    foreground_gpu_app: Optional[str],
    free_vram_mb: Optional[int],
) -> str:
    return json.dumps(
        {
            "v": SCHEMA_VERSION,
            "taken_at": taken_at,
            "fullscreen": fullscreen,
            "idle_seconds": idle_seconds,
            "foreground_gpu_app": foreground_gpu_app,
            "free_vram_mb": free_vram_mb,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def decode_snapshot(raw: Any) -> dict:
    """Strict parse of one helper line; raises :class:`SnapshotError` on anything unexpected."""
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else dict(raw)
    except (ValueError, TypeError):
        raise SnapshotError("snapshot is not valid JSON")
    if not isinstance(data, dict) or data.get("v") != SCHEMA_VERSION:
        raise SnapshotError("unsupported snapshot version")
    expected = {"v", "taken_at", "fullscreen", "idle_seconds", "foreground_gpu_app", "free_vram_mb"}
    if set(data) != expected:
        raise SnapshotError("snapshot has missing or unknown fields")
    taken = data["taken_at"]
    if isinstance(taken, bool) or not isinstance(taken, (int, float)) or not math.isfinite(taken):
        raise SnapshotError("bad taken_at")
    if data["fullscreen"] is not None and not isinstance(data["fullscreen"], bool):
        raise SnapshotError("bad fullscreen")
    idle = data["idle_seconds"]
    if idle is not None and (isinstance(idle, bool) or not isinstance(idle, (int, float)) or not math.isfinite(idle) or idle < 0):
        raise SnapshotError("bad idle_seconds")
    app = data["foreground_gpu_app"]
    if app is not None and (not isinstance(app, str) or len(app) > 260):
        raise SnapshotError("bad foreground_gpu_app")
    free = data["free_vram_mb"]
    if free is not None and (isinstance(free, bool) or not isinstance(free, int) or free < 0):
        raise SnapshotError("bad free_vram_mb")
    return data


# --------------------------------------------------------------------------- #
# Authenticated channel: per-install key, signed file, verifying source
# --------------------------------------------------------------------------- #
def generate_snapshot_key() -> str:
    """A fresh 256-bit key as 64 lower-case hex characters."""
    return secrets.token_hex(32)


def ensure_snapshot_key(store: Any) -> str:
    """The per-install snapshot key: created once, kept in the secret store (DPAPI), never generated twice."""
    existing = store.load(SNAPSHOT_KEY_NAME)
    if existing:
        if not _KEY_HEX_RE.match(existing):
            raise SnapshotError("the stored snapshot key is malformed")
        return existing
    key = generate_snapshot_key()
    store.store(SNAPSHOT_KEY_NAME, key)
    return key


def key_bytes(key_hex: Any) -> bytes:
    """Strict decode of the hex key (exactly 64 lower-case hex characters, so 32 bytes)."""
    if not isinstance(key_hex, str) or not _KEY_HEX_RE.match(key_hex.strip()):
        raise SnapshotError("the snapshot key is missing or malformed")
    return bytes.fromhex(key_hex.strip())


def _mac(key: bytes, line: str) -> str:
    return hmac.new(key, SNAPSHOT_MAC_DOMAIN + line.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_snapshot_line(key: bytes, line: str) -> str:
    """The file content for one snapshot line: ``<json>\\n<mac>\\n``."""
    if "\n" in line or "\r" in line:
        raise SnapshotError("a snapshot line must be a single line")
    return f"{line}\n{_mac(key, line)}\n"


def verify_signed_snapshot(key: bytes, blob: "bytes | str") -> str:
    """The verified snapshot JSON line from a signed file's content. Raises :class:`SnapshotError` on any deviation.

    The MAC is checked BEFORE the JSON is parsed, so unauthenticated bytes never reach a parser."""
    raw = blob.encode("utf-8", "strict") if isinstance(blob, str) else bytes(blob)
    if not raw or len(raw) > MAX_SNAPSHOT_FILE_BYTES:
        raise SnapshotError("signed snapshot has a bad size")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise SnapshotError("signed snapshot is not ASCII") from None
    lines = text.split("\n")
    if len(lines) == 3 and lines[2] == "":
        lines = lines[:2]
    if len(lines) != 2 or "\r" in text:
        raise SnapshotError("signed snapshot has a bad layout")
    line, mac = lines
    if not line or not _MAC_HEX_RE.match(mac):
        raise SnapshotError("signed snapshot has a bad layout")
    if not hmac.compare_digest(_mac(key, line).encode("ascii"), mac.encode("ascii")):
        raise SnapshotError("signed snapshot failed authentication")
    return line


def write_signed_snapshot(path: "str | Path", key: bytes, line: str) -> None:
    """Atomically replace ``path`` with the signed snapshot (write a sibling temp file, then ``os.replace``), so the
    supervisor never reads half a file."""
    target = Path(path)
    tmp = target.with_name(target.name + "." + secrets.token_hex(4) + ".tmp")
    try:
        tmp.write_text(sign_snapshot_line(key, line), encoding="ascii", newline="\n")
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _read_capped(path: Path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(MAX_SNAPSHOT_FILE_BYTES + 1)


class SignedSnapshotSource:
    """``SnapshotProbe`` source: the helper's signed file, verified. Calling it returns the verified JSON line, or None
    when the helper has never written the file; it raises :class:`SnapshotError` for everything else that is wrong."""

    def __init__(
        self,
        path: "str | Path",
        key_provider: Callable[[], Optional[str]],
        *,
        reader: Callable[[Path], bytes] = _read_capped,
    ) -> None:
        self._path = Path(path)
        self._key_provider = key_provider
        self._reader = reader
        self._key: Optional[bytes] = None
        self._last_taken = -math.inf
        self._lock = threading.Lock()

    def _key_now(self) -> bytes:
        if self._key is None:
            try:
                self._key = key_bytes(self._key_provider())
            except SnapshotError:
                raise
            except Exception as exc:  # noqa: BLE001 - an unreadable key store must block, never allow
                raise SnapshotError(f"the snapshot key could not be read ({type(exc).__name__})") from None
        return self._key

    def __call__(self) -> Optional[str]:
        try:
            blob = self._reader(self._path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SnapshotError(f"the signed snapshot could not be read ({type(exc).__name__})") from None
        line = verify_signed_snapshot(self._key_now(), blob)
        taken = decode_snapshot(line)["taken_at"]
        with self._lock:
            if taken < self._last_taken:  # replay of an older (still authentic) file
                raise SnapshotError("the signed snapshot is older than one already seen")
            self._last_taken = taken
        return line


class SnapshotProbe(ActivityProbe):
    """PAL ``ActivityProbe`` plus the owner-GPU methods, backed by the helper's latest report.

    ``source()`` returns the latest raw report (a JSON string or dict) or None when the helper has said nothing yet.
    """

    platform_name = "windows"

    def __init__(
        self,
        source: Callable[[], Any],
        *,
        max_age_seconds: float = 30.0,
        no_session: Optional[Callable[[], bool]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._source = source
        self._max_age = max_age_seconds
        self._no_session = no_session
        self._clock = clock

    @classmethod
    def authenticated(
        cls,
        path: "str | Path",
        key_provider: Callable[[], Optional[str]],
        *,
        max_age_seconds: float = 30.0,
        no_session: Optional[Callable[[], bool]] = None,
        clock: Callable[[], float] = time.time,
        reader: Callable[[Path], bytes] = _read_capped,
    ) -> "SnapshotProbe":
        """A probe fed by the helper's HMAC-signed file (see the module docstring): authentication, strict decode,
        replay check and freshness all fail closed."""
        return cls(SignedSnapshotSource(path, key_provider, reader=reader), max_age_seconds=max_age_seconds,
                   no_session=no_session, clock=clock)

    def _latest(self) -> Optional[dict]:
        """The fresh report, or None when no user is logged in. Raises SnapshotError when it should exist but does not."""
        if self._no_session is not None and self._no_session():
            return None
        raw = self._source()
        if raw is None:
            raise SnapshotError("the session helper has not reported")
        data = decode_snapshot(raw)
        age = self._clock() - data["taken_at"]
        if age > self._max_age or age < -5.0:  # a report from the future is as untrustworthy as a stale one
            raise SnapshotError("the session helper's report is stale")
        return data

    def snapshot(self) -> ActivitySnapshot:
        data = self._latest()
        if data is None:  # nobody is logged in
            return ActivitySnapshot(owner_active=False, fullscreen_app=False, idle_seconds=_AWAY_IDLE_SECONDS)
        idle = data["idle_seconds"]
        return ActivitySnapshot(owner_active=None, fullscreen_app=data["fullscreen"], idle_seconds=idle)

    # owner-GPU probe (same duck type as ``WindowsOwnerGpuProbe``)
    def foreground_gpu_app(self) -> Optional[str]:
        data = self._latest()
        return None if data is None else data["foreground_gpu_app"]

    def free_vram_mb(self) -> Optional[int]:
        data = self._latest()
        if data is None:
            raise ProbeUnsupported("no session: VRAM is read by the helper")
        return data["free_vram_mb"]

