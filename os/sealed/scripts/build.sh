#!/usr/bin/env bash
# Build the Sealed OS image reproducibly and emit out/expected-pcrs.json.
#
# Inputs (environment):
#   SNAPSHOT_DATE      Debian snapshot timestamp, e.g. 20260101T000000Z (required: pins all packages)
#   SOURCE_DATE_EPOCH  fixed build time (required)
#   SEALED_KEYS_DIR    directory with db.key db.crt pcr.key pcr.crt PK.esl KEK.esl db.esl [dbx.esl]
#                      (required; never in the repo, R10). For CI use scripts/gen-test-keys.sh.
#   SIGN_HOOK          optional executable called as `$SIGN_HOOK <input-pe> <output-pe>` instead of
#                      the local db.key, for HSM / remote signing (see scripts/sign-hook.example.sh).
#                      TODO(verify-docs): wired to mkosi's SecureBootSignTool= / external signer
#                      support, check the option name in the pinned mkosi version.
#   MKOSI              mkosi command (default: mkosi); pin its version in CI.
# Never installs anything system-wide: the agent wheelhouse is built in a project-local venv.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$here/../.." && pwd)"
: "${SNAPSHOT_DATE:?set SNAPSHOT_DATE}" "${SOURCE_DATE_EPOCH:?set SOURCE_DATE_EPOCH}" "${SEALED_KEYS_DIR:?set SEALED_KEYS_DIR}"
export SOURCE_DATE_EPOCH
for f in db.key db.crt pcr.key pcr.crt PK.esl KEK.esl db.esl; do
  [ -f "$SEALED_KEYS_DIR/$f" ] || { echo "missing $SEALED_KEYS_DIR/$f" >&2; exit 1; }
done
lock="$here/agent-requirements.lock"
if [ ! -f "$lock" ] && [ "${GENERATE_LOCK_IF_ABSENT:-0}" = "1" ]; then
  # CI only: needs network. A release build commits and reviews the lock instead (scripts/make-lock.sh).
  "$here/scripts/make-lock.sh"
fi
[ -f "$lock" ] || { echo "missing $lock (hash-pinned agent dependencies; generate with scripts/make-lock.sh, or set GENERATE_LOCK_IF_ABSENT=1 in CI)" >&2; exit 1; }

mkdir -p "$here/secureboot" "$here/out"
# The private keys are copied only for the mkosi run (mkosi.conf points at secureboot/): owner-only, and removed on
# every exit path so they never linger in the work tree.
trap 'rm -f "$here/secureboot/db.key" "$here/secureboot/pcr.key"' EXIT
(umask 077; for f in db.key db.crt pcr.key pcr.crt; do cp "$SEALED_KEYS_DIR/$f" "$here/secureboot/$f"; done)

# 1. Agent wheelhouse (deterministic wheel from the repo, dependencies by hash).
work="$here/out/.work"; rm -rf "$work"; mkdir -p "$work/wheels"
python3 -m venv "$work/venv"
"$work/venv/bin/pip" install --quiet build
(cd "$repo/provider-agent" && "$work/venv/bin/python" -m build --wheel --outdir "$work/wheels" .)
"$work/venv/bin/pip" download --quiet --no-deps --require-hashes -r "$lock" -d "$work/wheels"
agent_dir="$here/mkosi.extra/opt/computecoin-agent"
rm -rf "$agent_dir"; mkdir -p "$agent_dir"
cp -r "$work/wheels" "$agent_dir/wheels"
# The agent wheel itself is pinned by hash at build time (reproducible with SOURCE_DATE_EPOCH).
{ cat "$lock"; for w in "$work"/wheels/computecoin_agent-*.whl; do
    echo "computecoin-agent @ file:///opt/computecoin-agent/wheels/$(basename "$w") --hash=sha256:$(sha256sum "$w" | cut -d' ' -f1)"
  done; } > "$agent_dir/requirements.lock"

# 2. Image.
cd "$here"
${MKOSI:-mkosi} --force ${SIGN_HOOK:+--secure-boot-sign-tool="$SIGN_HOOK"} \
  --mirror "https://snapshot.debian.org/archive/debian/${SNAPSHOT_DATE}/" \
  build

# 3. Hash and expected PCRs.
image="$here/out/computecoin-sealed.raw"
sha256sum "$image" | cut -d' ' -f1 > "$here/out/image-hash.txt"
python3 "$here/scripts/predict_pcrs.py" \
  --uki "$here/out/computecoin-sealed.efi" \
  --bootloader "$here/out/systemd-bootx64.efi.signed" \
  --keys-dir "$SEALED_KEYS_DIR" \
  --image-hash "$(cat "$here/out/image-hash.txt")" \
  --out "$here/out/expected-pcrs.json"
echo "image hash: $(cat "$here/out/image-hash.txt")"
