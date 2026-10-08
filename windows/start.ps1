<#
.SYNOPSIS
    Start the bridge in the background and wait for the health endpoint.
#>
[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 60
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    throw 'Virtualenv missing. Run .\install.ps1 first.'
}

$appDir = Join-Path $env:LOCALAPPDATA 'CodexFeishuBridge'
New-Item -ItemType Directory -Force -Path $appDir | Out-Null
$pidFile = Join-Path $appDir 'bridge.pid'

if (Test-Path $pidFile) {
    $existing = Get-Content $pidFile -ErrorAction SilentlyContinue
    if ($existing -and (Get-Process -Id $existing -ErrorAction SilentlyContinue)) {
        Write-Host "Bridge already running (pid $existing)"
        exit 0
    }
}

$env:PYTHONPATH = Join-Path $root 'src'
$env:CODEX_FEISHU_BRIDGE_HOME = $appDir
$stdout = Join-Path $appDir 'bridge.out.log'
$stderr = Join-Path $appDir 'bridge.err.log'

$process = Start-Process -FilePath $venvPython `
    -ArgumentList '-m', 'feishu_bridge.bridge', 'run' `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru

$process.Id | Set-Content -Path $pidFile -Encoding ASCII
Write-Host "Bridge started (pid $($process.Id))"

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    if ($process.HasExited) {
        Write-Host 'Bridge exited during startup. Last 30 log lines:'
        Get-Content (Join-Path $appDir 'bridge.log') -Tail 30 -ErrorAction SilentlyContinue
        Get-Content $stderr -Tail 30 -ErrorAction SilentlyContinue
        exit 1
    }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$((Get-Content (Join-Path $appDir 'config.json') -Raw | ConvertFrom-Json).health_port)/" -TimeoutSec 3
        if ($health.ok) {
            Write-Host 'Health check: ok'
            exit 0
        }
    } catch {
        # still starting
    }
}
Write-Host 'Health check did not become ready in time; check bridge.log'
exit 1
