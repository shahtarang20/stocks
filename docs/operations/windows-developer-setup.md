# Windows developer setup

_Status: written 2026-11-06 (cross-platform plan W10, decision X3). **Not run on a Windows machine yet.** Vendor statements are marked
`TODO(verify-docs)` until someone has checked them against the current Microsoft, Docker, Python and Node documentation (X11)._

This guide is for people who **work on the code** from a Windows PC. It is not the guide for sharing your GPU (that is
[windows-host-guide.md](windows-host-guide.md)).

## What runs where (X3)

| Part | Native Windows | WSL2 or Docker |
|---|---|---|
| Provider agent unit tests (`provider-agent/`) | yes (the Windows CI job runs them) | also fine |
| Python SDK and TypeScript SDK tests | yes | also fine |
| Web app (`apps/web`): dev server, tests, typecheck, build | yes | also fine |
| Kit gates and unit tests (`scripts/kit_ci.py check`, `kits/tests`) | yes | also fine |
| Operator scripts (`scripts/windows/*.ps1`, `tasks.py`) | yes | n/a |
| **Control plane: API, worker, database, Redis, object storage** | **no, by design** | **yes: Linux containers or a WSL2 Linux shell** |
| Sealed OS image build, multi-node network script, QA stack script | no | WSL2 / Linux machine |
| Real GPU job tests on a Windows host | needs the Windows host agent and a supported NVIDIA GPU, see the host guide | n/a |

There is no Windows build of the API, and none is planned: the control plane ships as Linux containers (X3). Do not try to run `uvicorn`
or the database natively on Windows.

## 1. Install the tools

Use a normal (not administrator) PowerShell for everything after step 1a.

**1a. Windows features (once, administrator).** WSL2 is needed for the control plane and for Docker. `TODO(verify-docs)`: the minimum Windows
version for WSL2 and the exact command, against <https://learn.microsoft.com/windows/wsl/install>.

```powershell
wsl --install            # administrator PowerShell, then restart when asked
wsl --version            # afterwards, in a normal PowerShell
```

**1b. Git, Python, Node.** Pick versions from the project files, not from memory: Python **3.12 or newer** (`provider-agent/pyproject.toml`
says `requires-python >= 3.12`), Node **22** (the CI workflows use 22).

```powershell
winget install --id Git.Git -e
winget install --id Python.Python.3.12 -e
winget install --id OpenJS.NodeJS.LTS -e     # TODO(verify-docs): confirm the LTS line is 22 when you install
```

Open a **new** PowerShell window afterwards so the PATH is refreshed. Check:

```powershell
git --version
py -3 --version      # 3.12 or newer
node --version
npm --version
```

Do not rely on the Microsoft Store `python` alias: if `python` opens the Store, use `py -3` (the Python launcher from python.org).

**1c. Docker (only for the control plane).** Either Docker Desktop with the WSL2 backend, or Docker Engine installed inside your WSL2 Ubuntu.
Docker Desktop has its own licence terms for companies: check them (`TODO(verify-docs)`). For the Windows **host** product the project
chose Docker Engine inside its own managed distro, so developing against Engine-in-WSL2 matches it best.

## 2. Git settings that matter on Windows

Line endings are data in this repository (Kit manifests, Dockerfiles, test vectors are compared byte for byte; shell scripts and
Dockerfiles break with CRLF). `.gitattributes` pins LF for source types, and you should also set:

```powershell
git config --global core.autocrlf false     # do not rewrite line endings on checkout or commit
git config --global core.longpaths true     # some paths under node_modules exceed the old 260-character limit
```

Clone **before** other work so the settings apply:

```powershell
git clone <repository-url> C:\src\stocks
cd C:\src\stocks
```

Keep the clone on a short path such as `C:\src\...` (not deep inside OneDrive: sync clients lock files while tests delete them).
For control-plane work, clone **again inside WSL2** (`~/src/stocks` in your Ubuntu home): working through `/mnt/c/...` is slow and mixes
file permissions.

## 3. Everyday commands

`tasks.py` mirrors the Makefile (see [operator-cheatsheet.md](operator-cheatsheet.md)); nothing here needs `make`.

```powershell
py -3 tasks.py help
py -3 tasks.py agent-install        # creates provider-agent\.venv and installs the agent with test dependencies
py -3 tasks.py agent-test
py -3 tasks.py sdk-python-test      # needs: py -3 -m pip install -e "sdk\python[dev]"
py -3 tasks.py sdk-ts-test          # needs: cd sdk\typescript; npm install
py -3 tasks.py kits-check
py -3 tasks.py web-install
py -3 tasks.py web-check
py -3 tasks.py windows-test         # everything the Windows CI job runs
```

Equivalent bash (Linux, macOS, WSL2): `make agent-install`, `make agent-test`, `make web-check` and so on.

If PowerShell refuses to run a `.ps1` file: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned` (affects this window only).

### The control plane through WSL2

```powershell
py -3 tasks.py --wsl api-test       # runs `make api-test` in your default WSL distro in this folder
```

or open your WSL shell, go to the clone **inside** WSL, and use the Linux commands:

```bash
make infra          # Postgres, Redis, MinIO in containers
make api-install
make api-migrate
make api-run        # http://localhost:8000, reachable from Windows browsers
```

`TODO(verify-docs)`: that `wsl.exe --cd <dir> -- <command>` is the current syntax; if `--wsl` fails, run the commands in the WSL shell.

### The whole stack in Docker

```powershell
py -3 tasks.py up         # docker compose up --build -d
py -3 tasks.py ps
py -3 tasks.py down
```

Web at <http://localhost:3000>, API at <http://localhost:8000/health>.

## 4. Things that behave differently on Windows

| Topic | What to know |
|---|---|
| Open files | Windows cannot delete or rename a file another process holds open. Tests must close files (use `with`) before removing them; never `NamedTemporaryFile(delete=True)` and then reopen by name. Antivirus can briefly hold files too: if `node_modules` cleanup fails, retry. |
| Text encoding | Python's default text encoding on Windows is the legacy code page. Project code names `encoding="utf-8"`. The wrappers in `scripts\windows` set `PYTHONUTF8=1`; for your own runs: `$env:PYTHONUTF8 = '1'`. |
| Files made by PowerShell | Windows PowerShell 5.1 writes `>` output as UTF-16. Use `-Encoding utf8` or PowerShell 7. The receipt verifier and `sign_kit.py` accept UTF-16 and UTF-8 with a BOM, other tools may not. |
| Paths | In code use `pathlib`; paths **inside containers** are always POSIX (`PurePosixPath`), never join them with `os.path` (X9). |
| Permissions | POSIX modes (`0600`) do nothing on Windows. Secrets use the platform layer (DPAPI on the host); operator key files are restricted with `icacls`. |
| Event loop | Windows uses the Proactor loop by default; do not call `add_signal_handler` (it does not exist there). The SDKs are synchronous. |
| Symlinks | Creating one needs Developer Mode or an elevated shell. Tests that need them are skipped on Windows (`kits/tests/conftest.py`). |
| Long running commands | Use `Ctrl+C` in the window that started them; closing a terminal does not always stop child processes: check Task Manager for stray `node.exe` or `python.exe`. |

## 5. Before you push

1. `py -3 tasks.py windows-test` (or the pieces you touched).
2. No secret in any file, log or test output (L7): tests use fresh random keys.
3. If you touched shell scripts, keep the PowerShell wrapper in `scripts\windows` and the row in [operator-cheatsheet.md](operator-cheatsheet.md) in step.
4. CI runs the same suites on `windows-latest` (`.github/workflows/ci.yml`). Until the owner switches it from advisory to blocking, a red Windows
   job is information, not a merge blocker; read it anyway.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `python` opens the Microsoft Store | Use `py -3`, or install Python from python.org / winget and open a new window. |
| `running scripts is disabled on this system` | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`. |
| `npm` is not recognised after install | Open a new PowerShell window; check `where.exe npm`. |
| `UnicodeDecodeError` or `UnicodeEncodeError` | A tool used the legacy code page: `$env:PYTHONUTF8 = '1'`, or run through `tasks.py` / `scripts\windows\*.ps1`. |
| Tests fail only with `\r\n` differences | Your checkout converted line endings: `git config core.autocrlf false`, then clone again into a fresh folder (do not try to rewrite a working tree that holds uncommitted work). |
| `Filename too long` | `git config --global core.longpaths true` and enable long paths in Windows (`TODO(verify-docs)`: the registry/Group Policy setting). |
| `PermissionError: [WinError 32]` when deleting a temp file | Something still has it open. Close the handle, or fix the test (the file must be closed before removal). |
| `docker` not found | Start Docker Desktop, or install Engine in WSL2 and run docker commands from the WSL shell. |
| `py -3 tasks.py api-test` refuses to run | Intended (X3). Use `--wsl`, a WSL shell, or `up` for containers. |
