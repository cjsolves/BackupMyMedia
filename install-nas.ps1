<#
.SYNOPSIS
    One-command installer for the NAS (Windows) media library storage.
    Creates folder structure, SMB shares, permissions, and configures Plex.

.USAGE
    # On the NAS Windows machine, open PowerShell as Administrator:
    git clone https://github.com/cjsolves/BackupMyMedia.git C:\Dev\BackupMyMedia
    cd C:\Dev\BackupMyMedia
    .\install-nas.ps1

    # Or to specify a drive:
    .\install-nas.ps1 -MediaDrive "E:"

.NOTES
    Requires: PowerShell 5.1+, Administrator rights
    Plex Media Server should be installed before running for auto-configuration.
#>

#Requires -RunAsAdministrator

param(
    # Drive or root path for all media storage
    # Default: largest available non-system drive
    [string]$MediaDrive = "",

    # Share access: Everyone (home network) or specify a user/group
    [string]$ShareAccess = "Everyone"
)

$ErrorActionPreference = "Continue"

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  BackupMyMedia - NAS (Windows) Setup" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# --------------------------------------------------------------------------
# Step 1: Choose the storage drive
# --------------------------------------------------------------------------
Write-Host "=== Step 1: Storage drive ===" -ForegroundColor Yellow

if ([string]::IsNullOrWhiteSpace($MediaDrive)) {
    # Auto-pick: largest non-system fixed drive
    $drives = Get-PSDrive -PSProvider FileSystem | Where-Object {
        $_.Root -ne "C:\" -and $_.Used -ne $null
    } | Sort-Object Free -Descending

    if ($drives) {
        $MediaDrive = $drives[0].Root.TrimEnd('\')
        Write-Host "  Auto-selected drive: $MediaDrive ($([math]::Round($drives[0].Free/1GB,0)) GB free)" -ForegroundColor Green
    } else {
        Write-Host "  No non-system drive found — using C:\Media" -ForegroundColor Yellow
        $MediaDrive = "C:"
    }
}

$MediaDrive = $MediaDrive.TrimEnd('\')
$MediaRoot  = "$MediaDrive\PlexMedia"

Write-Host "  Media root: $MediaRoot" -ForegroundColor Green

# --------------------------------------------------------------------------
# Step 2: Create folder structure
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Step 2: Creating folder structure ===" -ForegroundColor Yellow

$dirs = @(
    "$MediaRoot\Lossless\Movies",     # Permanent lossless MKV archive
    "$MediaRoot\Lossless\TV",
    "$MediaRoot\Lossless\Music",      # FLAC — also the Plex music library
    "$MediaRoot\Plex\Movies",         # H.265 transcoded — Plex video library
    "$MediaRoot\Plex\TV",
    "$MediaRoot\BulkIngest"           # Drop existing ripped files here for processing
)

foreach ($d in $dirs) {
    New-Item -ItemType Directory -Path $d -Force | Out-Null
    Write-Host "  Created: $d" -ForegroundColor Green
}

# --------------------------------------------------------------------------
# Step 3: Set ACL permissions
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Step 3: Setting permissions ===" -ForegroundColor Yellow

$acl = Get-Acl $MediaRoot
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    $ShareAccess,
    "FullControl",
    "ContainerInherit,ObjectInherit",
    "None",
    "Allow"
)
$acl.SetAccessRule($rule)

foreach ($d in @("$MediaRoot\Lossless", "$MediaRoot\Plex", "$MediaRoot\BulkIngest")) {
    Set-Acl -Path $d -AclObject $acl -ErrorAction SilentlyContinue
    Write-Host "  Permissions set: $d" -ForegroundColor Green
}

# --------------------------------------------------------------------------
# Step 4: Create SMB shares
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Step 4: Creating SMB shares ===" -ForegroundColor Yellow

$shares = @(
    @{ Name="Lossless";   Path="$MediaRoot\Lossless";   Desc="Lossless MKV archive (permanent storage)" },
    @{ Name="Plex";       Path="$MediaRoot\Plex";       Desc="H.265 Plex library (read-write for Chrisdesktop)" },
    @{ Name="BulkIngest"; Path="$MediaRoot\BulkIngest"; Desc="Drop existing ripped library here for pipeline" }
)

foreach ($share in $shares) {
    $existing = Get-SmbShare -Name $share.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  Share '$($share.Name)' already exists — updating path" -ForegroundColor Gray
        Set-SmbShare -Name $share.Name -Description $share.Desc -Force -ErrorAction SilentlyContinue
    } else {
        New-SmbShare -Name $share.Name -Path $share.Path `
            -Description $share.Desc `
            -FullAccess $ShareAccess `
            -ErrorAction SilentlyContinue | Out-Null
        Write-Host "  Created share: \\$env:COMPUTERNAME\$($share.Name) -> $($share.Path)" -ForegroundColor Green
    }
}

# --------------------------------------------------------------------------
# Step 5: Firewall rules for SMB (port 445) and Plex (32400)
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Step 5: Firewall rules ===" -ForegroundColor Yellow

$fwRules = @(
    @{ Name="BackupMyMedia-SMB";  Port=445;   Proto="TCP"; Desc="SMB file sharing for media library" },
    @{ Name="BackupMyMedia-Plex"; Port=32400; Proto="TCP"; Desc="Plex Media Server web/API" }
)
foreach ($rule in $fwRules) {
    $existing = Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue
    if (-not $existing) {
        New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound `
            -Protocol $rule.Proto -LocalPort $rule.Port `
            -Action Allow -Description $rule.Desc | Out-Null
        Write-Host "  Firewall rule: $($rule.Name) (port $($rule.Port))" -ForegroundColor Green
    } else {
        Write-Host "  Firewall rule already exists: $($rule.Name)" -ForegroundColor Gray
    }
}

# --------------------------------------------------------------------------
# Step 6: Detect and configure Plex Media Server
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Step 6: Plex Media Server ===" -ForegroundColor Yellow

$plexDataPaths = @(
    "$env:LOCALAPPDATA\Plex Media Server",
    "C:\Users\$env:USERNAME\AppData\Local\Plex Media Server",
    "$env:APPDATA\Plex Media Server"
)
$plexExePaths = @(
    "C:\Program Files (x86)\Plex\Plex Media Server\Plex Media Server.exe",
    "C:\Program Files\Plex\Plex Media Server\Plex Media Server.exe"
)

$plexData = $plexDataPaths | Where-Object { Test-Path $_ } | Select-Object -First 1
$plexExe  = $plexExePaths  | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($plexData) {
    Write-Host "  Plex data found: $plexData" -ForegroundColor Green

    # Create a refresh script on the NAS itself
    $refreshScript = @"
# Plex library refresh — run this after new content arrives
# Or POST http://localhost:32400/library/sections/all/refresh?X-Plex-Token=YOUR_TOKEN
`$Token = `$env:PLEX_TOKEN
if (`$Token) {
    `$sections = (Invoke-WebRequest "http://localhost:32400/library/sections?X-Plex-Token=`$Token" -UseBasicParsing).Content
    Write-Host "Plex refresh triggered at `$(Get-Date)"
    Invoke-WebRequest "http://localhost:32400/library/sections/all/refresh?X-Plex-Token=`$Token" -UseBasicParsing | Out-Null
} else {
    Write-Host "Set `$env:PLEX_TOKEN to trigger Plex refresh automatically"
    Write-Host "Token guide: https://support.plex.tv/articles/204059436/"
}
"@
    $refreshScript | Set-Content "$MediaRoot\plex-refresh.ps1" -Encoding UTF8
    Write-Host "  Created: $MediaRoot\plex-refresh.ps1" -ForegroundColor Green
} else {
    Write-Host "  Plex not detected. Install from https://www.plex.tv/media-server-downloads/" -ForegroundColor Yellow
    Write-Host "  After installing, run this script again to configure library paths." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  Plex library paths to add in Plex web UI:" -ForegroundColor Cyan
Write-Host "    Movies: $MediaRoot\Plex\Movies"
Write-Host "    TV:     $MediaRoot\Plex\TV"
Write-Host "    Music:  $MediaRoot\Lossless\Music"

# --------------------------------------------------------------------------
# Step 7: Save config file for reference by other machines
# --------------------------------------------------------------------------
$configContent = @"
# BackupMyMedia NAS Configuration (Windows)
# Generated: $(Get-Date)
NAS_HOSTNAME=$env:COMPUTERNAME
NAS_IP=$((Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.InterfaceAlias -notmatch 'Loopback'} | Select-Object -First 1).IPAddress)
MEDIA_ROOT=$MediaRoot
LOSSLESS_PATH=$MediaRoot\Lossless
PLEX_PATH=$MediaRoot\Plex
BULK_INGEST_PATH=$MediaRoot\BulkIngest

# SMB share paths (use in Chrisdesktop docker-compose.yml volume mounts)
NAS_LOSSLESS_UNC=\\$env:COMPUTERNAME\Lossless
NAS_PLEX_UNC=\\$env:COMPUTERNAME\Plex
NAS_BULKINGEST_UNC=\\$env:COMPUTERNAME\BulkIngest

# Plex library paths (add in Plex web UI)
PLEX_MOVIES=$MediaRoot\Plex\Movies
PLEX_TV=$MediaRoot\Plex\TV
PLEX_MUSIC=$MediaRoot\Lossless\Music
"@
$configContent | Set-Content "$MediaRoot\BackupMyMedia-NAS-Config.txt" -Encoding UTF8
Write-Host ""
Write-Host "  Config saved: $MediaRoot\BackupMyMedia-NAS-Config.txt" -ForegroundColor Green

# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  NAS Setup Complete!" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Folder structure:" -ForegroundColor Cyan
Write-Host "    $MediaRoot\Lossless\Movies  ← Permanent lossless MKV archive"
Write-Host "    $MediaRoot\Lossless\TV"
Write-Host "    $MediaRoot\Lossless\Music   ← FLAC (Plex music library)"
Write-Host "    $MediaRoot\Plex\Movies      ← H.265 for Plex"
Write-Host "    $MediaRoot\Plex\TV"
Write-Host "    $MediaRoot\BulkIngest       ← Drop existing library here"
Write-Host ""
Write-Host "  SMB shares:" -ForegroundColor Cyan
Write-Host "    \\$env:COMPUTERNAME\Lossless"
Write-Host "    \\$env:COMPUTERNAME\Plex"
Write-Host "    \\$env:COMPUTERNAME\BulkIngest"
Write-Host ""
Write-Host "  NEXT STEPS:" -ForegroundColor Yellow
Write-Host "  1. On Chrisdesktop: update pipeline\docker-compose.yml:"
Write-Host "       //NAS/Lossless -> /media/nas/Lossless"
Write-Host "       //NAS/Plex     -> /media/nas/Plex"
Write-Host "     Replace 'NAS' with '$env:COMPUTERNAME' if hostname differs"
Write-Host ""
Write-Host "  2. In Plex (http://$($env:COMPUTERNAME):32400):"
Write-Host "     Add Movies library: $MediaRoot\Plex\Movies"
Write-Host "     Add TV library:     $MediaRoot\Plex\TV"
Write-Host "     Add Music library:  $MediaRoot\Lossless\Music"
Write-Host ""
Write-Host "  3. For bulk existing library:"
Write-Host "     Copy files to: \\$env:COMPUTERNAME\BulkIngest"
Write-Host "     Then trigger:  POST http://Chrisdesktop:8090/api/bulk_intake/scan"
Write-Host ""
Write-Host "  4. Get Plex token for auto-refresh:"
Write-Host "     https://support.plex.tv/articles/204059436/"
Write-Host "     Set in Chrisdesktop pipeline\docker-compose.yml: PLEX_TOKEN=xxxx"
Write-Host ""
