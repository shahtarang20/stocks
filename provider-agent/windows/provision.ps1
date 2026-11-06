<#
.SYNOPSIS
  Provision, repair, reset or remove the ComputeCoin runtime distro (managed WSL2) on this Windows PC.

.DESCRIPTION
  A thin wrapper. It checks the prerequisites, then calls the Python module
  computecoin_agent.windows.runtime.provision, which builds every wsl.exe command as an argument list (never a shell string).
  Nothing here edits your .wslconfig, your other WSL distros, or any credential.

  WRITTEN, NEVER RUN. PowerShell and WSL behaviour is marked TODO(verify-docs) (cross-platform plan X11).

.PARAMETER Action
  check      only run the requirements checker (what the installer and the website show)
  provision  create (or complete) the runtime distro
  repair     re-run every provisioning step in place; credentials are never touched
  reset      remove the distro and create a fresh one (the distro's data is lost; credentials are not in it)
  remove     unregister the distro and delete its disk
  health     print the health checks
  gate       run the health checks AND the GPU smoke test (what enrolment waits for)

.PARAMETER SettingsPath
  Runtime settings JSON (copy runtime-settings.template.json and fill in the pinned manifest public key).

.PARAMETER ManifestPath
  The signed runtime-manifest.json. Needed by every action except check and remove.

.PARAMETER PythonExe
  The Python interpreter that has computecoin-agent installed (the supervisor's own). Default: python.

.EXAMPLE
  .\provision.ps1 -Action check
  .\provision.ps1 -Action provision -SettingsPath C:\ProgramData\ComputeCoin\runtime-settings.json -ManifestPath C:\ProgramData\ComputeCoin\runtime-manifest.json

.NOTES
  Exit codes: 0 ok, 1 failed, 2 bad arguments or files, 3 requirement not met, 4 signature or digest check failed (nothing was installed).
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('check', 'provision', 'repair', 'reset', 'remove', 'health', 'gate')]
  [string]$Action,
  [string]$SettingsPath,
  [string]$ManifestPath,
  [string]$PythonExe = 'python',
  [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Stop-WithError {
  param([string]$Message, [int]$Code)
  [Console]::Error.WriteLine("ComputeCoin runtime: $Message")
  exit $Code
}

# ---- prerequisites, in plain language --------------------------------------------------------------------------
if ($PSVersionTable.PSVersion.Major -lt 5) {
  Stop-WithError 'PowerShell 5.1 or newer is required. Update Windows PowerShell and run this again.' 3
}
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
  Stop-WithError 'This script is for Windows PCs only.' 3
}

$python = Get-Command $PythonExe -ErrorAction SilentlyContinue
if (-not $python) {
  Stop-WithError "Python was not found ('$PythonExe'). Pass -PythonExe with the interpreter that has computecoin-agent installed." 2
}

$modulePrefix = @('-I', '-m')  # isolated mode: ignore PYTHONPATH and the current directory

if ($Action -eq 'check') {
  $checkArgs = $modulePrefix + @('computecoin_agent.windows.runtime.requirements_check')
  if ($Json) { $checkArgs += '--json' }
  & $python.Source @checkArgs
  exit $LASTEXITCODE
}

if (-not (Get-Command 'wsl.exe' -ErrorAction SilentlyContinue)) {
  Stop-WithError 'wsl.exe was not found. Install the Windows Subsystem for Linux first: open PowerShell as administrator, run "wsl --install --no-distribution", then restart.' 3
}
if (-not (Get-Command 'nvidia-smi' -ErrorAction SilentlyContinue) -and ($Action -in @('provision', 'repair', 'reset', 'gate'))) {
  Stop-WithError 'The NVIDIA driver was not found (nvidia-smi is missing). Install the latest NVIDIA driver, restart, and run this again.' 3
}

if (-not $SettingsPath -or -not (Test-Path -LiteralPath $SettingsPath -PathType Leaf)) {
  Stop-WithError 'Give -SettingsPath with the runtime settings JSON (see runtime-settings.template.json).' 2
}
if ($Action -notin @('remove') -and (-not $ManifestPath -or -not (Test-Path -LiteralPath $ManifestPath -PathType Leaf))) {
  Stop-WithError 'Give -ManifestPath with the signed runtime-manifest.json.' 2
}

$runArgs = $modulePrefix + @('computecoin_agent.windows.runtime.provision', $Action, '--settings', $SettingsPath)
if ($Action -ne 'remove') { $runArgs += @('--manifest', $ManifestPath) }

if ($Action -in @('reset', 'remove')) {
  Write-Host 'This permanently deletes the ComputeCoin runtime (its disk image and everything inside it).'
  Write-Host 'Your files, your other WSL distros and your ComputeCoin credentials are not affected.'
}

# The Python module prints one JSON document. Show it, then translate the exit code into a sentence.
$output = & $python.Source @runArgs
$code = $LASTEXITCODE
$output | ForEach-Object { Write-Output $_ }

switch ($code) {
  0 { }
  1 { [Console]::Error.WriteLine('ComputeCoin runtime: the step failed. See the JSON above ("error" and "steps"). repair can continue from where it stopped.') }
  2 { [Console]::Error.WriteLine('ComputeCoin runtime: bad settings or arguments.') }
  3 { [Console]::Error.WriteLine('ComputeCoin runtime: this PC does not meet a requirement. Run: provision.ps1 -Action check') }
  4 { [Console]::Error.WriteLine('ComputeCoin runtime: the signature or a download digest did not verify. Nothing was installed. Do not retry with a different manifest unless it comes from ComputeCoin.') }
  default { [Console]::Error.WriteLine("ComputeCoin runtime: unexpected exit code $code.") }
}
exit $code
