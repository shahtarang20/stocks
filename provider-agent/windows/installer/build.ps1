<#
.SYNOPSIS
  Build, sign and verify the ComputeCoin Agent MSI (cross-platform plan W6, X10).

.DESCRIPTION
  1. converts consent.txt to consent.rtf (the text of the install consent screen, X14),
  2. runs `wix build` on Package.wxs (WiX Toolset v4),
  3. signs the MSI with Authenticode through signtool, with the certificate taken ONLY from the environment,
  4. verifies the signature and prints the SHA-256 (the value that goes into the signed update manifest).

  WRITTEN, NEVER RUN. WiX and signtool switches are marked TODO(verify-docs) (X11): check
  https://wixtoolset.org/docs/tools/wixexe/ and
  https://learn.microsoft.com/windows/win32/seccrypto/signtool and record URL and date.

.PARAMETER Version
  Three-part version, e.g. 1.4.0. Becomes the MSI ProductVersion and the update manifest version.

.PARAMETER PayloadDir
  Folder with python\ (bundled runtime with computecoin-agent installed), msi_actions.py, consent.txt. See Package.wxs.

.PARAMETER OutDir
  Where the MSI is written. Default: .\out

.PARAMETER Unsigned
  DEV ONLY. Skip signing. The file is named *-UNSIGNED.msi and must never be released or put in an update manifest.

.NOTES
  CERTIFICATE (never in the repository, never printed). Set ONE of:
    COMPUTECOIN_SIGN_CERT_THUMBPRINT   SHA-1 thumbprint of a certificate in the machine or user store (also how a hardware
                                       token or a hosted-HSM key storage provider is addressed)
    COMPUTECOIN_SIGN_PFX_PATH          path to a .pfx file outside this repository
        COMPUTECOIN_SIGN_PFX_PASSWORD  its password (read from the environment; never put on a command line you log)
  and always:
    COMPUTECOIN_SIGN_TIMESTAMP_URL     RFC 3161 timestamp server URL (a signature without a timestamp dies with the certificate)
  Optional: COMPUTECOIN_SIGNTOOL (full path to signtool.exe), COMPUTECOIN_WIX (full path to wix.exe).
  Exit codes: 0 ok, 1 build or sign failed, 2 bad arguments or missing environment.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)] [ValidatePattern('^\d{1,5}\.\d{1,5}\.\d{1,5}$')] [string] $Version,
  [Parameter(Mandatory = $true)] [string] $PayloadDir,
  [string] $OutDir = (Join-Path $PSScriptRoot 'out'),
  [switch] $Unsigned
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Fail([string] $Message, [int] $Code = 1) {
  [Console]::Error.WriteLine("build.ps1: $Message")
  exit $Code
}

function Find-Tool([string] $EnvName, [string] $ExeName) {
  $fromEnv = [Environment]::GetEnvironmentVariable($EnvName)
  if ($fromEnv) {
    if (-not (Test-Path -LiteralPath $fromEnv -PathType Leaf)) { Fail "$EnvName points to a file that does not exist." 2 }
    return $fromEnv
  }
  $cmd = Get-Command $ExeName -ErrorAction SilentlyContinue
  if (-not $cmd) { Fail "$ExeName was not found. Install it or set $EnvName to its full path." 2 }
  return $cmd.Source
}

# ---- inputs --------------------------------------------------------------------------------------------------------
if (-not (Test-Path -LiteralPath $PayloadDir -PathType Container)) { Fail "PayloadDir does not exist: $PayloadDir" 2 }
foreach ($required in @('python\python.exe', 'msi_actions.py')) {
  if (-not (Test-Path -LiteralPath (Join-Path $PayloadDir $required))) { Fail "PayloadDir is missing $required" 2 }
}
$payloadFull = (Resolve-Path -LiteralPath $PayloadDir).Path
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
if ($payloadFull.StartsWith($repoRoot, [StringComparison]::OrdinalIgnoreCase)) {
  # A payload inside the repository is where a stray secret or certificate would end up committed (L7).
  Write-Warning 'PayloadDir is inside the repository. Keep build output and the certificate OUTSIDE it.'
}
$secretLike = Get-ChildItem -LiteralPath $payloadFull -Recurse -File -Include '*.pfx', '*.p12', '*.pem', '*.key', 'agent.toml', '*.secret' -ErrorAction SilentlyContinue
if ($secretLike) { Fail ("PayloadDir contains files that look like secrets: " + (($secretLike | Select-Object -First 5 -ExpandProperty Name) -join ', ')) 2 }

# ---- signing configuration (checked BEFORE building, so a long build is not wasted) ------------------------------------
$thumb = [Environment]::GetEnvironmentVariable('COMPUTECOIN_SIGN_CERT_THUMBPRINT')
$pfx = [Environment]::GetEnvironmentVariable('COMPUTECOIN_SIGN_PFX_PATH')
$tsUrl = [Environment]::GetEnvironmentVariable('COMPUTECOIN_SIGN_TIMESTAMP_URL')
if (-not $Unsigned) {
  if (-not $thumb -and -not $pfx) { Fail 'No signing certificate: set COMPUTECOIN_SIGN_CERT_THUMBPRINT or COMPUTECOIN_SIGN_PFX_PATH (or use -Unsigned for a dev build).' 2 }
  if ($thumb -and $pfx) { Fail 'Set only one of COMPUTECOIN_SIGN_CERT_THUMBPRINT and COMPUTECOIN_SIGN_PFX_PATH.' 2 }
  if (-not $tsUrl) { Fail 'COMPUTECOIN_SIGN_TIMESTAMP_URL is required.' 2 }
  if ($pfx -and -not (Test-Path -LiteralPath $pfx -PathType Leaf)) { Fail 'COMPUTECOIN_SIGN_PFX_PATH does not point to a file.' 2 }
  if ($pfx -and $pfx.StartsWith($repoRoot, [StringComparison]::OrdinalIgnoreCase)) { Fail 'The certificate file must not be inside the repository.' 2 }
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$outFull = (Resolve-Path -LiteralPath $OutDir).Path

# ---- 1. consent text -> RTF ----------------------------------------------------------------------------------------------
$consentTxt = Join-Path $PSScriptRoot 'consent.txt'
$consentRtf = Join-Path $outFull 'consent.rtf'
function ConvertTo-RtfText([string] $Text) {
  $sb = New-Object System.Text.StringBuilder
  [void]$sb.Append('{\rtf1\ansi\deff0{\fonttbl{\f0 Segoe UI;}}\fs20 ')
  foreach ($ch in $Text.ToCharArray()) {
    $code = [int]$ch
    if ($ch -eq '\') { [void]$sb.Append('\\') }
    elseif ($ch -eq '{') { [void]$sb.Append('\{') }
    elseif ($ch -eq '}') { [void]$sb.Append('\}') }
    elseif ($ch -eq "`n") { [void]$sb.Append('\par ') }
    elseif ($ch -eq "`r") { }
    elseif ($code -gt 127) { [void]$sb.Append('\u' + [int][int16]$code + '?') }
    else { [void]$sb.Append($ch) }
  }
  [void]$sb.Append('}')
  return $sb.ToString()
}
[IO.File]::WriteAllText($consentRtf, (ConvertTo-RtfText ([IO.File]::ReadAllText($consentTxt))), [Text.Encoding]::ASCII)

# ---- 2. wix build ----------------------------------------------------------------------------------------------------------
$wix = Find-Tool 'COMPUTECOIN_WIX' 'wix.exe'
$msiName = if ($Unsigned) { "ComputeCoinAgent-$Version-UNSIGNED.msi" } else { "ComputeCoinAgent-$Version.msi" }
$msi = Join-Path $outFull $msiName
$wixArgs = @(
  'build', (Join-Path $PSScriptRoot 'Package.wxs'),
  '-arch', 'x64',
  '-ext', 'WixToolset.UI.wixext',               # TODO(verify-docs): extension name and that it is installed (wix extension add)
  '-d', "Version=$Version",
  '-d', "PayloadDir=$payloadFull",
  '-d', "ConsentRtf=$consentRtf",
  '-o', $msi
)
& $wix @wixArgs
if ($LASTEXITCODE -ne 0) { Fail "wix build failed with exit code $LASTEXITCODE" }

# ---- 3. sign -----------------------------------------------------------------------------------------------------------------
if (-not $Unsigned) {
  $signtool = Find-Tool 'COMPUTECOIN_SIGNTOOL' 'signtool.exe'
  # TODO(verify-docs): sign /fd /td /tr /sha1 /f /p switches.
  $signArgs = @('sign', '/fd', 'sha256', '/td', 'sha256', '/tr', $tsUrl, '/d', 'ComputeCoin Agent')
  if ($thumb) { $signArgs += @('/sha1', $thumb) }
  else {
    $signArgs += @('/f', $pfx)
    $pw = [Environment]::GetEnvironmentVariable('COMPUTECOIN_SIGN_PFX_PASSWORD')
    if ($pw) { $signArgs += @('/p', $pw) }   # the value is never echoed by this script
  }
  $signArgs += $msi
  & $signtool @signArgs
  if ($LASTEXITCODE -ne 0) { Fail "signtool sign failed with exit code $LASTEXITCODE" }

  # ---- 4. verify (the release is refused unless this passes) ----------------------------------------------------------
  & $signtool verify '/pa' '/v' $msi
  if ($LASTEXITCODE -ne 0) { Fail "signtool verify failed with exit code $LASTEXITCODE (the MSI is NOT releasable)" }
}

$sha = (Get-FileHash -LiteralPath $msi -Algorithm SHA256).Hash.ToLowerInvariant()
$size = (Get-Item -LiteralPath $msi).Length
Write-Output "msi:      $msi"
Write-Output "version:  $Version"
Write-Output "sha256:   $sha"
Write-Output "size:     $size"
Write-Output 'Next: put sha256 and size into the update manifest, sign the manifest offline with the update key, publish it with the release API.'
exit 0
