<#
.SYNOPSIS
    Register the logon + keep-alive scheduled task for the bridge.

.DESCRIPTION
    One task does both jobs:
      * at logon (after a short delay, so the network is up) it starts the
        bridge;
      * every five minutes it re-runs ensure-running.ps1, which is a no-op
        while the bridge is healthy and restarts it after a crash.

    Runs as the current user with limited rights, so it needs no elevation.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'CodexFeishuBridge',
    [int]$RepeatMinutes = 5
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$ensure = Join-Path $PSScriptRoot 'ensure-running.ps1'
if (-not (Test-Path $ensure)) {
    throw "ensure-running.ps1 not found next to this script"
}

$shell = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $shell) { $shell = (Get-Command powershell).Source }

$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$atLogon.Delay = 'PT1M'

$keepAlive = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes)

$action = New-ScheduledTaskAction `
    -Execute $shell `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -File `"$ensure`"" `
    -WorkingDirectory $root

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Trigger $atLogon, $keepAlive `
    -Action $action `
    -Settings $settings `
    -Principal $principal `
    -Description 'Start the Codex Feishu Bridge at logon and keep it running.' `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName'."
$task = Get-ScheduledTask -TaskName $TaskName
foreach ($trigger in $task.Triggers) {
    $repeat = if ($trigger.Repetition.Interval) { " every $($trigger.Repetition.Interval)" } else { '' }
    Write-Host ("  trigger: {0}{1}" -f $trigger.CimClass.CimClassName, $repeat)
}
Write-Host ''
Write-Host 'Verify now with:  Start-ScheduledTask -TaskName CodexFeishuBridge'
Write-Host 'Remove later with: .\windows\uninstall-autostart.ps1'
