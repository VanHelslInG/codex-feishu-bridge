<#
.SYNOPSIS
    Make sure the bridge is up: no-op when it is healthy, restart it otherwise.

    Designed to be driven by a scheduled task (see install-autostart.ps1), but
    safe to run by hand at any time.
#>
[CmdletBinding()]
param(
    [int]$StartupGraceMinutes = 3
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'lib\appdir.ps1')
$appDir = Get-BridgeAppDir
$configPath = Join-Path $appDir 'config.json'
$pidFile = Join-Path $appDir 'bridge.pid'

$port = 49660
if (Test-Path $configPath) {
    try { $port = (Get-Content $configPath -Raw | ConvertFrom-Json).health_port } catch { }
}

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/" -TimeoutSec 5
    if ($health.ok) {
        exit 0
    }
    Write-Host "Health endpoint answered but the app-server is not alive: $($health | ConvertTo-Json -Compress)"
} catch {
    Write-Host "Health endpoint did not answer on port $port."
}

# A process that is still inside its startup window may simply be coming up.
# Killing it here would turn every logon into a start/stop/start churn.
if (Test-Path $pidFile) {
    $bridgePid = Get-Content $pidFile -ErrorAction SilentlyContinue
    $process = if ($bridgePid) { Get-Process -Id $bridgePid -ErrorAction SilentlyContinue } else { $null }
    if ($process -and ((Get-Date) - $process.StartTime).TotalMinutes -lt $StartupGraceMinutes) {
        Write-Host "Bridge pid $bridgePid started less than $StartupGraceMinutes minute(s) ago; leaving it alone."
        exit 0
    }
}

Write-Host 'Restarting the bridge.'
& (Join-Path $PSScriptRoot 'stop.ps1') | Out-Null
& (Join-Path $PSScriptRoot 'start.ps1') -TimeoutSeconds 90
exit $LASTEXITCODE
