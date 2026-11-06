"""Windows install step for the updater: ``msiexec`` through an injected runner. WRITTEN, NEVER RUN ON WINDOWS.

TODO(verify-docs) (X11): every msiexec switch and exit code below against
learn.microsoft.com/windows/win32/msi/command-line-options and .../msi/error-codes; record URL and date.

The runner is ``run(argv: list[str]) -> int`` (argv list, never a shell string). The MSI is Authenticode-signed (X10); a
real installer step must ALSO verify that signature before running it (``WinVerifyTrust`` or
``Get-AuthenticodeSignature``), over and above the manifest hash. That check is injected as ``verify_signature``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

MSIEXEC = r"C:\Windows\System32\msiexec.exe"
ERROR_SUCCESS = 0
ERROR_SUCCESS_REBOOT_REQUIRED = 3010  # installed; a restart finishes it. We never force a restart: the owner chooses.


class MsiInstaller:
    def __init__(
        self,
        run: Callable[[list], int],
        installed_version: Callable[[], str],
        *,
        verify_signature: Callable[[Path], bool],
        log_dir: Optional[Path] = None,
    ) -> None:
        self._run, self._installed, self._verify = run, installed_version, verify_signature
        self._log_dir = log_dir

    def argv(self, artifact: Path) -> list:
        argv = [MSIEXEC, "/i", str(artifact), "/qn", "/norestart"]
        if self._log_dir is not None:
            argv += ["/l*v", str(self._log_dir / "update-install.log")]
        return argv

    def install(self, artifact: Path, version: str) -> None:
        if not self._verify(artifact):
            raise RuntimeError("installer signature check failed")
        code = self._run(self.argv(artifact))
        if code not in (ERROR_SUCCESS, ERROR_SUCCESS_REBOOT_REQUIRED):
            raise RuntimeError(f"msiexec exited with {code}")

    def installed_version(self) -> str:
        return self._installed()
