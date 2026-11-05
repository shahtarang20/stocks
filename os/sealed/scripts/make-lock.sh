#!/usr/bin/env bash
# Generate os/sealed/agent-requirements.lock: every runtime dependency of provider-agent (transitive
# included) pinned by version AND sha256 hash, from provider-agent/pyproject.toml.
#
# Needs network (PyPI). Run in CI or on a build host; the result is reviewed and committed, or
# generated fresh in CI when absent (see .github/workflows/sealed-image.yml). Hashes are never
# written by hand. Uses a project-local venv under os/sealed/out/, nothing is installed system-wide.
#
# Environment: PYTHON (default python3), LOCK_OUT (default os/sealed/agent-requirements.lock).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$here/../.." && pwd)"
out="${LOCK_OUT:-$here/agent-requirements.lock}"
venv="$here/out/.lock-venv"
mkdir -p "$here/out"
rm -rf "$venv"
"${PYTHON:-python3}" -m venv "$venv"
"$venv/bin/pip" install --quiet --require-virtualenv -r "$here/requirements-build.txt"
# SOURCE_DATE_EPOCH keeps pip-compile's header deterministic. --no-header drops the command line
# that would embed absolute paths.
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1767225600}"
"$venv/bin/pip-compile" \
  --generate-hashes --allow-unsafe --strip-extras --no-header --quiet \
  --resolver=backtracking \
  --output-file "$out" \
  "$repo/provider-agent/pyproject.toml"
rm -rf "$venv"
grep -q -- '--hash=sha256:' "$out" || { echo "lock has no hashes: refusing" >&2; exit 1; }
echo "wrote $out"
