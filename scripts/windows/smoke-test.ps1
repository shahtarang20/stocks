# API smoke test: PowerShell equivalent of scripts/smoke-test.sh. Safe against staging and production (it registers one throwaway
# account with a random e-mail address and a random password generated for this run).
#   .\scripts\windows\smoke-test.ps1
#   .\scripts\windows\smoke-test.ps1 -ApiUrl https://api-staging.computecoin.ai
[CmdletBinding()]
param([string]$ApiUrl = 'http://localhost:8000')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ApiUrl = $ApiUrl.TrimEnd('/')
$script:failed = 0
$session = $null
$stamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$email = "smoke_${stamp}_$(Get-Random)@computecoin.ai"
$password = ([guid]::NewGuid().ToString('N') + 'Aa1!')

function Invoke-Check {
    param([string]$Name, [string]$Method, [string]$Endpoint, [int]$Expected, $Body = $null)
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $code = 0
    $content = ''
    try {
        $params = @{ Uri = "$ApiUrl$Endpoint"; Method = $Method; UseBasicParsing = $true; ErrorAction = 'Stop' }
        if ($null -ne $Body) { $params.Body = ($Body | ConvertTo-Json -Compress); $params.ContentType = 'application/json' }
        if ($null -eq $script:session) { $params.SessionVariable = 'newSession' } else { $params.WebSession = $script:session }
        $resp = Invoke-WebRequest @params
        if ($null -eq $script:session) { $script:session = $newSession }
        $code = [int]$resp.StatusCode
        $content = [string]$resp.Content
    }
    catch {
        $r = $_.Exception.Response
        if ($null -ne $r) { $code = [int]$r.StatusCode }
    }
    $watch.Stop()
    if ($code -eq $Expected) {
        Write-Host ("  [PASS] {0} ({1}) - {2} in {3}ms" -f $Name, $Endpoint, $code, $watch.ElapsedMilliseconds)
    }
    else {
        Write-Host ("  [FAIL] {0} ({1}) - expected {2}, got {3} in {4}ms" -f $Name, $Endpoint, $Expected, $code, $watch.ElapsedMilliseconds)
        $script:failed++
    }
    return $content
}

Write-Host '========================================================'
Write-Host " ComputeCoin API Smoke Test: $ApiUrl"
Write-Host '========================================================'

Write-Host "`n1. Basic Connectivity & Health:"
[void](Invoke-Check 'Root Service Info' 'GET' '/' 200)
[void](Invoke-Check 'Health Check (DB + Services)' 'GET' '/health' 200)

Write-Host "`n2. Public Catalog & Smart Contract Registry:"
[void](Invoke-Check 'GPU Marketplace Catalog' 'GET' '/v1/marketplace/machines' 200)
[void](Invoke-Check 'Settlement Contract Registry' 'GET' '/v1/settlement/contracts' 200)

Write-Host "`n3. Authentication & Account Creation:"
$creds = @{ email = $email; password = $password }
[void](Invoke-Check 'User Registration' 'POST' '/auth/register' 201 $creds)
$loginBody = Invoke-Check 'User Login (Sets Cookies)' 'POST' '/auth/login' 200 $creds

$accessToken = ''
try { $accessToken = [string](($loginBody | ConvertFrom-Json).access_token) } catch { $accessToken = '' }

Write-Host "`n4. Authenticated Endpoints:"
if ($accessToken) {
    foreach ($check in @(@('Current User Profile', '/auth/me'), @('Billing Account', '/v1/billing/account'))) {
        $code = 0
        try {
            $r = Invoke-WebRequest -Uri "$ApiUrl$($check[1])" -UseBasicParsing -Headers @{ Authorization = "Bearer $accessToken" } -ErrorAction Stop
            $code = [int]$r.StatusCode
        }
        catch { if ($null -ne $_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode } }
        if ($code -eq 200) { Write-Host ("  [PASS] {0} ({1}) - 200 OK" -f $check[0], $check[1]) }
        else { Write-Host ("  [FAIL] {0} ({1}) - got {2}" -f $check[0], $check[1], $code); $script:failed++ }
    }
}

Write-Host "`n========================================================"
if ($script:failed -eq 0) {
    Write-Host ' All Smoke Tests PASSED. Deployment is healthy and responsive.'
    exit 0
}
Write-Host " $($script:failed) check(s) FAILED. Inspect the output above."
exit 1
