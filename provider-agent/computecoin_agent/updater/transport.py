"""HTTP side of the updater: signed manifest fetch and a size-capped download. Transports are injectable (no network in tests)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from ..heartbeat import MACHINE_HEADER, SIGNATURE_HEADER, TIMESTAMP_HEADER, sign_contract
from .manifest import MAX_ARTIFACT_BYTES


class ApiManifestSource:
    """``GET /v1/agents/releases/manifest?platform=&ring=&current_version=`` signed with the machine HMAC (same scheme as
    the heartbeat; an empty body). 204 = no release for this host. The reply is untrusted until the Ed25519 check."""

    def __init__(self, base_url: str, machine_id: str, machine_secret: str, *, transport: Any = None, timeout: float = 20.0) -> None:
        self._base, self._id, self._secret = base_url.rstrip("/"), machine_id, machine_secret
        self._transport, self._timeout = transport, timeout

    def __call__(self, ring: str, platform: str, current_version: str) -> Optional[dict]:
        import httpx

        ts = int(time.time())
        headers = {MACHINE_HEADER: self._id, TIMESTAMP_HEADER: str(ts),
                   SIGNATURE_HEADER: sign_contract(self._id, ts, b"", self._secret)}
        client = httpx.Client(transport=self._transport, timeout=self._timeout) if self._transport else httpx.Client(timeout=self._timeout)
        with client:
            resp = client.get(self._base + "/v1/agents/releases/manifest",
                              params={"platform": platform, "ring": ring, "current_version": current_version}, headers=headers)
        if resp.status_code in (204, 404):
            return None
        if resp.status_code != 200:
            raise RuntimeError(f"manifest fetch failed: HTTP {resp.status_code}")
        data = resp.json()
        return data if isinstance(data, dict) else None


class HttpDownloader:
    """Streams the artifact to ``dest`` and stops at the size cap. The hash check is the caller's (core.py)."""

    def __init__(self, *, transport: Any = None, timeout: float = 120.0, max_bytes: int = MAX_ARTIFACT_BYTES) -> None:
        self._transport, self._timeout, self._max = transport, timeout, max_bytes

    def __call__(self, url: str, dest: Path) -> None:
        import httpx

        client = httpx.Client(transport=self._transport, timeout=self._timeout, follow_redirects=False) if self._transport \
            else httpx.Client(timeout=self._timeout, follow_redirects=False)
        with client, client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"download failed: HTTP {resp.status_code}")
            total = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > self._max:
                        raise RuntimeError("download exceeds the size cap")
                    fh.write(chunk)
