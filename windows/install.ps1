<#
.SYNOPSIS
    Prepare the Codex Feishu Bridge on Windows: virtualenv, dependencies,
    config file and credentials.

.EXAMPLE
    .\install.ps1 -AppId cli_xxx -AppSecret yyy
    .\install.ps1 -Prompt          # hidden interactive credential entry
#>
[CmdletBinding()]
param(
    [string]$AppId,
    [string]$AppSecret,
    [switch]$Prompt,
    [string]$CodexPath,
    [int]$HealthPort = 49660
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'

# The system "python" on this machine is the Microsoft Store placeholder
# (exit code 9009), so we pin the interpreter that ships with Codex.
$basePython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not (Test-Path $basePython)) {
    throw "Codex runtime python not found at $basePython. Install the Codex desktop app or pass a python explicitly."
}

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating virtualenv at $venv"
    & $basePython -m venv $venv
}

Write-Host 'Installing dependencies'
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r (Join-Path $root 'requirements.txt')

$appDir = Join-Path $env:LOCALAPPDATA 'CodexFeishuBridge'
New-Item -ItemType Directory -Force -Path $appDir | Out-Null
$configPath = Join-Path $appDir 'config.json'

if (-not (Test-Path $configPath)) {
    Write-Host "Writing $configPath from config.example.json"
    $config = Get-Content (Join-Path $root 'config.example.json') -Raw | ConvertFrom-Json
    $config.health_port = $HealthPort
    if ($CodexPath) { $config.codex_path = $CodexPath }
    $config | ConvertTo-Json -Depth 10 | Set-Content -Path $configPath -Encoding UTF8
} else {
    Write-Host "Keeping the existing $configPath"
}

if ($Prompt -and -not $AppId) {
    $secure = Read-Host -Prompt 'Feishu App ID' -AsSecureString
    $AppId = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    $secure = Read-Host -Prompt 'Feishu App Secret' -AsSecureString
    $AppSecret = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}

if ($AppId -and $AppSecret) {
    Write-Host 'Storing credentials in the DPAPI vault'
    $env:PYTHONPATH = Join-Path $root 'src'
    $env:BRIDGE_APP_ID = $AppId
    $env:BRIDGE_APP_SECRET = $AppSecret
    & $venvPython -c @"
import os
from feishu_bridge.core.config import app_dir, load_config
from feishu_bridge.platform.windows import WindowsPlatform

config = load_config(app_dir() / 'config.json')
platform = WindowsPlatform()
service = config['credential_service']
accounts = config['credential_accounts']
platform.secret_set(service, accounts['app_id'], os.environ['BRIDGE_APP_ID'])
platform.secret_set(service, accounts['app_secret'], os.environ['BRIDGE_APP_SECRET'])
print('credentials stored')
"@
    Remove-Item Env:\BRIDGE_APP_ID, Env:\BRIDGE_APP_SECRET -ErrorAction SilentlyContinue
}

Write-Host ''
Write-Host "Installed. Config: $configPath"
Write-Host "Next: .\start.ps1  (then send /bind <pair-code> in Feishu)"
Write-Host 'The pairing code is printed to the bridge log on startup:'
Write-Host "  $(Join-Path $appDir 'bridge.log')"
