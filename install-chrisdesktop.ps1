<#
.SYNOPSIS
    One-command installer for Chrisdesktop (transcoding + pipeline machine).
    Run this on Chrisdesktop after cloning the repo.

.USAGE
    # On Chrisdesktop, open PowerShell as Administrator and run:
    git clone https://github.com/cjsolves/BackupMyMedia.git C:\Dev\BackupMyMedia
    cd C:\Dev\BackupMyMedia
    .\install-chrisdesktop.ps1

.NOTES
    Requires: Docker Desktop, Git, Windows 10/11
    Optional: NVIDIA drivers + Container Toolkit for GPU-accelerated transcoding and upscaling
#>

#Requires -RunAsAdministrator
$ErrorActionPreference = "Continue"

$RepoRoot = $PSScriptRoot
$PlexMedia = "D:\PlexMedia"

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  BackupMyMedia - Chrisdesktop Install" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# --------------------------------------------------------------------------
# Step 1: Create folder structure
# --------------------------------------------------------------------------
Write-Host "=== Step 1: Creating folder structure on D:\PlexMedia ===" -ForegroundColor Yellow

$dirs = @(
    "$PlexMedia\Inbox\completed",
    "$PlexMedia\Inbox\music",
    "$PlexMedia\Lossless\Movies",
    "$PlexMedia\Lossless\TV",
    "$PlexMedia\Lossless\Music",
    "$PlexMedia\Plex\Movies",
    "$PlexMedia\Plex\TV",
    "$PlexMedia\Transcode-Temp",
    "$PlexMedia\UpscaleQueue",
    "$PlexMedia\UpscaleOutput"
)
foreach ($d in $dirs) {
    New-Item -ItemType Directory -Path $d -Force | Out-Null
    Write-Host "  OK $d" -ForegroundColor Green
}

# --------------------------------------------------------------------------
# Step 2: Share the Inbox folder as Videoinbox
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Step 2: Sharing Inbox as 'Videoinbox' ===" -ForegroundColor Yellow

$existing = Get-SmbShare -Name "Videoinbox" -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  Share 'Videoinbox' already exists" -ForegroundColor Gray
} else {
    New-SmbShare -Name "Videoinbox" -Path "$PlexMedia\Inbox" -FullAccess "Everyone" -ErrorAction SilentlyContinue
    Write-Host "  Created share: \\$env:COMPUTERNAME\Videoinbox -> $PlexMedia\Inbox" -ForegroundColor Green
}

# --------------------------------------------------------------------------
# Step 3: Enable OpenSSH Server (so Mini PC can deploy here remotely)
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Step 3: Enabling OpenSSH Server for remote Docker management ===" -ForegroundColor Yellow

$sshFeature = Get-WindowsCapability -Online | Where-Object Name -Like "OpenSSH.Server*"
if ($sshFeature.State -ne "Installed") {
    Write-Host "  Installing OpenSSH Server..." -ForegroundColor Gray
    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
}
Start-Service sshd -ErrorAction SilentlyContinue
Set-Service sshd -StartupType Automatic -ErrorAction SilentlyContinue
Write-Host "  OpenSSH Server: running (auto-start)" -ForegroundColor Green

# --------------------------------------------------------------------------
# Step 4: Add Mini PC's SSH public key for remote Docker context access
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Step 4: Authorising Mini PC (A8) SSH key ===" -ForegroundColor Yellow

# The Mini PC's public key (generated during setup on A8)
$miniPcPubKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAYBBE7deZY7hYxzFq0NuXFdB2msuRDv1eaNkE+PF4Yy A8-docker-remote"

$authKeysDir  = "$env:USERPROFILE\.ssh"
$authKeysPath = "$authKeysDir\authorized_keys"

New-Item -ItemType Directory -Path $authKeysDir -Force | Out-Null
$existing = if (Test-Path $authKeysPath) { Get-Content $authKeysPath -Raw } else { "" }
if ($existing -notmatch "A8-docker-remote") {
    Add-Content $authKeysPath "`n$miniPcPubKey"
    Write-Host "  Added Mini PC (A8) SSH key to authorized_keys" -ForegroundColor Green
} else {
    Write-Host "  Mini PC SSH key already authorized" -ForegroundColor Gray
}

# Fix SSH key file permissions (Windows SSH is strict about this)
icacls $authKeysPath /inheritance:r /grant:r "${env:USERNAME}:(F)" 2>&1 | Out-Null
icacls $authKeysDir /inheritance:r /grant:r "${env:USERNAME}:(F)" 2>&1 | Out-Null
Write-Host "  SSH permissions OK" -ForegroundColor Green

# --------------------------------------------------------------------------
# Step 5: Configure firewall for SSH
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Step 5: Firewall rule for SSH ===" -ForegroundColor Yellow
$fwRule = Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue
if (-not $fwRule) {
    New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (sshd)" `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
    Write-Host "  Firewall rule created for SSH (port 22)" -ForegroundColor Green
} else {
    Write-Host "  Firewall rule already exists" -ForegroundColor Gray
}

# --------------------------------------------------------------------------
# Step 6: Check Docker Desktop is installed
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Step 6: Checking Docker Desktop ===" -ForegroundColor Yellow
$dockerVersion = docker version --format "{{.Server.Version}}" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  Docker Desktop running: v$dockerVersion" -ForegroundColor Green
} else {
    Write-Host "  WARNING: Docker Desktop not running or not installed." -ForegroundColor Red
    Write-Host "  Download from: https://www.docker.com/products/docker-desktop" -ForegroundColor Yellow
    Write-Host "  After installing, re-run this script or manually run Step 7." -ForegroundColor Yellow
}

# --------------------------------------------------------------------------
# Step 7: Start the Chrisdesktop Docker stack
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Step 7: Starting Chrisdesktop Docker stack ===" -ForegroundColor Yellow

if ($LASTEXITCODE -eq 0) {
    # Chrisdesktop services (Tdarr, media-organizer, upscaler)
    Write-Host "  Starting chrisdesktop stack (Tdarr + media-organizer + upscaler)..." -ForegroundColor Gray
    Set-Location "$RepoRoot\chrisdesktop"
    docker compose up -d 2>&1 | Select-Object -Last 5
    Write-Host ""

    # Pipeline dashboard
    Write-Host "  Starting pipeline dashboard..." -ForegroundColor Gray
    Set-Location "$RepoRoot\pipeline"
    docker compose up -d 2>&1 | Select-Object -Last 5

    Set-Location $RepoRoot

    Start-Sleep -Seconds 10
    Write-Host ""
    Write-Host "  Running containers:" -ForegroundColor Cyan
    docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>&1
} else {
    Write-Host "  Skipping Docker start - Docker not available." -ForegroundColor Yellow
    Write-Host "  Once Docker Desktop is running, start manually:" -ForegroundColor Yellow
    Write-Host "    cd $RepoRoot\chrisdesktop && docker compose up -d" -ForegroundColor White
    Write-Host "    cd $RepoRoot\pipeline     && docker compose up -d" -ForegroundColor White
}

# --------------------------------------------------------------------------
# Step 8: Configure Tdarr Docker context back to Mini PC
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Step 8: Creating Docker context for Mini PC (A8) ===" -ForegroundColor Yellow

$sshConfigPath = "$env:USERPROFILE\.ssh\config"
$sshConfigEntry = @"

Host minipc
    HostName A8
    User chris
    StrictHostKeyChecking no
"@
$existingConfig = if (Test-Path $sshConfigPath) { Get-Content $sshConfigPath -Raw } else { "" }
if ($existingConfig -notmatch "Host minipc") {
    Add-Content $sshConfigPath $sshConfigEntry
}
docker context rm minipc 2>&1 | Out-Null
docker context create minipc --description "Mini PC (A8) - ripping machine" `
    --docker "host=ssh://chris@A8" 2>&1 | Out-Null
Write-Host "  Docker context 'minipc' created -> ssh://chris@A8" -ForegroundColor Green

# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Install complete!" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Services:" -ForegroundColor Cyan
Write-Host "    Tdarr UI:      http://localhost:8265" -ForegroundColor White
Write-Host "    Pipeline:      http://localhost:8090" -ForegroundColor White
Write-Host ""
Write-Host "  File share:     \\$env:COMPUTERNAME\Videoinbox" -ForegroundColor White
Write-Host "  SSH server:     Enabled (Mini PC can now remote-deploy here)" -ForegroundColor White
Write-Host ""
Write-Host "  NEXT STEPS:" -ForegroundColor Yellow
Write-Host "  1. On the NAS (Windows): run install-nas.ps1 as Administrator" -ForegroundColor White
Write-Host "     This creates the NAS folder structure, SMB shares, and configures Plex" -ForegroundColor Gray
Write-Host ""
Write-Host "  2. Update pipeline\docker-compose.yml NAS volume paths:" -ForegroundColor White
Write-Host "     Replace 'NAS' with your NAS hostname or IP in these lines:" -ForegroundColor Gray
Write-Host "       - //NAS/Lossless:/media/nas/Lossless" -ForegroundColor Gray
Write-Host "       - //NAS/Plex:/media/nas/Plex" -ForegroundColor Gray
Write-Host "     Then restart: docker compose -f pipeline\docker-compose.yml up -d" -ForegroundColor Gray
Write-Host ""
Write-Host "  3. Open Tdarr at http://localhost:8265 and add two libraries:" -ForegroundColor White
Write-Host "       Movies: /media/nas/Lossless/Movies  ->  /media/nas/Plex/Movies" -ForegroundColor Gray
Write-Host "       TV:     /media/nas/Lossless/TV       ->  /media/nas/Plex/TV" -ForegroundColor Gray
Write-Host "       FFmpeg args: -c:v hevc_nvenc -preset p4 -cq 18 -c:a copy -c:s copy" -ForegroundColor Gray
Write-Host ""
Write-Host "  4. In Plex on the NAS: add libraries pointing to NAS paths" -ForegroundColor White
Write-Host "       Movies: <NAS media root>\Plex\Movies" -ForegroundColor Gray
Write-Host "       TV:     <NAS media root>\Plex\TV" -ForegroundColor Gray
Write-Host "       Music:  <NAS media root>\Lossless\Music" -ForegroundColor Gray
Write-Host ""
Write-Host "  5. Open Pipeline dashboard at http://localhost:8090" -ForegroundColor White
Write-Host ""
Write-Host "  6. From Mini PC (after SSH key setup), deploy updates with:" -ForegroundColor Yellow
Write-Host "    docker --context chrisdesktop compose -f chrisdesktop/docker-compose.yml up -d --pull always" -ForegroundColor White
Write-Host ""
