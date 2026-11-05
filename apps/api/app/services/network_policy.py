"""Per-job network allowlist validation (B5, L10).

A job may declare ``network_policy = {"egress": ["host:port", ...]}``. Default is no network.
Fail closed: anything that is not a plain public-looking hostname with a port is refused.
Names are only a first filter: the agent's egress proxy must also refuse to connect to private,
loopback, link-local or metadata addresses after DNS resolution (it is what stops DNS rebinding).
"""

from __future__ import annotations

import ipaddress
import re

MAX_EGRESS_ENTRIES = 16
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_BLOCKED_NAMES = {"localhost", "metadata", "metadata.google.internal", "instance-data"}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".lan", ".home", ".corp", ".intranet", ".arpa")


class NetworkPolicyError(ValueError):
    pass


def _is_ip_literal(host: str) -> bool:
    h = host.strip("[]")
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    # Odd numeric forms resolvers accept (2130706433, 0x7f000001, 127.1): any all-numeric/hex label set.
    labels = h.split(".")
    return all(re.fullmatch(r"(0x[0-9a-f]+|[0-9]+)", lab) for lab in labels)


def validate_egress_entry(entry: object) -> str:
    if not isinstance(entry, str) or not entry or len(entry) > 270 or entry != entry.strip():
        raise NetworkPolicyError("egress entries must be 'host:port' strings")
    if "/" in entry or "@" in entry or "*" in entry or "?" in entry or "#" in entry or "[" in entry or "%" in entry:
        raise NetworkPolicyError(f"egress entry {entry!r} must be a plain hostname and port (no scheme, path, wildcard or IPv6)")
    host, sep, port_s = entry.rpartition(":")
    if not sep or not host or not port_s.isascii() or not port_s.isdigit():
        raise NetworkPolicyError(f"egress entry {entry!r} must be 'host:port'")
    port = int(port_s)
    if not 1 <= port <= 65535:
        raise NetworkPolicyError(f"egress entry {entry!r} has an invalid port")
    host = host.lower().rstrip(".")
    if _is_ip_literal(host):
        raise NetworkPolicyError(f"egress entry {entry!r}: IP addresses are not allowed, use a hostname")
    if host in _BLOCKED_NAMES or host.endswith(_BLOCKED_SUFFIXES):
        raise NetworkPolicyError(f"egress entry {entry!r}: local and internal names are not allowed")
    if len(host) > 253:
        raise NetworkPolicyError(f"egress entry {entry!r}: hostname too long")
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL.match(lab) for lab in labels) or labels[-1].isdigit():
        raise NetworkPolicyError(f"egress entry {entry!r} is not a valid public hostname")
    return f"{host}:{port}"


def validate_network_policy(policy: object, *, isolation_tier: str = "container", privacy_tiers: tuple[str, ...] = ("sealed", "sealed_pro", "confidential")) -> dict | None:
    """Return the normalized policy, or None for 'no network'. Raises NetworkPolicyError."""
    if policy is None:
        return None
    if not isinstance(policy, dict) or set(policy) - {"egress"}:
        raise NetworkPolicyError('network_policy must be {"egress": ["host:port", ...]}')
    egress = policy.get("egress")
    if egress is None or egress == []:
        return None
    if not isinstance(egress, list):
        raise NetworkPolicyError("network_policy.egress must be a list")
    if (isolation_tier or "container").lower() in privacy_tiers:
        raise NetworkPolicyError("privacy tiers never get network access")
    if len(egress) > MAX_EGRESS_ENTRIES:
        raise NetworkPolicyError(f"at most {MAX_EGRESS_ENTRIES} egress entries are allowed")
    out = sorted({validate_egress_entry(e) for e in egress})
    return {"egress": out}
