#!/usr/bin/env bash
# TEST-ONLY Secure Boot and PCR-signing keys, for CI and the QEMU boot test. Output directory is
# git-ignored (os/sealed/.test-keys). Production keys live in an HSM / offline, never here (R10).
set -euo pipefail
out="${1:?usage: gen-test-keys.sh <dir>}"
mkdir -p "$out"; cd "$out"
for k in PK KEK db pcr; do
  openssl req -x509 -newkey rsa:3072 -nodes -keyout "$([ $k = pcr ] && echo pcr.key || echo $k.key)" \
    -out "$([ $k = pcr ] && echo pcr.crt || echo $k.crt)" -days 3650 -subj "/CN=ComputeCoin TEST $k/" \
    -set_serial 1 -sha256 >/dev/null 2>&1
done
# EFI signature lists (cert-to-efi-sig-list is from efitools).
guid="$(printf '%s' "ComputeCoin-TEST" | sha256sum | cut -c1-32 | sed -E 's/(.{8})(.{4})(.{4})(.{4})(.{12})/\1-\2-\3-\4-\5/')"
for k in PK KEK db; do cert-to-efi-sig-list -g "$guid" "$k.crt" "$k.esl"; done
echo "$guid" > owner.guid
