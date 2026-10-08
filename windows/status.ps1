<#
.SYNOPSIS
    Report bridge health, pairing state and the Codex app-server pid.
#>
[CmdletBinding()]
param()

$appDir = Join-Path $env:LOCALAPPDATA 'CodexFeishuBridge'
$configPath = Join-Path $appDir 'config.json'
$pidFile = Join-Path $appDir 'bridge.pid'

$port = 49660
if (Test-Path $configPath) {
    $port = (Get-Content $configPath -Raw | ConvertFrom-Json).health_port
}

if (Test-Path $pidFile) {
    $bridgePid = Get-Content $pidFile
    $alive = [bool](Get-Process -Id $bridgePid -ErrorAction SilentlyContinue)
    Write-Host "pid file: $bridgePid (running: $alive)"
} else {
    Write-Host 'pid file: none'
}

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/" -TimeoutSec 5
    $health | ConvertTo-Json -Depth 5
} catch {
    Write-Host "health endpoint http://127.0.0.1:$port/ is not answering"
    exit 1
}
