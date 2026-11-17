# Windows container rental test kit (EXPERIMENTAL): run this on a Windows PC that hosts, to find out whether it can offer container
# rental, and write a plain-text report to send back. It only READS: it changes nothing, installs nothing, and never prints or stores a
# password, token or setting file. Run it in an ADMINISTRATOR PowerShell:
#   .\scripts\windows\rental-test-kit.ps1
#   .\scripts\windows\rental-test-kit.ps1 -SkipRentalCheck        (skip the slow container test)
#   .\scripts\windows\rental-test-kit.ps1 -OutDir C:\Temp
# ASCII only on purpose: Windows PowerShell 5.1 reads a file without a byte-order mark as the legacy code page.
[CmdletBinding()]
param(
    [string]$OutDir = (Get-Location).Path,
    [string]$Image = '',
    [switch]$SkipRentalCheck
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$Distro = 'computecoin'
$RunnerUser = 'ccrunner'
$Stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
$Report = Join-Path $OutDir ("computecoin-rental-report-$Stamp.txt")
$script:Fails = 0
$script:Warns = 0
$script:Lines = New-Object System.Collections.Generic.List[string]

function Add-Line {
    param([string]$Text)
    $script:Lines.Add($Text)
    Write-Host $Text
}

function Add-Result {
    param([ValidateSet('PASS', 'WARN', 'FAIL', 'INFO')][string]$Status, [string]$Name, [string]$Detail, [string]$Fix = '')
    if ($Status -eq 'FAIL') { $script:Fails++ }
    if ($Status -eq 'WARN') { $script:Warns++ }
    Add-Line ("[{0}] {1}: {2}" -f $Status, $Name, $Detail)
    if ($Fix -and $Status -ne 'PASS' -and $Status -ne 'INFO') { Add-Line ("       What to do: {0}" -f $Fix) }
}

function Invoke-Wsl {
    # Runs wsl.exe with a list of arguments (never a joined string) and returns @{ Code; Text }. WSL_UTF8 makes its own messages UTF-8.
    param([string[]]$Arguments, [int]$TimeoutSeconds = 120)
    $env:WSL_UTF8 = '1'
    try {
        $out = & wsl.exe @Arguments 2>&1 | Out-String
        return @{ Code = $LASTEXITCODE; Text = ($out -replace "`0", '').Trim() }
    }
    catch {
        return @{ Code = 127; Text = $_.Exception.GetType().Name }
    }
}

function Invoke-InDistro {
    param([string[]]$Program, [string]$User = $RunnerUser)
    return Invoke-Wsl -Arguments (@('--distribution', $Distro, '--user', $User, '--exec') + $Program)
}

Add-Line "ComputeCoin Windows rental test kit - $((Get-Date).ToString('u'))"
Add-Line "This report contains no passwords, tokens or settings. Read it before you send it."
Add-Line ''

# 1. Windows version
try {
    $os = Get-CimInstance Win32_OperatingSystem
    $build = [int]$os.BuildNumber
    $detail = "$($os.Caption), version $($os.Version), build $build"
    if ($build -ge 22000) { Add-Result PASS 'Windows version' $detail }
    else { Add-Result FAIL 'Windows version' $detail 'Upgrade to Windows 11 (build 22000 or newer). Microsoft documents the Linux system boot setting (systemd) as Windows 11 only, and the ComputeCoin Linux system needs it.' }
}
catch { Add-Result FAIL 'Windows version' 'could not be read' }

# 2. administrator
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) { Add-Result PASS 'Administrator' 'this window runs as administrator.' }
else { Add-Result WARN 'Administrator' 'this window is not an administrator window, so some checks may be incomplete.' 'Open PowerShell with "Run as administrator" and run the kit again.' }

# 3. virtualization
try {
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    if ($cpu.VirtualizationFirmwareEnabled) { Add-Result PASS 'Virtualization' 'switched on in the BIOS.' }
    else { Add-Result FAIL 'Virtualization' 'switched off (or not reported).' 'Switch on virtualization (Intel VT-x or AMD-V, often "SVM Mode") in the BIOS settings.' }
}
catch { Add-Result WARN 'Virtualization' 'could not be read.' 'Check in Task Manager, Performance, CPU: "Virtualization: Enabled".' }

# 4. WSL
$wslVersion = Invoke-Wsl -Arguments @('--version')
if ($wslVersion.Code -eq 0 -and $wslVersion.Text) { Add-Result PASS 'WSL' (($wslVersion.Text -split "`n")[0].Trim()) }
else { Add-Result FAIL 'WSL' 'is not installed, or did not answer.' 'In an administrator PowerShell run: wsl --update   (then restart Windows).' }

# 5. the managed Linux system
$list = Invoke-Wsl -Arguments @('--list', '--verbose')
$distroLine = ($list.Text -split "`n" | Where-Object { $_ -match "(^|\s)$Distro(\s|$)" } | Select-Object -First 1)
if (-not $distroLine) {
    Add-Result FAIL 'ComputeCoin Linux system' "the Linux system '$Distro' is not installed."  'Run the ComputeCoin Windows setup that installs it.'
}
else {
    $ver = ($distroLine.Trim() -split '\s+')[-1]
    if ($ver -eq '2') { Add-Result PASS 'ComputeCoin Linux system' "'$Distro' is installed and runs as WSL 2." }
    else { Add-Result FAIL 'ComputeCoin Linux system' "'$Distro' runs as WSL $ver." "Convert it: wsl --set-version $Distro 2" }
}

# 6. NVIDIA on Windows
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    $q = & $smi.Source --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0 -and $q.Trim()) {
        Add-Result PASS 'NVIDIA card (Windows)' ($q.Trim() -replace "`r?`n", '; ')
        # NVIDIA: GPU compute in WSL works on GeForce and RTX cards in WDDM mode, not on cards in TCC mode or data-centre cards.
        $mode = & $smi.Source --query-gpu=driver_model.current --format=csv,noheader 2>&1 | Out-String
        if ($LASTEXITCODE -eq 0 -and $mode.Trim()) {
            if ($mode -match 'TCC') { Add-Result FAIL 'NVIDIA card mode' ($mode.Trim() -replace "`r?`n", '; ') 'Cards in TCC mode do not work in WSL. Use a GeForce or RTX card in WDDM mode.' }
            else { Add-Result PASS 'NVIDIA card mode' ($mode.Trim() -replace "`r?`n", '; ') }
        }
    }
    else { Add-Result FAIL 'NVIDIA card (Windows)' 'nvidia-smi did not answer.' 'Install the current NVIDIA driver for Windows and restart.' }
}
else { Add-Result FAIL 'NVIDIA card (Windows)' 'nvidia-smi was not found.' 'Install the current NVIDIA driver for Windows. AMD and Intel cards are not supported for rentals.' }

# 7-10. inside the Linux system
if ($distroLine) {
    $dock = Invoke-InDistro -Program @('docker', 'version', '--format', '{{.Server.Version}}')
    if ($dock.Code -eq 0 -and $dock.Text) { Add-Result PASS 'Docker (inside)' "Docker $($dock.Text) is running." }
    else { Add-Result FAIL 'Docker (inside)' 'did not answer.' 'Run the ComputeCoin Windows setup again to repair the Linux system.' }

    $gpu = Invoke-InDistro -Program @('nvidia-smi', '-L')
    if ($gpu.Code -eq 0 -and $gpu.Text) { Add-Result PASS 'NVIDIA card (inside)' (($gpu.Text -split "`n")[0].Trim()) }
    else { Add-Result FAIL 'NVIDIA card (inside)' 'the Linux system cannot see the card.' 'Update the NVIDIA driver on Windows, run: wsl --shutdown, and try again.' }

    $cdrive = Invoke-InDistro -Program @('ls', '-A', '/mnt/c') -User 'root'
    if ($cdrive.Code -eq 0 -and $cdrive.Text) { Add-Result FAIL 'Windows drive hidden' 'your C: drive IS visible inside the Linux system.' 'Set [automount] enabled=false in /etc/wsl.conf inside the Linux system, then run: wsl --shutdown' }
    else { Add-Result PASS 'Windows drive hidden' 'the C: drive is not visible inside the Linux system.' }

    $interop = Invoke-InDistro -Program @('test', '-e', '/proc/sys/fs/binfmt_misc/WSLInterop') -User 'root'
    if ($interop.Code -eq 0) { Add-Result WARN 'Windows programs blocked' 'programs inside the Linux system can start Windows programs.' 'Set [interop] enabled=false in /etc/wsl.conf inside the Linux system, then run: wsl --shutdown' }
    else { Add-Result PASS 'Windows programs blocked' 'Windows programs cannot be started from inside.' }

    $df = Invoke-InDistro -Program @('df', '-h', '/') -User 'root'
    Add-Result INFO 'Disk (inside)' (($df.Text -split "`n" | Select-Object -Last 1).Trim())
}
else { Add-Result INFO 'Inside checks' 'skipped, because the Linux system is not installed.' }

# 11. the Windows service and the agent's own check
$svc = Get-Service -Name 'ComputeCoinAgent' -ErrorAction SilentlyContinue
if ($svc) { Add-Result INFO 'Service' "ComputeCoinAgent is $($svc.Status)." }
else { Add-Result WARN 'Service' 'the ComputeCoinAgent service is not installed.' 'The Windows agent must be installed and connected first.' }

$agent = Get-Command computecoin-agent -ErrorAction SilentlyContinue
if ($agent) {
    $status = & $agent.Source rental status 2>&1 | Out-String
    Add-Result INFO 'rental status' $status.Trim()
    if (-not $SkipRentalCheck) {
        Add-Line ''
        Add-Line 'Running: computecoin-agent rental check (this can take a few minutes the first time)'
        $argsList = @('rental', 'check')
        if ($Image) { $argsList += @('--image', $Image) }
        $check = & $agent.Source @argsList 2>&1 | Out-String
        Add-Line $check.Trim()
        if ($LASTEXITCODE -eq 0) { Add-Result PASS 'rental check' 'passed.' } else { Add-Result FAIL 'rental check' 'did not pass (see above).' }
    }
}
else { Add-Result WARN 'computecoin-agent' 'was not found on this computer.' 'Open a new PowerShell window after installing the Windows agent.' }

Add-Line ''
Add-Line ("Result: {0} failed, {1} warnings." -f $script:Fails, $script:Warns)
try {
    $script:Lines | Set-Content -LiteralPath $Report -Encoding UTF8
    Write-Host "Report saved to: $Report"
}
catch { Write-Host 'The report could not be saved. Copy the text above instead.' }
if ($script:Fails -gt 0) { exit 1 } else { exit 0 }
