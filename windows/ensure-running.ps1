<#
.SYNOPSIS
    Make sure the bridge is up, but only while the Codex desktop app is open.

    Driven by a scheduled task (see install-autostart.ps1). Each run answers the
    cheapest question first:

      1. Is the Codex desktop app running?  If not, do nothing at all — the
         bridge is only wanted while the user is actually using Codex.
      2. Is something listening on the health port?  A TCP connect is
         milliseconds; an HTTP GET against a closed port used to burn 2.2 s.
      3. Is the health payload healthy?

    Run it by hand with -IgnoreCodexApp when you want the bridge up regardless.
#>
[CmdletBinding()]
param(
    [int]$StartupGraceMinutes = 3,
    [switch]$IgnoreCodexApp,
    # Overridable so the "app is closed" branch is testable without closing it.
    [string]$CodexProcessName = 'ChatGPT'
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'lib\appdir.ps1')
$appDir = Get-BridgeAppDir
$configPath = Join-Path $appDir 'config.json'
$pidFile = Join-Path $appDir 'bridge.pid'

$port = 49660
if (Test-Path $configPath) {
    try {
        # -ErrorAction Stop so an unreadable file is caught here instead of
        # spilling an error record into the task log on every scan.
        $port = (Get-Content $configPath -Raw -ErrorAction Stop | ConvertFrom-Json).health_port
    } catch {
        Write-Warning "Could not read the health port from ${configPath}; using $port"
    }
}

function Test-CodexAppRunning {
    <#
        The desktop app is an MSIX package whose shell is ChatGPT.exe under
        ...\WindowsApps\OpenAI.Codex_*. Match on the package path: a bridge
        task must not be started by some unrelated ChatGPT window, and the
        bridge's own codex.exe child must never count as "the app is open".
    #>
    foreach ($process in @(Get-Process -Name $CodexProcessName -ErrorAction SilentlyContinue)) {
        try {
            if ($process.Path -and $process.Path -like '*OpenAI.Codex*') {
                return $true
            }
        } catch {
            # An unreadable path is not evidence either way; keep looking.
        }
    }
    return $false
}

function Test-PortOpen([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(500)) { return $false }
        $client.EndConnect($pending)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

if (-not $IgnoreCodexApp -and -not (Test-CodexAppRunning)) {
    Write-Host 'The Codex desktop app is not running; leaving the bridge alone.'
    exit 0
}

if (Test-PortOpen $port) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/" -TimeoutSec 5
        if ($health.ok) {
            exit 0
        }
        Write-Host "Health endpoint answered but the app-server is not alive: $($health | ConvertTo-Json -Compress)"
    } catch {
        Write-Host "Port $port is open but the health endpoint did not answer."
    }
} else {
    Write-Host "Nothing is listening on port $port."
}

# A process that is still inside its startup window may simply be coming up.
# Killing it here would turn every scan into a start/stop/start churn.
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
