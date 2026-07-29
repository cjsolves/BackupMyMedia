<#
.SYNOPSIS
    Master redeploy script — runs from Mini PC (A8) to update all machines.
    Pulls latest code from GitHub and redeploys to Chrisdesktop and/or NAS.

.USAGE
    # Deploy to all machines:
    .\scripts\redeploy-remote.ps1

    # Deploy to specific machine only:
    .\scripts\redeploy-remote.ps1 -Target Chrisdesktop
    .\scripts\redeploy-remote.ps1 -Target NAS
    .\scripts\redeploy-remote.ps1 -Target MiniPC

    # Pull latest code only (no deploy):
    .\scripts\redeploy-remote.ps1 -PullOnly

.NOTES
    For Chrisdesktop: uses Docker context 'chrisdesktop' (SSH) if available,
    falls back to file-share copy if SSH not yet configured.
    For NAS: uses WinRM (if enabled) or file-share copy.
    For Mini PC: runs locally.
#>

param(
    [ValidateSet("All","MiniPC","Chrisdesktop","NAS")]
    [string]$Target = "All",
    [switch]$PullOnly,
    [switch]$Force   # skip confirmation prompts
)

$ErrorActionPreference = "Continue"
$RepoRoot = "C:\Dev\BackupMyMedia"
$RepoUrl  = "https://github.com/cjsolves/BackupMyMedia.git"

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  BackupMyMedia Remote Redeploy" -ForegroundColor Cyan
Write-Host "  Target: $Target" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# --------------------------------------------------------------------------
# Step 1: Pull latest from GitHub
# --------------------------------------------------------------------------
Write-Host "=== Pulling latest from GitHub ===" -ForegroundColor Yellow
if (Test-Path "$RepoRoot\.git") {
    git -C $RepoRoot pull --ff-only origin main 2>&1
    Write-Host "Pulled: $RepoRoot" -ForegroundColor Green
} else {
    Write-Host "Cloning repo to $RepoRoot..." -ForegroundColor Gray
    git clone $RepoUrl $RepoRoot
}

if ($PullOnly) {
    Write-Host "Pull complete. Exiting (-PullOnly mode)." -ForegroundColor Green
    exit 0
}

# --------------------------------------------------------------------------
# Deploy helper functions
# --------------------------------------------------------------------------

function Deploy-ToChrisdesktop {
    Write-Host ""
    Write-Host "=== Deploying to Chrisdesktop ===" -ForegroundColor Yellow

    # Method A: Docker context (best — deploys containers directly)
    $contextOk = $false
    try {
        $info = docker --context chrisdesktop info 2>&1 | Select-String 'Server Version'
        if ($info) { $contextOk = $true }
    } catch {}

    if ($contextOk) {
        Write-Host "  Docker context: connected — deploying containers" -ForegroundColor Green

        # Pull and restart the Chrisdesktop stack
        docker --context chrisdesktop compose `
            -f "$RepoRoot\chrisdesktop\docker-compose.yml" pull 2>&1 | Select-Object -Last 3
        docker --context chrisdesktop compose `
            -f "$RepoRoot\chrisdesktop\docker-compose.yml" up -d --remove-orphans 2>&1 | Select-Object -Last 5

        # Pull and restart the pipeline stack
        docker --context chrisdesktop compose `
            -f "$RepoRoot\pipeline\docker-compose.yml" pull 2>&1 | Select-Object -Last 3
        docker --context chrisdesktop compose `
            -f "$RepoRoot\pipeline\docker-compose.yml" up -d --remove-orphans 2>&1 | Select-Object -Last 5

        Write-Host "  Chrisdesktop containers updated" -ForegroundColor Green

    } else {
        Write-Host "  Docker context not available — using file share fallback" -ForegroundColor Yellow

        # Method B: Copy installer to D share and trigger via scheduled task
        $deployShare = "\\Chrisdesktop\D\BackupMyMedia-Deploy"
        if (Test-Path $deployShare -ErrorAction SilentlyContinue) {
            Write-Host "  Copying installer to $deployShare..." -ForegroundColor Gray
            Copy-Item "$RepoRoot\install-chrisdesktop.ps1" "$deployShare\" -Force
            Copy-Item "$RepoRoot\scripts\register-startup-task.ps1" "$deployShare\" -Force
            Write-Host "  Files copied to \\Chrisdesktop\D\BackupMyMedia-Deploy\" -ForegroundColor Green
            Write-Host ""
            Write-Host "  ACTION REQUIRED on Chrisdesktop:" -ForegroundColor Red
            Write-Host "    Double-click: D:\BackupMyMedia-Deploy\INSTALL-RUN-AS-ADMIN.bat" -ForegroundColor White
            Write-Host "    OR open admin PowerShell and run:" -ForegroundColor White
            Write-Host "      & 'D:\BackupMyMedia-Deploy\install-chrisdesktop.ps1'" -ForegroundColor White
        } else {
            Write-Host "  Cannot reach \\Chrisdesktop\D — is the machine online?" -ForegroundColor Red
            Write-Host "  Manual install command for Chrisdesktop:" -ForegroundColor Yellow
            Write-Host "    git clone $RepoUrl C:\Dev\BackupMyMedia" -ForegroundColor White
            Write-Host "    cd C:\Dev\BackupMyMedia && .\install-chrisdesktop.ps1" -ForegroundColor White
        }
    }
}


function Deploy-ToNAS {
    Write-Host ""
    Write-Host "=== Deploying to NAS ===" -ForegroundColor Yellow

    # Method A: WinRM (if enabled and TrustedHosts configured)
    $winrmOk = $false
    try {
        $session = New-PSSession -ComputerName NAS -ErrorAction Stop
        $winrmOk = $true
        Write-Host "  WinRM: connected — running install-nas.ps1 remotely" -ForegroundColor Green

        # Copy and run the installer
        $installScript = Get-Content "$RepoRoot\install-nas.ps1" -Raw
        $result = Invoke-Command -Session $session -ScriptBlock {
            param($script)
            $tmpPath = "$env:TEMP\install-nas.ps1"
            Set-Content $tmpPath $script -Encoding UTF8
            & powershell.exe -ExecutionPolicy Bypass -File $tmpPath
        } -ArgumentList $installScript
        Write-Host $result
        Remove-PSSession $session
        Write-Host "  NAS setup complete" -ForegroundColor Green

    } catch {
        Write-Host "  WinRM not available: $($_.Exception.Message.Split('.')[0])" -ForegroundColor Yellow
    }

    if (-not $winrmOk) {
        # Method B: Try file share
        $nasShares = net view \\NAS 2>&1
        $hasShare = $nasShares -match 'Disk'

        if ($hasShare) {
            # Find a writable share
            foreach ($shareLine in ($nasShares | Where-Object {$_ -match 'Disk'})) {
                $shareName = ($shareLine -split '\s+')[0]
                $sharePath = "\\NAS\$shareName\BackupMyMedia-Deploy"
                try {
                    New-Item -ItemType Directory $sharePath -Force -ErrorAction Stop | Out-Null
                    Copy-Item "$RepoRoot\install-nas.ps1" "$sharePath\" -Force
                    $bat = "@echo off`r`npowershell.exe -ExecutionPolicy Bypass -File `"%~dp0install-nas.ps1`""
                    [System.IO.File]::WriteAllText("$sharePath\INSTALL-RUN-AS-ADMIN.bat", $bat, [System.Text.ASCIIEncoding]::new())
                    Write-Host "  Files copied to \\NAS\$shareName\BackupMyMedia-Deploy\" -ForegroundColor Green
                    Write-Host "  ACTION on NAS: double-click INSTALL-RUN-AS-ADMIN.bat" -ForegroundColor Red
                    break
                } catch {}
            }
        } else {
            Write-Host "  Cannot reach NAS shares — manual install required" -ForegroundColor Yellow
            Write-Host "  Manual commands for NAS (run as Administrator):" -ForegroundColor Yellow
            Write-Host "    git clone $RepoUrl C:\Dev\BackupMyMedia" -ForegroundColor White
            Write-Host "    cd C:\Dev\BackupMyMedia && .\install-nas.ps1" -ForegroundColor White
        }
    }
}


function Deploy-ToMiniPC {
    Write-Host ""
    Write-Host "=== Redeploying Mini PC (local) ===" -ForegroundColor Yellow

    # Re-register startup tasks (needs admin — check first)
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($isAdmin) {
        & "$RepoRoot\scripts\register-startup-task.ps1"
    } else {
        Write-Host "  Not admin — startup tasks NOT re-registered" -ForegroundColor Yellow
        Write-Host "  Run as admin to update tasks:" -ForegroundColor Yellow
        Write-Host "    & '$RepoRoot\scripts\register-startup-task.ps1'" -ForegroundColor White
    }

    # Restart ARM with latest config
    Write-Host "  Restarting ARM with latest config..." -ForegroundColor Gray
    docker cp "$RepoRoot\ripping-machine\config\arm.yaml" arm-rippers:/etc/arm/config/arm.yaml 2>&1 | Out-Null
    docker exec arm-rippers chown arm:arm /etc/arm/config/arm.yaml 2>&1 | Out-Null
    docker restart arm-rippers 2>&1 | Out-Null
    Start-Sleep -Seconds 8
    $status = docker ps --filter "name=arm-rippers" --format "{{.Status}}" 2>&1
    Write-Host "  ARM: $status" -ForegroundColor Green
}

# --------------------------------------------------------------------------
# Enable WinRM TrustedHosts (needs admin — best-effort)
# --------------------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {
    Write-Host "Configuring WinRM TrustedHosts for Chrisdesktop and NAS..." -ForegroundColor Gray
    $current = (Get-Item WSMan:\localhost\Client\TrustedHosts).Value
    $needed = @("Chrisdesktop","NAS") | Where-Object { $current -notmatch $_ }
    if ($needed) {
        $newHosts = ($current, ($needed -join ",") | Where-Object {$_}) -join ","
        Set-Item WSMan:\localhost\Client\TrustedHosts -Value $newHosts -Force -ErrorAction SilentlyContinue
        Write-Host "  TrustedHosts updated: Chrisdesktop, NAS" -ForegroundColor Green
    } else {
        Write-Host "  TrustedHosts already configured" -ForegroundColor Gray
    }
} else {
    Write-Host "Not admin — WinRM TrustedHosts not updated (SSH deploy still works)" -ForegroundColor Gray
}

# --------------------------------------------------------------------------
# Run the requested deployments
# --------------------------------------------------------------------------
switch ($Target) {
    "All"          { Deploy-ToMiniPC; Deploy-ToChrisdesktop; Deploy-ToNAS }
    "MiniPC"       { Deploy-ToMiniPC }
    "Chrisdesktop" { Deploy-ToChrisdesktop }
    "NAS"          { Deploy-ToNAS }
}

Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Redeploy complete" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Future redeployment from this machine (Mini PC):" -ForegroundColor Cyan
Write-Host "  .\scripts\redeploy-remote.ps1                    # all machines" -ForegroundColor White
Write-Host "  .\scripts\redeploy-remote.ps1 -Target Chrisdesktop" -ForegroundColor White
Write-Host "  .\scripts\redeploy-remote.ps1 -Target NAS" -ForegroundColor White
Write-Host ""
