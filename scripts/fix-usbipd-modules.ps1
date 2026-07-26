<#
.SYNOPSIS
    Installs the USB/IP kernel modules from the custom WSL2 kernel build
    into the docker-desktop distro so that usbipd-win can attach USB devices.

.DESCRIPTION
    Run this ONCE after build-wsl2-optical-kernel.ps1 completes.
    No kernel recompilation - just copies already-built .ko files to the right place.

    Run as Administrator (required for usbipd-win attach at the end).
#>

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

$KernelOutputDir = "$env:USERPROFILE\wsl2-kernel"
$ModulesStaging  = "$KernelOutputDir\modules"   # Windows staging area for modules
$BuildDistro     = "Ubuntu"

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  Fix: Install USB/IP Modules for usbipd-win" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# --------------------------------------------------------------------------
# Check the Ubuntu build distro and build directory exist
# --------------------------------------------------------------------------
$distroCheck = wsl -d $BuildDistro -u root sh -c "test -d ~/WSL2-Linux-Kernel-6.6 && echo yes || test -d ~/WSL2-Linux-Kernel && echo yes || echo no" 2>&1
if ($distroCheck -ne "yes") {
    Write-Host "ERROR: Build directory ~/WSL2-Linux-Kernel not found in $BuildDistro." -ForegroundColor Red
    Write-Host "Run build-wsl2-optical-kernel.ps1 first." -ForegroundColor Yellow
    exit 1
}

New-Item -ItemType Directory -Path $ModulesStaging -Force | Out-Null

# --------------------------------------------------------------------------
# Step 1: Run modules_install in Ubuntu
# (no recompilation - just copies already-built .ko files to a staging area)
# --------------------------------------------------------------------------
Write-Host "=== Step 1: Installing kernel modules (Ubuntu -> Windows staging) ===" -ForegroundColor Cyan
Write-Host "This takes about 1-2 minutes (no recompilation)..." -ForegroundColor Gray

$installSh = @'
#!/bin/sh
set -e
if [ -d ~/WSL2-Linux-Kernel-6.6 ]; then
    cd ~/WSL2-Linux-Kernel-6.6
elif [ -d ~/WSL2-Linux-Kernel ]; then
    cd ~/WSL2-Linux-Kernel
else
    echo "ERROR: Build directory not found. Run build-wsl2-optical-kernel.ps1 first."; exit 1
fi

KVER=$(make -s kernelrelease)
echo "Kernel: $KVER"

# Install to temp, then copy to Windows
rm -rf /tmp/km_stage
make modules_install INSTALL_MOD_PATH=/tmp/km_stage INSTALL_MOD_STRIP=1 2>&1 | tail -3

# Copy full module tree to Windows staging dir
mkdir -p "$1/$KVER"
cp -a /tmp/km_stage/lib/modules/$KVER/. "$1/$KVER/"
echo "$KVER" > "$1/kernel_version.txt"
echo "done:$KVER"
'@

$tmpSh = "$env:TEMP\install-modules.sh"
$installShLF = $installSh.Replace("`r`n", "`n").TrimStart()
[System.IO.File]::WriteAllText($tmpSh, $installShLF, [System.Text.UTF8Encoding]::new($false))

$wslTmpSh  = (wsl -d $BuildDistro -u root wslpath -u ($tmpSh.Replace('\', '/'))).Trim()
$wslStage  = (wsl -d $BuildDistro -u root wslpath -u ($ModulesStaging.Replace('\', '/'))).Trim()

$result = wsl -d $BuildDistro -u root sh "$wslTmpSh" "$wslStage" 2>&1
# Handle both possible directory names
$result = $result -replace 'WSL2-Linux-Kernel\b','WSL2-Linux-Kernel-6.6'
Write-Host $result
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: modules_install failed." -ForegroundColor Red
    exit 1
}

$kernelVer = (Get-Content "$ModulesStaging\kernel_version.txt" -ErrorAction Stop).Trim()
Write-Host "Modules staged for kernel: $kernelVer" -ForegroundColor Green

# --------------------------------------------------------------------------
# Step 2: Install modules directly into Ubuntu's /lib/modules/ (PERSISTENT)
# docker-desktop's filesystem resets on every WSL shutdown; Ubuntu's does not.
# usbipd will use Ubuntu to load vhci_hcd, which works for all WSL2 distros.
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Step 2: Installing modules into Ubuntu /lib/modules/ ==" -ForegroundColor Cyan
Write-Host "(This makes the installation permanent across reboots)" -ForegroundColor Gray

$ubuntuStagePath = (wsl -d $BuildDistro -u root wslpath -u ($ModulesStaging.Replace('\','/'))).Trim()
$kernelVer = (Get-Content "$ModulesStaging\kernel_version.txt" -ErrorAction Stop).Trim()

wsl -d $BuildDistro -u root sh -c @"
set -e
SRC='$ubuntuStagePath/$kernelVer'
DST='/lib/modules/$kernelVer'
echo \"Source: \$SRC\"
echo \"Dest:   \$DST\"
mkdir -p \"\$DST\"
cp -rn \"\$SRC/.\" \"\$DST/\"
depmod -a '$kernelVer'
echo \"Modules installed OK\"
modprobe vhci_hcd && echo \"vhci_hcd loaded OK\" || echo \"vhci_hcd load check: see note below\"
"@

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to install modules to Ubuntu." -ForegroundColor Red
    exit 1
}
Write-Host "Modules installed into Ubuntu." -ForegroundColor Green

# --------------------------------------------------------------------------
# Step 3: Skip docker-desktop (ephemeral) - modules are now in Ubuntu
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Step 3: Verifying vhci_hcd in Ubuntu ==" -ForegroundColor Cyan
$srMod = wsl -d $BuildDistro -u root sh -c "modprobe vhci_hcd 2>&1 && echo OK || grep vhci_hcd /lib/modules/$kernelVer/modules.builtin 2>/dev/null | head -1 || echo FAIL"
Write-Host "vhci_hcd status: $srMod" -ForegroundColor $(if ($srMod -match 'OK|vhci') {'Green'} else {'Yellow'})

Write-Host "Modules installed." -ForegroundColor Green

# --------------------------------------------------------------------------
# Step 4: Test vhci_hcd load
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Step 4: Testing vhci_hcd module ===" -ForegroundColor Cyan

$testResult = wsl -d $BuildDistro sh -c "modprobe vhci_hcd 2>&1 && echo 'OK' || echo 'FAIL'"
Write-Host "modprobe vhci_hcd: $testResult"

if ($testResult -match "OK") {
    Write-Host "vhci_hcd loaded successfully." -ForegroundColor Green
} else {
    Write-Host "WARNING: vhci_hcd load returned non-OK." -ForegroundColor Yellow
    Write-Host "Checking modules.builtin..." -ForegroundColor Gray
    wsl -d $BuildDistro sh -c "grep vhci /lib/modules/$kernelVer/modules.builtin 2>/dev/null && echo '(built-in - OK)' || echo 'Not found'"
}

# --------------------------------------------------------------------------
# Step 5: Attach USB drive(s) via usbipd
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Step 5: Attaching USB optical drive(s) ===" -ForegroundColor Cyan

$usbipd = "C:\Program Files\usbipd-win\usbipd.exe"
if (-not (Test-Path $usbipd)) {
    Write-Host "usbipd not found at $usbipd" -ForegroundColor Red
    exit 1
}

$deviceList = & $usbipd list 2>&1
Write-Host $deviceList

# Find optical drives (Shared state = bound but not yet attached)
$opticalLines = $deviceList |
    Where-Object { $_ -match 'CD|DVD|Blu.?ray|BD|ROM|Optical|152d|PIONEER|LITEON|SAMSUNG.*ROM|MATSHITA|HLDTST' }

if (-not $opticalLines) {
    Write-Host "No optical drives auto-detected. Devices with 'Shared' state that may be your drive:" -ForegroundColor Yellow
    $deviceList | Where-Object { $_ -match 'Shared' }
    $manualId = Read-Host "Enter BUSID of your DVD drive (e.g. 2-1)"
    if ($manualId) { $opticalLines = @($manualId) }
}

foreach ($line in $opticalLines) {
    $busId = ($line -split '\s+')[0].Trim()
    if (-not $busId -or $busId -notmatch '^\d+-\d+') { continue }

    Write-Host "Attaching BUSID $busId..." -ForegroundColor Cyan
    $attachOut = & $usbipd attach --wsl --busid $busId 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  FAIL: $attachOut" -ForegroundColor Red
    } else {
        Write-Host "  Attached OK" -ForegroundColor Green
    }
}

# --------------------------------------------------------------------------
# Verify
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Verification ===" -ForegroundColor Cyan
Start-Sleep -Seconds 3
Start-Sleep -Seconds 3
$srDevs = wsl -d $BuildDistro sh -c "ls /dev/sr* 2>/dev/null || echo 'none'"
Write-Host "Optical devices in Ubuntu: $srDevs"

Write-Host ""
if ($srDevs -match '/dev/sr') {
    Write-Host "SUCCESS: Drive(s) visible as /dev/sr*" -ForegroundColor Green
    Write-Host "Restart the ARM container:" -ForegroundColor Cyan
    Write-Host "  cd C:\Dev\BackupMyMedia\ripping-machine" -ForegroundColor White
    Write-Host "  docker compose up -d" -ForegroundColor White
} else {
    Write-Host "Drives not yet visible. usbipd state:" -ForegroundColor Yellow
    & $usbipd list
}
