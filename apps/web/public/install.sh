#!/bin/sh
# Computecoin host installer (Linux).
#
#   curl -fsSL https://<your-site>/install.sh | sudo sh -s -- --token enr_xxxxx --server https://<api>
#
# What it does, in order. Nothing outside this list is touched.
#   1. checks the computer (Linux, systemd, Docker, graphics card, no customer job running) and stops with a plain
#      explanation if something is missing
#   2. downloads the agent package and its hash-locked dependency list, and checks their SHA-256 BEFORE changing anything
#   3. installs the agent into its own folder (default /opt/computecoin) in its own Python environment; if the computer has
#      no usable Python 3.12 it fetches a private copy (checked download), it never changes the system Python
#   4. connects this computer to your account with the one-time token (skipped if it is already connected)
#   5. installs and starts systemd services: the agent and its heat watchdog (both restart after a crash or a reboot) and, on an
#      NVIDIA computer, a tiny root-only service that applies the graphics-card power cap, so the agent itself needs no root
#
# It does NOT install Docker or graphics drivers, and does not change firewall, users' files or any other service.
# Safe to run again: it upgrades the agent and restarts the services, and if the upgrade fails the previous version is put back.
# `--dry-run` shows every step without doing any. `--uninstall` removes the services and the program folder (your connection
# key is kept; the message says where). Run `--help` for the options.
#
# Everything is inside functions and `main` runs on the last line, so a download that is cut short runs nothing.

set -eu

DEFAULT_PACKAGE_BASE="https://github.com/shahtarang20/stocks/releases/download/agent-latest"
# The private Python's installer (uv): an exact version, with its SHA-256 built into THIS script, so the download is checked against
# something the website's own copy of the script says, not against a file that sits next to the download.
UV_VERSION="0.13.8"
UV_SHA256_X86_64="5acfd9177cfecf0bf87578f059728215ccc884388988f55f7a65041b57387d91"
UV_SHA256_AARCH64="74f2f5c6024d397481db228db8fc25eed420db192243f88438c118e7e013e585"
SERVICE_USER="computecoin"
AGENT_UNIT="computecoin-agent"
WATCHDOG_UNIT="computecoin-watchdog"
POWERCAP_UNIT="computecoin-powercap"
UNIT_DIR="${COMPUTECOIN_UNIT_DIR:-/etc/systemd/system}"                  # overridable only so the tests can use a scratch folder
SYSTEMD_RUN_DIR="${COMPUTECOIN_SYSTEMD_RUN_DIR:-/run/systemd/system}"  # overridable only so the tests can fake a systemd machine
KFD_PATH="${COMPUTECOIN_KFD_PATH:-/dev/kfd}"                            # AMD compute device; overridable for the same reason
TTY="${COMPUTECOIN_TTY:-/dev/tty}"                                      # where the token prompt reads; overridable for the same reason
MARKER=".computecoin-install"  # a file this installer puts in its folders; upgrade and uninstall refuse folders without it

DRY_RUN=0
UNINSTALL=0
NO_START=0
RUN_AS_ROOT=0
FORCE=0
ALLOW_RENTAL=0
REENROLL=0
GPU_VENDOR=""
TOKEN="${COMPUTECOIN_ENROLL_TOKEN:-}"
SERVER="${COMPUTECOIN_SERVER:-}"
PACKAGE_BASE="${COMPUTECOIN_PACKAGE_BASE:-$DEFAULT_PACKAGE_BASE}"
INSTALL_DIR="${COMPUTECOIN_INSTALL_DIR:-/opt/computecoin}"
DATA_HOME="${COMPUTECOIN_DATA_HOME:-/var/lib/computecoin}"
TMP_DIR=""
PY=""
USE_UV=0
UV_BIN=""
WHEEL=""
GPU="none"
ARCH=""
LOCK_FILE=""
ECHO_OFF=0
STOPPED_SERVICES=0
ALREADY_CONNECTED=0

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'Note: %s\n' "$*" >&2; }
die()  { printf '\nStopped: %s\n' "$*" >&2; exit 1; }

cleanup() {
  # Typing must never be left hidden if the install is interrupted at the token prompt.
  restore_typing
  # The one-time token may have been written to a file for the connect step: it must never outlive the run.
  if [ -n "$DATA_HOME" ] && [ -f "$DATA_HOME/.enroll-token" ]; then rm -f "$DATA_HOME/.enroll-token"; fi
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then rm -rf "$TMP_DIR"; fi
}

usage() {
  cat <<'EOF'
Computecoin host installer (Linux)

  curl -fsSL https://<your-site>/install.sh | sudo sh -s -- --token enr_xxxxx --server https://<api>

Options:
  --token T          the one-time setup token (starts with enr_). Optional: if you leave it out the installer asks for it,
                     with your typing hidden, so it never appears in the list of running programs
  --server URL       the Computecoin server address shown on the setup page (https, or http for localhost)
  --package-base URL where the agent package is published (default: the project's release page)
  --gpu-vendor V     nvidia or amd: use this when the computer has both (an AMD processor graphics chip can look like an AMD card)
  --allow-rental     also let customers rent this computer and run THEIR OWN code on its GPU in a locked container (container rental).
                     Off unless you ask for it. Only for non-sensitive work: a stranger's code runs on this computer. You can turn it off
                     any time with: computecoin-agent rental off
  --reenroll         connect again with a new token, replacing a saved connection that is no longer accepted
  --run-as-root      run the services as root (lets the agent apply the graphics-card power cap)
  --force            go ahead even if a customer job is running now (it is stopped; the customer is not charged)
  --no-start         install but do not start the services
  --dry-run          show every step without changing anything
  --uninstall        remove the services and the program folder (your connection key is kept)
  --help             this text

It checks the computer, installs the agent in its own folder, connects the computer to your account, and installs two
services (the agent and its heat watchdog). It does not install Docker or graphics drivers.
EOF
}

# Run a command, or only show it in a dry run. The token never appears in the output.
mask() { printf '%s' "$*" | sed 's/enr_[A-Za-z0-9_-]*/enr_****/g'; }
run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '+ %s\n' "$(mask "$*")"
  else
    "$@"
  fi
}

# Write stdin to a file (a dry run only says so).
write_file() {
  target="$1"; mode="$2"
  if [ "$DRY_RUN" = 1 ]; then
    printf '+ would write %s (mode %s):\n' "$target" "$mode"
    sed 's/^/    | /'
  else
    (umask 077; cat > "$target")  # private while it is being written, then the mode it is meant to have
    chmod "$mode" "$target"
  fi
}

# Download with retries and time limits, so a stalled connection cannot hang the install.
fetch() {
  curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 900 "$1" -o "$2" </dev/null
}

# ---- values that end up in commands and unit files must be plain --------------------------------------------------
# grep tests line by line, so a value holding a newline could slip a second line past it: such values are refused first.
no_newline() { [ "$(printf '%s' "$1" | wc -l | tr -d ' ')" = "0" ]; }
valid_token() { no_newline "$1" && printf '%s' "$1" | grep -Eq '^enr_[A-Za-z0-9_-]{8,200}$'; }
valid_url() {
  # https anywhere; plain http only for this computer itself (a token must not travel unencrypted).
  no_newline "$1" || return 1
  printf '%s' "$1" | grep -Eq '^https://[A-Za-z0-9._-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$' && return 0
  printf '%s' "$1" | grep -Eq '^http://(localhost|127\.0\.0\.1)(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$'
}
valid_path() {
  no_newline "$1" || return 1
  printf '%s' "$1" | grep -Eq '^/[A-Za-z0-9._/-]{1,200}$' || return 1
  case "$1" in
    / | /bin | /boot | /dev | /etc | /home | /lib | /lib32 | /lib64 | /media | /mnt | /opt | /proc | /root | /run | /sbin \
      | /srv | /sys | /tmp | /usr | /usr/bin | /usr/lib | /usr/local | /usr/sbin | /var | /var/lib | /var/log | /var/tmp \
      | /proc/* | /sys/* | /dev/* | /boot/* | /bin/* | /sbin/* | /lib/* | /lib64/* | /etc/*)
      return 1 ;;
  esac
  return 0
}

# ---- asking for the token without putting it on a command line ------------------------------------------------------
tty_usable() { ( exec 3< "$TTY" ) 2>/dev/null; }

# Where the prompt and the answer go. If this script was started from a saved file (`sudo sh install.sh`), its own standard
# input (kept as file 4) is the terminal: that works under every sudo. If it came through a pipe (`curl | sudo sh`), the answer
# comes from the terminal device instead (file 3).
prompt_out() {
  if [ "$IN" = 4 ]; then printf '%b' "$1" >&2; else { printf '%b' "$1" >> "$TTY"; } 2>/dev/null; fi
}
hide_typing() {
  if [ "$IN" = 4 ]; then
    stty -echo <&4 >/dev/null 2>&1 && ECHO_OFF=1 || true
  else
    stty -echo < "$TTY" >/dev/null 2>&1 && ECHO_OFF=1 || true
  fi
}
restore_typing() {
  [ "$ECHO_OFF" = 1 ] || return 0
  { stty echo <&4; } >/dev/null 2>&1 || stty echo < "$TTY" >/dev/null 2>&1 || true
  ECHO_OFF=0
}

# Read the token from the terminal (not from the pipe this script arrived through), typing hidden. Three tries.
ask_for_token() {
  IN=""
  if { [ -t 4 ]; } 2>/dev/null; then
    IN=4
  elif tty_usable; then
    exec 3< "$TTY"  # one handle for the whole conversation, so each line read continues where the last stopped
    IN=3
    prompt_out '(Reading from the terminal. If nothing you type is accepted, press Ctrl+C and run the installer again with --token.)\n'
  else
    return 1
  fi
  attempts=0
  while [ "$attempts" -lt 3 ]; do
    attempts=$((attempts + 1))
    prompt_out 'Paste your one-time setup token (typing is hidden), then press Enter: ' || { [ "$IN" != 3 ] || exec 3<&-; return 1; }
    hide_typing
    value=""
    IFS= read -r value <&"$IN" || value=""
    if [ "$ECHO_OFF" = 1 ]; then restore_typing; prompt_out '\n' || true; fi
    value="$(printf '%s' "$value" | tr -d ' \t\r')"
    if valid_token "$value"; then
      TOKEN="$value"
      [ "$IN" != 3 ] || exec 3<&-
      return 0
    fi
    prompt_out 'That does not look like a setup token (it starts with enr_). Try again.\n' || true
  done
  [ "$IN" != 3 ] || exec 3<&-
  return 1
}

# A computer that must be connected needs a token: ask for it NOW, before anything is downloaded, so a mistake costs nothing.
collect_token() {
  if has_credential && [ "$REENROLL" = 0 ]; then
    # A saved connection the server rejects is found NOW, before anything is stopped or replaced (needs the installed agent to ask).
    if [ -x "$INSTALL_DIR/venv/bin/computecoin-agent" ] && ! credential_works; then
      die "this computer has a saved connection that the server no longer accepts (the computer may have been removed on the website). Nothing was changed. Create a new setup token, then run again with --reenroll."
    fi
    return 0
  fi
  [ -z "$TOKEN" ] || return 0
  if [ "$DRY_RUN" = 1 ]; then say "+ would ask you to paste the setup token (typing hidden)"; return 0; fi
  [ -n "$SERVER" ] || die "the server address is missing. Copy the whole line from the setup page (it includes --server https://...)."
  step "Your setup token"
  ask_for_token || die "no setup token was entered, and there is no terminal to ask on. Run again with --token enr_... (or set COMPUTECOIN_ENROLL_TOKEN)."
}

# A folder we are about to fill (and later may delete) must be absent, empty, or one this installer made.
dir_ours_or_empty() {
  [ -e "$1" ] || return 0
  [ -d "$1" ] || return 1
  [ -f "$1/$MARKER" ] && return 0
  [ -d "$1/.computecoin" ] && return 0  # an earlier install's data folder
  # the agent account's own home (only ever for the DATA folder: the program folder must never belong to that account)
  [ "${2:-}" = "agent-home" ] && [ "$(stat -c %U "$1" 2>/dev/null || true)" = "$SERVICE_USER" ] && return 0
  [ -z "$(ls -A "$1" 2>/dev/null)" ]
}

# True if this folder, or any folder above it, belongs to the agent's own account. The program folder must not: whoever owns it
# could replace the programs that run with more rights than the agent has.
belongs_to_agent_account() {
  p="$1"
  while [ -n "$p" ] && [ "$p" != "/" ] && [ "$p" != "." ]; do
    if [ -e "$p" ] && [ "$(stat -c %U "$p" 2>/dev/null || true)" = "$SERVICE_USER" ]; then return 0; fi
    p="$(dirname "$p")"
  done
  return 1
}

# Make the program folder ours straight away (the marker), so a run that stops halfway can be repeated or removed.
claim_install_dir() {
  run mkdir -p "$INSTALL_DIR" || return 1
  if [ "$DRY_RUN" = 0 ]; then : > "$INSTALL_DIR/$MARKER" || return 1; fi
  return 0
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --token)         [ $# -ge 2 ] || die "--token needs a value"; TOKEN="$2"; shift 2 ;;
      --server)        [ $# -ge 2 ] || die "--server needs a value"; SERVER="$2"; shift 2 ;;
      --package-base)  [ $# -ge 2 ] || die "--package-base needs a value"; PACKAGE_BASE="$2"; shift 2 ;;
      --gpu-vendor)    [ $# -ge 2 ] || die "--gpu-vendor needs nvidia or amd"; GPU_VENDOR="$2"; shift 2 ;;
      --reenroll)      REENROLL=1; shift ;;
      --allow-rental)  ALLOW_RENTAL=1; shift ;;
      --run-as-root)   RUN_AS_ROOT=1; shift ;;
      --force)         FORCE=1; shift ;;
      --no-start)      NO_START=1; shift ;;
      --dry-run)       DRY_RUN=1; shift ;;
      --uninstall)     UNINSTALL=1; shift ;;
      -h|--help)       usage; exit 0 ;;
      *)               die "unknown option: $1 (try --help)" ;;
    esac
  done
  PACKAGE_BASE="$(printf '%s' "$PACKAGE_BASE" | sed 's:/*$::')"
  SERVER="$(printf '%s' "$SERVER" | sed 's:/*$::')"
  INSTALL_DIR="$(printf '%s' "$INSTALL_DIR" | sed 's:/*$::')"
  DATA_HOME="$(printf '%s' "$DATA_HOME" | sed 's:/*$::')"
  [ -z "$TOKEN" ] || valid_token "$TOKEN" || die "that does not look like a Computecoin setup token (it starts with enr_). Copy it again from the setup page."
  [ -z "$SERVER" ] || valid_url "$SERVER" || die "the server address must look like https://api.example.com (https, or http only for localhost)"
  valid_url "$PACKAGE_BASE" || die "the package address must look like https://example.com/path (https, or http only for localhost)"
  valid_path "$INSTALL_DIR" || die "COMPUTECOIN_INSTALL_DIR must be a plain absolute path to your own folder, such as /opt/computecoin (not a system folder)"
  valid_path "$DATA_HOME" || die "COMPUTECOIN_DATA_HOME must be a plain absolute path to your own folder, such as /var/lib/computecoin (not a system folder)"
  case "$GPU_VENDOR" in "" | nvidia | amd) ;; *) die "--gpu-vendor must be nvidia or amd" ;; esac
}

# ---- checks ---------------------------------------------------------------------------------------------------------
detect_arch() {
  case "$(uname -m)" in
    x86_64|amd64)  ARCH="x86_64" ;;
    aarch64|arm64) ARCH="aarch64" ;;
    *) die "this computer's processor ($(uname -m)) is not supported. Supported: x86_64 and aarch64." ;;
  esac
}

# Customer containers running right now (so an upgrade does not cut a job off without warning).
running_customer_work() {
  docker ps --format '{{.Names}}' 2>/dev/null </dev/null | grep -Ec '^(ccoin-job-|ccoin-sess-|cc-svc-)' || true
}

preflight() {
  step "Checking this computer"
  [ "$(uname -s)" = "Linux" ] || die "this installer is for Linux. (Windows is planned but not available yet.)"
  detect_arch
  if [ "$DRY_RUN" = 0 ] && [ "$(id -u)" != "0" ]; then
    die "run it with sudo:  curl -fsSL <this address> | sudo sh -s -- --token ... --server ..."
  fi
  [ -d "$SYSTEMD_RUN_DIR" ] || die "this computer does not use systemd, which the installer needs to keep the agent running. Install the agent by hand (see the setup page, 'manual install')."
  command -v curl >/dev/null 2>&1 || die "curl is missing. Install it (for example: sudo apt install curl) and run this again."
  command -v sha256sum >/dev/null 2>&1 || die "sha256sum is missing (it is part of coreutils). Install it and run this again."
  command -v docker >/dev/null 2>&1 || die "Docker is not installed. Install Docker Engine 24 or newer (https://docs.docker.com/engine/install/), then run this again."
  docker info >/dev/null 2>&1 </dev/null || die "Docker is installed but not running, or this account may not use it. Start it with: sudo systemctl start docker"

  case "$INSTALL_DIR/" in "$DATA_HOME"/*) die "the program folder ($INSTALL_DIR) cannot be inside the agent's data folder ($DATA_HOME): the agent could then replace the programs that run with more rights than it has. Choose another COMPUTECOIN_INSTALL_DIR." ;; esac
  case "$DATA_HOME/" in "$INSTALL_DIR"/*) die "the agent's data folder ($DATA_HOME) cannot be inside the program folder ($INSTALL_DIR). Choose another COMPUTECOIN_DATA_HOME." ;; esac
  dir_ours_or_empty "$INSTALL_DIR" || die "$INSTALL_DIR already exists and was not made by this installer. Choose another folder with COMPUTECOIN_INSTALL_DIR."
  dir_ours_or_empty "$DATA_HOME" agent-home || die "$DATA_HOME already exists and was not made by this installer. Choose another folder with COMPUTECOIN_DATA_HOME."
  if [ "$RUN_AS_ROOT" = 0 ] && belongs_to_agent_account "$INSTALL_DIR"; then
    die "$INSTALL_DIR (or a folder above it) belongs to the agent's own account ($SERVICE_USER). The agent could replace the programs installed there, which run with more rights than it has. Choose another COMPUTECOIN_INSTALL_DIR, owned by root."
  fi

  GPU="none"
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1 </dev/null; then GPU="nvidia"; fi
  if [ -e "$KFD_PATH" ]; then
    if [ "$GPU" = "nvidia" ]; then
      if [ -z "$GPU_VENDOR" ]; then
        # A gaming laptop with an AMD processor and an NVIDIA card shows both. Container rental and most jobs need the NVIDIA card, so take it
        # and say so; the host can still choose the AMD card with --gpu-vendor amd. (The agent refuses an unchosen mix, so the choice is passed on.)
        GPU_VENDOR="nvidia"
        warn "this computer shows both an NVIDIA and an AMD graphics device (an AMD processor's built-in graphics can look like this). Using the NVIDIA card. If you mean the AMD card instead, run again with --gpu-vendor amd."
      fi
    else
      GPU="amd"
    fi
  fi
  [ -z "$GPU_VENDOR" ] || GPU="$GPU_VENDOR"
  case "$GPU" in
    nvidia) say "Graphics card: NVIDIA" ;;
    amd)    say "Graphics card: AMD (support is new and has not been tried on a real AMD card yet)" ;;
    none)   warn "no graphics card was detected. The agent will install, but this computer can only take CPU-only work, and only after cpu_only_kits_enabled is turned on in its settings." ;;
  esac
  if [ "$GPU" = "nvidia" ] && ! docker info 2>/dev/null </dev/null | grep -qi nvidia; then
    warn "Docker does not list an NVIDIA runtime. Install the NVIDIA Container Toolkit (the setup page explains how), or graphics-card jobs will fail."
  fi
  if [ "$GPU" = "nvidia" ] && [ "$RUN_AS_ROOT" = 0 ] \
    && nvidia-smi --query-gpu=persistence_mode --format=csv,noheader 2>/dev/null </dev/null | grep -qi disabled; then
    warn "NVIDIA persistence mode is off. The power cap can be forgotten when no program is using the card; enable the nvidia-persistenced service to keep it."
  fi
  if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce 2>/dev/null || true)" = "Enforcing" ]; then
    warn "SELinux is enforcing on this computer. If the agent service does not start, check 'journalctl -u $AGENT_UNIT' for denials; the program folder may need an SELinux label."
  fi

  if [ -f "$UNIT_DIR/$AGENT_UNIT.service" ] && [ "$FORCE" = 0 ]; then
    busy="$(running_customer_work)"
    if [ "${busy:-0}" -gt 0 ]; then
      die "a customer job is running on this computer right now, and upgrading would stop it. Run this again later, or add --force to stop it (the customer is not charged and the job re-runs on another computer)."
    fi
  fi
}

# ---- the agent package: downloaded and checked before anything is changed ---------------------------------------------
download_agent() {
  step "Downloading the agent"
  if [ "$DRY_RUN" = 1 ]; then
    run fetch "$PACKAGE_BASE/LATEST" "$TMP_DIR/LATEST"
    WHEEL="computecoin_agent-<version>-py3-none-any.whl"
    LOCK_FILE="$TMP_DIR/requirements.lock"
    say "+ would read the file names and SHA-256 values from LATEST, download $PACKAGE_BASE/$WHEEL and $PACKAGE_BASE/requirements.lock, and check both"
    return 0
  fi
  fetch "$PACKAGE_BASE/LATEST" "$TMP_DIR/LATEST" \
    || die "could not download the agent list from $PACKAGE_BASE/LATEST. Check the internet connection, or ask the Computecoin team whether a release is published. Nothing was changed."
  WHEEL="$(awk 'NR==1 {print $1}' "$TMP_DIR/LATEST")"
  want="$(awk 'NR==1 {print $2}' "$TMP_DIR/LATEST")"
  lock_name="$(awk 'NR==2 {print $1}' "$TMP_DIR/LATEST")"
  lock_want="$(awk 'NR==2 {print $2}' "$TMP_DIR/LATEST")"
  printf '%s' "$WHEEL" | grep -Eq '^computecoin_agent-[0-9A-Za-z.+_-]+-py3-none-any\.whl$' || die "the agent list had an unexpected file name"
  printf '%s' "$want" | grep -Eq '^[0-9a-f]{64}$' || die "the agent list had an unexpected checksum"
  [ "$lock_name" = "requirements.lock" ] || die "this release has no hash-locked dependency list (requirements.lock), so it cannot be installed safely. Ask the Computecoin team for a newer release. Nothing was changed."
  printf '%s' "$lock_want" | grep -Eq '^[0-9a-f]{64}$' || die "the agent list had an unexpected checksum for the dependency list"
  fetch "$PACKAGE_BASE/$WHEEL" "$TMP_DIR/$WHEEL" || die "could not download $WHEEL. Nothing was changed."
  got="$(sha256sum "$TMP_DIR/$WHEEL" | awk '{print $1}')"
  [ "$want" = "$got" ] || die "the downloaded agent does not match its SHA-256, so it was not installed. Nothing was changed."
  fetch "$PACKAGE_BASE/requirements.lock" "$TMP_DIR/requirements.lock" || die "could not download the dependency list. Nothing was changed."
  got="$(sha256sum "$TMP_DIR/requirements.lock" | awk '{print $1}')"
  [ "$lock_want" = "$got" ] || die "the downloaded dependency list does not match its SHA-256, so it was not installed. Nothing was changed."
  LOCK_FILE="$TMP_DIR/requirements.lock"
  say "Downloaded and checked $WHEEL and its dependency list"
}

# ---- Python 3.12 (a private one if the system has none usable) --------------------------------------------------------
python_ok() {
  "$1" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1 </dev/null
}

find_python() {
  for candidate in python3.13 python3.12 python3; do
    path="$(command -v "$candidate" 2>/dev/null || true)"
    [ -n "$path" ] || continue
    if [ "$RUN_AS_ROOT" = 0 ]; then
      # A Python inside someone's home folder cannot be run by the agent's own account.
      case "$path" in /home/* | /root/*) continue ;; esac
    fi
    if python_ok "$path"; then
      PY="$path"
      return 0
    fi
  done
  return 1
}

fetch_uv() {
  asset="uv-${ARCH}-unknown-linux-gnu.tar.gz"
  base="https://github.com/astral-sh/uv/releases/download/$UV_VERSION"
  case "$ARCH" in x86_64) uv_want="$UV_SHA256_X86_64" ;; *) uv_want="$UV_SHA256_AARCH64" ;; esac
  step "Fetching a private Python (uv $UV_VERSION) because this computer has no Python 3.12 the agent can use"
  command -v tar >/dev/null 2>&1 && command -v gzip >/dev/null 2>&1 \
    || die "tar and gzip are needed to unpack the private Python. Install them (for example: sudo apt install tar gzip), or install Python 3.12 yourself, and run this again."
  claim_install_dir || die "could not create $INSTALL_DIR"
  run mkdir -p "$INSTALL_DIR/uv"
  if [ "$DRY_RUN" = 1 ]; then
    run fetch "$base/$asset" "$TMP_DIR/$asset"
    say "+ would check it against the SHA-256 built into this installer, then unpack uv into $INSTALL_DIR/uv"
  else
    fetch "$base/$asset" "$TMP_DIR/$asset" || die "could not download uv from $base"
    got="$(sha256sum "$TMP_DIR/$asset" | awk '{print $1}')"
    [ "$uv_want" = "$got" ] || die "the uv download does not match the checksum built into this installer, so it was not used"
    tar -xzf "$TMP_DIR/$asset" -C "$INSTALL_DIR/uv" --strip-components=1 --no-same-owner || die "could not unpack uv"
  fi
  UV_BIN="$INSTALL_DIR/uv/uv"
  USE_UV=1
}

choose_python() {
  if find_python; then
    say "Using $PY ($("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])'))"
  else
    fetch_uv
  fi
}

pip_install() {
  if [ "$USE_UV" = 1 ]; then
    run "$UV_BIN" pip install --python "$INSTALL_DIR/venv/bin/python" --upgrade "$@"
  else
    run "$INSTALL_DIR/venv/bin/python" -m pip install --quiet --upgrade "$@"
  fi
}

# Every dependency, at an exact version, from a list whose hashes must all match: a swapped or tampered dependency is refused.
pip_install_locked() {
  if [ "$USE_UV" = 1 ]; then
    run "$UV_BIN" pip install --python "$INSTALL_DIR/venv/bin/python" --require-hashes --no-deps -r "$1"
  else
    run "$INSTALL_DIR/venv/bin/python" -m pip install --quiet --require-hashes --no-deps -r "$1"
  fi
}

# ---- replacing the installed agent, with the old one kept until the new one works -------------------------------------
# One unit at a time, and only units that exist: systemctl given a missing name among several can stop early.
stop_units() {
  for u in "$@"; do
    if [ -f "$UNIT_DIR/$u.service" ]; then run systemctl stop "$u" || true; fi
  done
}
disable_units() {
  for u in "$@"; do
    if [ -f "$UNIT_DIR/$u.service" ]; then run systemctl disable --now "$u" || true; fi
  done
}

stop_existing() {
  # A re-run is an upgrade: stop the old services so nothing runs from an environment that is being replaced.
  if [ -f "$UNIT_DIR/$AGENT_UNIT.service" ] || [ -f "$UNIT_DIR/$WATCHDOG_UNIT.service" ]; then
    step "Stopping the services from the earlier install"
    stop_units "$AGENT_UNIT" "$WATCHDOG_UNIT"
    STOPPED_SERVICES=1
  fi
}

build_environment() {
  # Every step ends in `|| return 1`: this runs inside an `if`, where `set -e` does not stop the function by itself.
  step "Installing the agent's own Python environment in $INSTALL_DIR"
  claim_install_dir || return 1
  if [ -f "$INSTALL_DIR/$MARKER" ] || [ "$DRY_RUN" = 1 ]; then
    run rm -rf "$INSTALL_DIR/venv.old" || return 1
    if [ -e "$INSTALL_DIR/venv" ]; then run mv "$INSTALL_DIR/venv" "$INSTALL_DIR/venv.old" || return 1; fi
  fi
  if [ "$USE_UV" = 1 ]; then
    # Keep the private Python inside the program folder (readable by the agent's account), not in root's home.
    export UV_PYTHON_INSTALL_DIR="$INSTALL_DIR/python" UV_CACHE_DIR="$TMP_DIR/uv-cache" UV_PYTHON_PREFERENCE="only-managed"
    run "$UV_BIN" venv --python 3.12 "$INSTALL_DIR/venv" || return 1
  else
    run "$PY" -m venv "$INSTALL_DIR/venv" || return 1
  fi
  step "Installing the agent"
  pip_install_locked "$LOCK_FILE" || return 1                 # the dependencies, every hash checked
  pip_install --no-deps "$TMP_DIR/$WHEEL" || return 1         # the agent itself (its SHA-256 was checked on download)
  if [ "$GPU" = "amd" ]; then
    # amdsmi comes with ROCm and cannot be hash-locked from here: it is installed on its own, and the agent runs without it.
    if ! pip_install --no-deps --only-binary :all: amdsmi; then
      warn "the AMD monitoring add-on could not be installed (it comes with ROCm). The agent will run without it."
    else
      warn "the AMD monitoring add-on was installed without a hash check (it comes with ROCm)."
    fi
  fi
  run chown -R root:root "$INSTALL_DIR" || return 1  # the agent's account may run these programs, never change them
  run chmod 755 "$INSTALL_DIR" || return 1
  run chmod -R a+rX "$INSTALL_DIR" || return 1  # the agent's own account must be able to read and run it
  return 0
}

rollback_environment() {
  warn "the new agent could not be installed. Putting the previous one back."
  if [ -f "$INSTALL_DIR/$MARKER" ]; then
    run rm -rf "$INSTALL_DIR/venv" || true
    if [ -e "$INSTALL_DIR/venv.old" ]; then run mv "$INSTALL_DIR/venv.old" "$INSTALL_DIR/venv" || true; fi
  fi
  if [ "$STOPPED_SERVICES" = 1 ] && [ "$NO_START" = 0 ]; then
    run systemctl start "$WATCHDOG_UNIT" "$AGENT_UNIT" || true
  fi
}

replace_agent() {
  stop_existing
  if ! build_environment; then
    rollback_environment
    die "installing the agent failed (the reason is printed above). The previous version, if there was one, is running again."
  fi
  if [ -f "$INSTALL_DIR/$MARKER" ]; then run rm -rf "$INSTALL_DIR/venv.old"; fi
}

# ---- the account the services run as --------------------------------------------------------------------------------
setup_user() {
  step "Preparing the account the agent runs as"
  if [ "$RUN_AS_ROOT" = 1 ]; then
    say "Running as root (--run-as-root): the power cap can be applied. The agent and the watchdog then have full access to this computer."
    run mkdir -p "$DATA_HOME"
    run chmod 700 "$DATA_HOME"
    [ "$DRY_RUN" = 1 ] || : > "$DATA_HOME/$MARKER"
    return 0
  fi
  if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    run useradd --system --home-dir "$DATA_HOME" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
  run mkdir -p "$DATA_HOME/.computecoin"
  [ "$DRY_RUN" = 1 ] || : > "$DATA_HOME/$MARKER"
  # Everything inside, too: an earlier run as root may have left files the agent's account cannot read.
  run chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_HOME"
  run chmod 700 "$DATA_HOME"
  for group in docker render video; do
    if getent group "$group" >/dev/null 2>&1; then run usermod -aG "$group" "$SERVICE_USER"; fi
  done
  say "Note: access to Docker is access to this computer. The agent account is a normal system account, but treat it as trusted."
}

as_service_user() {
  # HOME decides where the agent keeps its connection key; the graphics-brand choice is passed on when one was given.
  if [ "$RUN_AS_ROOT" = 1 ]; then
    run env "HOME=$DATA_HOME" ${GPU_VENDOR:+"COMPUTECOIN_GPU_VENDOR=$GPU_VENDOR"} "$@"
  elif command -v runuser >/dev/null 2>&1; then
    run runuser -u "$SERVICE_USER" -- env "HOME=$DATA_HOME" ${GPU_VENDOR:+"COMPUTECOIN_GPU_VENDOR=$GPU_VENDOR"} "$@"
  else
    run sudo -u "$SERVICE_USER" env "HOME=$DATA_HOME" ${GPU_VENDOR:+"COMPUTECOIN_GPU_VENDOR=$GPU_VENDOR"} "$@"
  fi
}

check_service_user_can_run() {
  [ "$RUN_AS_ROOT" = 1 ] && return 0
  if [ "$DRY_RUN" = 1 ]; then say "+ would check that the agent's account can run the agent"; return 0; fi
  as_service_user "$INSTALL_DIR/venv/bin/python" -c 'import computecoin_agent' >/dev/null 2>&1 \
    || die "the agent's account ($SERVICE_USER) cannot run the installed agent (a folder in the path may not be readable by it). Run again with --run-as-root, or install Python 3.12 system-wide."
}

# ---- connect to the account -----------------------------------------------------------------------------------------
credential_file() { printf '%s' "$DATA_HOME/.computecoin/agent.toml"; }
has_credential() { [ -f "$(credential_file)" ] && grep -q '^machine_secret' "$(credential_file)" 2>/dev/null; }

# One heartbeat with the saved connection: exit status 1 means the server no longer accepts it.
credential_works() {
  [ "$DRY_RUN" = 1 ] && return 0
  rc=0
  as_service_user "$INSTALL_DIR/venv/bin/computecoin-agent" run --iterations 1 >/dev/null 2>&1 || rc=$?
  [ "$rc" != "1" ]
}

enroll_machine() {
  step "Connecting this computer to your account"
  if has_credential; then
    if [ "$REENROLL" = 1 ]; then
      { [ -n "$TOKEN" ] && [ -n "$SERVER" ]; } || die "--reenroll needs a new token and the server: --token enr_... --server https://..."
      stamp="$(date +%Y%m%d%H%M%S)"
      run mv "$(credential_file)" "$(credential_file).old-$stamp"
      say "The earlier connection was kept as agent.toml.old-$stamp"
    else
      if credential_works; then
        ALREADY_CONNECTED=1
        say "This computer is already connected. Not connecting it a second time."
        [ -z "$TOKEN" ] || warn "the setup token you passed was not used, so it is still valid. Cancel it on the setup page if you do not need it."
        return 0
      fi
      die "this computer has a saved connection that the server no longer accepts (the computer may have been removed on the website). Create a new setup token, then run again with --reenroll --token enr_... --server https://..."
    fi
  fi
  if [ -z "$TOKEN" ] || [ -z "$SERVER" ]; then
    if [ "$DRY_RUN" = 1 ]; then
      say "+ would connect with your token and server (none given in this dry run)"
      return 0
    fi
    die "this computer is not connected yet, and no token and server were given. Run again with: --token enr_... --server https://..."
  fi
  # The token goes to the agent in a private file, not on a command line other users could read with `ps`.
  if [ "$DRY_RUN" = 1 ]; then
    say "+ would save the token in a private file, connect with --token-file, and delete the file"
    run "$INSTALL_DIR/venv/bin/computecoin-agent" enroll --token-file "$DATA_HOME/.enroll-token" --server "$SERVER"
  else
    (umask 077; printf '%s\n' "$TOKEN" > "$DATA_HOME/.enroll-token")
    [ "$RUN_AS_ROOT" = 1 ] || chown "$SERVICE_USER:$SERVICE_USER" "$DATA_HOME/.enroll-token"
    ok=1
    as_service_user "$INSTALL_DIR/venv/bin/computecoin-agent" enroll --token-file "$DATA_HOME/.enroll-token" --server "$SERVER" || ok=0
    rm -f "$DATA_HOME/.enroll-token"
    TOKEN=""
    [ "$ok" = 1 ] || die "connecting to your account failed (the reason is printed above). A setup token works once and expires: create a new one on the setup page and run this again."
  fi
  ALREADY_CONNECTED=1
}

# ---- container rental (opt-in) -----------------------------------------------------------------------------------------
enable_rental() {
  [ "$ALLOW_RENTAL" = 1 ] || return 0
  step "Turning on container rental (you asked for it)"
  say "Customers who rent this computer run their own code on its graphics card inside a locked container: no extra rights, no network unless they"
  say "ask for allowed software sites, nothing outside a private work folder. It is NOT a private environment, and it is for non-sensitive work."
  say "You can turn it off at any time with:  computecoin-agent rental off"
  if ! as_service_user "$INSTALL_DIR/venv/bin/computecoin-agent" rental on; then
    warn "this version of the agent could not turn container rental on (it may be too old to know about it). The computer works as before."
    return 0
  fi
  # Test the computer now, so the host learns about a missing piece here and not from a customer. A failing test never stops the install.
  step "Testing this computer for container rental (the first test downloads a small image and can take a few minutes)"
  if ! as_service_user "$INSTALL_DIR/venv/bin/computecoin-agent" rental check; then
    warn "the container rental test found a problem (the lines marked FAIL above each say what to do). The computer is still connected and works for ordinary jobs."
    say "After you fix it, test again with:  computecoin-agent rental check"
    say "If the only FAIL is the graphics card and this computer has none, that is expected: it can still offer rentals that need no card."
  fi
}

# ---- services --------------------------------------------------------------------------------------------------------
unit_text() {
  # $1 = description, $2 = command line, $3 = seconds to wait before a restart
  user_line=""
  [ "$RUN_AS_ROOT" = 1 ] || user_line="User=$SERVICE_USER"
  vendor_line=""
  [ -z "$GPU_VENDOR" ] || vendor_line="Environment=COMPUTECOIN_GPU_VENDOR=$GPU_VENDOR"
  cat <<EOF
[Unit]
Description=$1
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
Type=simple
$user_line
Environment=HOME=$DATA_HOME
$vendor_line
ExecStart=$2
Restart=always
RestartSec=${3:-5}
TimeoutStopSec=60
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
}

# The one service that runs as root: it only applies the graphics-card power cap and puts the old limit back when stopped.
# Its record of what it changed lives in a root-owned folder the agent's account cannot write to.
powercap_unit_text() {
  cat <<EOF
[Unit]
Description=Computecoin power cap (runs the graphics card a little cooler; puts the old limit back when stopped)
After=network-online.target nvidia-persistenced.service
Before=$AGENT_UNIT.service

[Service]
Type=oneshot
RemainAfterExit=yes
Restart=on-failure
RestartSec=30
Environment=HOME=$DATA_HOME
Environment=COMPUTECOIN_POWER_STATE_FILE=/var/lib/computecoin-powercap/state.json
StateDirectory=computecoin-powercap
ExecStart=$INSTALL_DIR/venv/bin/computecoin-agent power-cap apply
ExecStop=$INSTALL_DIR/venv/bin/computecoin-agent power-cap restore
TimeoutStopSec=30
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
}

# Only an NVIDIA computer whose agent is NOT root needs it (a root agent applies the cap itself; AMD cards are not capped).
needs_powercap_service() { [ "$GPU" = "nvidia" ] && [ "$RUN_AS_ROOT" = 0 ]; }

install_services() {
  step "Installing the services"
  if needs_powercap_service; then
    powercap_unit_text | write_file "$UNIT_DIR/$POWERCAP_UNIT.service" 644
  elif [ -f "$UNIT_DIR/$POWERCAP_UNIT.service" ]; then
    disable_units "$POWERCAP_UNIT"  # an earlier install had it, this one does not need it
    run rm -f "$UNIT_DIR/$POWERCAP_UNIT.service"
  fi
  unit_text "Computecoin agent (takes jobs and keeps this computer online)" "$INSTALL_DIR/venv/bin/computecoin-agent start" 15 \
    | write_file "$UNIT_DIR/$AGENT_UNIT.service" 644
  unit_text "Computecoin heat watchdog (removes customer containers if the graphics card overheats and the agent is gone)" \
    "$INSTALL_DIR/venv/bin/computecoin-agent watchdog" 5 | write_file "$UNIT_DIR/$WATCHDOG_UNIT.service" 644
  run systemctl daemon-reload
  if needs_powercap_service; then
    run systemctl enable "$POWERCAP_UNIT"
    if [ "$NO_START" = 0 ]; then run systemctl restart "$POWERCAP_UNIT"; fi
  fi
  if [ "$NO_START" = 1 ]; then
    run systemctl enable "$WATCHDOG_UNIT" "$AGENT_UNIT"
    say "Not started (--no-start). Start with: sudo systemctl start $WATCHDOG_UNIT $AGENT_UNIT"
  elif [ "$ALREADY_CONNECTED" = 1 ]; then
    run systemctl enable "$WATCHDOG_UNIT" "$AGENT_UNIT"
    run systemctl restart "$WATCHDOG_UNIT" "$AGENT_UNIT"
  else
    run systemctl enable "$WATCHDOG_UNIT" "$AGENT_UNIT"
    warn "not started: this computer is not connected yet."
  fi
}

verify_running() {
  [ "$DRY_RUN" = 0 ] && [ "$NO_START" = 0 ] && [ "$ALREADY_CONNECTED" = 1 ] || return 0
  sleep 3
  if systemctl is-active --quiet "$AGENT_UNIT"; then
    say "The agent is running."
  else
    warn "the agent did not stay running. See why with: journalctl -u $AGENT_UNIT -n 50 --no-pager"
    return 1
  fi
  if ! systemctl is-active --quiet "$WATCHDOG_UNIT"; then
    warn "the heat watchdog is not running. See: journalctl -u $WATCHDOG_UNIT -n 50 --no-pager"
  fi
  if needs_powercap_service && ! systemctl is-active --quiet "$POWERCAP_UNIT"; then
    warn "the power cap service did not start. The agent works without it. See: journalctl -u $POWERCAP_UNIT -n 50 --no-pager"
  fi
}

do_uninstall() {
  step "Removing the Computecoin services and program folder"
  [ "$DRY_RUN" = 1 ] || [ "$(id -u)" = "0" ] || die "run it with sudo."
  if [ -f "$UNIT_DIR/$AGENT_UNIT.service" ] && [ "$FORCE" = 0 ] && command -v docker >/dev/null 2>&1; then
    busy="$(running_customer_work)"
    if [ "${busy:-0}" -gt 0 ]; then
      die "a customer job is running on this computer right now, and removing the agent would stop it. Run this again later, or add --force."
    fi
  fi
  disable_units "$AGENT_UNIT" "$WATCHDOG_UNIT" "$POWERCAP_UNIT"  # the power cap goes last: stopping it puts the old limit back
  run rm -f "$UNIT_DIR/$AGENT_UNIT.service" "$UNIT_DIR/$WATCHDOG_UNIT.service" "$UNIT_DIR/$POWERCAP_UNIT.service"
  run rm -f /var/lib/computecoin-powercap/state.json
  run rmdir /var/lib/computecoin-powercap 2>/dev/null || true
  run systemctl daemon-reload
  # Remove the program folder only if this installer made it (the marker file), so a wrong COMPUTECOIN_INSTALL_DIR
  # can never delete someone else's folder.
  if [ -f "$INSTALL_DIR/$MARKER" ]; then
    run rm -rf "$INSTALL_DIR"
  elif [ -e "$INSTALL_DIR" ]; then
    warn "$INSTALL_DIR was not made by this installer, so it was left alone."
  fi
  say "Removed. Your connection key and settings are kept in $DATA_HOME/.computecoin."
  say "To delete them as well:  sudo rm -rf $DATA_HOME   (and, if you want, sudo userdel $SERVICE_USER)"
  say "Remove the computer from your account on the 'My computers' page too. To connect it again later, use --reenroll with a new token."
}

main() {
  parse_args "$@"
  if ( exec 4<&0 ) 2>/dev/null; then exec 4<&0; fi  # keep the original standard input: it is the terminal when run from a saved file
  exec </dev/null  # nothing below may read the rest of this script from the pipe
  trap cleanup EXIT
  trap 'cleanup; exit 130' INT
  trap 'cleanup; exit 143' TERM
  trap 'cleanup; exit 129' HUP
  trap 'cleanup; exit 131' QUIT
  if [ "$DRY_RUN" = 1 ]; then say "DRY RUN: nothing will be changed."; fi
  if [ "$UNINSTALL" = 1 ]; then do_uninstall; exit 0; fi
  TMP_DIR="$(mktemp -d)"
  preflight
  collect_token
  exec 4<&-  # the terminal is not needed any more
  download_agent
  choose_python
  replace_agent
  setup_user
  check_service_user_can_run
  enroll_machine
  enable_rental
  install_services
  verify_running
  step "Done"
  if [ "$DRY_RUN" = 1 ]; then
    say "That was a dry run. Run again without --dry-run to install."
  elif [ "$ALREADY_CONNECTED" = 1 ]; then
    say "Open 'My computers' on the website: this computer should show as online within a minute."
    say "Logs: journalctl -u $AGENT_UNIT -f      Status: systemctl status $AGENT_UNIT"
  fi
}

# Sourced by the tests (COMPUTECOIN_INSTALLER_SOURCED=1) so each function can be checked on its own.
if [ "${COMPUTECOIN_INSTALLER_SOURCED:-0}" != 1 ]; then
  main "$@"
fi
