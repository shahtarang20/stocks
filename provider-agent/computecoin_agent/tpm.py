"""TPM 2.0 support for the provider agent (WP-F): AK enrolment and quotes, through tpm2-tools.

Keys live in the TPM, not on disk: the EK and the attestation key (AK) are made persistent at
fixed handles, and the AK is a restricted signing key that only signs TPM-generated quotes.

- ``ensure_keys``: create the EK and AK once (idempotent).
- ``activate``: TPM2_ActivateCredential for the platform's enrolment challenge. Only this TPM's EK,
  with this AK loaded, recovers the secret, which binds the AK to the EK.
- ``quote``: a quote over the configured SHA-256 PCRs with extraData = SHA-256(nonce), plus the PCR
  values; read before and after the quote so a PCR extended mid-quote is noticed.

``tcti`` is a tpm2-tools TCTI string, e.g. ``device:/dev/tpmrm0`` on a real host or
``swtpm:host=127.0.0.1,port=2321`` for a software TPM. ``tools_root`` optionally points at an
unpacked tpm2-tools tree (``<root>/usr/bin``, ``<root>/usr/lib/...``) instead of the system one.
"""

from __future__ import annotations

import hashlib
import os
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from cryptography import x509
from cryptography.hazmat.primitives import serialization

EK_HANDLE = "0x81010001"
AK_HANDLE = "0x81010002"
EK_CERT_NV_INDEX = "0x01c00002"  # TCG: RSA-2048 EK certificate
DEFAULT_PCRS = (0, 1, 2, 3, 4, 5, 6, 7)


class TpmError(RuntimeError):
    """A tpm2-tools command failed or the TPM returned something unusable."""


class Tpm2:
    def __init__(
        self,
        tcti: str,
        *,
        tools_root: Optional[str] = None,
        ek_cert_path: Optional[str] = None,
        ek_intermediate_paths: Sequence[str] = (),
        pcrs: Sequence[int] = DEFAULT_PCRS,
        runner: Callable[..., Any] = subprocess.run,
    ):
        if not tcti:
            raise TpmError("no TPM configured (tpm_tcti)")
        self.tcti = tcti
        self.ek_cert_path = ek_cert_path
        self.ek_intermediate_paths = list(ek_intermediate_paths)
        self.pcrs = sorted(set(int(p) for p in pcrs))
        self._run_proc = runner
        self.env = dict(os.environ, TPM2TOOLS_TCTI=tcti)
        if tools_root:
            root = Path(tools_root)
            libs = [str(root / "usr/lib/x86_64-linux-gnu"), str(root / "usr/lib/x86_64-linux-gnu/swtpm")]
            self.env["PATH"] = f"{root / 'usr/bin'}{os.pathsep}{self.env.get('PATH', '')}"
            self.env["LD_LIBRARY_PATH"] = os.pathsep.join(libs + [self.env.get("LD_LIBRARY_PATH", "")]).rstrip(os.pathsep)

    # ------------------------------------------------------------------ plumbing
    def _run(self, *args: str, cwd: Optional[str] = None, check: bool = True) -> bytes:
        proc = self._run_proc(list(args), cwd=cwd, env=self.env, capture_output=True, timeout=60)
        if check and proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise TpmError(f"{args[0]} failed: {err[-1] if err else proc.returncode}")
        return proc.stdout or b""

    def _flush(self) -> None:
        # Without a resource manager (e.g. swtpm) transient objects and sessions are not freed.
        self._run("tpm2_flushcontext", "-t", check=False)
        self._run("tpm2_flushcontext", "-s", check=False)

    def _has(self, handle: str) -> bool:
        try:
            self._run("tpm2_readpublic", "-c", handle)
            return True
        except TpmError:
            return False

    # ------------------------------------------------------------------ keys
    def ensure_keys(self) -> None:
        """Create and persist the EK and the AK if they are not there yet."""
        with tempfile.TemporaryDirectory(prefix="ccagent-tpm-") as d:
            if not self._has(EK_HANDLE):
                self._run("tpm2_createek", "-c", EK_HANDLE, "-G", "rsa", "-u", "ek.pub", cwd=d)
                self._flush()
            if not self._has(AK_HANDLE):
                self._run("tpm2_createak", "-C", EK_HANDLE, "-c", "ak.ctx", "-G", "ecc", "-g", "sha256", "-s", "ecdsa",
                          "-u", "ak.pub", "-n", "ak.name", cwd=d)
                self._flush()
                self._run("tpm2_evictcontrol", "-C", "o", "-c", "ak.ctx", AK_HANDLE, cwd=d)
                self._flush()

    def ak_public(self) -> bytes:
        """The AK's TPMT_PUBLIC (what its TPM name is computed over)."""
        with tempfile.TemporaryDirectory(prefix="ccagent-tpm-") as d:
            self._run("tpm2_readpublic", "-c", AK_HANDLE, "-f", "tpmt", "-o", "ak.tpmt", cwd=d)
            return (Path(d) / "ak.tpmt").read_bytes()

    def ek_public_pem(self) -> bytes:
        with tempfile.TemporaryDirectory(prefix="ccagent-tpm-") as d:
            self._run("tpm2_readpublic", "-c", EK_HANDLE, "-f", "pem", "-o", "ek.pem", cwd=d)
            return (Path(d) / "ek.pem").read_bytes()

    def ek_certificate(self) -> tuple[str, list[str]]:
        """(EK certificate PEM, intermediate PEMs). From a configured file, else the TPM's NV index."""
        if self.ek_cert_path:
            cert = Path(self.ek_cert_path).read_text()
        else:
            with tempfile.TemporaryDirectory(prefix="ccagent-tpm-") as d:
                self._run("tpm2_nvread", EK_CERT_NV_INDEX, "-o", "ek.der", cwd=d)
                der = (Path(d) / "ek.der").read_bytes()
            # Some TPMs pad the NV area; load_der needs exactly the certificate.
            cert = x509.load_der_x509_certificate(_trim_der(der)).public_bytes(serialization.Encoding.PEM).decode()
        return cert, [Path(p).read_text() for p in self.ek_intermediate_paths]

    # ------------------------------------------------------------------ protocol steps
    def activate(self, credential_blob: bytes, encrypted_secret: bytes) -> bytes:
        """TPM2_ActivateCredential with the persistent AK and EK; returns the recovered secret."""
        with tempfile.TemporaryDirectory(prefix="ccagent-tpm-") as d:
            # tpm2-tools credential file: magic, version, TPM2B_ID_OBJECT, TPM2B_ENCRYPTED_SECRET.
            blob = (struct.pack(">II", 0xBADCC0DE, 1) + struct.pack(">H", len(credential_blob)) + credential_blob
                    + struct.pack(">H", len(encrypted_secret)) + encrypted_secret)
            (Path(d) / "cred.blob").write_bytes(blob)
            try:
                self._run("tpm2_startauthsession", "--policy-session", "-S", "s.ctx", cwd=d)
                self._run("tpm2_policysecret", "-S", "s.ctx", "-c", "e", cwd=d)
                self._run("tpm2_activatecredential", "-c", AK_HANDLE, "-C", EK_HANDLE, "-i", "cred.blob",
                          "-o", "secret", "-P", "session:s.ctx", cwd=d)
                return (Path(d) / "secret").read_bytes()
            finally:
                self._flush()

    def quote(self, nonce: str) -> dict[str, Any]:
        """Hardware evidence for ``nonce`` in the API's ``tpm2`` format."""
        selection = "sha256:" + ",".join(str(p) for p in self.pcrs)
        qualifying = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        with tempfile.TemporaryDirectory(prefix="ccagent-tpm-") as d:
            before = self._pcr_values(selection, d, "before")
            try:
                self._run("tpm2_quote", "-c", AK_HANDLE, "-l", selection, "-q", qualifying, "-g", "sha256",
                          "-m", "q.msg", "-s", "q.sig", "-f", "plain", cwd=d)
            finally:
                self._flush()
            after = self._pcr_values(selection, d, "after")
            if before != after:
                raise TpmError("a PCR changed while quoting; try again")
            attest, signature = (Path(d) / "q.msg").read_bytes(), (Path(d) / "q.sig").read_bytes()
        ek_cert, intermediates = self.ek_certificate()
        return {
            "quote": {"attest": attest.hex(), "signature": signature.hex(), "ak_public": self.ak_public().hex()},
            "pcrs": {str(p): v.hex() for p, v in zip(self.pcrs, before)},
            "ek_cert": ek_cert,
            "ek_intermediates": intermediates,
        }

    def _pcr_values(self, selection: str, d: str, name: str) -> list[bytes]:
        self._run("tpm2_pcrread", selection, "-o", name, cwd=d)
        raw = (Path(d) / name).read_bytes()
        if len(raw) != 32 * len(self.pcrs):
            raise TpmError("unexpected PCR read size")
        return [raw[i * 32:(i + 1) * 32] for i in range(len(self.pcrs))]


def _trim_der(der: bytes) -> bytes:
    """Cut a DER SEQUENCE to its encoded length (NV areas may be larger than the certificate)."""
    if len(der) < 4 or der[0] != 0x30:
        raise TpmError("EK certificate in NV is not DER")
    if der[1] & 0x80:
        n = der[1] & 0x7F
        length = int.from_bytes(der[2:2 + n], "big")
        return der[:2 + n + length]
    return der[:2 + der[1]]


def hardware_provider(tpm: Optional[Tpm2]) -> Optional[Callable[[str], tuple[str, dict[str, Any]]]]:
    """``nonce -> (flavor, evidence)`` for perform_attestation, or None without a TPM."""
    if tpm is None:
        return None
    return lambda nonce: ("tpm2", tpm.quote(nonce))


def enrol(base_url: str, machine_id: str, machine_secret: str, tpm: Tpm2, *, transport: Any = None, timeout: float = 30.0) -> str:
    """Bind this TPM's AK to its EK with the backend. Returns the enrolled AK name (hex)."""
    from .jobrunner import JobRunnerError, _signed_post

    url = base_url.rstrip("/") + "/v1/agents/tpm/enrolments"
    tpm.ensure_keys()
    ek_cert, intermediates = tpm.ek_certificate()
    start = _signed_post(url, machine_id, machine_secret,
                         {"ek_cert": ek_cert, "ek_intermediates": intermediates, "ak_public": tpm.ak_public().hex()},
                         transport=transport, timeout=timeout)
    if start.status_code != 200:
        raise JobRunnerError(f"TPM enrolment refused: HTTP {start.status_code} {_detail(start)}")
    challenge = start.json()
    secret = tpm.activate(bytes.fromhex(challenge["credential_blob"]), bytes.fromhex(challenge["encrypted_secret"]))
    done = _signed_post(f"{url}/{challenge['enrolment_id']}/activate", machine_id, machine_secret, {"secret": secret.hex()},
                        transport=transport, timeout=timeout)
    if done.status_code != 200:
        raise JobRunnerError(f"TPM activation refused: HTTP {done.status_code} {_detail(done)}")
    return str(done.json()["ak_name"])


def _detail(resp: Any) -> str:
    try:
        return str(resp.json().get("detail", ""))
    except Exception:
        return ""
