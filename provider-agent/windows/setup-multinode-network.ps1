<#
.SYNOPSIS
  EXPERIMENTAL, OFF BY DEFAULT. Template for exposing ONE rendezvous port of the ComputeCoin runtime distro to a private
  cluster network on a Windows host. Written, never run. See docs/architecture/W8-multinode-on-windows.md (default: NO-GO).

.DESCRIPTION
  Multi-node jobs (U13) need the rank-0 container reachable at the host's cluster address. WSL2's default NAT network hides the
  distro behind the Windows host, so nothing on the LAN can reach it. This template shows the two mechanisms the W8 note
  evaluated:

    portproxy  A Windows port proxy forwards ONE TCP port on the cluster address to the distro's current NAT address, plus a
               Windows Firewall rule that allows only the cluster CIDR. It covers the rendezvous port only. NCCL data
               connections use other, dynamic ports, so this mode is NOT enough for real multi-node training; it exists to
               test reachability of the rendezvous port.
    mirrored   Prints the .wslconfig lines for mirrored networking. It NEVER edits .wslconfig (that file is per user and shared
               with the owner's other distros) and never runs wsl --shutdown. The owner applies it by hand.

  Nothing runs unless BOTH hold: the environment variable COMPUTECOIN_EXPERIMENTAL_MULTINODE=1 and the -Apply switch. Without
  -Apply the script only prints what it would do. The agent's own gate stays closed regardless: multinode_network_firewalled
  is never set by this script, and the multi_node capability is never advertised from a Windows host (X5, W8.2).

  Requires an elevated PowerShell for -Apply and -Remove.

.PARAMETER Mode           portproxy | mirrored
.PARAMETER ClusterAddr    This host's literal private cluster address (RFC 1918), the address the API hands out as master_addr.
.PARAMETER ClusterCidr    RFC 1918 CIDR of the cluster network; the firewall rule allows only this source range.
.PARAMETER RendezvousPort The single rank-0 rendezvous TCP port (1024-65535).
.PARAMETER Apply          Actually make the change (otherwise: print only).
.PARAMETER Verify         Print the current port proxy and firewall rule for this port.
.PARAMETER Remove         Delete the port proxy and firewall rule for this port.

.EXAMPLE
  $env:COMPUTECOIN_EXPERIMENTAL_MULTINODE = '1'
  .\setup-multinode-network.ps1 -Mode portproxy -ClusterAddr 10.20.0.11 -ClusterCidr 10.20.0.0/24 -RendezvousPort 29500 -Apply
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][ValidateSet('portproxy', 'mirrored')][string]$Mode,
  [Parameter(Mandatory = $true)][string]$ClusterAddr,
  [Parameter(Mandatory = $true)][string]$ClusterCidr,
  [Parameter(Mandatory = $true)][ValidateRange(1024, 65535)][int]$RendezvousPort,
  [string]$Distro = 'computecoin',
  [switch]$Apply,
  [switch]$Verify,
  [switch]$Remove
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Fail([string]$Message) { [Console]::Error.WriteLine("setup-multinode-network: $Message"); exit 2 }

if ($env:COMPUTECOIN_EXPERIMENTAL_MULTINODE -ne '1') {
  Fail 'experimental and off by default: set COMPUTECOIN_EXPERIMENTAL_MULTINODE=1 to use it. Read docs/architecture/W8-multinode-on-windows.md first.'
}

# RFC 1918 only, same rule as scripts/setup-multinode-net.sh.
$rfc1918Addr = '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)\d{1,3}(\.\d{1,3}){1,2}$'
$rfc1918Cidr = '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)[0-9.]+/\d{1,2}$'
if ($ClusterAddr -notmatch $rfc1918Addr) { Fail '-ClusterAddr must be a literal RFC 1918 address (for example 10.20.0.11).' }
if ($ClusterCidr -notmatch $rfc1918Cidr) { Fail '-ClusterCidr must be an RFC 1918 CIDR (for example 10.20.0.0/24).' }
if ($Distro -ne 'computecoin') { Fail 'only the managed distro "computecoin" may be used.' }

$ruleName = "computecoin-multinode-$RendezvousPort"

function Show-Or-Run([string]$Description, [scriptblock]$Action) {
  if ($Apply) { Write-Host "APPLY: $Description"; & $Action }
  else { Write-Host "DRY RUN (add -Apply to execute): $Description" }
}

if ($Verify) {
  Write-Host '--- port proxy ---'
  & netsh.exe interface portproxy show v4tov4
  Write-Host '--- firewall rule ---'
  Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Format-List DisplayName, Enabled, Direction, Action
  exit 0
}

if ($Mode -eq 'mirrored') {
  Write-Host 'Mirrored networking is configured in the owner''s %UserProfile%\.wslconfig, which this script never edits.'
  Write-Host 'The owner would add, then run "wsl --shutdown" (this stops ALL their WSL distros):'
  Write-Host '    [wsl2]'
  Write-Host '    networkingMode=mirrored'
  Write-Host 'and allow inbound traffic for the distro with the Hyper-V firewall (TODO(verify-docs): Set-NetFirewallHyperVVMSetting, VM creator id).'
  Write-Host 'TODO(verify-docs): minimum Windows 11 version, behaviour with the NVIDIA GPU, and interaction with Docker bridge networks.'
  exit 0
}

# ---- portproxy mode ----
if ($Remove) {
  Show-Or-Run "delete port proxy $ClusterAddr`:$RendezvousPort and firewall rule $ruleName" {
    & netsh.exe interface portproxy delete v4tov4 listenaddress=$ClusterAddr listenport=$RendezvousPort
    Remove-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
  }
  exit 0
}

# The distro's NAT address changes at every WSL restart: this must be re-run after one (TODO(verify-docs): hostname -I output).
$distroIp = (& wsl.exe --distribution $Distro --user root --exec hostname -I) -split '\s+' | Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' } | Select-Object -First 1
if (-not $distroIp) { Fail 'could not read the distro address; is the distro running?' }

Show-Or-Run "forward $ClusterAddr`:$RendezvousPort to $distroIp`:$RendezvousPort" {
  & netsh.exe interface portproxy add v4tov4 listenaddress=$ClusterAddr listenport=$RendezvousPort connectaddress=$distroIp connectport=$RendezvousPort
}
Show-Or-Run "allow inbound TCP $RendezvousPort on $ClusterAddr only from $ClusterCidr" {
  New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $RendezvousPort `
    -LocalAddress $ClusterAddr -RemoteAddress $ClusterCidr -Profile Any | Out-Null
}
Write-Host 'Reminder: this does NOT make multi-node work by itself (NCCL data ports). The agent stays refused for multi-node on Windows.'
