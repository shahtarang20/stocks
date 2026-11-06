# Generate a NEW Kit signing key (Ed25519, PEM) and print its public key to configure on the server.
#   .\scripts\windows\new_kit_signing_key.ps1 -Path C:\keys\kit_signing.pem
# Do this on the signer machine only. Windows ignores POSIX file modes, so sign_kit.py removes inherited permissions and grants
# only your account (icacls); this script then checks the result and deletes the key again if anyone else could read it.
# Same thing in bash:  python3 scripts/sign_kit.py --gen-key /path/to/new_private.pem
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$Path)
. "$PSScriptRoot\_common.ps1"

$full = [System.IO.Path]::GetFullPath($Path)
if (Test-Path -LiteralPath $full) { throw "refusing to overwrite an existing file: $full" }
$dir = Split-Path -Parent $full
if (-not (Test-Path -LiteralPath $dir -PathType Container)) { throw "the folder does not exist: $dir" }

$code = Invoke-RepoPython -Arguments @('scripts/sign_kit.py', '--gen-key', $full)
if ($code -ne 0) { exit $code }

$bad = Test-OwnerOnlyAcl -Path $full
if ($bad.Count -gt 0) {
    Remove-Item -LiteralPath $full -Force
    throw ("deleted the new key: it was readable by " + ($bad -join ', ') + ". Nothing was created.")
}
Write-Host "Key written to $full (owner-only). Keep a backup in a safe place; it cannot be recovered."
