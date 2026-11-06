# Shared helpers for the Windows operator scripts (cross-platform plan W10). Dot-source it: . "$PSScriptRoot\_common.ps1"
# ASCII only on purpose: Windows PowerShell 5.1 reads a file without a byte-order mark as the legacy code page.
# Works in Windows PowerShell 5.1 and PowerShell 7. Nothing here reads, prints or stores a secret.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Python must read and write UTF-8 whatever the console code page is (the SDK and the scripts name encodings explicitly,
# this also covers the standard streams).
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

function Get-RepoRoot {
    return (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
}

function Get-PythonCommand {
    # The Python launcher first (python.org installs it), then python.exe. The Microsoft Store "python" alias is only a stub:
    # if it is all there is, install Python 3.12 and open a new window.
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { return @($launcher.Source, '-3') }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { return @($python.Source) }
    throw 'Python 3 was not found. Install Python 3.12 (python.org, or: winget install Python.Python.3.12), then open a new PowerShell window.'
}

function Invoke-RepoPython {
    # Runs python with the given arguments from the repository root and returns its exit code. Arguments are passed as a list,
    # never joined into a string, so paths with spaces are safe.
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $cmd = Get-PythonCommand
    $exe = $cmd[0]
    $prefix = @()
    if ($cmd.Count -gt 1) { $prefix = $cmd[1..($cmd.Count - 1)] }
    Push-Location (Get-RepoRoot)
    try {
        & $exe @prefix @Arguments
        return $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}

function Test-OwnerOnlyAcl {
    # Returns the names of any principals that have an Allow rule on the file although they should not
    # (Everyone, Users, Authenticated Users). An empty result means no such rule.
    # TODO(verify-docs): Get-Acl / SecurityIdentifier.Translate behaviour on a current Windows against Microsoft's documentation.
    param([Parameter(Mandatory = $true)][string]$Path)
    $risky = @('S-1-1-0', 'S-1-5-32-545', 'S-1-5-11')
    $bad = @()
    $acl = Get-Acl -LiteralPath $Path
    foreach ($ace in $acl.Access) {
        $sid = ''
        try { $sid = $ace.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { $sid = '' }
        if (($risky -contains $sid) -and ($ace.AccessControlType -eq 'Allow')) { $bad += $ace.IdentityReference.Value }
    }
    return , $bad
}
