# Sign a Kit manifest with the OFFLINE Kit signing key (U1). Run it on the machine that holds the key, never in CI.
#   .\scripts\windows\sign_kit.ps1 -Manifest dist\ai-image.json -KeyPath C:\keys\kit_signing.pem -Out dist\ai-image.signed.json
# The key is read from the path you give; it is never copied, printed or written anywhere else (L7).
# Same thing in bash:  python3 scripts/sign_kit.py --manifest dist/ai-image.json --key /path/to/key.pem --out dist/ai-image.signed.json
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Manifest,
    [Parameter(Mandatory = $true)][string]$KeyPath,
    [string]$Out
)
. "$PSScriptRoot\_common.ps1"

if (-not (Test-Path -LiteralPath $Manifest -PathType Leaf)) { throw "manifest not found: $Manifest" }
if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) { throw "key file not found: $KeyPath" }
$bad = Test-OwnerOnlyAcl -Path $KeyPath
if ($bad.Count -gt 0) {
    throw ("the key file is readable by: " + ($bad -join ', ') + ". Restrict it to your own account first (icacls <file> /inheritance:r /grant:r ""$env:USERNAME"":F).")
}
$arguments = @('scripts/sign_kit.py', '--manifest', (Resolve-Path -LiteralPath $Manifest).Path, '--key', (Resolve-Path -LiteralPath $KeyPath).Path)
if ($Out) {
    $outFull = [System.IO.Path]::GetFullPath($Out)
    $arguments += @('--out', $outFull)
}
exit (Invoke-RepoPython -Arguments $arguments)
