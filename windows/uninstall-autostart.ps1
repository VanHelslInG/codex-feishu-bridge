<#
.SYNOPSIS
    Remove the bridge's scheduled task (does not stop a running bridge).
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'CodexFeishuBridge'
)

$ErrorActionPreference = 'Stop'
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
} else {
    Write-Host "Scheduled task '$TaskName' is not registered."
}
