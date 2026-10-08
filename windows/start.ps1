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

. (Join-Path $PSScriptRoot 'lib\appdir.ps1')
$appDir = Get-BridgeAppDir
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

# The port must be readable even when config.json has never been written: a
# missing file used to make every probe throw, which the wait loop then read as
# "still starting" until it timed out on a perfectly healthy bridge.
$configPath = Join-Path $appDir 'config.json'
$port = 49660
if (Test-Path -LiteralPath $configPath) {
    try {
        $port = (Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json).health_port
    } catch {
        Write-Warning "Could not read the health port from $configPath; using $port"
    }
}

# Preflight in the foreground so import errors (missing dependency, syntax
# error) surface as a real traceback instead of disappearing into a hidden
# process.
Write-Host 'Preflight: importing the bridge'
& $venvPython -c 'import feishu_bridge.bridge'
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Preflight failed; fix the error above before starting the daemon.'
    exit 1
}

# Deliberately launched WITHOUT -RedirectStandardOutput/-RedirectStandardError.
# .NET passes bInheritHandles=TRUE whenever it redirects, which would leak this
# shell's stdout pipe into the long-running daemon: any caller reading our
# output (a pipeline, a CI job, another tool) would block until the bridge
# exits. The bridge writes its own bridge.log, so nothing is lost.
$process = Start-Process -FilePath $venvPython `
    -ArgumentList '-m', 'feishu_bridge.bridge', 'run' `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -PassThru

$process.Id | Set-Content -Path $pidFile -Encoding ASCII
Write-Host "Bridge started (pid $($process.Id))"

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    if ($process.HasExited) {
        Write-Host 'Bridge exited during startup. Last 30 log lines:'
        Get-Content (Join-Path $appDir 'bridge.log') -Tail 30 -ErrorAction SilentlyContinue
        Write-Host ''
        Write-Host 'For a full traceback run it in the foreground:'
        Write-Host "  `$env:PYTHONPATH='$(Join-Path $root 'src')'; & '$venvPython' -m feishu_bridge.bridge run --verbose"
        exit 1
    }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/" -TimeoutSec 3
        if ($health.ok) {
            Write-Host 'Health check: ok'
            exit 0
        }
    } catch {
        # still starting
    }
}
Write-Host "Health check did not become ready in time on port $port; check bridge.log"
exit 1
