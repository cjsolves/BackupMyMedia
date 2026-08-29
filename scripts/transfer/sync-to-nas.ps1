<#
.SYNOPSIS
    Legacy wrapper for NAS transfer.

.NOTES
    This script used to bypass Tdarr by moving video directly from Lossless.
    It now delegates to process-and-move-to-nas.ps1 so there is only one
    safe transfer path for both video and music.
#>

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Target = Join-Path $ScriptDir "process-and-move-to-nas.ps1"

Write-Host "sync-to-nas.ps1 is deprecated; delegating to process-and-move-to-nas.ps1"
& $Target
