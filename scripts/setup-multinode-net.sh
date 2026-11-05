#!/usr/bin/env bash
# ==============================================================================
# ComputeCoin U13 - create the group network for multi-node jobs AND the host firewall rules that confine it
# (operator step, run as root on each GPU host). Written, never run. VERIFY every docker/nft/iptables flag against the
# docs of the installed versions before use (L4).
#
# Usage:
#   scripts/setup-multinode-net.sh <network-name> --subnet <cidr> --cluster-cidr <cidr> [--driver bridge|overlay|macvlan]
#                                  [--parent <iface>] [--backend nft|iptables]
#   scripts/setup-multinode-net.sh --verify <network-name> --subnet <cidr> --cluster-cidr <cidr>
#   scripts/setup-multinode-net.sh --remove <network-name> --subnet <cidr> --cluster-cidr <cidr>
#
# DESIGN (ADR-0003 addenda U13/U13.1): the group network is a NON-internal user-defined network. Docker ignores
# --publish on --internal networks, and rank 0 must publish its rendezvous port on the host cluster_addr, so the network
# cannot be --internal. Confinement is done by HOST FIREWALL rules instead: forward traffic from the group subnet is
# dropped unless the destination is inside the group subnet or the private cluster range (--cluster-cidr), and traffic
# to the cloud metadata address 169.254.169.254 is always dropped. Every rule carries the marker comment
# "computecoin-multinode:<network-name>" so --verify and the agent self-check can find it.
# The script REFUSES to create the network when the rules cannot be installed, and rolls the network back if the
# post-install verification fails.
#   bridge   : single-host only (testing).
#   overlay  : needs hosts in one docker swarm; created --attachable. Encrypt if the fabric is not trusted (owner).
#   macvlan  : needs --parent <iface> (the private cluster NIC).
# Rules live in the kernel only: persist them (nft: add `include` of the printed ruleset file to /etc/nftables.conf and
# enable nftables.service; iptables: use iptables-persistent / netfilter-persistent save) or they vanish at reboot, and
# restart docker AFTER them (docker may rewrite DOCKER-USER ordering). Put the same <network-name> in agent.toml
# `multinode_network` and set `multinode_network_firewalled = true` ONLY after `--verify` printed OK.
# Whether the driver makes rank 0 reachable at the host cluster_addr is an OWNER DECISION and is NOT verified here.
# ==============================================================================
set -euo pipefail

usage() { sed -n '3,9p' "$0" >&2; exit 2; }
die() { echo "FAIL: $*" >&2; exit 1; }

MODE="create"
case "${1:-}" in
  --verify) MODE="verify"; shift ;;
  --remove) MODE="remove"; shift ;;
esac
NAME="${1:-}"; [[ -n "$NAME" ]] || usage
shift
SUBNET=""; CLUSTER=""; DRIVER="bridge"; PARENT=""; BACKEND=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --subnet) SUBNET="${2:-}"; shift 2 ;;
    --cluster-cidr) CLUSTER="${2:-}"; shift 2 ;;
    --driver) DRIVER="${2:-}"; shift 2 ;;
    --parent) PARENT="${2:-}"; shift 2 ;;
    --backend) BACKEND="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done
[[ "$NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$ ]] || { echo "invalid network name" >&2; exit 2; }
RFC1918='^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)[0-9./]+$'
[[ "$SUBNET" =~ $RFC1918 ]] || { echo "--subnet must be an RFC 1918 CIDR (e.g. 10.20.0.0/24)" >&2; exit 2; }
[[ "$CLUSTER" =~ $RFC1918 ]] || { echo "--cluster-cidr must be the RFC 1918 CIDR of the private cluster NICs" >&2; exit 2; }
MARK="computecoin-multinode:${NAME}"
TABLE="computecoin_mn_${NAME//[^a-zA-Z0-9]/_}"

if [[ -z "$BACKEND" ]]; then
  if command -v nft >/dev/null 2>&1; then BACKEND="nft"
  elif command -v iptables >/dev/null 2>&1; then BACKEND="iptables"
  else die "neither nft nor iptables found; cannot install the firewall rules, refusing"; fi
fi
[[ "$BACKEND" == "nft" || "$BACKEND" == "iptables" ]] || { echo "unsupported --backend" >&2; exit 2; }
[[ "$(id -u)" == "0" || "$MODE" == "verify" ]] || die "must run as root to install firewall rules"

# ---- firewall: nftables (own table, hook forward priority -10 = before docker's chains, drops win over accepts) ----
nft_rules() {
  cat <<EOF
table inet ${TABLE} {
  chain forward {
    type filter hook forward priority -10; policy accept;
    ip saddr ${SUBNET} ip daddr 169.254.169.254 counter drop comment "${MARK}"
    ip saddr ${SUBNET} ip daddr { ${SUBNET}, ${CLUSTER} } counter accept comment "${MARK}"
    ip saddr ${SUBNET} ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 } counter drop comment "${MARK}"
    ip saddr ${SUBNET} counter drop comment "${MARK}"
  }
}
EOF
}
nft_install() {
  nft list table inet "$TABLE" >/dev/null 2>&1 && nft delete table inet "$TABLE"   # idempotent: replace atomically below
  nft_rules | nft -f - || return 1
}
nft_has() { nft list table inet "$TABLE" 2>/dev/null | grep -q "$MARK"; }
nft_remove() { nft list table inet "$TABLE" >/dev/null 2>&1 && nft delete table inet "$TABLE" || true; }

# ---- firewall: iptables DOCKER-USER fallback (evaluated before docker's own forward rules) ----
ipt() { iptables -w "$@"; }
ipt_rule_args() {
  # one rule per line: <action> <extra match args>
  echo "DROP -s ${SUBNET} -d 169.254.169.254"
  echo "ACCEPT -s ${SUBNET} -d ${SUBNET}"
  echo "ACCEPT -s ${SUBNET} -d ${CLUSTER}"
  echo "DROP -s ${SUBNET} -d 10.0.0.0/8"
  echo "DROP -s ${SUBNET} -d 172.16.0.0/12"
  echo "DROP -s ${SUBNET} -d 192.168.0.0/16"
  echo "DROP -s ${SUBNET}"
}
ipt_remove() {
  while read -r act rest; do
    # shellcheck disable=SC2086
    while ipt -C DOCKER-USER $rest -m comment --comment "$MARK" -j "$act" 2>/dev/null; do
      ipt -D DOCKER-USER $rest -m comment --comment "$MARK" -j "$act"
    done
  done < <(ipt_rule_args)
}
ipt_install() {
  ipt -nL DOCKER-USER >/dev/null 2>&1 || ipt -N DOCKER-USER || return 1
  ipt_remove   # idempotent: remove ours, re-insert in order at the top
  local lines=() i=1
  mapfile -t lines < <(ipt_rule_args)
  for l in "${lines[@]}"; do
    read -r act rest <<<"$l"
    # shellcheck disable=SC2086
    ipt -I DOCKER-USER "$i" $rest -m comment --comment "$MARK" -j "$act" || return 1
    i=$((i + 1))
  done
}
ipt_has() {
  local n=0 want=0
  while read -r act rest; do
    want=$((want + 1))
    # shellcheck disable=SC2086
    ipt -C DOCKER-USER $rest -m comment --comment "$MARK" -j "$act" 2>/dev/null && n=$((n + 1))
  done < <(ipt_rule_args)
  [[ "$n" -eq "$want" ]]
}

fw_install() { if [[ "$BACKEND" == nft ]]; then nft_install; else ipt_install; fi; }
fw_has() { if [[ "$BACKEND" == nft ]]; then nft_has; else ipt_has; fi; }
fw_remove() { if [[ "$BACKEND" == nft ]]; then nft_remove; else ipt_remove; fi; }

verify() {
  local ok=1
  if ! docker network inspect "$NAME" >/dev/null 2>&1; then echo "FAIL: network $NAME does not exist"; ok=0
  elif [[ "$(docker network inspect -f '{{.Internal}}' "$NAME")" != "false" ]]; then
    echo "FAIL: network $NAME is internal (--publish would be ignored); recreate it with this script"; ok=0
  fi
  if ! fw_has; then echo "FAIL: firewall rules with marker '$MARK' not found (backend $BACKEND)"; ok=0; fi
  [[ "$ok" == 1 ]] && { echo "OK: network $NAME is non-internal and firewall rules '$MARK' are present"; return 0; }
  return 1
}

case "$MODE" in
  verify) verify; exit $? ;;
  remove)
    fw_remove
    docker network rm "$NAME" 2>/dev/null || true
    echo "removed network $NAME and its firewall rules (persisted copies, if any, must be removed by hand)"; exit 0 ;;
esac

# ---- create ----
ARGS=(network create --driver "$DRIVER" --subnet "$SUBNET")   # deliberately NO --internal
case "$DRIVER" in
  bridge) ;;
  overlay) ARGS+=(--attachable) ;;
  macvlan) [[ -n "$PARENT" ]] || { echo "macvlan needs --parent <iface>" >&2; exit 2; }; ARGS+=(-o "parent=$PARENT") ;;
  *) echo "unsupported driver: $DRIVER" >&2; exit 2 ;;
esac
ARGS+=("$NAME")

if docker network inspect "$NAME" >/dev/null 2>&1; then
  [[ "$(docker network inspect -f '{{.Internal}}' "$NAME")" == "false" ]] \
    || die "network $NAME exists but is internal; remove it (--remove) and re-run"
  CREATED=0
else
  CREATED=1
fi

# Rules FIRST: never leave a routable network without its confinement.
if ! fw_install; then
  die "could not install firewall rules (backend $BACKEND); refusing to create the network"
fi
if [[ "$CREATED" == 1 ]]; then
  docker "${ARGS[@]}" || { fw_remove; die "docker network create failed; rules rolled back"; }
fi
if ! verify; then
  [[ "$CREATED" == 1 ]] && docker network rm "$NAME" >/dev/null 2>&1 || true
  fw_remove
  die "post-install verification failed; rolled back"
fi
echo "PERSIST the rules or they are lost at reboot:"
if [[ "$BACKEND" == nft ]]; then
  echo "  $0 prints them with: nft list table inet ${TABLE} > /etc/nftables.d/${TABLE}.nft ; add an include line to /etc/nftables.conf ; systemctl enable nftables"
else
  echo "  netfilter-persistent save   (or iptables-save > /etc/iptables/rules.v4)"
fi
echo "Then set in ~/.computecoin/agent.toml:  multinode_network = \"$NAME\"   multinode_network_firewalled = true"
