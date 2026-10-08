<#
.SYNOPSIS
    Register the keep-alive scheduled task for the bridge.

.DESCRIPTION
    One task, one repeating trigger. It re-runs ensure-running.ps1 every few
    minutes; that script starts the bridge only while the Codex desktop app is
    open, is a no-op while the bridge is healthy, and restarts it after a
    crash. There is deliberately no logon trigger: the bridge is wanted when
    the operator opens Codex, not whenever the machine boots.

    Runs as the current user with limited rights, so it needs no elevation.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'CodexFeishuBridge',
    [int]$RepeatMinutes = 2
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$ensure = Join-Path $PSScriptRoot 'ensure-running.ps1'
if (-not (Test-Path $ensure)) {
    throw "ensure-running.ps1 not found next to this script"
}

$shell = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $shell) { $shell = (Get-Command powershell).Source }

$vbs = Join-Path $PSScriptRoot 'run-hidden.vbs'
if (-not (Test-Path $vbs)) {
    throw "run-hidden.vbs not found next to this script"
}
$wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'

$keepAlive = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes)

# Launch through wscript.exe, not pwsh.exe directly: Task Scheduler gives a
# console application a console window, and `-WindowStyle Hidden` only hides it
# after the fact — that flash is what this indirection removes.
$action = New-ScheduledTaskAction `
    -Execute $wscript `
    -Argument "//nologo `"$vbs`" `"$shell`" `"$ensure`"" `
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
    -Trigger $keepAlive `
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
Write-Host 'The task starts the bridge only while the Codex desktop app is open.'
Write-Host ''
Write-Host 'Verify now with:  Start-ScheduledTask -TaskName CodexFeishuBridge'
Write-Host 'Remove later with: .\windows\uninstall-autostart.ps1'
