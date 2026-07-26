<#
.SYNOPSIS
    Registers setup-usb-drives.ps1 as a Task Scheduler task that runs automatically
    at logon (with a 45-second delay to allow Docker Desktop and WSL2 to start first).

.NOTES
    Run this ONCE as Administrator. After that, the task runs automatically on every logon.
#>

#Requires -RunAsAdministrator

$TaskName    = "ARM-USB-Drive-Setup"
$ScriptPath  = "C:\Dev\BackupMyMedia\scripts\setup-usb-drives.ps1"
$Description = "Attaches USB optical drives to WSL2 for the ARM ripping container"

$SyncTaskName   = "ARM-Auto-Sync"
$SyncScriptPath = "C:\Dev\BackupMyMedia\scripts\transfer\watch-and-sync.ps1"
$SyncDescription = "Watches for completed ARM rips and auto-syncs them to Chrisdesktop"

# Remove existing task if present
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# Build the action: run PowerShell hidden, bypass execution policy
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -NonInteractive -WindowStyle Hidden -File `"$ScriptPath`""

# Trigger: at logon of any user, with a 45-second delay
# (allows Docker Desktop + WSL2 to initialise before the script runs)
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = "PT45S"   # ISO 8601: 45 seconds

# Settings: run with highest privileges, allow running on battery, etc.
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

# Principal: run as current user with highest privileges (UAC elevation)
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Highest

$task = Register-ScheduledTask `
    -TaskName    $TaskName `
    -Action      $action `
    -Trigger     $trigger `
    -Settings    $settings `
    -Principal   $principal `
    -Description $Description `
    -Force

# --------------------------------------------------------------------------
# Task 2: ARM-Auto-Sync
# Watches completed/ folder and syncs finished rips to Chrisdesktop silently
# Does NOT need admin - runs as current user, starts at logon with no delay
# --------------------------------------------------------------------------
Unregister-ScheduledTask -TaskName $SyncTaskName -Confirm:$false -ErrorAction SilentlyContinue

$syncAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -NonInteractive -WindowStyle Hidden -File `"$SyncScriptPath`""

$syncTrigger = New-ScheduledTaskTrigger -AtLogOn
# No delay needed - this just watches the filesystem, doesn't depend on Docker

$syncSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$syncPrincipal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited    # Does NOT need admin

Register-ScheduledTask `
    -TaskName    $SyncTaskName `
    -Action      $syncAction `
    -Trigger     $syncTrigger `
    -Settings    $syncSettings `
    -Principal   $syncPrincipal `
    -Description $SyncDescription `
    -Force | Out-Null

Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Task Scheduler entries registered!" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  $TaskName     - USB drives attached at logon +45s" -ForegroundColor Cyan
Write-Host "  $SyncTaskName - Auto-sync rips to Chrisdesktop (background)" -ForegroundColor Cyan
Write-Host ""
Write-Host "From now on, after every Windows restart:" -ForegroundColor Yellow
Write-Host "  1. Log in normally" -ForegroundColor White
Write-Host "  2. Wait ~60 seconds for Docker Desktop + drives to initialise" -ForegroundColor White
Write-Host "  3. Insert a disc - ARM rips it, then auto-syncs to Chrisdesktop" -ForegroundColor White
Write-Host ""

# Verify
Get-ScheduledTask | Where-Object { $_.TaskName -match 'ARM' } | Select-Object TaskName, State | Format-Table -AutoSize
