# Generate or check the ccpp-fanout-v1 test vectors (docs/specs/ccpp-v1-vectors.json). See docs/operations/fanout-vectors-howto.md.
#   .\scripts\windows\gen_fanout_vectors.ps1 -Write -CheckApp
#   .\scripts\windows\gen_fanout_vectors.ps1 -Check
# Same thing in bash:  python3 scripts/gen_fanout_vectors.py --write && python3 scripts/gen_fanout_vectors.py --check-app
# The file is written with LF line endings on Windows too (the script writes bytes).
[CmdletBinding()]
param([switch]$Write, [switch]$Check, [switch]$CheckApp)
. "$PSScriptRoot\_common.ps1"

$arguments = @('scripts/gen_fanout_vectors.py')
if ($CheckApp) { $arguments += '--check-app' }
if ($Check) { $arguments += '--check' }
if ($Write) { $arguments += '--write' }
exit (Invoke-RepoPython -Arguments $arguments)
