#!/usr/bin/env bash
# Example Secure Boot signing hook: SIGN_HOOK=this-script scripts/build.sh
# Called as: sign-hook <unsigned-pe> <signed-pe>. Replace the body with your HSM / remote signer
# (e.g. sbsign with a PKCS#11 engine). It must fail loudly; the build never falls back to unsigned.
set -euo pipefail
in="$1"; out="$2"
: "${SB_PKCS11_KEY_URI:?set SB_PKCS11_KEY_URI}" "${SB_CERT:?set SB_CERT}"
sbsign --engine pkcs11 --key "$SB_PKCS11_KEY_URI" --cert "$SB_CERT" --output "$out" "$in"
