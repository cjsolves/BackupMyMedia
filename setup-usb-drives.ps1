<#
.SYNOPSIS
    Attaches USB optical drives (DVD/Blu-ray) to WSL2 so ARM Docker can see them.

.DESCRIPTION
    ARM requires /dev/sr0, /dev/sr1 etc. inside its container.
    On Windows, USB drives must be forwarded into WSL2 using usbipd-win before
    Docker can pass them through via --device.

    Run this script as Administrator after each Windows restart,
    BEFORE running "docker compose up" in ripping-machine/.

.NOTES
    Prerequisite: usbipd-win
    Install: winget install --interactive --exact dorssel.usbipd-win
    Docs:    https://github.com/dorssel/usbipd-win
#>

#Requires -RunAsAdministrator

$ErrorActionPreference = "Continue"

# --------------------------------------------------------------------------
# Check usbipd-win is installed
# --------------------------------------------------------------------------
if (-not (Get-Command usbipd -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "ERROR: usbipd-win is not installed." -ForegroundColor Red
    Write-Host ""
    Write-Host "Install it now with:" -ForegroundColor Yellow
    Write-Host "    winget install --interactive --exact dorssel.usbipd-win" -ForegroundColor White
    Write-Host ""
    Write-Host "Or download from: https://github.com/dorssel/usbipd-win/releases" -ForegroundColor White
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  ARM USB Drive Setup" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# --------------------------------------------------------------------------
# Ensure Ubuntu WSL2 is running - usbipd needs it active to load vhci_hcd
# --------------------------------------------------------------------------
Write-Host "Ensuring Ubuntu WSL2 is running..." -ForegroundColor Yellow
$running = (wsl --list --running 2>&1) -replace '[\x00]','' | Where-Object { $_ -match 'Ubuntu' }
if (-not $running) {
    Write-Host "  Starting Ubuntu..." -ForegroundColor Gray
    # Start Ubuntu in background; sleep keeps it alive through the attach process
    Start-Process -FilePath "wsl.exe" -ArgumentList "-d Ubuntu sh -c `"sleep 300`"" -WindowStyle Hidden
    Start-Sleep -Seconds 5
    Write-Host "  Ubuntu started." -ForegroundColor Green
} else {
    Write-Host "  Ubuntu already running." -ForegroundColor Green
}
Write-Host ""

# --------------------------------------------------------------------------
# List all USB devices
# --------------------------------------------------------------------------
Write-Host "All attached USB devices:" -ForegroundColor Yellow
$rawList = usbipd list 2>&1
Write-Host ($rawList | Out-String) -ForegroundColor Gray

# --------------------------------------------------------------------------
# Identify optical drives via Windows PnP device tree (most reliable).
# usbipd shows ALL drives as "USB Mass Storage Device", so name-matching
# doesn't work. Instead: walk CDROM -> USBSTOR -> USB\VID_&PID_ parent
# and match the VID:PID against the usbipd list.
# --------------------------------------------------------------------------
Write-Host "Identifying optical drives via Windows device tree..." -ForegroundColor Gray
$opticalVidPids = @()
try {
    $cdromDevices = Get-PnpDevice -Class CDROM -ErrorAction SilentlyContinue |
        Where-Object { $_.Present -eq $true }
    foreach ($cd in $cdromDevices) {
        $instanceId = $cd.InstanceId
        for ($depth = 0; $depth -lt 4; $depth++) {
            $parentProp = Get-PnpDeviceProperty -InstanceId $instanceId `
                -KeyName 'DEVPKEY_Device_Parent' -ErrorAction SilentlyContinue
            if (-not $parentProp) { break }
            $instanceId = $parentProp.Data
            if ($instanceId -match 'USB\\VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})') {
                $vidHex = $Matches[1].ToLower()
                $pidHex = $Matches[2].ToLower()
                $opticalVidPids += "${vidHex}:${pidHex}"
                Write-Host "  Optical: $($cd.FriendlyName)  -->  $vidHex`:$pidHex" -ForegroundColor Green
                break
            }
        }
    }
} catch {
    Write-Host "  (PnP device tree query unavailable: $_)" -ForegroundColor Gray
}

$deviceLines = $rawList | Where-Object { $_ -match '^\s*\d+-\d+' } | ForEach-Object { $_.Trim() }

# Match optical VID:PIDs against usbipd output
$busIds = @()
if ($opticalVidPids.Count -gt 0) {
    foreach ($line in $deviceLines) {
        foreach ($vidpid in $opticalVidPids) {
            if ($line -match [regex]::Escape($vidpid)) {
                $busId = ($line -split '\s+')[0]
                if ($busId -match '^\d+-\d+' -and $busIds -notcontains $busId) {
                    $busIds += $busId
                }
            }
        }
    }
}

# Fallback: name-based keyword match (catches drives missed by PnP walk)
if ($busIds.Count -eq 0) {
    $opticalKeywords = 'CD|DVD|Blu.?[Rr]ay|BD-?ROM|Optical|CD-?ROM|PIONEER|LG HL|SAMSUNG.*ROM|ASUS.*ROM|LITEON|TSSTcorp|MATSHITA|TEAC|PLEXTOR|BENQ|HLDS|HLDTST'
    $busIds = $deviceLines | Where-Object { $_ -match $opticalKeywords } |
        ForEach-Object { ($_ -split '\s+')[0] } | Where-Object { $_ -match '^\d+-\d+' }
}

if ($busIds.Count -eq 0) {
    Write-Host "WARNING: Could not automatically identify optical drives." -ForegroundColor Yellow
    Write-Host "         Check the device list above for your DVD/Blu-ray drives." -ForegroundColor Yellow
    Write-Host ""
    $input = Read-Host "Enter BUSID values separated by commas (e.g. 2-1,3-4), or press Enter to exit"
    if ([string]::IsNullOrWhiteSpace($input)) {
        Write-Host "No drives selected. Exiting." -ForegroundColor Red
        exit 0
    }
    $busIds = $input -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' }
}

Write-Host ""

# --------------------------------------------------------------------------
# Bind and attach each drive to WSL2
# --------------------------------------------------------------------------
$attachedCount = 0

foreach ($busId in $busIds) {
    Write-Host "--- Processing BUSID $busId ---" -ForegroundColor Cyan

    # Bind (makes the device shareable; persists across reboots once bound)
    Write-Host "  Binding..." -ForegroundColor Gray
    $bindOut = usbipd bind --busid $busId 2>&1
    if ($LASTEXITCODE -ne 0 -and ($bindOut -notmatch 'already bound|already shared')) {
        Write-Host "  WARN: $bindOut" -ForegroundColor Yellow
    } else {
        Write-Host "  Bound OK" -ForegroundColor Green
    }

    # Attach to WSL2 via Ubuntu (modules are persistent there; docker-desktop is ephemeral)
    Write-Host "  Attaching to WSL2 (via Ubuntu)..." -ForegroundColor Gray
    $attachResult = usbipd attach --wsl Ubuntu --busid $busId 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: Failed to attach BUSID $busId" -ForegroundColor Red
        Write-Host "  $attachOut" -ForegroundColor Red
    } else {
        Write-Host "  Attached OK" -ForegroundColor Green
        $attachedCount++
    }

    Write-Host ""
}

# --------------------------------------------------------------------------
# Verify inside WSL2
# --------------------------------------------------------------------------
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Verification" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Optical drives visible in WSL2 (expect /dev/sr0, /dev/sr1 etc.):" -ForegroundColor Yellow

$lsscsi = wsl lsscsi -g 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  lsscsi not found in WSL2 - install with: wsl sudo apt install lsscsi" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Checking /dev/sr* directly..." -ForegroundColor Gray
    wsl ls -la /dev/sr* 2>&1 | ForEach-Object { Write-Host "  $_" }
} else {
    $lsscsi | Where-Object { $_ -match 'cd/dvd|optical|rom' } |
        ForEach-Object { Write-Host "  $_" -ForegroundColor Green }
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan

if ($attachedCount -gt 0) {
    Write-Host "  $attachedCount drive(s) attached successfully." -ForegroundColor Green
    Write-Host ""
    Write-Host "  Start ARM:" -ForegroundColor White
    Write-Host "    cd ripping-machine" -ForegroundColor White
    Write-Host "    docker compose up -d" -ForegroundColor White
} else {
    Write-Host "  No drives were attached. Check errors above." -ForegroundColor Red
}

Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "TIP: To run this script automatically at logon:" -ForegroundColor Yellow
Write-Host "     Task Scheduler -> New Task -> Trigger: At logon" -ForegroundColor Yellow
Write-Host "     Action: powershell.exe -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -ForegroundColor Yellow
Write-Host "     Check: 'Run with highest privileges'" -ForegroundColor Yellow
Write-Host ""
