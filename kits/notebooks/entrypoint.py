"""notebooks Kit entrypoint: JupyterLab for a U10 interactive session.

Session contract (provider-agent/computecoin_agent/session_agent.py): the agent starts the image detached with NO command
override (the image ENTRYPOINT must keep running by itself), no published port and `--network none`, then attaches with
`docker exec -i <container> nc 127.0.0.1 8888` (manifest `session.mode` tcp, `tcp_port` 8888). So:
  * Jupyter binds 127.0.0.1:8888 only; `nc` must exist in the image (see nc_shim.py, installed as /usr/local/bin/nc);
  * the token is disabled on purpose: the only way in is the end-to-end encrypted session channel opened by the owner;
  * the workspace is /tmp/workspace on the `/tmp` tmpfs; everything is lost when the session ends (download what you need).
The command is kept identical to kit.json `session.command` (tests/test_kits_notebooks.py checks this).
Jupyter option names: verify against the jupyter-server docs for the pinned version before use (L4).
"""

from __future__ import annotations

import os
import sys

WORKSPACE = "/tmp/workspace"
PORT = 8888
HOST = "127.0.0.1"


def build_command() -> list[str]:
    return [
        "jupyter", "lab", f"--ip={HOST}", f"--port={PORT}", "--no-browser", f"--ServerApp.root_dir={WORKSPACE}",
        "--IdentityProvider.token=", "--PasswordIdentityProvider.hashed_password=", "--ServerApp.password=",
        "--ServerApp.disable_check_xsrf=False", "--ServerApp.allow_root=False",
    ]


def child_env(env: dict[str, str]) -> dict[str, str]:
    out = dict(env)
    out.update({
        "HOME": "/tmp", "JUPYTER_CONFIG_DIR": "/tmp/jupyter/config", "JUPYTER_DATA_DIR": "/tmp/jupyter/data",
        "JUPYTER_RUNTIME_DIR": "/tmp/jupyter/runtime", "IPYTHONDIR": "/tmp/ipython", "MPLCONFIGDIR": "/tmp/mpl",
        "XDG_CACHE_HOME": "/tmp/cache", "HF_HUB_OFFLINE": "1", "PYTHONUNBUFFERED": "1",
    })
    return out


def main() -> None:
    env = child_env(dict(os.environ))
    for d in (WORKSPACE, env["JUPYTER_CONFIG_DIR"], env["JUPYTER_DATA_DIR"], env["JUPYTER_RUNTIME_DIR"], env["IPYTHONDIR"],
              env["MPLCONFIGDIR"], env["XDG_CACHE_HOME"]):
        os.makedirs(d, mode=0o700, exist_ok=True)
    cmd = build_command()
    os.execvpe(cmd[0], cmd, env)


if __name__ == "__main__":
    main()
