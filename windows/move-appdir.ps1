<#
.SYNOPSIS
    Relocate the bridge application directory off the system drive.

.DESCRIPTION
    Stops the bridge, moves %LOCALAPPDATA%\CodexFeishuBridge to the directory
    Get-BridgeAppDir resolves (D:\Codex\CodexFeishuBridge), starts the bridge
    from there and prints a verification block. Config, DPAPI secrets, SQLite
    state, logs and downloaded images all travel with the directory, so
    pairing and thread history survive the move.

    Run it in a plain terminal, not from inside a Feishu task: stopping the
    bridge tears down the Codex app-server tree that a task runs in.
#>
[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\appdir.ps1')

$legacyDir = Join-Path $env:LOCALAPPDATA 'CodexFeishuBridge'

# A shell spawned by the running bridge inherits its CODEX_FEISHU_BRIDGE_HOME,
# which still points at the old location. Only that self-referential value is
# dropped; an override aimed anywhere else is honoured as usual.
if ($env:CODEX_FEISHU_BRIDGE_HOME -eq $legacyDir) {
    $env:CODEX_FEISHU_BRIDGE_HOME = $null
}
$targetDir = Get-BridgeAppDir

$moved = $false
if ($targetDir -eq $legacyDir) {
    Write-Host "The app dir already resolves to $targetDir; nothing to move."
} elseif (-not (Test-Path -LiteralPath $legacyDir)) {
    Write-Host "Nothing to move: $legacyDir does not exist."
} else {
    if (Test-Path -LiteralPath $targetDir) {
        # An empty leftover (a stray probe of the new location) is safe to
        # clear; anything holding files is not ours to delete.
        if (@(Get-ChildItem -LiteralPath $targetDir -Force).Count -eq 0) {
            Remove-Item -LiteralPath $targetDir
            Write-Host "Cleared the empty leftover $targetDir"
        } else {
            throw "$targetDir already exists. Merge or remove it, then rerun this script."
        }
    }

    # Create the destination parent while the bridge is still up, so a bad
    # drive fails loudly instead of leaving the bridge stopped.
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $targetDir) | Out-Null

    # The pid file lives in the old directory, so the pid is read straight from
    # there; stop.ps1 now resolves the new location and would miss it.
    $legacyPidFile = Join-Path $legacyDir 'bridge.pid'
    if (Test-Path -LiteralPath $legacyPidFile) {
        $bridgePid = (Get-Content -LiteralPath $legacyPidFile -Raw).Trim()
        if ($bridgePid -and (Get-Process -Id $bridgePid -ErrorAction SilentlyContinue)) {
            Write-Host "Stopping bridge pid $bridgePid (including the app-server tree)"
            & taskkill /PID $bridgePid /T /F | Out-Null
            for ($i = 0; $i -lt 30; $i++) {
                if (-not (Get-Process -Id $bridgePid -ErrorAction SilentlyContinue)) { break }
                Start-Sleep -Milliseconds 200
            }
            if (Get-Process -Id $bridgePid -ErrorAction SilentlyContinue) {
                throw "Bridge pid $bridgePid is still alive; stop it by hand before moving."
            }
        }
    }

    try {
        Move-Item -LiteralPath $legacyDir -Destination $targetDir
        $moved = $true
        Write-Host "Moved $legacyDir -> $targetDir"
    } catch {
        # Never leave the bridge down: bring it back up where the files still are.
        Write-Warning "Move failed: $_"
        $env:CODEX_FEISHU_BRIDGE_HOME = $legacyDir
    }
}

Write-Host 'Starting the bridge from the resolved app dir'
try {
    & (Join-Path $PSScriptRoot 'start.ps1') -TimeoutSeconds $TimeoutSeconds
} catch {
    Write-Warning "start.ps1 failed: $_"
}

# --- verification -----------------------------------------------------
Write-Host ''
Write-Host 'Verification'
$configPath = Join-Path $targetDir 'config.json'
$port = 49660
if (Test-Path -LiteralPath $configPath) {
    $port = (Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json).health_port
}

$problems = @()
$pidFile = Join-Path $targetDir 'bridge.pid'
$bridgePid = if (Test-Path -LiteralPath $pidFile) { (Get-Content -LiteralPath $pidFile -Raw).Trim() } else { $null }
if (-not $bridgePid -or -not (Get-Process -Id $bridgePid -ErrorAction SilentlyContinue)) {
    $problems += "no live bridge pid in $pidFile"
}

$healthy = $false
try {
    $healthy = [bool](Invoke-RestMethod -Uri "http://127.0.0.1:$port/" -TimeoutSec 5).ok
} catch { }
if (-not $healthy) { $problems += "health endpoint on port $port did not report ok" }

$legacyLeft = Test-Path -LiteralPath $legacyDir
if ($legacyLeft) { $problems += "$legacyDir still exists" }

$inbox = Join-Path $targetDir 'inbox'
$inboxFiles = @(Get-ChildItem -LiteralPath $inbox -File -ErrorAction SilentlyContinue).Count
$artifacts = Join-Path $targetDir 'artifacts'
$artifactFiles = @(Get-ChildItem -LiteralPath $artifacts -File -ErrorAction SilentlyContinue).Count

Write-Host ("  app dir    : {0}" -f $targetDir)
Write-Host ("  bridge pid : {0}" -f $(if ($bridgePid) { $bridgePid } else { 'none' }))
Write-Host ("  health     : {0}" -f $(if ($healthy) { "ok (127.0.0.1:$port)" } else { 'NOT OK' }))
Write-Host ("  inbox      : {0} file(s)" -f $inboxFiles)
Write-Host ("  artifacts  : {0} file(s)" -f $artifactFiles)
Write-Host ("  C: leftover: {0}" -f $(if ($legacyLeft) { $legacyDir } else { 'none' }))

if ($problems.Count) {
    Write-Host ''
    foreach ($problem in $problems) { Write-Warning $problem }
    exit 1
}
Write-Host ''
Write-Host $(if ($moved) { 'All checks passed.' } else { 'All checks passed (nothing was moved).' })
exit 0
