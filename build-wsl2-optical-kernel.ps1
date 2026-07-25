<#
.SYNOPSIS
    Builds a custom WSL2 kernel with USB optical drive (sr_mod/cdrom) support.
    Required because the default Microsoft WSL2 kernel does not include these modules.

.DESCRIPTION
    This is a one-time setup. Once complete, WSL2 will recognise USB DVD/Blu-ray
    drives attached via usbipd-win, and Docker Desktop can pass them to containers.

    Steps:
      1. Install kernel build tools in WSL2
      2. Clone the WSL2 kernel source from Microsoft's GitHub
      3. Enable optical drive modules in the kernel config
      4. Build the kernel (~15-20 min)
      5. Copy the built kernel to Windows
      6. Update %USERPROFILE%\.wslconfig to point WSL2 at the new kernel
      7. Restart WSL2

    Re-run this script only if you update WSL2 and the new kernel version drops
    optical drive support again.
#>

$ErrorActionPreference = "Stop"

$KernelOutputDir = "$env:USERPROFILE\wsl2-kernel"
$KernelImageName = "bzImage-optical"
$WslConfigPath   = "$env:USERPROFILE\.wslconfig"

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  WSL2 Custom Kernel Build - Optical Drive" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# --------------------------------------------------------------------------
# Prerequisite check: WSL2 installed
# --------------------------------------------------------------------------
try {
    $wslVersion = wsl --version 2>&1 | Select-String 'WSL version' | ForEach-Object { $_.Line }
    Write-Host "WSL2 found: $wslVersion" -ForegroundColor Green
} catch {
    Write-Host "ERROR: WSL2 is not installed." -ForegroundColor Red
    Write-Host "Install it with: wsl --install" -ForegroundColor Yellow
    exit 1
}

# --------------------------------------------------------------------------
# Ensure a full Ubuntu WSL2 distro is available for the kernel build.
# Docker Desktop only ships a minimal 'docker-desktop' distro that lacks
# apt/gcc/make, so we install Ubuntu if no other distro is present.
# --------------------------------------------------------------------------
# wsl --list outputs UTF-16 on some Windows versions; clean up null chars
$distros = (wsl --list --quiet 2>&1) -replace '[\x00]','' | Where-Object { $_ -notmatch 'docker-desktop|^\s*$' }
if (-not $distros) {
    Write-Host "No Ubuntu/Debian WSL2 distro found. Installing Ubuntu..." -ForegroundColor Yellow
    Write-Host "(This downloads ~500 MB and may take a few minutes)" -ForegroundColor Gray
    wsl --install -d Ubuntu --no-launch
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Ubuntu WSL2 installation failed." -ForegroundColor Red
        exit 1
    }
    Write-Host "Ubuntu installed." -ForegroundColor Green
    # Brief wait for distro registration
    Start-Sleep -Seconds 5
} else {
    Write-Host "WSL2 build distro: $($distros | Select-Object -First 1)" -ForegroundColor Green
}

# Use Ubuntu as the build distro (fall back to first non-docker distro)
$allDistros = (wsl --list --quiet 2>&1) -replace '[\x00]',''
$BuildDistro = $allDistros | Where-Object { $_ -match 'Ubuntu|Debian' } | Select-Object -First 1
if (-not $BuildDistro) {
    $BuildDistro = $allDistros | Where-Object { $_ -notmatch 'docker-desktop|^\s*$' } | Select-Object -First 1
}
$BuildDistro = $BuildDistro.Trim()
Write-Host "Building in distro: '$BuildDistro'" -ForegroundColor Cyan

# --------------------------------------------------------------------------
# Create output directory on Windows for the built kernel
# --------------------------------------------------------------------------
New-Item -ItemType Directory -Path $KernelOutputDir -Force | Out-Null
Write-Host "Kernel output directory: $KernelOutputDir"
Write-Host ""

# --------------------------------------------------------------------------
# Build script to run inside WSL2
# --------------------------------------------------------------------------
$buildScript = @'
#!/bin/sh
set -e

# Output path is passed in as first argument (Windows path converted to WSL2)
KERNEL_OUT="$1"
KERNEL_NAME="bzImage-optical"

echo ""
echo "=== Step 1: Install build dependencies ==="
apt-get update -qq
apt-get install -y --no-install-recommends \
    build-essential flex bison dwarves libssl-dev libelf-dev \
    bc git pahole cpio zstd 2>&1 | tail -5

echo ""
echo "=== Step 2: Get WSL2 kernel source (6.6.x branch - Docker Desktop compatible) ==="
# IMPORTANT: Must use the 6.6.x branch to stay ABI-compatible with Docker Desktop.
# The 6.18.x kernel (default branch) breaks Docker Desktop's init process.
cd ~
if [ -d "WSL2-Linux-Kernel-6.6" ]; then
    echo "Repo already exists, pulling latest..."
    cd WSL2-Linux-Kernel-6.6 && git pull --ff-only
else
    echo "Cloning Microsoft WSL2 6.6.x kernel..."
    git clone --depth=1 --branch linux-msft-wsl-6.6.y https://github.com/microsoft/WSL2-Linux-Kernel.git WSL2-Linux-Kernel-6.6
    cd WSL2-Linux-Kernel-6.6
fi

echo ""
echo "=== Step 3: Configure kernel with optical drive support ==="
cp Microsoft/config-wsl .config

# Enable optical drive support
scripts/config --enable CONFIG_BLK_DEV_SR
scripts/config --enable CONFIG_CDROM
scripts/config --enable CONFIG_ISO9660_FS
scripts/config --enable CONFIG_JOLIET
scripts/config --enable CONFIG_ZISOFS
scripts/config --enable CONFIG_UDF_FS

# Accept all defaults for any new config options
yes "" | make oldconfig > /dev/null 2>&1

echo "Optical drive config applied:"
grep -E 'CONFIG_BLK_DEV_SR|CONFIG_CDROM|CONFIG_ISO9660|CONFIG_UDF' .config

echo ""
echo "=== Step 4: Build kernel (this takes 15-20 minutes) ==="
NCPUS=$(nproc)
echo "Using $NCPUS CPU cores..."
make -j"$NCPUS" KCONFIG_CONFIG=.config 2>&1 | grep -E 'error:|warning:|Kernel:|CC |LD ' | tail -20

echo ""
echo "=== Step 5: Copy kernel to Windows ==="
mkdir -p "$KERNEL_OUT"
cp arch/x86/boot/bzImage "$KERNEL_OUT/$KERNEL_NAME"
echo "Kernel written to: $KERNEL_OUT/$KERNEL_NAME"

echo ""
echo "BUILD COMPLETE"
'@

# Write the build script with Unix line endings (LF only) and no BOM.
# PowerShell's Set-Content -Encoding UTF8 adds a BOM and uses CRLF;
# both break sh inside WSL2.
$scriptPath = "$env:TEMP\build-wsl2-kernel.sh"
$buildScriptLF = $buildScript.Replace("`r`n", "`n").Replace("`r", "`n").TrimStart()
[System.IO.File]::WriteAllText($scriptPath, $buildScriptLF, [System.Text.UTF8Encoding]::new($false))

# Convert Windows paths to WSL2 paths (must use the build distro, not docker-desktop)
$wslScriptPath  = wsl -d $BuildDistro -u root wslpath -u ($scriptPath.Replace('\', '/')) 2>&1
$wslKernelOut   = wsl -d $BuildDistro -u root wslpath -u ($KernelOutputDir.Replace('\', '/')) 2>&1

Write-Host "Build script prepared. Starting WSL2 kernel build..." -ForegroundColor Yellow
Write-Host "This will take approximately 15-20 minutes." -ForegroundColor Yellow
Write-Host ""

# --------------------------------------------------------------------------
# Run the build inside Ubuntu as root (no sudo needed)
# Pass the WSL2 kernel output path as $1 to the build script
# --------------------------------------------------------------------------
wsl -d $BuildDistro -u root sh "$wslScriptPath" "$wslKernelOut"

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "ERROR: Kernel build failed (exit code $LASTEXITCODE)." -ForegroundColor Red
    Write-Host "Check the output above for errors." -ForegroundColor Yellow
    exit 1
}

# --------------------------------------------------------------------------
# Verify the kernel was built
# --------------------------------------------------------------------------
$kernelPath = Join-Path $KernelOutputDir $KernelImageName
if (-not (Test-Path $kernelPath)) {
    Write-Host "ERROR: Kernel image not found at $kernelPath" -ForegroundColor Red
    exit 1
}

$kernelSize = (Get-Item $kernelPath).Length / 1MB
Write-Host ""
Write-Host "Kernel built: $kernelPath ($([math]::Round($kernelSize, 1)) MB)" -ForegroundColor Green

# --------------------------------------------------------------------------
# Update .wslconfig to use the new kernel
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Updating $WslConfigPath ===" -ForegroundColor Cyan

$kernelPathForConfig = $kernelPath.Replace('\', '\\')

# Read existing .wslconfig if present
$wslConfig = ""
if (Test-Path $WslConfigPath) {
    $wslConfig = Get-Content $WslConfigPath -Raw
    Write-Host "Existing .wslconfig found - updating..."
} else {
    Write-Host "Creating new .wslconfig..."
}

# Update or add the [wsl2] section with the kernel path
if ($wslConfig -match '\[wsl2\]') {
    # Update existing kernel= line or insert one
    if ($wslConfig -match '(?m)^kernel\s*=') {
        $wslConfig = $wslConfig -replace '(?m)^kernel\s*=.*', "kernel=$kernelPathForConfig"
    } else {
        $wslConfig = $wslConfig -replace '(\[wsl2\])', "`$1`nkernel=$kernelPathForConfig"
    }
} else {
    # Append new [wsl2] section
    $wslConfig += "`n[wsl2]`nkernel=$kernelPathForConfig`n"
}

# Write .wslconfig without BOM (standard INI format)
[System.IO.File]::WriteAllText($WslConfigPath, $wslConfig, [System.Text.UTF8Encoding]::new($false))
Write-Host "Updated .wslconfig:" -ForegroundColor Green
Get-Content $WslConfigPath

# --------------------------------------------------------------------------
# Shutdown WSL2 to load new kernel
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Restarting WSL2 with new kernel ===" -ForegroundColor Cyan
wsl --shutdown
Start-Sleep -Seconds 3

# Verify new kernel is loaded
$newKernel = wsl -d $BuildDistro uname -r 2>&1
Write-Host "WSL2 now running kernel: $newKernel" -ForegroundColor Green

# Check sr_mod is available
$srMod = wsl -d $BuildDistro sh -c "modinfo sr_mod 2>&1 | head -3"
if ($srMod -match 'filename') {
    Write-Host "sr_mod module: AVAILABLE" -ForegroundColor Green
} else {
    Write-Host "sr_mod: $srMod" -ForegroundColor Yellow
    Write-Host "Note: sr_mod may be compiled in (=y) rather than as a module (=m)." -ForegroundColor Yellow
    $srBuiltIn = wsl sh -c "grep CONFIG_BLK_DEV_SR /proc/config.gz 2>/dev/null || grep CONFIG_BLK_DEV_SR /boot/config-\$(uname -r) 2>/dev/null || grep CONFIG_BLK_DEV_SR /lib/modules/\$(uname -r)/build/.config 2>/dev/null | head -2"
    Write-Host "Kernel config: $srBuiltIn"
}

Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Custom kernel installed successfully!" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Run setup-usb-drives.ps1 as Administrator" -ForegroundColor White
Write-Host "  2. Verify drive appears: wsl ls /dev/sr*" -ForegroundColor White
Write-Host "  3. Restart ARM:  docker restart arm-rippers" -ForegroundColor White
Write-Host ""
