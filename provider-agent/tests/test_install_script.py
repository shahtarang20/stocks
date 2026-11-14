"""The one-line host installer (apps/web/public/install.sh), run in a sandbox.

Every test runs the REAL script with a PATH that contains only symlinks to a few basic tools plus fake `docker`, `curl`,
`nvidia-smi`, `uname` and `python3.12`, inside pytest's temporary folder. Nothing on this computer is installed, started or
changed. Dry runs show what a real run would do; the pieces that cannot run without root (the download check, the connect
step, the upgrade and rollback, the uninstall guard) are sourced and called on their own with fake system commands.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import subprocess
import tarfile
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "apps" / "web" / "public" / "install.sh"
TOOLS = ("awk", "grep", "sed", "cat", "id", "mktemp", "rm", "sleep", "sha256sum", "tr", "head", "getent", "env", "chmod",
         "mkdir", "tar", "dirname", "basename", "touch", "cut", "sort", "wc", "true", "false", "test", "tee", "printf", "date",
         "cp", "gzip", "stat", "ls", "mv", "stty", "rmdir")
TOKEN = "enr_abcdefgh12345678XYZ"
SERVER = "https://api.example.com"
WHEEL = "computecoin_agent-0.1.0-py3-none-any.whl"


def _write_exec(path: Path, body: str) -> None:
    path.unlink(missing_ok=True)  # replace a symlink to a real tool instead of writing THROUGH it to the real file
    assert path.parent.name == "bin" and "pytest" in str(path), "test fakes must live inside pytest's temporary folder"
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Box:
    """A fake computer: a bin folder with only what the test puts in it."""

    def __init__(self, root: Path):
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.sh = self.bin / "sh"
        os.symlink(os.path.realpath(shutil.which("sh")), self.sh)
        for tool in TOOLS:
            found = shutil.which(tool)
            if found and not (self.bin / tool).exists():
                os.symlink(found, self.bin / tool)
        self.systemd = root / "run-systemd"
        self.systemd.mkdir()
        self.units = root / "units"
        self.units.mkdir()
        self.kfd = root / "no-kfd"  # does not exist unless a test creates it
        self.served = root / "served"
        self.served.mkdir()
        self.install_dir = root / "opt" / "computecoin"
        self.data_home = root / "data"
        self.tmp = root / "tmp"
        self.tmp.mkdir()
        self.log = root / "calls.log"
        _write_exec(self.bin / "uname", '''case "$1" in -s) echo "${FAKE_UNAME_S:-Linux}";; -m) echo "${FAKE_UNAME_M:-x86_64}";; *) echo Linux;; esac''')

    def with_docker(self, runtimes="io.containerd.runc.v2 nvidia runc", ok=True):
        info = f"echo 'Runtimes: {runtimes}'; exit 0" if ok else "exit 1"
        _write_exec(self.bin / "docker", f'''
            case "$1" in
              info) {info} ;;
              ps) [ -z "${{FAKE_PS:-}}" ] || printf '%s\\n' $FAKE_PS; exit 0 ;;
            esac
            exit 0''')
        return self

    def with_nvidia(self):
        _write_exec(self.bin / "nvidia-smi", '''echo "GPU 0: Fake RTX (UUID: x)"''')
        return self

    def with_python(self, name="python3.12"):
        _write_exec(self.bin / name, '''[ "$1" = "-m" ] && [ "$2" = "venv" ] && mkdir -p "$3/bin"; echo 3.12; exit 0''')
        return self

    def with_curl(self):
        """curl [options] URL -o FILE: copies served/<last part of URL> to FILE, or fails like a 404. Logs its arguments."""
        _write_exec(self.bin / "curl", f'''
            echo "curl $*" >> "{self.log}"
            url=""; out=""
            while [ $# -gt 0 ]; do
              case "$1" in
                -o) out="$2"; shift 2 ;;
                --retry|--retry-delay|--connect-timeout|--max-time) shift 2 ;;
                -*) shift ;;
                *) url="$1"; shift ;;
              esac
            done
            name="${{url##*/}}"
            [ -f "$FAKE_SERVED/$name" ] || exit 22
            cp "$FAKE_SERVED/$name" "$out"
        ''')
        return self

    def record(self, *names, rc=0):
        """Fake commands that only write their arguments to the log."""
        for name in names:
            _write_exec(self.bin / name, f'''echo "{name} $*" >> "{self.log}"; exit {rc}''')
        return self

    def calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def serve(self, name: str, content: bytes | str) -> Path:
        path = self.served / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return path

    def serve_release(self, content=b"wheel-bytes", digest=None, lock=b"httpx==0.28.1 --hash=sha256:aa\n", lock_digest=None):
        digest = digest or hashlib.sha256(content).hexdigest()
        lock_digest = lock_digest or hashlib.sha256(lock).hexdigest()
        self.serve("LATEST", f"{WHEEL} {digest}\nrequirements.lock {lock_digest}\n")
        self.serve(WHEEL, content)
        self.serve("requirements.lock", lock)
        return digest

    def env(self, **extra) -> dict:
        env = {
            "PATH": str(self.bin), "TMPDIR": str(self.tmp), "HOME": str(self.root),
            "COMPUTECOIN_SYSTEMD_RUN_DIR": str(self.systemd), "COMPUTECOIN_KFD_PATH": str(self.kfd),
            "COMPUTECOIN_UNIT_DIR": str(self.units),
            "COMPUTECOIN_INSTALL_DIR": str(self.install_dir), "COMPUTECOIN_DATA_HOME": str(self.data_home),
            "FAKE_SERVED": str(self.served),
        }
        env.update(extra)
        return env

    def run(self, *args, **extra) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.sh), str(SCRIPT), *args], capture_output=True, text=True, env=self.env(**extra), timeout=60)

    def source(self, code: str, **extra) -> subprocess.CompletedProcess:
        """Load the script's functions without running main, then run `code`."""
        env = self.env(COMPUTECOIN_INSTALLER_SOURCED="1", **extra)
        return subprocess.run([str(self.sh), "-c", f'. "{SCRIPT}"\n{code}'], capture_output=True, text=True, env=env, timeout=60)

    def own_install_dir(self):
        self.install_dir.mkdir(parents=True, exist_ok=True)
        (self.install_dir / ".computecoin-install").write_text("")


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


@pytest.fixture
def ready(box):
    """A normal NVIDIA machine with Docker and Python 3.12."""
    return box.with_docker().with_nvidia().with_python().with_curl()


def out(p) -> str:
    return p.stdout + p.stderr


# ---- the file itself --------------------------------------------------------------------------------------------------
def test_the_script_has_valid_shell_syntax():
    assert subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True).returncode == 0


def test_it_runs_nothing_if_the_download_is_cut_short(box):
    text = SCRIPT.read_text()
    partial = box.root / "partial.sh"
    partial.write_text(text[: text.index("main() {")])  # everything up to (not including) the function that does the work
    done = subprocess.run([str(box.sh), str(partial), "--token", TOKEN], capture_output=True, text=True, env=box.env())
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""
    assert not box.install_dir.exists()


def test_the_last_lines_of_the_script_are_the_ones_that_start_it():
    lines = [line for line in SCRIPT.read_text().splitlines() if line.strip()]
    assert any(line.strip() == 'main "$@"' for line in lines[-3:])


def test_help_works_and_changes_nothing(box):
    done = box.run("--help")
    assert done.returncode == 0 and all(o in done.stdout for o in ("--dry-run", "--uninstall", "--reenroll", "--gpu-vendor", "--force"))
    assert not box.install_dir.exists()


def test_an_unknown_option_is_refused(box):
    done = box.run("--wat")
    assert done.returncode == 1 and "unknown option" in out(done)


# ---- what is accepted ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("token", ["enr_short", "abc", "enr_has space1234567", "enr_a;rm -rf /1234567", "enr_$(id)12345678",
                                   "ENR_abcdefgh12345678", "enr_abcdefgh12345678\nExecStartPre=/bin/evil"])
def test_a_token_that_is_not_plain_is_refused_before_anything_runs(ready, token):
    done = ready.run("--dry-run", "--token", token, "--server", SERVER)
    assert done.returncode == 1 and "does not look like a Computecoin setup token" in out(done)
    assert not ready.install_dir.exists()


@pytest.mark.parametrize("server", ["api.example.com", "ftp://x.com", "https://x.com/a b", "https://x.com;id", "https://x.com/$(id)",
                                    "javascript:alert(1)", "https://x.com/a\nExecStartPre=/bin/evil",
                                    "http://api.example.com", "http://localhost.evil.com"])
def test_a_server_address_that_is_not_plain_or_not_encrypted_is_refused(ready, server):
    done = ready.run("--dry-run", "--token", TOKEN, "--server", server)
    assert done.returncode == 1 and "server address" in out(done)


@pytest.mark.parametrize("server", ["http://localhost:8000", "http://127.0.0.1:8000/v1", "https://api.example.com:8443/v1"])
def test_https_and_a_local_test_server_are_accepted(ready, server):
    assert ready.run("--dry-run", "--token", TOKEN, "--server", server).returncode == 0


@pytest.mark.parametrize("bad", ["relative/dir", "/opt/x y", "/opt/$(id)", "/opt/a;b", "/opt/a\nExecStartPre=/bin/evil",
                                 "/", "/opt", "/usr", "/usr/local", "/etc", "/etc/computecoin", "/var", "/var/lib", "/home", "/root",
                                 "/tmp", "/proc/x", "/bin/x"])
def test_a_folder_that_is_not_plain_or_is_a_system_folder_is_refused(ready, bad):
    for var in ("COMPUTECOIN_INSTALL_DIR", "COMPUTECOIN_DATA_HOME"):
        done = ready.run("--dry-run", **{var: bad})
        assert done.returncode == 1 and "plain absolute path" in out(done), (var, bad)


def test_a_trailing_slash_on_a_system_folder_does_not_get_it_past_the_check(ready):
    assert ready.run("--dry-run", COMPUTECOIN_INSTALL_DIR="/opt/").returncode == 1
    assert ready.run("--dry-run", COMPUTECOIN_DATA_HOME="/var/lib//").returncode == 1


def test_a_package_address_must_be_plain_and_encrypted_too(ready):
    for bad in ("https://x.com/a;b", "http://dl.example.com/agent", "https://x.com/a\nb"):
        done = ready.run("--dry-run", "--package-base", bad)
        assert done.returncode == 1 and "package address" in out(done), bad


def test_trailing_slashes_are_removed_from_addresses(ready):
    text = out(ready.run("--dry-run", "--package-base", "https://dl.example.com/agent//", "--server", f"{SERVER}//", "--token", TOKEN))
    assert "https://dl.example.com/agent/LATEST" in text and "agent//" not in text


def test_a_gpu_vendor_must_be_nvidia_or_amd(ready):
    assert "nvidia or amd" in out(ready.run("--dry-run", "--gpu-vendor", "intel"))


# ---- the checks on the computer -----------------------------------------------------------------------------------------
def test_it_refuses_anything_but_linux(ready):
    done = ready.run("--dry-run", FAKE_UNAME_S="Darwin")
    assert done.returncode == 1 and "for Linux" in out(done)


def test_it_refuses_an_unsupported_processor(ready):
    done = ready.run("--dry-run", FAKE_UNAME_M="riscv64")
    assert done.returncode == 1 and "not supported" in out(done)


def test_it_needs_systemd(ready):
    shutil.rmtree(ready.systemd)
    done = ready.run("--dry-run")
    assert done.returncode == 1 and "systemd" in out(done)


def test_it_needs_curl(box):
    box.with_docker().with_python()
    done = box.run("--dry-run")
    assert done.returncode == 1 and "curl is missing" in out(done)


def test_it_does_not_install_docker_but_says_exactly_what_to_do(box):
    box.with_python().with_curl()
    done = box.run("--dry-run")
    assert done.returncode == 1 and "Docker is not installed" in out(done) and "docs.docker.com" in out(done)


def test_a_docker_that_is_not_running_is_explained(box):
    box.with_docker(ok=False).with_python().with_curl()
    done = box.run("--dry-run")
    assert done.returncode == 1 and "not running" in out(done) and "systemctl start docker" in out(done)


def test_without_root_a_real_run_stops_at_once_and_a_dry_run_does_not(ready):
    if os.geteuid() == 0:
        pytest.skip("running as root")
    real = ready.run("--token", TOKEN, "--server", SERVER)
    assert real.returncode == 1 and "sudo" in out(real)
    assert not ready.install_dir.exists()
    assert ready.run("--dry-run", "--token", TOKEN, "--server", SERVER).returncode == 0


def test_both_graphics_brands_are_refused_unless_the_host_chooses(ready):
    ready.kfd.write_text("")  # an AMD compute device AND an NVIDIA card
    done = ready.run("--dry-run")
    assert done.returncode == 1 and "both an NVIDIA and an AMD" in out(done) and "--gpu-vendor nvidia" in out(done)
    chosen = ready.run("--dry-run", "--gpu-vendor", "nvidia", "--token", TOKEN, "--server", SERVER)
    assert chosen.returncode == 0 and "Graphics card: NVIDIA" in out(chosen)
    assert "Environment=COMPUTECOIN_GPU_VENDOR=nvidia" in out(chosen)
    amd = ready.run("--dry-run", "--gpu-vendor", "amd")
    assert amd.returncode == 0 and "Graphics card: AMD" in out(amd) and "amdsmi" in out(amd)


def test_no_graphics_card_is_a_warning_not_a_failure(box):
    box.with_docker().with_python().with_curl()
    done = box.run("--dry-run", "--token", TOKEN, "--server", SERVER)
    assert done.returncode == 0 and "no graphics card was detected" in out(done)
    assert "amdsmi" not in done.stdout and "computecoin-powercap" not in done.stdout


def test_an_nvidia_machine_without_the_container_toolkit_is_warned(box):
    box.with_docker(runtimes="runc").with_nvidia().with_python().with_curl()
    done = box.run("--dry-run")
    assert done.returncode == 0 and "NVIDIA Container Toolkit" in out(done)


def test_selinux_enforcing_is_mentioned(ready):
    _write_exec(ready.bin / "getenforce", "echo Enforcing")
    assert "SELinux is enforcing" in out(ready.run("--dry-run"))


# ---- folders it is about to fill or delete must be its own ----------------------------------------------------------------
def test_an_unrelated_non_empty_program_folder_is_refused(ready):
    ready.install_dir.mkdir(parents=True)
    (ready.install_dir / "precious.txt").write_text("not ours")
    done = ready.run("--dry-run", "--token", TOKEN, "--server", SERVER)
    assert done.returncode == 1 and "was not made by this installer" in out(done)
    assert (ready.install_dir / "precious.txt").exists()


def test_an_empty_or_marked_program_folder_is_fine(ready):
    ready.install_dir.mkdir(parents=True)
    assert ready.run("--dry-run").returncode == 0  # empty
    ready.own_install_dir()
    (ready.install_dir / "more.txt").write_text("")
    assert ready.run("--dry-run").returncode == 0  # ours


def test_an_unrelated_non_empty_data_folder_is_refused_but_an_earlier_installs_is_not(ready):
    ready.data_home.mkdir(parents=True)
    (ready.data_home / "photos").write_text("not ours")
    done = ready.run("--dry-run")
    assert done.returncode == 1 and str(ready.data_home) in out(done) and "was not made by this installer" in out(done)
    (ready.data_home / ".computecoin").mkdir()
    assert ready.run("--dry-run").returncode == 0


# ---- a dry run shows the whole plan and changes nothing -------------------------------------------------------------
def test_a_dry_run_shows_every_step_and_changes_nothing(ready):
    done = ready.run("--dry-run", "--token", TOKEN, "--server", SERVER)
    text = out(done)
    assert done.returncode == 0, text
    assert "DRY RUN" in text and "Graphics card: NVIDIA" in text and "Using " in text and "python3.12" in text
    assert "venv" in text and "--require-hashes --no-deps -r" in text and "requirements.lock" in text  # every dependency hash-checked
    assert "would save the token in a private file" in text and "enroll --token-file" in text and "--server https://api.example.com" in text
    assert "useradd --system" in text
    assert "systemctl enable" in text and "systemctl restart" in text and "That was a dry run" in text
    assert not ready.install_dir.exists() and not ready.data_home.exists()  # nothing was created


def test_the_download_comes_first_so_a_failure_changes_nothing(ready):
    text = out(ready.run("--dry-run", "--token", TOKEN, "--server", SERVER))
    download = text.index("Downloading the agent")
    assert download < text.index("Installing the agent's own Python environment")
    assert download < text.index("Preparing the account")
    assert download < text.index("Installing the services")


def test_the_token_is_never_printed_and_never_on_a_command_line(ready):
    done = ready.run("--dry-run", "--token", TOKEN, "--server", SERVER)
    assert TOKEN not in out(done) and "enr_abcdefgh" not in out(done)
    assert "--token enr_" not in out(done)  # the agent is given a file, not the token itself


def test_the_token_can_come_from_the_environment(ready):
    done = ready.run("--dry-run", COMPUTECOIN_ENROLL_TOKEN=TOKEN, COMPUTECOIN_SERVER=SERVER)
    assert done.returncode == 0 and TOKEN not in out(done) and "--token-file" in out(done)


def test_both_services_are_written_with_a_restart_and_the_right_command(ready):
    text = out(ready.run("--dry-run", "--token", TOKEN, "--server", SERVER))
    assert "computecoin-agent.service (mode 644)" in text and "computecoin-watchdog.service (mode 644)" in text
    assert f"ExecStart={ready.install_dir}/venv/bin/computecoin-agent start" in text
    assert f"ExecStart={ready.install_dir}/venv/bin/computecoin-agent watchdog" in text
    assert text.count("Restart=always") == 2 and text.count("User=computecoin") == 2
    assert "RestartSec=15" in text and "RestartSec=5" in text  # the agent waits longer, the safety watchdog comes back fast
    assert text.count(f"Environment=HOME={ready.data_home}") == 3  # agent, watchdog and the power cap service
    assert "Wants=network-online.target docker.service" in text and "Requires=" not in text
    assert "NoNewPrivileges=true" in text and "PrivateTmp" not in text  # PrivateTmp would hide job folders from Docker


def test_run_as_root_drops_the_user_line_and_says_what_that_means(ready):
    text = out(ready.run("--dry-run", "--run-as-root", "--token", TOKEN, "--server", SERVER))
    assert "User=" not in text and "full access to this computer" in text and "useradd" not in text


def test_an_amd_machine_asks_for_the_amd_add_on(box):
    box.with_docker().with_python().with_curl()
    box.kfd.write_text("")
    text = out(box.run("--dry-run"))
    assert "Graphics card: AMD" in text and "amdsmi" in text and "computecoin-powercap" not in text


def test_no_python_312_means_a_private_one_with_a_checked_download(box):
    box.with_docker().with_nvidia().with_curl()  # no python at all
    text = out(box.run("--dry-run"))
    assert "private Python (uv " in text
    assert "uv-x86_64-unknown-linux-gnu.tar.gz" in text and "built into this installer" in text and "uv venv --python 3.12" in text and "uv pip install" in text
    assert "releases/download/0.13.8/" in text and "releases/latest" not in text  # an exact version, not "whatever is newest"


def test_the_private_python_download_matches_the_processor(box):
    box.with_docker().with_nvidia().with_curl()
    assert "uv-aarch64-unknown-linux-gnu.tar.gz" in out(box.run("--dry-run", FAKE_UNAME_M="aarch64"))


def test_an_older_system_python_is_not_used(box):
    box.with_docker().with_nvidia().with_curl()
    _write_exec(box.bin / "python3", '''echo 3.10; exit 1''')  # the version check fails: too old
    assert "private Python (uv " in out(box.run("--dry-run"))


def test_the_private_python_needs_tar_and_gzip_and_says_so(box):
    box.with_docker().with_nvidia().with_curl()
    (box.bin / "gzip").unlink()
    done = box.run("--dry-run")
    assert done.returncode == 1 and "tar and gzip are needed" in out(done)


def test_no_start_enables_but_does_not_start(ready):
    text = out(ready.run("--dry-run", "--no-start", "--token", TOKEN, "--server", SERVER))
    assert "systemctl enable" in text and "systemctl restart" not in text and "Not started (--no-start)" in text


# ---- the download is checked, before anything is changed -------------------------------------------------------------------
DOWNLOAD = '''
DRY_RUN=0; PACKAGE_BASE="https://releases.invalid/agent-latest"; TMP_DIR="$TMPDIR/w"; mkdir -p "$TMP_DIR"
download_agent
echo "WHEEL=$WHEEL"
'''


def test_a_download_that_matches_its_checksum_is_accepted(ready):
    ready.serve_release()
    done = ready.source(DOWNLOAD)
    assert done.returncode == 0, out(done)
    assert f"Downloaded and checked {WHEEL}" in done.stdout and f"WHEEL={WHEEL}" in done.stdout
    assert (ready.tmp / "w" / WHEEL).exists()


def test_a_download_that_does_not_match_is_refused_and_nothing_was_changed(ready):
    ready.serve_release(digest="0" * 64)
    done = ready.source(DOWNLOAD)
    assert done.returncode == 1 and "does not match its SHA-256" in out(done) and "Nothing was changed" in out(done)


@pytest.mark.parametrize("name", ["../evil.whl", "evil.whl", "computecoin_agent-1.0-py3-none-any.whl;id", "/etc/passwd", "computecoin_agent-1.0.tar.gz"])
def test_a_strange_file_name_in_the_release_list_is_refused(ready, name):
    ready.serve("LATEST", f"{name} {'a' * 64}\n")
    done = ready.source(DOWNLOAD)
    assert done.returncode == 1 and "unexpected file name" in out(done)


@pytest.mark.parametrize("digest", ["", "xyz", "A" * 64, "a" * 63, "a" * 65])
def test_a_strange_checksum_in_the_release_list_is_refused(ready, digest):
    ready.serve("LATEST", f"{WHEEL} {digest}\n")
    done = ready.source(DOWNLOAD)
    assert done.returncode == 1 and "unexpected checksum" in out(done)


def test_a_missing_release_gives_a_plain_message_and_changes_nothing(ready):
    done = ready.source(DOWNLOAD)  # nothing served
    assert done.returncode == 1 and "could not download the agent list" in out(done) and "Nothing was changed" in out(done)


def test_every_download_has_retries_and_time_limits(ready):
    ready.serve_release()
    ready.source(DOWNLOAD)
    lines = ready.calls().splitlines()
    assert lines
    for line in lines:
        assert "--retry 3" in line and "--connect-timeout 15" in line and "--max-time 900" in line, line


# ---- installing, upgrading, and putting the old agent back ----------------------------------------------------------------
BUILD_SETUP = '''
DRY_RUN=0; USE_UV=0; PY="$(command -v python3.12)"; TMP_DIR="$TMPDIR/w"; WHEEL=w.whl; LOCK_FILE="$TMPDIR/w/requirements.lock"
GPU={gpu}; NO_START=0; STOPPED_SERVICES=0
mkdir -p "$TMP_DIR" "$(dirname "$INSTALL_DIR")"
pip_install_locked() {{ echo "LOCKED $*"; {locked_rc}; }}
pip_install() {{ echo "PIP $*"; {pip_rc}; }}
'''


def _build(box, code, gpu="none", pip_rc="return 0", locked_rc="return 0", **env):
    box.record("chown")  # the installer hands the program folder to root; here it only notes that it did
    return box.source(BUILD_SETUP.format(gpu=gpu, pip_rc=pip_rc, locked_rc=locked_rc) + code, **env)


def test_the_dependencies_are_installed_from_the_hash_locked_list_before_the_agent(ready):
    done = _build(ready, "build_environment", gpu="nvidia")
    assert done.returncode == 0, out(done)
    lines = [line for line in done.stdout.splitlines() if line.startswith(("LOCKED", "PIP"))]
    assert lines == [f"LOCKED {ready.tmp}/w/requirements.lock", f"PIP --no-deps {ready.tmp}/w/w.whl"]  # no extras, no dependency lookups


def test_a_dependency_list_that_fails_stops_the_install_before_the_agent_is_installed(ready):
    done = _build(ready, "build_environment", locked_rc="return 1")
    assert done.returncode == 1 and "PIP" not in done.stdout  # nothing from outside the checked list was installed


def test_the_amd_monitoring_add_on_is_separate_and_the_host_is_told_it_is_not_hash_checked(ready):
    done = _build(ready, "build_environment", gpu="amd")
    assert done.returncode == 0 and "PIP --no-deps --only-binary :all: amdsmi" in done.stdout and "without a hash check" in done.stderr  # no sdist built as root, no dependencies pulled
    failed = _build(ready, "build_environment", gpu="amd", pip_rc='case "$*" in *amdsmi*) return 1;; esac')
    assert failed.returncode == 0 and "could not be installed" in failed.stderr  # the agent still installs and runs
    assert "amdsmi" not in _build(ready, "build_environment", gpu="nvidia").stdout


def test_the_private_python_stays_inside_the_program_folder_where_the_agent_account_can_read_it(ready):
    uv = ready.bin / "uv-stub"
    _write_exec(uv, f'''echo "uv $* DIR=$UV_PYTHON_INSTALL_DIR CACHE=$UV_CACHE_DIR PREF=$UV_PYTHON_PREFERENCE" >> "{ready.log}"
                        [ "$1" = venv ] && mkdir -p "$4/bin"; exit 0''')
    done = _build(ready, f'USE_UV=1; UV_BIN="{uv}"; build_environment')
    assert done.returncode == 0, out(done)
    venv_line = next(line for line in ready.calls().splitlines() if line.startswith("uv venv"))
    assert f"DIR={ready.install_dir}/python" in venv_line  # not in root's home folder
    assert f"CACHE={ready.tmp}/w/uv-cache" in venv_line and "PREF=only-managed" in venv_line


def test_the_program_folder_is_readable_by_the_agents_own_account(ready):
    _build(ready, "build_environment")
    assert ready.install_dir.stat().st_mode & 0o005 == 0o005  # others can enter and read


def test_an_upgrade_keeps_the_old_environment_until_the_new_one_works(ready):
    ready.own_install_dir()
    (ready.install_dir / "venv").mkdir()
    (ready.install_dir / "venv" / "old.txt").write_text("old agent")
    done = _build(ready, "build_environment")
    assert done.returncode == 0
    assert (ready.install_dir / "venv.old" / "old.txt").read_text() == "old agent"  # kept for a rollback


def test_a_failed_upgrade_puts_the_old_agent_back_and_restarts_it(ready):
    ready.record("systemctl")
    ready.own_install_dir()
    (ready.install_dir / "venv").mkdir()
    (ready.install_dir / "venv" / "old.txt").write_text("old agent")
    (ready.units / "computecoin-agent.service").write_text("")
    done = _build(ready, "replace_agent", pip_rc="return 1")
    assert done.returncode == 1 and "previous version" in out(done)
    assert (ready.install_dir / "venv" / "old.txt").read_text() == "old agent"  # the working agent is back
    assert not (ready.install_dir / "venv.old").exists()
    calls = ready.calls().splitlines()
    assert "systemctl stop computecoin-agent" in calls
    assert "systemctl stop computecoin-watchdog" not in calls  # that unit file does not exist here: it is not named in a command
    assert calls[-1] == "systemctl start computecoin-watchdog computecoin-agent"  # and running again


def test_a_failed_first_install_leaves_no_half_environment(ready):
    ready.record("systemctl")
    done = _build(ready, "replace_agent", pip_rc="return 1")
    assert done.returncode == 1 and not (ready.install_dir / "venv").exists()
    assert "systemctl start" not in ready.calls()  # nothing had been running, so nothing is started


def test_a_good_upgrade_removes_the_old_copy(ready):
    ready.record("systemctl")
    ready.own_install_dir()
    (ready.install_dir / "venv").mkdir()
    done = _build(ready, "replace_agent")
    assert done.returncode == 0 and not (ready.install_dir / "venv.old").exists()


def test_a_marker_is_left_as_soon_as_the_folder_is_claimed_so_a_half_run_can_be_repeated(ready):
    _build(ready, "claim_install_dir")
    assert (ready.install_dir / ".computecoin-install").exists()
    (ready.install_dir / "uv").mkdir()  # what a half-finished first run leaves
    assert ready.run("--dry-run").returncode == 0  # a second run is not refused


def test_a_second_run_stops_the_old_services_first(ready):
    (ready.units / "computecoin-agent.service").write_text("")
    (ready.units / "computecoin-watchdog.service").write_text("")
    text = out(ready.source('DRY_RUN=1; stop_existing'))
    assert "systemctl stop computecoin-agent\n" in text and "systemctl stop computecoin-watchdog\n" in text  # one unit at a time
    (ready.units / "computecoin-agent.service").unlink()
    (ready.units / "computecoin-watchdog.service").unlink()
    assert "systemctl stop" not in out(ready.source('DRY_RUN=1; stop_existing'))


def test_an_upgrade_does_not_cut_off_a_customer_job_without_being_told(ready):
    (ready.units / "computecoin-agent.service").write_text("")
    busy = ready.run("--dry-run", FAKE_PS="ccoin-job-abc123 unrelated-container")
    assert busy.returncode == 1 and "customer job is running" in out(busy) and "--force" in out(busy)
    assert ready.run("--dry-run", "--force", FAKE_PS="ccoin-job-abc123").returncode == 0
    assert ready.run("--dry-run", FAKE_PS="the-owners-database").returncode == 0  # only Computecoin's containers count
    (ready.units / "computecoin-agent.service").unlink()
    assert ready.run("--dry-run", FAKE_PS="ccoin-job-abc123").returncode == 0  # a first install has nothing to cut off


def test_running_customer_work_counts_jobs_sessions_and_services(ready):
    done = ready.source("running_customer_work", FAKE_PS="ccoin-job-1 ccoin-sess-2 cc-svc-3 mydb ccoin-proxy-1")
    assert done.stdout.strip() == "3"
    assert ready.source("running_customer_work").stdout.strip() == "0"


# ---- the account the services run as ---------------------------------------------------------------------------------------
def test_the_agent_account_is_made_once_with_a_locked_home_and_only_the_groups_that_exist(box):
    box.record("useradd", "usermod", "chown")
    _write_exec(box.bin / "getent", '''[ "$1" = group ] && [ "$2" = docker ] && echo "docker:x:998:" && exit 0; exit 2''')
    done = box.source("DRY_RUN=0; RUN_AS_ROOT=0; setup_user")
    assert done.returncode == 0, out(done)
    calls = box.calls()
    assert f"useradd --system --home-dir {box.data_home} --create-home --shell /usr/sbin/nologin computecoin" in calls
    assert "usermod -aG docker computecoin" in calls
    assert "usermod -aG render" not in calls and "usermod -aG video" not in calls  # those groups do not exist here
    assert f"chown -R computecoin:computecoin {box.data_home}" in calls  # everything inside, not only the top folder
    assert oct(box.data_home.stat().st_mode & 0o777) == "0o700"  # only the account itself can read the connection key
    assert (box.data_home / ".computecoin-install").exists()


def test_an_existing_agent_account_is_reused(box):
    box.record("useradd", "usermod", "chown")
    _write_exec(box.bin / "id", '''exit 0''')  # the account already exists
    box.source("DRY_RUN=0; RUN_AS_ROOT=0; setup_user")
    assert "useradd" not in box.calls()


def test_run_as_root_makes_no_account(box):
    box.record("useradd", "usermod", "chown")
    box.source("DRY_RUN=0; RUN_AS_ROOT=1; setup_user")
    assert "useradd" not in box.calls() and oct(box.data_home.stat().st_mode & 0o777) == "0o700"


def test_the_agents_account_must_be_able_to_run_the_agent_or_it_says_why(box):
    failing = box.source('DRY_RUN=0; RUN_AS_ROOT=0; as_service_user() { return 1; }; check_service_user_can_run')
    assert failing.returncode == 1 and "cannot run the installed agent" in out(failing) and "--run-as-root" in out(failing)
    assert box.source('DRY_RUN=0; RUN_AS_ROOT=0; as_service_user() { return 0; }; check_service_user_can_run').returncode == 0
    assert box.source('DRY_RUN=0; RUN_AS_ROOT=1; as_service_user() { return 1; }; check_service_user_can_run').returncode == 0


# ---- connecting ------------------------------------------------------------------------------------------------------------
def _credential(box, secret=True):
    cfg = box.data_home / ".computecoin"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "agent.toml").write_text('machine_id = "m"\nmachine_secret = "s"\n' if secret else 'api_url = "https://x"\n')
    return cfg / "agent.toml"


# `as_service_user` is replaced by a function that records its arguments; the exit status of `run` and of `enroll` is chosen per test.
CONNECT = '''
DRY_RUN=0; RUN_AS_ROOT=0
as_service_user() { echo "SVC $*"; case "$*" in *" run "*) return ${RUN_RC:-0};; esac; return ${ENROLL_RC:-0}; }
PRE
enroll_machine
echo "CONNECTED=$ALREADY_CONNECTED"
'''


def _connect(box, pre="", **env):
    box.data_home.mkdir(parents=True, exist_ok=True)
    box.record("chown")
    return box.source(CONNECT.replace("PRE", pre), **env)


def test_a_saved_connection_that_still_works_is_not_connected_twice(ready):
    _credential(ready)
    done = _connect(ready, RUN_RC="0")
    assert "already connected" in done.stdout and "enroll" not in done.stdout and "CONNECTED=1" in done.stdout
    with_token = _connect(ready, f"TOKEN={TOKEN}", RUN_RC="0")
    assert "enroll" not in with_token.stdout and "still valid" in with_token.stderr  # the unused token is not wasted silently


def test_a_saved_connection_the_server_rejects_is_not_silently_kept(ready):
    _credential(ready)
    done = _connect(ready, f"TOKEN={TOKEN}; SERVER={SERVER}", RUN_RC="1")
    assert done.returncode == 1 and "no longer accepts" in out(done) and "--reenroll" in out(done)
    assert "enroll --token-file" not in done.stdout  # it did not pretend a new token was used


def test_reenroll_keeps_the_old_connection_aside_and_connects_again(ready):
    old = _credential(ready)
    done = _connect(ready, f"TOKEN={TOKEN}; SERVER={SERVER}; REENROLL=1")
    assert done.returncode == 0, out(done)
    assert not old.exists() and list(old.parent.glob("agent.toml.old-*"))
    assert "enroll --token-file" in done.stdout and "CONNECTED=1" in done.stdout


def test_reenroll_needs_a_token_and_a_server(ready):
    _credential(ready)
    done = _connect(ready, "REENROLL=1")
    assert done.returncode == 1 and "--reenroll needs a new token" in out(done)
    assert (ready.data_home / ".computecoin" / "agent.toml").exists()  # nothing was moved


def test_a_new_computer_is_connected_with_a_token_file_not_a_command_line_token(ready):
    done = _connect(ready, f"TOKEN={TOKEN}; SERVER={SERVER}")
    assert f"enroll --token-file {ready.data_home}/.enroll-token --server {SERVER}" in done.stdout
    assert TOKEN not in out(done) and "CONNECTED=1" in done.stdout
    assert not (ready.data_home / ".enroll-token").exists()  # deleted straight after
    assert f"chown computecoin:computecoin {ready.data_home}/.enroll-token" in ready.calls()  # readable by the agent's account only


def test_the_token_file_is_private_while_it_exists(ready):
    code = CONNECT.replace('echo "SVC $*";', 'stat -c "%a" "$DATA_HOME/.enroll-token" >> "$TMPDIR/mode"; echo "SVC $*";').replace(
        "PRE", f"TOKEN={TOKEN}; SERVER={SERVER}")
    ready.data_home.mkdir(parents=True, exist_ok=True)
    ready.record("chown")
    ready.source(code)
    assert (ready.tmp / "mode").read_text().strip() == "600"


def test_a_failed_connection_says_the_token_may_have_expired_and_still_removes_the_file(ready):
    done = _connect(ready, f"TOKEN={TOKEN}; SERVER={SERVER}", ENROLL_RC="1")
    assert done.returncode == 1 and "connecting to your account failed" in out(done) and "new one" in out(done)
    assert not (ready.data_home / ".enroll-token").exists()


def test_a_computer_with_no_token_is_not_silently_started(ready):
    done = _connect(ready)
    assert done.returncode == 1 and "not connected yet" in out(done) and "--token enr_" in out(done)


def test_a_settings_file_without_a_machine_secret_is_not_a_connection(ready):
    _credential(ready, secret=False)
    done = _connect(ready, f"TOKEN={TOKEN}; SERVER={SERVER}")
    assert "enroll --token-file" in done.stdout


def test_the_token_file_is_removed_even_if_the_installer_is_interrupted(ready):
    ready.data_home.mkdir(parents=True)
    (ready.data_home / ".enroll-token").write_text(TOKEN)
    ready.source('cleanup')
    assert not (ready.data_home / ".enroll-token").exists()


# ---- services --------------------------------------------------------------------------------------------------------------
SERVICES = '''
DRY_RUN=0; NO_START=@N@; RUN_AS_ROOT=0; ALREADY_CONNECTED=@C@; UNIT_DIR="$TMPDIR/units"; mkdir -p "$UNIT_DIR"
install_services
'''


def _services(box, no_start, connected):
    return box.source(SERVICES.replace("@N@", str(no_start)).replace("@C@", str(connected)))


def test_the_services_are_written_enabled_and_started(box):
    box.record("systemctl")
    done = _services(box, 0, 1)
    assert done.returncode == 0, out(done)
    agent = (box.tmp / "units" / "computecoin-agent.service").read_text()
    watchdog = (box.tmp / "units" / "computecoin-watchdog.service").read_text()
    assert f"ExecStart={box.install_dir}/venv/bin/computecoin-agent start" in agent and "User=computecoin" in agent and "RestartSec=15" in agent
    assert f"ExecStart={box.install_dir}/venv/bin/computecoin-agent watchdog" in watchdog and "RestartSec=5" in watchdog
    assert oct((box.tmp / "units" / "computecoin-agent.service").stat().st_mode & 0o777) == "0o644"
    assert box.calls().splitlines() == ["systemctl daemon-reload", "systemctl enable computecoin-watchdog computecoin-agent",
                                        "systemctl restart computecoin-watchdog computecoin-agent"]


def test_nothing_is_started_when_the_computer_is_not_connected_or_when_asked_not_to(box):
    box.record("systemctl")
    _services(box, 0, 0)
    assert "restart" not in box.calls() and "enable" in box.calls()
    box.log.unlink()
    _services(box, 1, 1)
    assert "restart" not in box.calls() and "enable" in box.calls()


def test_the_unit_text_matches_what_the_agent_documents(box):
    from computecoin_agent import watchdog

    assert "ExecStart=/x/computecoin-agent watchdog" in watchdog.systemd_unit("/x/computecoin-agent")
    text = box.source('RUN_AS_ROOT=0; unit_text "d" "/x/computecoin-agent watchdog" 5').stdout
    assert "ExecStart=/x/computecoin-agent watchdog" in text and "Restart=always" in text and "RestartSec=5" in text
    assert "COMPUTECOIN_GPU_VENDOR" not in text
    assert "Environment=COMPUTECOIN_GPU_VENDOR=amd" in box.source('RUN_AS_ROOT=0; GPU_VENDOR=amd; unit_text "d" "/x" 5').stdout


def test_a_service_that_does_not_stay_up_is_reported_with_where_to_look(box):
    _write_exec(box.bin / "sleep", "exit 0")
    _write_exec(box.bin / "systemctl", '''[ "$1" = is-active ] && exit 3; exit 0''')
    done = box.source("DRY_RUN=0; NO_START=0; ALREADY_CONNECTED=1; verify_running")
    assert done.returncode == 1 and "did not stay running" in done.stderr and "journalctl -u computecoin-agent" in done.stderr
    _write_exec(box.bin / "systemctl", '''exit 0''')
    ok = box.source("DRY_RUN=0; NO_START=0; ALREADY_CONNECTED=1; verify_running")
    assert ok.returncode == 0 and "The agent is running." in ok.stdout


def test_writing_a_file_is_private_while_written_and_does_not_leak_the_umask(box):
    target = box.root / "f.service"
    done = box.source(f'DRY_RUN=0; before=$(umask); echo hi | write_file "{target}" 644; echo "$before $(umask)"')
    assert target.read_text() == "hi\n" and oct(target.stat().st_mode & 0o777) == "0o644"
    before, after = done.stdout.split()
    assert before == after


# ---- the private Python download -------------------------------------------------------------------------------------------
def _uv_tarball(arch="x86_64", payload=b"#!/bin/sh\necho uv\n"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(f"uv-{arch}-unknown-linux-gnu/uv")
        info.size, info.mode = len(payload), 0o755
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


UV_CALL = 'DRY_RUN=0; ARCH=x86_64; TMP_DIR="$TMPDIR/u"; mkdir -p "$TMP_DIR" "$(dirname "$INSTALL_DIR")"; {pin} fetch_uv; echo "UV=$UV_BIN USE=$USE_UV"'


def test_the_private_python_is_used_only_if_it_matches_the_checksum_built_into_the_installer(ready):
    asset = "uv-x86_64-unknown-linux-gnu.tar.gz"
    blob = _uv_tarball()
    ready.serve(asset, blob)
    good = ready.source(UV_CALL.format(pin=f'UV_SHA256_X86_64={hashlib.sha256(blob).hexdigest()};'))
    assert good.returncode == 0, out(good)
    assert f"UV={ready.install_dir}/uv/uv USE=1" in good.stdout and (ready.install_dir / "uv" / "uv").exists()
    assert (ready.install_dir / ".computecoin-install").exists()  # claimed, so a half-finished run can be repeated
    assert "releases/download/0.13.8/uv-x86_64" in ready.calls()  # an exact, pinned version

    bad = ready.source(UV_CALL.format(pin=f"UV_SHA256_X86_64={'0' * 64};"))  # not the checksum the installer expects
    assert bad.returncode == 1 and "does not match the checksum built into this installer" in out(bad)


def test_a_tampered_download_is_refused_even_with_a_matching_checksum_file_next_to_it(ready):
    asset = "uv-x86_64-unknown-linux-gnu.tar.gz"
    tampered = _uv_tarball(payload=b"#!/bin/sh\necho evil\n")
    ready.serve(asset, tampered)
    ready.serve(asset + ".sha256", f"{hashlib.sha256(tampered).hexdigest()}  {asset}\n")  # an attacker who controls the download can supply this
    done = ready.source(UV_CALL.format(pin=""))  # the REAL checksum built into the script
    assert done.returncode == 1 and "does not match the checksum built into this installer" in out(done)
    assert not (ready.install_dir / "uv" / "uv").exists()


def test_the_built_in_checksums_are_real_looking_and_per_processor(box):
    done = box.source('echo "$UV_VERSION $UV_SHA256_X86_64 $UV_SHA256_AARCH64"')
    version, x86, arm = done.stdout.split()
    assert version.count(".") == 2 and len(x86) == 64 and len(arm) == 64 and x86 != arm
    assert all(c in "0123456789abcdef" for c in x86 + arm)
    arm_call = box.source('DRY_RUN=0; ARCH=aarch64; TMP_DIR="$TMPDIR/u"; mkdir -p "$TMP_DIR" "$(dirname "$INSTALL_DIR")"; fetch_uv', FAKE_UNAME_M="aarch64")
    assert "uv-aarch64-unknown-linux-gnu" in arm_call.stderr + arm_call.stdout or arm_call.returncode == 1  # asks for the ARM file


# ---- uninstall never touches a folder the installer did not make -----------------------------------------------------------
def test_uninstall_removes_the_folder_only_when_the_installer_made_it(ready):
    ready.install_dir.mkdir(parents=True)
    (ready.install_dir / "precious.txt").write_text("not ours")
    text = out(ready.run("--uninstall", "--dry-run"))
    assert "was not made by this installer" in text and "+ rm -rf" not in text  # no removal command at all
    (ready.install_dir / ".computecoin-install").write_text("")
    text = out(ready.run("--uninstall", "--dry-run"))
    assert f"+ rm -rf {ready.install_dir}" in text and "connection key and settings are kept" in text and "--reenroll" in text
    assert (ready.install_dir / "precious.txt").exists()  # a dry run removed nothing


def test_uninstall_never_removes_the_connection_key(ready):
    text = out(ready.run("--uninstall", "--dry-run"))
    assert str(ready.data_home) in text and f"+ rm -rf {ready.data_home}" not in text  # only advice, never a command it runs


def test_uninstall_does_not_cut_off_a_customer_job_without_being_told(ready):
    (ready.units / "computecoin-agent.service").write_text("")
    busy = ready.run("--uninstall", "--dry-run", FAKE_PS="ccoin-job-abc")
    assert busy.returncode == 1 and "customer job is running" in out(busy)
    assert ready.run("--uninstall", "--dry-run", "--force", FAKE_PS="ccoin-job-abc").returncode == 0


# ---- the pieces ----------------------------------------------------------------------------------------------------------
def test_validators_accept_normal_values_and_reject_the_rest(box):
    good = box.source('valid_token enr_abcdefgh12345678 && valid_url https://api.x.com:8443/v1 && valid_url http://localhost:8000 '
                      '&& valid_path /opt/computecoin && valid_path /srv/computecoin/data && echo GOOD')
    assert "GOOD" in good.stdout
    for bad in ('valid_token "enr_a b"', 'valid_url "https://x y"', 'valid_url "http://x.com"', 'valid_path "/"', 'valid_path "/opt"',
                'valid_path "x"', "valid_token ''"):
        assert box.source(bad).returncode != 0, bad


def test_a_value_with_a_second_line_is_refused_by_every_validator(box):
    for name, value in (("valid_token", "enr_abcdefgh12345678\nX"), ("valid_url", "https://x.com\nX"), ("valid_path", "/opt/a\nX")):
        done = box.source(f'{name} "$V"', V=value)
        assert done.returncode != 0, name


def test_the_masking_hides_every_token_in_a_line(box):
    done = box.source('mask "a enr_abc123 b enr_zzz999-_ c"')
    assert done.stdout == "a enr_**** b enr_**** c"


def test_the_locked_install_uses_hash_checking_and_no_dependency_lookups(ready):
    py = ready.source('DRY_RUN=1; USE_UV=0; pip_install_locked /x/requirements.lock').stdout
    assert f"+ {ready.install_dir}/venv/bin/python -m pip install --quiet --require-hashes --no-deps -r /x/requirements.lock" in py
    uv = ready.source('DRY_RUN=1; USE_UV=1; UV_BIN=/u/uv; pip_install_locked /x/requirements.lock').stdout
    assert f"+ /u/uv pip install --python {ready.install_dir}/venv/bin/python --require-hashes --no-deps -r /x/requirements.lock" in uv


# ---- the release must come with a hash-locked dependency list --------------------------------------------------------------------
def test_a_release_without_a_dependency_list_is_refused_and_nothing_was_changed(ready):
    ready.serve("LATEST", f"{WHEEL} {hashlib.sha256(b'wheel-bytes').hexdigest()}\n")  # an old-style one-line list
    ready.serve(WHEEL, b"wheel-bytes")
    done = ready.source(DOWNLOAD)
    assert done.returncode == 1 and "no hash-locked dependency list" in out(done) and "Nothing was changed" in out(done)


def test_a_dependency_list_that_does_not_match_its_checksum_is_refused(ready):
    ready.serve_release(lock_digest="0" * 64)
    done = ready.source(DOWNLOAD)
    assert done.returncode == 1 and "dependency list does not match its SHA-256" in out(done)


def test_a_missing_dependency_list_file_is_refused(ready):
    ready.serve_release()
    (ready.served / "requirements.lock").unlink()
    done = ready.source(DOWNLOAD)
    assert done.returncode == 1 and "could not download the dependency list" in out(done)


@pytest.mark.parametrize("line", ["other.txt " + "a" * 64, "requirements.lock xyz", "requirements.lock " + "A" * 64])
def test_a_strange_dependency_entry_in_the_release_list_is_refused(ready, line):
    ready.serve("LATEST", f"{WHEEL} {'a' * 64}\n{line}\n")
    assert ready.source(DOWNLOAD).returncode == 1


def test_a_good_release_downloads_both_files(ready):
    ready.serve_release()
    done = ready.source(DOWNLOAD)
    assert done.returncode == 0 and "and its dependency list" in done.stdout
    assert (ready.tmp / "w" / "requirements.lock").exists()


# ---- the token is asked for, not put on a command line -----------------------------------------------------------------------------
def _tty(box, text):
    path = box.root / "fake-tty"
    path.write_text(text)
    return path


ASK = '''
DRY_RUN=0; REENROLL=0; TOKEN=""; SERVER="https://api.example.com"
collect_token
echo "TOKEN_SET=${TOKEN:+yes}"
'''


def test_the_token_is_read_from_the_terminal_and_never_printed(ready):
    tty = _tty(ready, TOKEN + "\n")
    done = ready.source(ASK, COMPUTECOIN_TTY=str(tty))
    assert done.returncode == 0, out(done)
    assert "TOKEN_SET=yes" in done.stdout and TOKEN not in out(done)  # accepted, never echoed back
    assert "typing is hidden" in tty.read_text()  # the prompt itself went to the terminal, not to the script's output


def test_a_wrong_entry_is_asked_again_and_three_wrong_ones_stop_it(ready):
    ok = ready.source(ASK, COMPUTECOIN_TTY=str(_tty(ready, "oops\nstill wrong\n" + TOKEN + "\n")))
    assert ok.returncode == 0 and "TOKEN_SET=yes" in ok.stdout
    bad = ready.source(ASK, COMPUTECOIN_TTY=str(_tty(ready, "a\nb\nc\n" + TOKEN + "\n")))
    assert bad.returncode == 1 and "no setup token was entered" in out(bad)  # the fourth line is not read


def test_spaces_and_a_stray_return_character_around_a_pasted_token_are_ignored(ready):
    done = ready.source(ASK, COMPUTECOIN_TTY=str(_tty(ready, f"  {TOKEN}\r  \n")))
    assert done.returncode == 0 and "TOKEN_SET=yes" in done.stdout


def test_with_no_terminal_it_says_to_use_the_token_option_instead(ready):
    done = ready.source(ASK, COMPUTECOIN_TTY=str(ready.root / "no-such-tty"))
    assert done.returncode == 1 and "there is no terminal to ask on" in out(done) and "--token enr_" in out(done)


def test_typing_is_hidden_on_a_real_terminal_and_always_shown_again(ready):
    ready.record("stty")
    done = ready.source(ASK, COMPUTECOIN_TTY=str(_tty(ready, TOKEN + "\n")))
    assert done.returncode == 0
    stty = [line for line in ready.calls().splitlines() if line.startswith("stty")]
    assert stty == ["stty -echo", "stty echo"]  # hidden while typing, restored straight after


def test_typing_is_shown_again_even_if_the_installer_is_interrupted_at_the_prompt(ready):
    ready.record("stty")
    ready.source('ECHO_OFF=1; cleanup', COMPUTECOIN_TTY=str(_tty(ready, "")))
    assert "stty echo" in ready.calls()


def test_nothing_is_asked_when_a_token_was_given_or_the_computer_is_already_connected(ready):
    given = ready.source(ASK.replace('TOKEN=""', f'TOKEN={TOKEN}'), COMPUTECOIN_TTY=str(ready.root / "no-such-tty"))
    assert given.returncode == 0 and "TOKEN_SET=yes" in given.stdout
    _credential(ready)
    connected = ready.source(ASK, COMPUTECOIN_TTY=str(ready.root / "no-such-tty"))
    assert connected.returncode == 0 and "TOKEN_SET=" in connected.stdout and "yes" not in connected.stdout
    again = ready.source(ASK.replace("REENROLL=0", "REENROLL=1"), COMPUTECOIN_TTY=str(ready.root / "no-such-tty"))
    assert again.returncode == 1  # connecting again needs a NEW token


def test_a_missing_server_is_reported_before_anything_is_asked_or_downloaded(ready):
    done = ready.source(ASK.replace('SERVER="https://api.example.com"', 'SERVER=""'), COMPUTECOIN_TTY=str(_tty(ready, TOKEN + "\n")))
    assert done.returncode == 1 and "server address is missing" in out(done)


def test_a_dry_run_says_it_would_ask(ready):
    text = out(ready.run("--dry-run", "--server", SERVER))
    assert "would ask you to paste the setup token" in text


def test_the_installer_asks_before_it_downloads(ready):
    # in a dry run both steps are announced, in order: the question comes first so a mistake costs nothing
    text = out(ready.run("--dry-run", "--server", SERVER))
    assert text.index("would ask you to paste") < text.index("Downloading the agent")


# ---- the power cap service ------------------------------------------------------------------------------------------------
def test_an_nvidia_computer_with_an_unprivileged_agent_gets_a_small_root_only_cap_service(ready):
    text = out(ready.run("--dry-run", "--token", TOKEN, "--server", SERVER))
    assert "computecoin-powercap.service (mode 644)" in text
    unit = text.split("computecoin-powercap.service (mode 644):")[1].split("would write")[0]
    assert "Type=oneshot" in unit and "RemainAfterExit=yes" in unit
    assert f"ExecStart={ready.install_dir}/venv/bin/computecoin-agent power-cap apply" in unit
    assert f"ExecStop={ready.install_dir}/venv/bin/computecoin-agent power-cap restore" in unit
    assert "User=" not in unit  # root: that is the whole point, and why it does nothing else
    assert "StateDirectory=computecoin-powercap" in unit
    assert "COMPUTECOIN_POWER_STATE_FILE=/var/lib/computecoin-powercap/state.json" in unit  # root-owned, not writable by the agent's account
    assert "systemctl enable computecoin-powercap" in text and "systemctl restart computecoin-powercap" in text


def test_the_agent_and_watchdog_still_run_without_root(ready):
    text = out(ready.run("--dry-run", "--token", TOKEN, "--server", SERVER))
    assert text.count("User=computecoin") == 2  # agent and watchdog; the cap service is the only one without a User line


def test_no_cap_service_where_it_is_not_needed(box):
    box.with_docker().with_python().with_curl()
    assert "computecoin-powercap" not in out(box.run("--dry-run"))  # no graphics card
    box.with_nvidia()
    assert "computecoin-powercap" not in out(box.run("--dry-run", "--run-as-root"))  # a root agent applies the cap itself
    (box.root / "amd").mkdir()
    amd = Box(box.root / "amd").with_docker().with_python().with_curl()  # AMD cards are not capped
    amd.kfd.write_text("")
    assert "computecoin-powercap" not in out(amd.run("--dry-run"))


def test_no_start_enables_the_cap_service_but_does_not_start_it(ready):
    text = out(ready.run("--dry-run", "--no-start", "--token", TOKEN, "--server", SERVER))
    assert "systemctl enable computecoin-powercap" in text and "systemctl restart computecoin-powercap" not in text


def test_the_cap_service_is_written_enabled_and_started_before_the_agent(box):
    box.record("systemctl")
    code = SERVICES.replace("@N@", "0").replace("@C@", "1").replace("RUN_AS_ROOT=0;", "RUN_AS_ROOT=0; GPU=nvidia;")
    done = box.source(code)
    assert done.returncode == 0, out(done)
    assert (box.tmp / "units" / "computecoin-powercap.service").exists()
    assert box.calls().splitlines() == [
        "systemctl daemon-reload", "systemctl enable computecoin-powercap", "systemctl restart computecoin-powercap",
        "systemctl enable computecoin-watchdog computecoin-agent", "systemctl restart computecoin-watchdog computecoin-agent"]


def test_an_old_cap_service_is_removed_when_it_is_no_longer_needed(box):
    box.record("systemctl")
    units = box.tmp / "units"
    units.mkdir()
    (units / "computecoin-powercap.service").write_text("")
    box.source(SERVICES.replace("@N@", "0").replace("@C@", "1"))  # GPU=none now
    assert not (units / "computecoin-powercap.service").exists()
    assert "systemctl disable --now computecoin-powercap" in box.calls()


def test_uninstall_stops_the_cap_service_last_so_the_old_limit_is_put_back_before_the_program_goes(ready):
    for unit in ("computecoin-agent", "computecoin-watchdog", "computecoin-powercap"):
        (ready.units / f"{unit}.service").write_text("")
    ready.own_install_dir()
    text = out(ready.run("--uninstall", "--dry-run", "--force"))
    order = [text.index(f"systemctl disable --now {u}") for u in ("computecoin-agent", "computecoin-watchdog", "computecoin-powercap")]
    assert order == sorted(order)  # the agent first, the cap service last
    assert order[-1] < text.index(f"rm -rf {ready.install_dir}")  # and all of it before the program folder is removed
    assert "rm -f /var/lib/computecoin-powercap/state.json" in text and "rmdir /var/lib/computecoin-powercap" in text


def test_uninstall_names_only_the_units_that_exist(ready):
    (ready.units / "computecoin-agent.service").write_text("")  # an AMD or CPU computer never had the cap service
    text = out(ready.run("--uninstall", "--dry-run", "--force"))
    assert "systemctl disable --now computecoin-agent" in text
    assert "disable --now computecoin-powercap" not in text and "disable --now computecoin-watchdog" not in text


def test_the_agents_data_folder_is_made_by_the_agents_own_account_before_any_root_service_runs(box):
    box.record("useradd", "usermod", "chown")
    box.source("DRY_RUN=0; RUN_AS_ROOT=0; setup_user")
    assert (box.data_home / ".computecoin").is_dir()
    assert "chown -R computecoin:computecoin" in box.calls()


def test_a_cap_service_that_did_not_start_is_reported_and_is_not_fatal(box):
    _write_exec(box.bin / "sleep", "exit 0")
    _write_exec(box.bin / "systemctl", '''case "$*" in *powercap*) exit 3;; *) exit 0;; esac''')
    done = box.source("DRY_RUN=0; NO_START=0; ALREADY_CONNECTED=1; GPU=nvidia; RUN_AS_ROOT=0; verify_running")
    assert done.returncode == 0 and "power cap service did not start" in done.stderr and "works without it" in done.stderr


# ---- review fixes: the program folder must never belong to the agent's account ----------------------------------------------
def _stat_says_owned_by(box, owned_paths):
    """A fake `stat -c %U` that reports the agent's account as the owner of the given folders, and the real stat for the rest."""
    real = shutil.which("stat")
    conds = " ".join(f'[ "$3" = "{p}" ] && echo computecoin && exit 0;' for p in owned_paths)
    _write_exec(box.bin / "stat", f'''{conds}\nexec {real} "$@"''')


def test_a_program_folder_inside_the_agents_data_folder_is_refused(ready):
    inside = ready.data_home / "program"
    done = ready.run("--dry-run", COMPUTECOIN_INSTALL_DIR=str(inside))
    assert done.returncode == 1 and "cannot be inside the agent's data folder" in out(done)
    outside_in = ready.run("--dry-run", COMPUTECOIN_DATA_HOME=str(ready.install_dir / "data"))
    assert outside_in.returncode == 1 and "cannot be inside the program folder" in out(outside_in)


def test_a_program_folder_owned_by_the_agents_account_is_refused(ready):
    ready.install_dir.mkdir(parents=True)
    _stat_says_owned_by(ready, [str(ready.install_dir)])
    done = ready.run("--dry-run")
    assert done.returncode == 1 and "belongs to the agent's own account" in out(done)


def test_a_folder_above_the_program_folder_owned_by_the_agents_account_is_refused_too(ready):
    ready.install_dir.parent.mkdir(parents=True)
    _stat_says_owned_by(ready, [str(ready.install_dir.parent)])
    done = ready.run("--dry-run")
    assert done.returncode == 1 and "belongs to the agent's own account" in out(done)


def test_with_a_root_agent_that_ownership_rule_does_not_apply(ready):
    ready.install_dir.mkdir(parents=True)
    _stat_says_owned_by(ready, [str(ready.install_dir)])
    assert ready.run("--dry-run", "--run-as-root").returncode == 0


def test_the_agents_own_data_folder_is_accepted_even_when_that_account_owns_it(ready):
    ready.data_home.mkdir(parents=True)
    (ready.data_home / ".bashrc").write_text("")  # what useradd --create-home leaves
    _stat_says_owned_by(ready, [str(ready.data_home)])
    assert ready.run("--dry-run").returncode == 0


def test_the_program_folder_is_handed_to_root_and_the_agent_account_only_gets_to_read_it(ready):
    done = _build(ready, "build_environment")
    assert done.returncode == 0
    assert f"chown -R root:root {ready.install_dir}" in ready.calls()


# ---- review fixes: the token prompt ---------------------------------------------------------------------------------------------
def test_the_installer_traps_hangups_and_quits_as_well_as_ctrl_c(box):
    text = SCRIPT.read_text()
    for sig in ("INT", "TERM", "HUP", "QUIT"):
        assert f"; exit" in text and f"' {sig}" in text, sig


def _on_a_terminal(box, code, answers, **extra):
    """Run the installer's functions with a REAL pseudo-terminal as standard input (as `sudo sh install.sh` has), typing the given
    answers when the prompt appears. Returns (script output, everything the terminal showed, whether typing was shown again)."""
    import pty
    import select
    import termios
    import time

    master, slave = pty.openpty()
    env = box.env(COMPUTECOIN_INSTALLER_SOURCED="1", **extra)
    script = f'. "{SCRIPT}"\nexec 4<&0\n{code}'
    proc = subprocess.Popen([str(box.sh), "-c", script], stdin=slave, stdout=subprocess.PIPE, stderr=slave, env=env)
    shown = b""

    def wait_for_prompts(count: int, limit: float = 15.0):
        """Read what the terminal shows until the prompt has appeared `count` times (or the installer has ended)."""
        nonlocal shown
        end = time.time() + limit
        while shown.count(b"press Enter: ") < count and time.time() < end:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    shown += os.read(master, 4096)
                except OSError:
                    break
            if proc.poll() is not None and not select.select([master], [], [], 0.1)[0]:
                break

    for number, answer in enumerate(answers, start=1):
        wait_for_prompts(number)
        assert shown.count(b"press Enter: ") >= number, shown
        time.sleep(0.2)  # let the installer switch typing off before the answer is typed
        os.write(master, answer.encode() + b"\n")
        time.sleep(0.3)
    try:
        stdout, _ = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    while select.select([master], [], [], 0.2)[0]:
        try:
            chunk = os.read(master, 4096)
        except OSError:
            break
        if not chunk:
            break
        shown += chunk
    echo_back_on = bool(termios.tcgetattr(slave)[3] & termios.ECHO)
    os.close(master)
    os.close(slave)
    return stdout.decode(), shown.decode(errors="replace"), echo_back_on, proc.returncode


def test_on_a_real_terminal_the_token_is_hidden_while_typed_and_typing_comes_back(ready):
    stdout, shown, echo_back_on, rc = _on_a_terminal(ready, ASK, [TOKEN])
    assert rc == 0 and "TOKEN_SET=yes" in stdout
    assert "Paste your one-time setup token" in shown
    assert TOKEN not in shown and TOKEN not in stdout  # the terminal did not echo it, and the installer did not print it
    assert echo_back_on  # and typing is visible again afterwards


def test_on_a_real_terminal_a_wrong_entry_is_asked_again(ready):
    stdout, shown, echo_back_on, rc = _on_a_terminal(ready, ASK, ["not a token", TOKEN])
    assert rc == 0 and "TOKEN_SET=yes" in stdout and "does not look like a setup token" in shown
    assert TOKEN not in shown and echo_back_on


def test_run_from_a_saved_file_it_uses_the_real_terminal_and_not_the_device_fallback(ready):
    # the device fallback is pointed at a file that does not exist: a success proves the terminal on standard input was used
    stdout, shown, echo_back_on, rc = _on_a_terminal(ready, ASK, [TOKEN], COMPUTECOIN_TTY=str(ready.root / "no-such-tty"))
    assert rc == 0 and "TOKEN_SET=yes" in stdout


def test_when_run_through_a_pipe_the_hint_says_what_to_do_if_nothing_is_accepted(ready):
    tty = _tty(ready, TOKEN + "\n")
    ready.source(ASK, COMPUTECOIN_TTY=str(tty))
    assert "press Ctrl+C and run the installer again with --token" in tty.read_text()


def test_a_saved_connection_the_server_rejects_is_caught_before_anything_is_changed(ready):
    _credential(ready)
    agent = ready.install_dir / "venv" / "bin" / "computecoin-agent"
    agent.parent.mkdir(parents=True)
    agent.write_text("#!/bin/sh\n")
    agent.chmod(0o755)
    code = 'DRY_RUN=0; REENROLL=0; TOKEN=""; SERVER="https://x.com"\nas_service_user() { return 1; }\ncollect_token'
    done = ready.source(code, COMPUTECOIN_TTY=str(ready.root / "no-such-tty"))
    assert done.returncode == 1 and "no longer accepts" in out(done) and "Nothing was changed" in out(done) and "--reenroll" in out(done)
    fine = ready.source(code.replace("return 1", "return 0"), COMPUTECOIN_TTY=str(ready.root / "no-such-tty"))
    assert fine.returncode == 0
    agent.unlink()  # no installed agent to ask: nothing can be checked early, so it goes on
    assert ready.source(code, COMPUTECOIN_TTY=str(ready.root / "no-such-tty")).returncode == 0


# ---- review fixes: the cap service ---------------------------------------------------------------------------------------------
def test_the_cap_service_is_retried_when_the_driver_is_not_ready_and_waits_for_the_persistence_daemon(ready):
    text = out(ready.run("--dry-run", "--token", TOKEN, "--server", SERVER))
    unit = text.split("computecoin-powercap.service (mode 644):")[1].split("would write")[0]
    assert "Restart=on-failure" in unit and "RestartSec=30" in unit
    assert "After=network-online.target nvidia-persistenced.service" in unit


def test_a_card_without_persistence_mode_is_warned_about(ready):
    _write_exec(ready.bin / "nvidia-smi", '''case "$*" in *persistence_mode*) echo Disabled;; *) echo "GPU 0: Fake (UUID: x)";; esac''')
    assert "persistence mode is off" in out(ready.run("--dry-run"))
    _write_exec(ready.bin / "nvidia-smi", '''case "$*" in *persistence_mode*) echo Enabled;; *) echo "GPU 0: Fake (UUID: x)";; esac''')
    assert "persistence mode" not in out(ready.run("--dry-run"))


# ---- review fixes: the private Python and the AMD add-on ----------------------------------------------------------------------------
def test_pip_is_told_to_use_only_ready_made_packages_and_no_dependencies_for_the_amd_add_on(ready):
    done = _build(ready, "build_environment", gpu="amd")
    line = next(line for line in done.stdout.splitlines() if "amdsmi" in line)
    assert "--no-deps" in line and "--only-binary :all:" in line
