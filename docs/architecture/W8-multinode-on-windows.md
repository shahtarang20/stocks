# W8: networking on Windows hosts: egress proxy verification and multi-node feasibility

_Cross-platform plan W8 · Status: **egress proxy: step and spec written, never run; multi-node: default NO-GO** · Every Windows,
WSL2 and Docker networking statement is `TODO(verify-docs)`: none was checked against documentation or tried on a machine (X11)._

## 1. Egress allowlist proxy (B5) inside the distro: unchanged

The proxy image (`workloads/egress-proxy`), the per-job `--internal` network and `DockerExecutor._setup_egress` all run **inside
the distro exactly as on Linux** (X1). Windows adds two things (`windows/runtime/network.py`):

1. A provisioning step that pulls the manifest's digest-pinned proxy image and writes `COMPUTECOIN_EGRESS_PROXY_IMAGE=<image>` to
   `/etc/computecoin/runtime.env` (the runner's `egress_proxy_image` setting reads the `COMPUTECOIN_*` environment).
2. `verify_egress_proxy`, run at the end of provisioning and by the isolation tests.

### Verification spec (hardware item W8.1)

All attempts run from a throw-away probe container on a throw-away `--internal` network next to a proxy started with the same
flags as the executor and the allowlist `allowed.invalid:443`. No internet access is needed.

| Id | Attempt | Expected |
|---|---|---|
| E1 | direct TCP connect to a public IP from the internal network | blocked (no route) |
| E2 | proxy `CONNECT 169.254.169.254:80` | HTTP 403 |
| E3 | proxy `CONNECT not-allowlisted.invalid:443` | HTTP 403 |
| E4 | proxy log after E2 and E3 | at least two lines containing `DENIED` |

A failure withholds the `egress_proxy` capability; it does not block enrolment. The opt-in escape test
`test_egress_proxy_denies_metadata_and_unlisted_hosts` runs the same spec. Open question (`TODO(verify-docs)`): that WSL2's NAT gives an internal Docker
network no route out; to be proven on hardware.

## 2. Multi-node (U13) on Windows: feasibility

Multi-node needs the rank-0 container reachable at the host's `cluster_addr` (ADR-0003 addendum U13.1) and NCCL traffic between ranks.

### Networking modes

| Mode | How | Works for multi-node? | Problems |
|---|---|---|---|
| **WSL2 NAT (default)** | The distro sits behind a virtual NAT; the Windows host has the LAN address | **No** | Nothing on the LAN can reach the distro; `cluster_addr` is a Windows address, not the container's |
| **Windows port proxy** (`netsh interface portproxy`) | Forward one TCP port on `cluster_addr` to the distro's NAT address | **Rendezvous port only** | NCCL opens other, dynamic ports; the distro address changes at every WSL restart; needs re-run and a Windows firewall rule; `setup-multinode-network.ps1 -Mode portproxy` is only a reachability experiment |
| **Mirrored networking** (`networkingMode=mirrored` in `.wslconfig`) | Distro shares the host's network interfaces | Possibly | The setting is **per user and shared with the owner's other distros** (we never write it); needs a recent Windows 11; the Hyper-V firewall has its own default-deny inbound rule; interaction with Docker bridge networks and with our group network is unknown |
| **Bridged distro** (`networkingMode=bridged`, a Hyper-V external switch) | Distro gets its own LAN address | Possibly | Marked experimental by Microsoft (`TODO(verify-docs)`); the distro is then directly on the owner's LAN, which is the opposite of the Job vs LAN boundary (section 4.4 of the plan) |

### Firewall interplay

* `scripts/setup-multinode-net.sh` (nftables or iptables inside the distro) confines the **Docker group network** and drops metadata
  traffic. It does not see traffic on the Windows side.
* Windows Defender Firewall (host) and the Hyper-V firewall (distro) are two more policy layers that would have to allow exactly the rendezvous
  and NCCL ports from the cluster CIDR and nothing else, and be verified.
* The agent's gate is an operator **attestation** (`multinode_network_firewalled`) backed by an in-distro self-check
  (`executor.check_multinode_firewall`). That self-check cannot see the Windows layers, so on Windows the attestation would be
  false assurance. `setup-multinode-network.ps1` therefore never sets it.

### Other blockers

* NCCL data ports are not covered by the single published rendezvous port, so a port proxy cannot carry real training traffic.
* The datacenter-group benchmark responder (`netbench.py`) binds `cluster_addr`; that must be the address the cluster sees.
* Multi-node needs the cluster network to be a closed datacenter segment (U13.1). A PC on a home LAN is not one.

## 3. Recommendation

**Default NO-GO.** `multi_node` stays off for every Windows host (it is in `gpu.FORBIDDEN_ON_WINDOWS` and the capability layer's
forbidden list); a rendezvous job on a Windows host is refused. Revisit only if (a) mirrored networking is proven on a recorded run to
let two Windows hosts run a 2-node NCCL all-reduce through the confined group network, (b) a Windows-side firewall verification
exists that can honestly back the attestation, and (c) the owner approves Windows hosts for datacenter groups.

`windows/setup-multinode-network.ps1` is a template for that experiment: it refuses to run unless `COMPUTECOIN_EXPERIMENTAL_MULTINODE=1`, only
prints what it would do unless `-Apply` is given, never edits `.wslconfig`, never runs `wsl --shutdown`, and accepts RFC 1918 addresses only.

## 4. Decision record

| Field | Value |
|---|---|
| Decision | NO-GO (default) |
| Decided by / date | pending the owner |
| Evidence needed to flip | recorded 2-node NCCL run across Windows hosts; Windows firewall verification; owner approval |
