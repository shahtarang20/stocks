"""Test doubles for pywin32 modules, following the documented DPAPI / Credential Manager contract. Written, not run."""

from __future__ import annotations

import hashlib


class FakeWin32Crypt:
    """Opaque 'encryption': blob = keystream-xor(data) tagged with an entropy digest. Plaintext never appears in it."""

    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    CRYPTPROTECT_LOCAL_MACHINE = 0x4

    def __init__(self):
        self.protect_calls: list[tuple] = []
        self.fail = False

    @staticmethod
    def _ks(entropy: bytes, n: int) -> bytes:
        out, i = b"", 0
        while len(out) < n:
            out += hashlib.sha256(b"fake-dpapi" + entropy + i.to_bytes(4, "big")).digest()
            i += 1
        return out[:n]

    def CryptProtectData(self, data, descr, entropy, reserved, prompt, flags):  # noqa: N802
        if self.fail:
            raise OSError("dpapi down")
        assert reserved is None and prompt is None
        self.protect_calls.append((descr, entropy, flags))
        ent = entropy or b""
        body = bytes(a ^ b for a, b in zip(data, self._ks(ent, len(data))))
        return hashlib.sha256(ent).digest()[:8] + body

    def CryptUnprotectData(self, blob, entropy, reserved, prompt, flags):  # noqa: N802
        if self.fail:
            raise OSError("dpapi down")
        ent = entropy or b""
        if blob[:8] != hashlib.sha256(ent).digest()[:8]:
            raise OSError("entropy mismatch")
        body = blob[8:]
        return "descr", bytes(a ^ b for a, b in zip(body, self._ks(ent, len(body))))


class FakeWin32Cred:
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.fail = False

    def CredWrite(self, cred, flags):  # noqa: N802
        if self.fail:
            raise OSError("credman down")
        self.items[cred["TargetName"]] = dict(cred)

    def CredRead(self, target, typ, flags):  # noqa: N802
        if target not in self.items:
            err = OSError("not found")
            err.winerror = 1168
            raise err
        return self.items[target]

    def CredDelete(self, target, typ, flags):  # noqa: N802
        self.items.pop(target, None)
