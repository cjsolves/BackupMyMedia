<#
.SYNOPSIS
    Registers all ARM startup tasks with Task Scheduler and enables Docker Desktop
    auto-start so the full pipeline resumes automatically after every reboot.

    Startup sequence after reboot:
      1. [Logon]       Docker Desktop starts (registry auto-start)
      2. [Logon +45s]  ARM-USB-Drive-Setup  — attaches USB drives to WSL2 (ADMIN)
                       then restarts ARM container to see new /dev/sr* devices
      3. [Logon +0s]   ARM-Auto-Sync        — watches completed/ and syncs to Chrisdesktop

    ARM container itself restarts automatically via Docker's restart:unless-stopped policy.

.NOTES
    Run this ONCE as Administrator.
    After that, everything starts automatically on every Windows restart.
#>

#Requires -RunAsAdministrator

$RepoRoot = "C:\Dev\BackupMyMedia"

# Task definitions
$Tasks = @(
    @{
        Name        = "ARM-USB-Drive-Setup"
        Script      = "$RepoRoot\scripts\setup-usb-drives.ps1"
        Description = "Attaches USB optical drives to WSL2, restarts ARM container. ADMIN required."
        Delay       = "PT45S"    # 45s — lets Docker Desktop initialise first
        RunLevel    = "Highest"  # needs admin for usbipd bind/attach
        RestartCount = 3
        RestartMins  = 2
    },
    @{
        Name        = "ARM-Auto-Sync"
        Script      = "$RepoRoot\scripts\transfer\watch-and-sync.ps1"
        Description = "Watches completed/ rips and auto-syncs to Chrisdesktop. No admin needed."
        Delay       = "PT10S"    # 10s — just enough for filesystem to settle
        RunLevel    = "Limited"  # no admin needed
        RestartCount = 5
        RestartMins  = 1
    }
)

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  ARM Startup Task Registration" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# --------------------------------------------------------------------------
# Step 1: Enable Docker Desktop auto-start at login
# (sets HKCU registry run key — no admin needed, but we're admin so fine)
# --------------------------------------------------------------------------
Write-Host "=== Docker Desktop auto-start ==" -ForegroundColor Yellow

$dockerExe = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
if (Test-Path $dockerExe) {
    $regKey = "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
    $existing = Get-ItemProperty $regKey -Name "Docker Desktop" -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  Docker Desktop auto-start: already configured" -ForegroundColor Gray
    } else {
        Set-ItemProperty $regKey -Name "Docker Desktop" -Value "`"$dockerExe`""
        Write-Host "  Docker Desktop auto-start: enabled" -ForegroundColor Green
    }
} else {
    Write-Host "  Docker Desktop not found at $dockerExe" -ForegroundColor Yellow
}

# --------------------------------------------------------------------------
# Step 2: Register each task
# --------------------------------------------------------------------------
foreach ($t in $Tasks) {
    Write-Host ""
    Write-Host "=== Task: $($t.Name) ==" -ForegroundColor Yellow

    # Remove old registration (in case path changed)
    Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction SilentlyContinue

    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-ExecutionPolicy Bypass -NonInteractive -WindowStyle Hidden -File `"$($t.Script)`""

    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $trigger.Delay = $t.Delay

    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -RestartCount $t.RestartCount `
        -RestartInterval (New-TimeSpan -Minutes $t.RestartMins) `
        -MultipleInstances IgnoreNew

    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel $t.RunLevel

    $registered = Register-ScheduledTask `
        -TaskName    $t.Name `
        -Action      $action `
        -Trigger     $trigger `
        -Settings    $settings `
        -Principal   $principal `
        -Description $t.Description `
        -Force

    Write-Host "  Registered: $($t.Name)" -ForegroundColor Green
    Write-Host "    Script  : $($t.Script)"
    Write-Host "    Delay   : $($t.Delay) after logon"
    Write-Host "    RunLevel: $($t.RunLevel)"
    Write-Host "    Restarts: up to $($t.RestartCount)x every $($t.RestartMins) min if it fails"
}

# --------------------------------------------------------------------------
# Step 3: Verify
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  All tasks registered!" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Startup sequence after next reboot:" -ForegroundColor Cyan
Write-Host "  [Logon]      Docker Desktop auto-starts (registry run key)" -ForegroundColor White
Write-Host "  [Logon +10s] ARM-Auto-Sync starts (watches completed/ -> Chrisdesktop)" -ForegroundColor White
Write-Host "  [Logon +45s] ARM-USB-Drive-Setup runs (admin):" -ForegroundColor White
Write-Host "               1. Start Ubuntu WSL2" -ForegroundColor Gray
Write-Host "               2. Attach USB optical drives via usbipd" -ForegroundColor Gray
Write-Host "               3. Restart ARM container to see drives" -ForegroundColor Gray
Write-Host "               4. ARM web UI ready at http://localhost:8080" -ForegroundColor Gray
Write-Host ""
Write-Host "Registered tasks:" -ForegroundColor Cyan
Get-ScheduledTask | Where-Object { $_.TaskName -match 'ARM' } |
    Select-Object TaskName, State | Format-Table -AutoSize
Write-Host ""
Write-Host "To run immediately without rebooting:" -ForegroundColor Yellow
Write-Host "  Start-ScheduledTask 'ARM-Auto-Sync'" -ForegroundColor White
Write-Host "  Start-ScheduledTask 'ARM-USB-Drive-Setup'" -ForegroundColor White

