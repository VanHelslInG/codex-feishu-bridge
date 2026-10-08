<#
.SYNOPSIS
    Print the current pairing code.

    Safe to run whether or not the bridge is up: the code is created on demand
    and stored in SQLite, so the running bridge and this script always agree.
    Pairing rotates the code immediately, so each code is single use.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    throw 'Virtualenv missing. Run .\install.ps1 first.'
}

$appDir = Join-Path $env:LOCALAPPDATA 'CodexFeishuBridge'
$env:PYTHONPATH = Join-Path $root 'src'
$env:CODEX_FEISHU_BRIDGE_HOME = $appDir

$code = & $venvPython -m feishu_bridge.bridge pair-code
Write-Host ''
Write-Host "配对码：$code"
Write-Host ''
Write-Host '在飞书里发送：'
Write-Host "  /bind $code"
Write-Host ''
Write-Host '应用已开通「获取群组中所有消息」时群里直接发即可；'
Write-Host '没开通该权限时飞书只会把 @机器人 的消息推给 Bridge，那就发：'
Write-Host "  @机器人 /bind $code"
Write-Host ''
Write-Host '配对成功后这个码会立刻失效。'
