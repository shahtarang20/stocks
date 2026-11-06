# Kit CI helper: the offline gate, filling a digest into a manifest copy, and publishing a signed manifest.
#   .\scripts\windows\kit_ci.ps1 check
#   .\scripts\windows\kit_ci.ps1 check --kits-dir kits
#   .\scripts\windows\kit_ci.ps1 fill-digest --kit kits\ai-image\kit.json --repo ghcr.io/org/kit-ai-image --digest sha256:<64 hex> --out dist\ai-image.json
#   $env:KIT_ADMIN_TOKEN = '<token from your secret store>'; .\scripts\windows\kit_ci.ps1 publish --signed dist\ai-image.signed.json --api-url https://api.example
# Same thing in bash:  python3 scripts/kit_ci.py check --kits-dir kits
# The admin token is read from an environment variable by kit_ci.py; it is never a parameter, never printed (L7).
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)][ValidateSet('check', 'fill-digest', 'publish')][string]$Command,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)
. "$PSScriptRoot\_common.ps1"

if ($Command -eq 'publish') {
    $tokenVar = 'KIT_ADMIN_TOKEN'
    for ($i = 0; $i -lt $Rest.Count - 1; $i++) { if ($Rest[$i] -eq '--token-env') { $tokenVar = $Rest[$i + 1] } }
    if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($tokenVar))) {
        throw "environment variable $tokenVar is empty. Set it from your secret store in this window first."
    }
}
$arguments = @('scripts/kit_ci.py', $Command)
if ($Rest) { $arguments += $Rest }
exit (Invoke-RepoPython -Arguments $arguments)
