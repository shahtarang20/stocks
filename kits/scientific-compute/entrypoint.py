"""scientific-compute Kit entrypoint: runs ONE customer-supplied Python script (numpy / scipy / pandas) (container contract: kitrt).

SECURITY: the script is UNTRUSTED CODE. This entrypoint only adds defence in depth (isolated interpreter, clean environment,
rlimits for CPU time, memory, file size and process count, wall-clock kill of the whole process group, output sanitising).
The real boundary is the hardened container the platform runs this image in: --network none, read-only root filesystem,
non-root uid 10001, capabilities dropped, /tmp tmpfs noexec, only /out writable. Never relax any of those for this Kit.

Inputs: `script` (required, one UTF-8 .py file, run as `python -I -B script.py <args>`), `data` (optional zip, safely extracted;
its directory is $DATA_DIR). The script's working directory and $RESULTS_DIR are /out/results; everything it writes there is
collected (symlinks and special files are deleted). stdout and stderr go to /out/logs (tail-capped).
Determinism: with params.deterministic=true the environment pins PYTHONHASHSEED, BLAS/OpenMP threads (1), TZ, locale,
SOURCE_DATE_EPOCH and $KIT_SEED, and logs/determinism.json says so; verification is exact-hash only then (plug-in
exact_deterministic). The Kit cannot prove the script itself is reproducible. A failing script is the customer's error: logs
are kept and the Kit exits non-zero with script_failed, script_timeout or output_too_large.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import zipfile

import kitrt

SCHEMA = os.environ.get("PARAMS_SCHEMA", "/app/params.schema.json")
MAX_SCRIPT_BYTES = 1024 * 1024
MAX_DATA_BYTES = 1024**3
MAX_DATA_UNCOMPRESSED = 2 * 1024**3
MAX_DATA_FILES = 5000
MAX_RESULT_FILES = 10_000
LOG_TAIL_BYTES = 64 * 1024
GRACE_CPU_SECONDS = 5


def safe_zip_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Regular files only: no absolute or '..' names, no symlinks or other special entries, bounded count and size. Pure."""
    members: list[zipfile.ZipInfo] = []
    total = 0
    for info in zf.infolist():
        name = info.filename
        if info.is_dir():
            continue
        if name.startswith("/") or "\\" in name or ".." in name.split("/") or "\x00" in name:
            raise kitrt.KitError("invalid_input", f"unsafe path in data archive: {name[:80]}")
        mode = (info.external_attr >> 16) & 0xFFFF
        # Only an explicit non-regular file type is refused. Archives written by Python's zipfile.writestr (and DOS
        # tools) carry permission bits without a type, and those are plain files.
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            raise kitrt.KitError("invalid_input", f"non-regular entry in data archive: {name[:80]}")
        total += info.file_size
        members.append(info)
        if len(members) > MAX_DATA_FILES or total > MAX_DATA_UNCOMPRESSED:
            raise kitrt.KitError("invalid_input", "data archive too large")
    return members


def build_env(params: dict, work: str, data_dir: str, results_dir: str) -> dict[str, str]:
    """Clean environment for the script (nothing inherited from the container). Pure."""
    det = bool(params["deterministic"])
    threads = "1" if det else str(min(4, os.cpu_count() or 1))
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": work, "TMPDIR": work, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": str(params["seed"]) if det else "random", "PYTHONDONTWRITEBYTECODE": "1",
        "OMP_NUM_THREADS": threads, "OPENBLAS_NUM_THREADS": threads, "MKL_NUM_THREADS": threads,
        "NUMEXPR_NUM_THREADS": threads, "KIT_SEED": str(params["seed"]), "DATA_DIR": data_dir,
        "RESULTS_DIR": results_dir, "MPLCONFIGDIR": work,
    }
    if det:
        env.update({"TZ": "UTC", "SOURCE_DATE_EPOCH": "0"})
    return env


def build_argv(script: str, args: list[str]) -> list[str]:
    """Isolated interpreter (-I ignores PYTHON* variables, user site and the cwd), no bytecode. argv list, never a shell. Pure."""
    return [sys.executable, "-I", "-B", script, *args]


def limits_for(params: dict):
    """preexec function applying rlimits in the child (before exec)."""
    import resource

    mem = params["max_memory_mb"] * 1024 * 1024
    fsize = params["max_output_mb"] * 1024 * 1024
    cpu = params["timeout_seconds"] + GRACE_CPU_SECONDS

    def apply() -> None:
        os.setsid()
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        resource.setrlimit(resource.RLIMIT_NPROC, (512, 512))

    return apply


def tail_file(path: str, limit: int = LOG_TAIL_BYTES) -> None:
    """Keep only the last `limit` bytes of a log file (deterministic for a deterministic script)."""
    size = os.path.getsize(path)
    if size <= limit:
        return
    with open(path, "rb") as fh:
        fh.seek(size - limit)
        data = fh.read()
    with open(path, "wb") as fh:
        fh.write(data)


def sanitize_results(root: str, max_bytes: int) -> list[str]:
    """Delete symlinks and special files, enforce the file-count and total-size caps. Returns sorted relative file names."""
    names: list[str] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for d in list(dirnames):
            p = os.path.join(dirpath, d)
            if os.path.islink(p):
                os.unlink(p)
                dirnames.remove(d)
        for f in filenames:
            p = os.path.join(dirpath, f)
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode):
                os.unlink(p)
                continue
            total += st.st_size
            names.append(os.path.relpath(p, root))
            if len(names) > MAX_RESULT_FILES or total > max_bytes:
                raise kitrt.KitError("output_too_large", "script wrote too many or too large result files")
    return sorted(names)


def extract_data(data_zip: str, dest: str) -> None:
    if os.path.getsize(data_zip) > MAX_DATA_BYTES:
        raise kitrt.KitError("invalid_input", "data archive too large")
    try:
        with zipfile.ZipFile(data_zip) as zf:
            for info in safe_zip_members(zf):
                target = os.path.join(dest, info.filename)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
    except zipfile.BadZipFile as exc:
        raise kitrt.KitError("invalid_input", "data is not a valid zip archive") from exc


def main() -> dict:
    params = kitrt.load_params(SCHEMA)
    source = kitrt.input_file("script")
    size = os.path.getsize(source)
    if size == 0 or size > MAX_SCRIPT_BYTES:
        raise kitrt.KitError("invalid_input", "script empty or larger than 1 MiB")
    try:
        with open(source, "r", encoding="utf-8") as fh:
            code = fh.read()
        compile(code, "script.py", "exec")  # syntax check only; nothing is executed in this process
    except (UnicodeDecodeError, SyntaxError, ValueError) as exc:
        raise kitrt.KitError("invalid_input", f"script is not valid UTF-8 Python: {type(exc).__name__}") from exc

    results_dir = os.path.join(kitrt.OUT_DIR, "results")
    logs_dir = os.path.join(kitrt.OUT_DIR, "logs")
    os.makedirs(results_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="sci-")  # /tmp is tmpfs; the script and extracted data are NOT part of the outputs
    data_dir = os.path.join(work, "data")
    os.makedirs(data_dir)
    script_path = os.path.join(work, "script.py")
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(code)
    data_in = kitrt.input_file("data", required=False)
    if data_in:
        extract_data(data_in, data_dir)

    out_path, err_path = os.path.join(logs_dir, "stdout.txt"), os.path.join(logs_dir, "stderr.txt")
    timed_out = False
    with open(out_path, "wb") as so, open(err_path, "wb") as se:
        proc = subprocess.Popen(build_argv(script_path, params["args"]), cwd=results_dir,
                                env=build_env(params, work, data_dir, results_dir), stdin=subprocess.DEVNULL,
                                stdout=so, stderr=se, preexec_fn=limits_for(params), close_fds=True)
        try:
            code_rc = proc.wait(timeout=params["timeout_seconds"])
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # the child called setsid, so its group id is its pid
            except ProcessLookupError:
                pass
            code_rc = proc.wait()
    for p in (out_path, err_path):
        tail_file(p)
    with open(os.path.join(logs_dir, "determinism.json"), "w", encoding="utf-8") as fh:
        json.dump({"deterministic": bool(params["deterministic"]), "seed": params["seed"]}, fh, sort_keys=True)
    shutil.rmtree(work, ignore_errors=True)

    if timed_out:
        raise kitrt.KitError("script_timeout", f"script exceeded {params['timeout_seconds']} s")
    names = sanitize_results(results_dir, params["max_output_mb"] * 1024 * 1024)
    if code_rc != 0:
        raise kitrt.KitError("script_failed", f"script exited with status {code_rc} (see logs/stderr.txt)")
    files = [{"file": n, "sha256": kitrt.sha256_file(os.path.join(results_dir, n))} for n in names]
    return {"kit": "scientific-compute", "deterministic": bool(params["deterministic"]), "exit_code": code_rc,
            "result_files": files}


if __name__ == "__main__":
    kitrt.run_main(main)
