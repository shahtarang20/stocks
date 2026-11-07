#!/usr/bin/env bash
# Run ONE command inside a bubblewrap sandbox so an end-to-end run cannot touch anything outside this project.
#
#   scripts/e2e_sandbox.sh <command> [args...]              # offline (no network at all)
#   E2E_NET=on scripts/e2e_sandbox.sh pip install ...       # network allowed (only for installs)
#
# What the sandbox guarantees:
#   * the whole file system is READ-ONLY except this repository (and its .tools/ folder);
#   * HOME and every cache (pip, npm, XDG, Playwright, tmp) point inside .tools/, never your real home;
#   * every .env file in the repository is replaced by an EMPTY file, so real credentials cannot be read;
#   * credential folders (ssh, cloud CLIs, GitHub CLI, Docker config, Claude settings) are hidden;
#   * the Docker socket is hidden (/run is empty), so containers cannot be started or touched;
#   * the network is OFF unless E2E_NET=on (then only a "package install" step should use it);
#   * host processes and IPC are not visible; the sandbox dies with its parent.
# Settings come from .tools/e2e.local.env (local, throwaway values only).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$ROOT/.tools"
ENV_FILE="$T/e2e.local.env"
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (see the checklist, section 3.0)"; exit 2; }
command -v bwrap >/dev/null || { echo "bubblewrap (bwrap) is not installed"; exit 2; }

mkdir -p "$T/home" "$T/cache" "$T/npm" "$T/browsers" "$T/tmp"

args=(
  --ro-bind / /
  --dev /dev
  --proc /proc
  --tmpfs /tmp
  --tmpfs /run
  --bind "$ROOT" "$ROOT"
  --unshare-pid --unshare-ipc --unshare-uts
  --die-with-parent --new-session
)

if [ "${E2E_NET:-off}" = "on" ]; then
  args+=(--share-net)
  # /run is hidden (Docker socket); DNS settings live under it on systemd-resolved hosts, so expose just that read-only.
  [ -d /run/systemd/resolve ] && args+=(--ro-bind /run/systemd/resolve /run/systemd/resolve)
else
  args+=(--unshare-net)
fi

REAL_HOME="${HOME:-/home/$(id -un)}"
# Hide credential locations in the real home (directories become empty, files become empty).
for d in .ssh .aws .azure .gnupg .kube .docker .railway .vercel .config/gh .config/gcloud .config/gcloud .config/hub \
         .config/stripe .config/Cloudflare .wrangler .claude .config/claude .mozilla .config/google-chrome .config/chromium; do
  [ -d "$REAL_HOME/$d" ] && args+=(--tmpfs "$REAL_HOME/$d")
done
for f in .netrc .npmrc .pypirc .git-credentials .config/git/credentials; do
  [ -e "$REAL_HOME/$f" ] && args+=(--ro-bind /dev/null "$REAL_HOME/$f")
done

# Blank every .env file in the repository (real credentials live there).
while IFS= read -r envf; do
  args+=(--ro-bind /dev/null "$envf")
done < <(find "$ROOT" -maxdepth 4 \( -name node_modules -o -name .venv -o -name .git -o -name .next -o -name .tools \) -prune -o \
           -type f \( -name ".env" -o -name ".env.*" \) ! -name "*.example" -print 2>/dev/null)

# Environment: an allow-list, nothing inherited by accident (tokens in your shell do not enter).
envargs=(
  --clearenv
  --setenv PATH "$PATH"
  --setenv HOME "$T/home"
  --setenv TMPDIR /tmp
  --setenv XDG_CACHE_HOME "$T/cache"
  --setenv PIP_CACHE_DIR "$T/cache/pip"
  --setenv npm_config_cache "$T/npm"
  --setenv PLAYWRIGHT_BROWSERS_PATH "$T/browsers"
  --setenv PYTHONDONTWRITEBYTECODE 1
  --setenv LANG "${LANG:-C.UTF-8}"
  --setenv TERM "${TERM:-dumb}"
  --setenv E2E_SANDBOX 1
)
while IFS= read -r line; do
  case "$line" in ''|\#*) continue ;; esac
  key="${line%%=*}"; val="${line#*=}"
  envargs+=(--setenv "$key" "$val")
done < "$ENV_FILE"

cd "$ROOT"
exec bwrap "${args[@]}" "${envargs[@]}" --chdir "$PWD" "$@"
