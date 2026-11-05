<#
.SYNOPSIS
  TEMPLATE guest-side job runner for the ComputeCoin Windows VM tier (U14). NEVER RUN against a real image yet: written
  from the host side's contract (provider-agent/computecoin_agent/vm_runner.py), not tested in a Windows guest.

.DESCRIPTION
  The CUSTOMER bakes this script (or their own equivalent) into the licensed Windows image they supply and starts it at boot
  (for example a Scheduled Task "At startup" running as SYSTEM). The platform ships no Windows image and no licence.

  Host contract (see docs/architecture/U14-windows-vm-design.md):
    * disks: the system disk, an INPUT disk (FAT32, label CCDATA, attached READ-ONLY, holds the customer's input files and
      job.json) and an OUTPUT disk (FAT32, label CCDATA, read-write, empty);
    * no network device at all;
    * a virtio-serial port named "computecoin.ctl" (needs the virtio-serial / vioserial driver installed in the image),
      host side is a unix socket. The host sends ONE JSON line first ({"job_id": ..., "job_file": "job.json"}); this script
      answers with capped plain text lines: "PROGRESS <text>" and finally "DONE <exit code>".
    * job.json on the input disk: {"job_id": str, "command": [string, ...] | null, "params": {...}}.
      "command" (optional) is the program and its arguments. Without it, the script runs run.ps1 / run.cmd / run.bat from
      the input disk, if one exists. The working directory is the input disk; write results to $env:CC_OUTPUT_DIR (the output
      disk root). Input files are also reachable as $env:CC_INPUT_DIR.
    * when finished the script flushes the output disk and powers the VM off. The host treats QEMU exiting with status 0 as
      "the guest finished" and then reads the output disk with mtools (it re-checks everything; the guest is untrusted).

  The host reads at most 64 KiB from the control channel; keep PROGRESS lines short.
#>
[CmdletBinding()]
param(
    [string]$PortName = 'computecoin.ctl',
    [int]$DiskWaitSeconds = 120,
    [switch]$NoShutdown   # for building/debugging the image only; the real run must power off
)

$ErrorActionPreference = 'Stop'
$script:Port = $null
$exitCode = 1

function Send-Line([string]$Text) {
    # Best effort: a failed control write never decides the result (the output disk does).
    if ($null -eq $script:Port) { return }
    try {
        $clean = ($Text -replace '[\r\n]+', ' ')
        if ($clean.Length -gt 200) { $clean = $clean.Substring(0, 200) }
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($clean + "`n")
        $script:Port.Write($bytes, 0, $bytes.Length)
        $script:Port.Flush()
    } catch { }
}

function Open-ControlPort {
    # virtio-serial ports show up as \\.\Global\<name> once the vioserial driver is installed.
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $fs = New-Object System.IO.FileStream("\\.\Global\$PortName",
                [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::ReadWrite)
            return $fs
        } catch { Start-Sleep -Seconds 1 }
    }
    return $null
}

function Read-FirstLine($Stream, [int]$TimeoutSeconds = 30) {
    # The host's single JSON line. Not required for the run (job.json on the input disk is authoritative).
    $buf = New-Object byte[] 4096
    $sb = New-Object System.Text.StringBuilder
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $n = $Stream.Read($buf, 0, $buf.Length)
            if ($n -gt 0) {
                [void]$sb.Append([System.Text.Encoding]::UTF8.GetString($buf, 0, $n))
                if ($sb.ToString().Contains("`n")) { break }
            }
        } catch { Start-Sleep -Milliseconds 200 }
    }
    return $sb.ToString().Trim()
}

function Find-Disks {
    # Both data disks are labelled CCDATA; tell them apart by the read-only flag (the host attaches INPUT read-only).
    $deadline = (Get-Date).AddSeconds($DiskWaitSeconds)
    while ((Get-Date) -lt $deadline) {
        $vols = @(Get-Volume | Where-Object { $_.FileSystemLabel -eq 'CCDATA' -and $_.DriveLetter })
        $in = $null; $out = $null
        foreach ($v in $vols) {
            $disk = Get-Partition -DriveLetter $v.DriveLetter | Get-Disk
            if ($disk.IsReadOnly) { $in = "$($v.DriveLetter):\" } else { $out = "$($v.DriveLetter):\" }
        }
        if ($in -and $out) { return @{ Input = $in; Output = $out } }
        Start-Sleep -Seconds 2
    }
    throw 'input and output disks were not found'
}

try {
    $script:Port = Open-ControlPort
    if ($script:Port) { [void](Read-FirstLine $script:Port) }
    Send-Line 'PROGRESS started'

    $disks = Find-Disks
    $env:CC_INPUT_DIR = $disks.Input
    $env:CC_OUTPUT_DIR = $disks.Output
    Send-Line 'PROGRESS disks ready'

    $jobFile = Join-Path $disks.Input 'job.json'
    $job = $null
    if (Test-Path -LiteralPath $jobFile) { $job = Get-Content -LiteralPath $jobFile -Raw | ConvertFrom-Json }

    Push-Location $disks.Input
    try {
        if ($job -and $job.command -and $job.command.Count -gt 0) {
            $exe = [string]$job.command[0]
            $argList = @($job.command | Select-Object -Skip 1 | ForEach-Object { [string]$_ })
            Send-Line 'PROGRESS running command'
            & $exe @argList *> (Join-Path $disks.Output 'job.log')
            $exitCode = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
        } else {
            $entry = @('run.ps1', 'run.cmd', 'run.bat') | Where-Object { Test-Path -LiteralPath (Join-Path $disks.Input $_) } | Select-Object -First 1
            if (-not $entry) { throw 'no command in job.json and no run.ps1 / run.cmd / run.bat on the input disk' }
            Send-Line "PROGRESS running $entry"
            if ($entry -eq 'run.ps1') { & (Join-Path $disks.Input $entry) *> (Join-Path $disks.Output 'job.log') }
            else { & cmd.exe /c (Join-Path $disks.Input $entry) *> (Join-Path $disks.Output 'job.log') }
            $exitCode = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
        }
    } finally { Pop-Location }
}
catch {
    Send-Line "PROGRESS error $($_.Exception.GetType().Name)"
    $exitCode = 1
}
finally {
    # Flush the output volume so the host sees every byte, then report and power off.
    try { if ($env:CC_OUTPUT_DIR) { Write-VolumeCache -DriveLetter $env:CC_OUTPUT_DIR.Substring(0, 1) } } catch { }
    Send-Line "DONE $exitCode"
    if (-not $NoShutdown) { Stop-Computer -Force }
}
