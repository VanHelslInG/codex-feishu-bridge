<#
.SYNOPSIS
    Stop the bridge and its Codex app-server child tree.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\appdir.ps1')
$appDir = Get-BridgeAppDir
$pidFile = Join-Path $appDir 'bridge.pid'

if (-not (Test-Path $pidFile)) {
    Write-Host 'No pid file; the bridge does not appear to be running.'
    exit 0
}

$bridgePid = Get-Content $pidFile -ErrorAction SilentlyContinue
if (-not $bridgePid) {
    Remove-Item -LiteralPath $pidFile
    Write-Host 'Empty pid file removed.'
    exit 0
}

$process = Get-Process -Id $bridgePid -ErrorAction SilentlyContinue
if (-not $process) {
    Remove-Item -LiteralPath $pidFile
    Write-Host "Process $bridgePid is gone; stale pid file removed."
    exit 0
}

Write-Host "Stopping bridge pid $bridgePid (including the app-server tree)"
& taskkill /PID $bridgePid /T /F | Out-Null
Remove-Item -LiteralPath $pidFile
Write-Host 'Stopped. SQLite state and artifacts were preserved.'
