"""Rental template entrypoint: JupyterLab for a container-rental session (docs/architecture/RENTAL-container-rental-design.md).

Session contract (provider-agent/computecoin_agent/session_agent.py): the agent starts the image detached with the image's own
ENTRYPOINT, no published port and no network (unless the rental asked for allowed hosts), then attaches with
`docker exec -i <container> nc 127.0.0.1 8888` (session mode tcp, port 8888). So:
  * Jupyter binds 127.0.0.1:8888 only; `nc` must exist in the image (nc_shim.py is installed as /usr/local/bin/nc);
  * the token is disabled on purpose: the only way in is the end-to-end encrypted session channel the customer opens;
  * the agent passes HOME=/workspace and mounts the customer's workspace there (it survives while the rental runs);
    results the customer wants to keep go in /workspace/results (the agent saves them to the customer's storage on request and at the end);
  * COMPUTECOIN_SESSION_BASE_PATH (set by the agent) is the URL prefix the browser serves the app under: Jupyter must know it.
The image runs as an unprivileged user with a read-only root file system, so every directory Jupyter and the libraries write to is pointed
into /workspace.
"""

from __future__ import annotations

import os
import re

WORKSPACE = "/workspace"
PORT = 8888
HOST = "127.0.0.1"
_BASE_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]{0,200}/$")


def base_url(env: dict[str, str]) -> str:
    """The URL prefix for Jupyter: the agent's value when it is a plain path ending in a slash, else '/'."""
    value = env.get("COMPUTECOIN_SESSION_BASE_PATH", "")
    return value if _BASE_PATH_RE.fullmatch(value) and ".." not in value.split("/") else "/"


def build_command(env: dict[str, str]) -> list[str]:
    return [
        "jupyter", "lab", f"--ip={HOST}", f"--port={PORT}", "--no-browser", f"--ServerApp.root_dir={WORKSPACE}",
        f"--ServerApp.base_url={base_url(env)}",
        "--IdentityProvider.token=", "--PasswordIdentityProvider.hashed_password=", "--ServerApp.password=",
        "--ServerApp.disable_check_xsrf=False", "--ServerApp.allow_root=False",
    ]


def child_env(env: dict[str, str]) -> dict[str, str]:
    out = dict(env)
    out.update({
        "HOME": WORKSPACE,
        "JUPYTER_CONFIG_DIR": f"{WORKSPACE}/.jupyter/config",
        "JUPYTER_DATA_DIR": f"{WORKSPACE}/.jupyter/data",
        "JUPYTER_RUNTIME_DIR": f"{WORKSPACE}/.jupyter/runtime",
        "IPYTHONDIR": f"{WORKSPACE}/.ipython",
        "MPLCONFIGDIR": f"{WORKSPACE}/.config/matplotlib",
        "XDG_CACHE_HOME": f"{WORKSPACE}/.cache",
        "TORCH_HOME": f"{WORKSPACE}/.cache/torch",
        "HF_HOME": f"{WORKSPACE}/.cache/huggingface",
        "PIP_CACHE_DIR": f"{WORKSPACE}/.cache/pip",
        "PYTHONUSERBASE": f"{WORKSPACE}/.local",
        "TMPDIR": f"{WORKSPACE}/.tmp",
        "PYTHONUNBUFFERED": "1",
    })
    out["PATH"] = f"{WORKSPACE}/.local/bin:" + out.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    return out


def main() -> None:
    env = child_env(dict(os.environ))
    for d in (WORKSPACE, f"{WORKSPACE}/results", env["JUPYTER_CONFIG_DIR"], env["JUPYTER_DATA_DIR"], env["JUPYTER_RUNTIME_DIR"],
              env["IPYTHONDIR"], env["MPLCONFIGDIR"], env["XDG_CACHE_HOME"], env["TMPDIR"]):
        os.makedirs(d, mode=0o700 if d != WORKSPACE and d != f"{WORKSPACE}/results" else 0o755, exist_ok=True)
    cmd = build_command(env)
    os.execvpe(cmd[0], cmd, env)


if __name__ == "__main__":
    main()
