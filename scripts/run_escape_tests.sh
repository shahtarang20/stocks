#!/usr/bin/env bash
# Run the container escape tests on THIS machine. They start real containers, so read what they do first:
# provider-agent/tests/escape/. Run it on a spare Linux host, not on a computer you cannot afford to disturb.
#
#   scripts/run_escape_tests.sh <image-pinned-by-digest>      e.g. python@sha256:<64 hex>
#
# The image must contain python3. It is pulled by docker as usual. The ordinary container tier is always tested;
# gVisor and Kata are tested too when docker has them registered (this script says which).
set -euo pipefail

image="${1:-${COMPUTECOIN_ESCAPE_IMAGE:-}}"
if [[ -z "$image" || ! "$image" =~ @sha256:[0-9a-f]{64}$ ]]; then
  echo "usage: $0 <image@sha256:digest>   (an image with python3, pinned by digest)" >&2
  exit 2
fi
command -v docker >/dev/null || { echo "docker is not installed" >&2; exit 2; }
docker info >/dev/null 2>&1 || { echo "docker is installed but its service is not reachable (is it running, and may you use it?)" >&2; exit 2; }

runtimes="$(docker info --format '{{range $k, $_ := .Runtimes}}{{$k}} {{end}}' 2>/dev/null || true)"
echo "docker runtimes: ${runtimes:-none listed}"
for need in runsc kata-qemu; do
  case " $runtimes " in
    *" $need "*) echo "  $need: registered, its tier will be tested" ;;
    *) echo "  $need: NOT registered, its tier will be skipped" ;;
  esac
done

cd "$(dirname "$0")/../provider-agent"
export COMPUTECOIN_ESCAPE_TESTS=1 COMPUTECOIN_ESCAPE_IMAGE="$image"
exec python3 -m pytest tests/escape -v -p no:cacheprovider
