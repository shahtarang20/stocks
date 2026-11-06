# Windows PowerShell entry point for the developer tasks (the Makefile equivalent).
#   .\scripts\windows\tasks.ps1 help
#   .\scripts\windows\tasks.ps1 agent-test
#   .\scripts\windows\tasks.ps1 -DryRun test
# Everything is implemented in tasks.py at the repository root; this wrapper only finds Python and forwards the arguments.
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Wsl,
    [Parameter(Position = 0)][string]$Target = 'help',
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)
. "$PSScriptRoot\_common.ps1"

$args2 = @('tasks.py')
if ($DryRun) { $args2 += '--dry-run' }
if ($Wsl) { $args2 += '--wsl' }
$args2 += $Target
if ($Rest -and $Rest.Count -gt 0) { $args2 += '--'; $args2 += $Rest }
exit (Invoke-RepoPython -Arguments $args2)
