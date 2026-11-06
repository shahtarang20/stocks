"""Machine credential storage with DPAPI and a Credential Manager fallback (W2, X7, L7).

Rules:
  * a secret is NEVER written to disk as plain text, not even briefly, by this module;
  * the primary store is a DPAPI blob file under ``<root>\\secrets`` (ACL: SYSTEM + Administrators, see paths.py);
  * the fallback is Windows Credential Manager (generic credential, machine persistence);
  * if neither works, :class:`SecretStoreError` is raised (fail closed, L1) and the caller must not continue with a
    plain-text file;
  * :func:`migrate_plain_config` moves an existing plain ``~/.computecoin/agent.toml`` credential into the store,
    verifies the round trip, and only then overwrites and deletes the old secret-bearing file.

DPAPI scope. ``machine`` (default) uses ``CRYPTPROTECT_LOCAL_MACHINE``: per the documentation "any user on the computer
on which CryptProtectData is called can use CryptUnprotectData to decrypt the data"
(https://learn.microsoft.com/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata, read 2026-11-06). That is what lets an
elevated ``enroll`` run by an administrator and the service running as SYSTEM share one credential; the protection
against other local users is therefore the directory ACL plus a per-name entropy, NOT DPAPI itself. ``user`` scope
(flags 0) ties the blob to the account that wrote it; use it only when enrolment and the service run as the same account.

Doc facts used (same page): ``szDataDescr`` and ``pOptionalEntropy`` are optional; the entropy used to protect must be
supplied again to unprotect; ``pvReserved`` must be NULL; the prompt-struct flow is deprecated (we pass NULL).

TODO(verify-docs) (pywin32, not read from documentation here):
  * ``win32crypt.CryptProtectData(DataIn, DataDescr, OptionalEntropy, Reserved, PromptStruct, Flags)`` returns bytes;
    ``win32crypt.CryptUnprotectData(DataIn, OptionalEntropy, Reserved, PromptStruct, Flags)`` returns
    ``(description, data)``; constants ``CRYPTPROTECT_LOCAL_MACHINE`` and ``CRYPTPROTECT_UI_FORBIDDEN`` exist on the module.
  * ``win32cred.CredWrite(dict, Flags)`` / ``CredRead(TargetName, Type, Flags)`` / ``CredDelete(TargetName, Type, Flags)``
    with ``CRED_TYPE_GENERIC`` and ``CRED_PERSIST_LOCAL_MACHINE``, and that ``CredentialBlob`` accepts and returns bytes.
  * the blob size cap of a generic credential (assumed 2560 bytes; we refuse more than 2000).
Credential Manager entries are per-account (the SYSTEM profile when run as the service); treat the fallback as
"works for the account that wrote it" until verified on Windows.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets as _secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import tomli_w

SECRET_CONFIG_KEYS = ("machine_secret", "signing_secret", "enrollment_token")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAGIC = b"CCDPAPI1"
_FALLBACK_UI_FORBIDDEN = 0x1  # TODO(verify-docs): wincrypt.h value, used only if the module lacks the constant
_FALLBACK_LOCAL_MACHINE = 0x4  # TODO(verify-docs)
_CRED_TARGET_PREFIX = "ComputeCoin/"
_CRED_MAX_BYTES = 2000


class SecretStoreError(RuntimeError):
    """The secret could not be stored, read or removed. Never contains the secret."""


def _load_win32(name: str) -> Any:
    try:
        return __import__(name)
    except ImportError as exc:
        raise SecretStoreError(f"{name} (pywin32) is not available; install the 'windows' extra") from exc


class DpapiSecretStore:
    """Named-secret store. ``win32crypt`` / ``win32cred`` are injectable (tests pass fakes; production loads pywin32)."""

    def __init__(
        self,
        directory: "str | Path",
        *,
        scope: str = "machine",
        win32crypt: Any = None,
        win32cred: Any = None,
        use_credential_manager_fallback: bool = True,
        prepare_directory: Optional[Callable[[Path], None]] = None,
    ) -> None:
        if scope not in ("machine", "user"):
            raise ValueError("scope must be 'machine' or 'user'")
        self._dir = Path(directory)
        self._scope = scope
        self._crypt = win32crypt
        self._cred = win32cred
        self._fallback = use_credential_manager_fallback
        self._prepare = prepare_directory  # e.g. paths.restrict_path; must raise to refuse
        self._prepared = False

    # -- public --------------------------------------------------------------------------------------------- #
    def store(self, name: str, value: str) -> str:
        """Store ``value``. Returns the backend used: ``"dpapi"`` or ``"credential_manager"``."""
        self._check_name(name)
        if not isinstance(value, str) or not value:
            raise SecretStoreError("secret must be a non-empty string")
        data = value.encode("utf-8")
        try:
            self._store_dpapi(name, data)
            self._best_effort(self._delete_credman, name)
            return "dpapi"
        except SecretStoreError as primary:
            if not self._fallback:
                raise
            try:
                self._store_credman(name, data)
            except SecretStoreError:
                raise SecretStoreError(f"no secret backend worked ({primary})") from None
            return "credential_manager"

    def load(self, name: str) -> Optional[str]:
        """The stored secret, or None when there is none. A blob that cannot be decrypted raises (never returns junk)."""
        self._check_name(name)
        path = self._file(name)
        if path.exists():
            return self._load_dpapi(name, path)
        if self._fallback:
            try:
                return self._load_credman(name)
            except SecretStoreError:
                return None
        return None

    def delete(self, name: str) -> None:
        self._check_name(name)
        path = self._file(name)
        if path.exists():
            _overwrite_and_remove(path)
        self._best_effort(self._delete_credman, name)

    def has(self, name: str) -> bool:
        try:
            return self.load(name) is not None
        except SecretStoreError:
            return True  # something is stored but unreadable; callers must not overwrite blindly

    # -- DPAPI ---------------------------------------------------------------------------------------------- #
    def _flags(self, crypt: Any) -> int:
        flags = getattr(crypt, "CRYPTPROTECT_UI_FORBIDDEN", _FALLBACK_UI_FORBIDDEN)
        if self._scope == "machine":
            flags |= getattr(crypt, "CRYPTPROTECT_LOCAL_MACHINE", _FALLBACK_LOCAL_MACHINE)
        return flags

    @staticmethod
    def _entropy(name: str) -> bytes:
        # Binds a blob to its name so one blob file cannot be swapped for another. Not a secret.
        return b"computecoin-agent/v1|" + name.encode("ascii")

    def _crypt_mod(self) -> Any:
        if self._crypt is None:
            self._crypt = _load_win32("win32crypt")
        return self._crypt

    def _store_dpapi(self, name: str, data: bytes) -> None:
        crypt = self._crypt_mod()
        self._prepare_dir()
        try:
            blob = crypt.CryptProtectData(data, f"computecoin:{name}", self._entropy(name), None, None, self._flags(crypt))
        except Exception as exc:  # noqa: BLE001 - pywintypes.error and friends
            raise SecretStoreError(f"DPAPI protect failed: {type(exc).__name__}") from None
        if not isinstance(blob, (bytes, bytearray)) or not blob:
            raise SecretStoreError("DPAPI returned no data")
        path = self._file(name)
        tmp = path.with_name(path.name + "." + _secrets.token_hex(4) + ".tmp")
        try:
            tmp.write_bytes(_MAGIC + (b"M" if self._scope == "machine" else b"U") + bytes(blob))
            os.replace(tmp, path)
        except OSError as exc:
            _silent_remove(tmp)
            raise SecretStoreError(f"could not write the protected blob: {exc.strerror or type(exc).__name__}") from None

    def _load_dpapi(self, name: str, path: Path) -> str:
        crypt = self._crypt_mod()
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SecretStoreError(f"could not read the protected blob: {exc.strerror or type(exc).__name__}") from None
        if not raw.startswith(_MAGIC) or len(raw) <= len(_MAGIC) + 1:
            raise SecretStoreError("protected blob is malformed")
        blob = raw[len(_MAGIC) + 1:]
        try:
            _descr, data = crypt.CryptUnprotectData(blob, self._entropy(name), None, None, _ui_only(crypt))
        except Exception as exc:  # noqa: BLE001
            raise SecretStoreError(f"DPAPI unprotect failed: {type(exc).__name__}") from None
        try:
            return bytes(data).decode("utf-8")
        except UnicodeDecodeError:
            raise SecretStoreError("protected secret is not valid text") from None

    def _prepare_dir(self) -> None:
        if self._prepared:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        if self._prepare is not None:
            try:
                self._prepare(self._dir)
            except Exception as exc:  # noqa: BLE001
                raise SecretStoreError(f"secrets directory could not be locked down: {type(exc).__name__}") from None
        self._prepared = True

    def _file(self, name: str) -> Path:
        return self._dir / f"{name}.dpapi"

    # -- Credential Manager --------------------------------------------------------------------------------- #
    def _cred_mod(self) -> Any:
        if self._cred is None:
            self._cred = _load_win32("win32cred")
        return self._cred

    def _target(self, name: str) -> str:
        return _CRED_TARGET_PREFIX + name

    def _store_credman(self, name: str, data: bytes) -> None:
        if len(data) > _CRED_MAX_BYTES:
            raise SecretStoreError("secret too large for Credential Manager")
        cred = self._cred_mod()
        try:
            cred.CredWrite(
                {
                    "Type": cred.CRED_TYPE_GENERIC,
                    "TargetName": self._target(name),
                    "UserName": "computecoin",
                    "CredentialBlob": data,
                    "Persist": cred.CRED_PERSIST_LOCAL_MACHINE,
                    "Comment": "ComputeCoin agent credential",
                },
                0,
            )
        except Exception as exc:  # noqa: BLE001
            raise SecretStoreError(f"Credential Manager write failed: {type(exc).__name__}") from None

    def _load_credman(self, name: str) -> Optional[str]:
        cred = self._cred_mod()
        try:
            item = cred.CredRead(self._target(name), cred.CRED_TYPE_GENERIC, 0)
        except Exception as exc:  # noqa: BLE001 - not found is an error in pywin32
            if getattr(exc, "winerror", None) == 1168 or (getattr(exc, "args", (None,))[0] == 1168):  # ERROR_NOT_FOUND
                return None
            raise SecretStoreError(f"Credential Manager read failed: {type(exc).__name__}") from None
        blob = item.get("CredentialBlob")
        if blob is None:
            return None
        try:
            return bytes(blob).decode("utf-8")
        except UnicodeDecodeError:
            raise SecretStoreError("stored credential is not valid text") from None

    def _delete_credman(self, name: str) -> None:
        cred = self._cred_mod()
        cred.CredDelete(self._target(name), cred.CRED_TYPE_GENERIC, 0)

    # -- helpers --------------------------------------------------------------------------------------------- #
    @staticmethod
    def _check_name(name: str) -> None:
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise SecretStoreError("invalid secret name")

    @staticmethod
    def _best_effort(fn: Callable[[str], None], name: str) -> None:
        try:
            fn(name)
        except Exception:  # noqa: BLE001 - stale copy removal only
            pass


def _ui_only(crypt: Any) -> int:
    """Mask that keeps only the UI-forbidden bit when unprotecting (the scope flag is not needed to decrypt)."""
    return getattr(crypt, "CRYPTPROTECT_UI_FORBIDDEN", _FALLBACK_UI_FORBIDDEN)


def _silent_remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _overwrite_and_remove(path: Path) -> None:
    """Overwrite a file's bytes with zeros, flush to disk, then delete it.

    HONEST LIMIT: this does not defeat SSD wear levelling, copy-on-write or journaling file systems, volume shadow
    copies or backups. It removes the easy recovery (undelete) and is the best a portable user-space call can do.
    Passwords that mattered must also be rotated server-side (re-enrol) when a plain file may have been copied.
    """
    try:
        size = path.stat().st_size
        with open(path, "r+b") as fh:
            fh.write(b"\x00" * size)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass  # still try to delete below
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SecretStoreError(f"could not delete {path.name}: {exc.strerror or type(exc).__name__}") from None


# --------------------------------------------------------------------------- #
# Migration of an existing plain-text credential
# --------------------------------------------------------------------------- #
@dataclass
class MigrationResult:
    migrated: bool
    names: list[str] = field(default_factory=list)  # which secrets moved (never the values)
    backend: dict[str, str] = field(default_factory=dict)
    old_file_removed: bool = False
    note: str = ""


def migrate_plain_config(
    source: "str | Path",
    store: DpapiSecretStore,
    *,
    dest: Optional["str | Path"] = None,
) -> MigrationResult:
    """Move the secrets of a plain ``agent.toml`` into ``store`` and remove them from disk.

    Steps (the old file is touched only after every earlier step succeeded):
      1. read the file; collect the secret keys (``machine_secret``, ``signing_secret``, ``enrollment_token``);
      2. store each, load it back, and compare in constant time;
      3. write a copy WITHOUT the secret keys to ``dest`` (default: the same path, rewritten in place);
      4. overwrite the old bytes with zeros and delete the old file (when ``dest`` is a different path), or overwrite
         the old bytes before the redacted rewrite (same path).
    A missing file is not an error (``migrated=False``).
    """
    src = Path(source)
    if not src.exists():
        return MigrationResult(False, note="no plain credential file found")
    try:
        data = tomllib.loads(src.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SecretStoreError(f"cannot read the old config: {type(exc).__name__}") from None
    found = {k: data[k] for k in SECRET_CONFIG_KEYS if isinstance(data.get(k), str) and data[k]}
    result = MigrationResult(False)
    for key, value in found.items():
        result.backend[key] = store.store(key, value)
        back = store.load(key)
        if back is None or not hmac.compare_digest(back.encode("utf-8"), value.encode("utf-8")):
            raise SecretStoreError(f"{key} did not round-trip through the secret store; old file left in place")
        result.names.append(key)
    redacted = {k: v for k, v in data.items() if k not in SECRET_CONFIG_KEYS}
    target = Path(dest) if dest is not None else src
    same = target.resolve() == src.resolve()
    payload = tomli_w.dumps(redacted).encode("utf-8")
    if same:
        _zero_file(src)  # destroy the old bytes first, then reuse the file for the redacted copy
        _write_new(target, payload)
    else:
        _write_new(target, payload)
        _overwrite_and_remove(src)
        result.old_file_removed = True
    result.migrated = bool(found)
    return result


def _zero_file(path: Path) -> None:
    try:
        size = path.stat().st_size
        with open(path, "r+b") as fh:
            fh.write(b"\x00" * size)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        raise SecretStoreError(f"could not overwrite the old file: {exc.strerror or type(exc).__name__}") from None


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + _secrets.token_hex(4) + ".tmp")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    except OSError as exc:
        _silent_remove(tmp)
        raise SecretStoreError(f"could not write the redacted config: {exc.strerror or type(exc).__name__}") from None
