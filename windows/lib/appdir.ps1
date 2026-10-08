<#
.SYNOPSIS
    Resolve the bridge application directory.

.DESCRIPTION
    Mirrors app_dir() in src/feishu_bridge/core/config.py: an explicit
    CODEX_FEISHU_BRIDGE_HOME wins, otherwise state, logs and downloaded
    images live in D:\Codex\CodexFeishuBridge so nothing lands on the system
    drive, and %LOCALAPPDATA% is the fallback when there is no D: drive.
#>
function Get-BridgeAppDir {
    [CmdletBinding()]
    param()

    if ($env:CODEX_FEISHU_BRIDGE_HOME) {
        return $env:CODEX_FEISHU_BRIDGE_HOME
    }
    if (Test-Path -LiteralPath 'D:\') {
        return 'D:\Codex\CodexFeishuBridge'
    }
    if ($env:LOCALAPPDATA) {
        return (Join-Path $env:LOCALAPPDATA 'CodexFeishuBridge')
    }
    return (Join-Path $HOME 'AppData\Local\CodexFeishuBridge')
}
