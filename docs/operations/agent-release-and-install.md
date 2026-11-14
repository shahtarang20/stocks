# Releasing the agent and the one-line host install

Hosts install the agent with one line from the setup page (`/provider/onboarding`, step 3c):

```
f=$(mktemp) && curl -fsSL https://<your-site>/install.sh -o "$f" && sudo sh "$f" --server https://<api>; rm -f "$f"
```

(The script is saved and then run, not piped, so the installer keeps the host's real terminal for the token question under every version of
`sudo`. With a plain `curl ... | sudo sh -s -- ...` the question is asked on the terminal device instead; on a newer `sudo` with `use_pty` that
can hang, and the installer prints a hint to press Ctrl+C and use `--token`.)

The line has no token in it: the installer asks for the one-time token at a prompt (typing hidden), so the token is never on a command
line that other users of the computer could read with `ps`. For scripts that cannot answer a prompt, the page also offers a form with
`--token enr_...` inside (the token is then visible to other users of that computer while the install runs).

The script (`apps/web/public/install.sh`) is served by the web app. It downloads the agent package from a release page you
publish with the **Agent release** workflow (`.github/workflows/agent-release.yml`).

## Release a new agent version

1. Change `version` in `provider-agent/pyproject.toml` and commit it to `main`.
2. Tag and push: `git tag agent-v<version>` then `git push origin agent-v<version>` (the version must equal the one in step 1; the job refuses otherwise).
3. The job runs the agent tests, builds one wheel and a **hash-locked list of every dependency** (`requirements.lock`, built with
   `uv pip compile --universal --generate-hashes`, covering x86 and ARM and Python 3.12 and newer), installs both into a clean environment the way the
   installer does (every hash checked) and runs `computecoin-agent --help`, then publishes:
   - a release named after the tag, with the wheel, `requirements.lock` and a `LATEST` file (two lines: `<wheel file name> <sha256>` and `requirements.lock <sha256>`);
   - the same files on the fixed release `agent-latest`, replacing the previous ones. The installer reads `agent-latest/LATEST`.
4. Hosts who run the line again get the new version (it upgrades and restarts the services). Hosts already running keep getting signed updates through the existing agent update system.

## Where the installer downloads from

Default: `https://github.com/shahtarang20/stocks/releases/download/agent-latest` (set in `install.sh` as `DEFAULT_PACKAGE_BASE`).

**This only works if the repository is public.** If it is private, hosts cannot download release files without a login. Then:
publish the two files (`LATEST` and the wheel) at any address hosts can reach, and set `NEXT_PUBLIC_AGENT_PACKAGE_BASE` on the web app to that
address (the setup page then adds `--package-base <address>` to the line). The agent code is licensed Apache-2.0; making it
downloadable is a business decision for you.

## What the installer does and does not do

Does, in this order: checks Linux, systemd, Docker, the graphics card, and that no customer job is running; **asks for the setup token**
(before downloading anything, so a mistake costs nothing); **downloads the agent package and the dependency list and checks their SHA-256
before changing anything**; installs the agent in `/opt/computecoin` in its own Python environment
(a private Python 3.12 is fetched if the computer has no usable one: `uv` at an exact version, checked against a SHA-256 that is **built into
the installer script**, not against a file next to the download; a Python under someone's home folder is never used for the agent's account); connects the computer with the one-time
token (once); installs and starts systemd services that restart after a crash or reboot: `computecoin-agent`, `computecoin-watchdog`
and, on an NVIDIA computer, `computecoin-powercap`.

**Dependencies are installed from the locked list with `--require-hashes --no-deps`**: every package at an exact version, and a package whose
hash does not match is refused (checked here: a list with wrong hashes installs nothing). The agent itself is then installed from the
checked wheel. The one exception is `amdsmi` on an AMD computer (it ships with ROCm and cannot be hash-locked); the installer says so.

**Power cap without a root agent.** The agent and watchdog run as the `computecoin` account. The graphics-card power cap needs root, so a
separate tiny service, `computecoin-powercap` (a one-shot service that runs `computecoin-agent power-cap apply` and, when stopped,
`power-cap restore`), is the only thing with root. It keeps its record of what it changed in `/var/lib/computecoin-powercap/`, which the agent's
account cannot write. It reads **only the percentage** from the agent's settings file (`gpu_power_limit_percent`, 90 by default, bounded 50 to
100; 100 turns it off): because that file belongs to the unprivileged account, an exact `gpu_power_limit_w` from it is ignored by this service
(an exact limit needs `--run-as-root`). If the driver is not ready at boot the service fails and systemd retries every 30 seconds; a card that
says no is final. The installer warns when NVIDIA persistence mode is off, because the cap can then be forgotten when no program uses the card
(enable the `nvidia-persistenced` service). Uninstalling stops the service first, which puts the old limit back. It is not installed with `--run-as-root` (that agent applies the cap itself) or on AMD
(AMD cards are not capped).

A saved connection the server no longer accepts is detected **before** anything is stopped or replaced (it tells the host to use `--reenroll`).

Safe to run again: it upgrades. The old environment is kept until the new one is installed, and put back (and restarted) if the
upgrade fails. If a customer job is running it stops and says so; `--force` goes ahead (the job is stopped, the customer is not charged).
`--dry-run` shows every step without changing anything. `--uninstall` removes the services and program folder and keeps the connection key.

Does not: install Docker or graphics drivers, change the firewall, or touch other users' files. It only ever fills or deletes a folder
that is empty or that it made itself (a marker file says so), and refuses system folders such as `/opt`, `/usr` or `/var/lib`.
**The program folder must belong to root** and may not be inside the agent's data folder, nor owned (itself or any folder above it) by the agent's
account: otherwise the agent could replace programs that run with more rights than it has (the power cap service runs as root).

Options worth knowing: `--gpu-vendor nvidia|amd` (a computer with both, for example an AMD processor's built-in graphics plus an NVIDIA
card), `--reenroll` (connect again with a new token after the computer was removed on the website; the old connection is kept aside),
`--run-as-root` (lets the agent apply the graphics-card power cap), `--no-start`, `--package-base URL`.

By default the services run as a dedicated `computecoin` account (member of the `docker` group, and `render`/`video` when they exist).
Docker access is access to the computer, so treat that account as trusted.

## Limits to know about

- The SHA-256 protects against a corrupted or swapped package file, not against someone who controls the release page (the checksum
  comes from the same place). The signed update system covers later updates. Signing the first install is a later step.
  Download addresses must be https (plain http only for localhost).
- The dependency lock is built when you release. It pins exactly what was on PyPI at that moment; it does not check that those packages are
  safe, only that a host gets the same bytes the release job tested.
- The normal line has no token on it, so it is not visible in `ps`. The prompt needs a terminal; with none (for example over `ssh host 'cmd'`
  without `-t`) the installer says to use `--token`, which does put the token on the command line while the install runs. The token is
  one-time and expires in about an hour, and the connect step hands it to the agent in a private file that is deleted straight after.
- The agent service restarts every 15 seconds if it keeps failing (for example a rejected connection), so a broken setup shows in
  `journalctl -u computecoin-agent` rather than stopping silently.
- Tested here only with a fake computer (fake `docker`, `curl` and so on), with a real wheel built and run from a scratch folder, and
  by an independent review. It has **not** been run as root on a real Linux machine, the private-Python path and the power cap service have not
  been run for real (the cap logic is tested against a fake NVIDIA library, never a real card), and the release workflow has never run on GitHub.
- Windows is not covered (its installer has never been built).
