<#
.SYNOPSIS
    Sets up SSH-based Docker contexts so this machine can manage Docker
    on the other machine (and vice versa).

    Run this script on BOTH machines.
    See docs/CONNECTIONS.md for the full setup guide including SSH key creation.

.NOTES
    Requires: Docker Desktop, OpenSSH client (built into Windows 10/11)
#>

$ErrorActionPreference = "Continue"

# --------------------------------------------------------------------------
# Detect which machine we're on and set the context to create
# --------------------------------------------------------------------------
$hostname = $env:COMPUTERNAME.ToLower()

if ($hostname -match 'minipc|mini|rip') {
    $RemoteHost  = "Chrisdesktop"
    $RemoteName  = "chrisdesktop"
    $RemoteUser  = "chris"
} elseif ($hostname -match 'chris|desktop|transcode') {
    $RemoteHost  = "MiniPC"
    $RemoteName  = "minipc"
    $RemoteUser  = "chris"
} else {
    Write-Host "Cannot auto-detect machine role." -ForegroundColor Yellow
    $RemoteHost = Read-Host "Remote machine hostname (e.g. Chrisdesktop or MiniPC)"
    $RemoteName = $RemoteHost.ToLower()
    $RemoteUser = Read-Host "Remote username (e.g. chris)"
}

$SshKey = "$env:USERPROFILE\.ssh\docker_remote"

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  Docker Context Setup" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  This machine : $env:COMPUTERNAME" -ForegroundColor White
Write-Host "  Remote target: $RemoteUser@$RemoteHost" -ForegroundColor White
Write-Host "  Context name : $RemoteName" -ForegroundColor White
Write-Host ""

# --------------------------------------------------------------------------
# Step 1: Check SSH key exists
# --------------------------------------------------------------------------
if (-not (Test-Path $SshKey)) {
    Write-Host "SSH key not found at $SshKey" -ForegroundColor Yellow
    Write-Host "Generating SSH key pair..." -ForegroundColor Cyan
    ssh-keygen -t ed25519 -C "$env:COMPUTERNAME-docker-remote" -f $SshKey -N '""'
    Write-Host ""
    Write-Host "Public key to add to $RemoteHost (~/.ssh/authorized_keys):" -ForegroundColor Yellow
    Get-Content "$SshKey.pub"
    Write-Host ""
    Write-Host "Run this command to copy it (you'll be asked for the remote password once):" -ForegroundColor Yellow
    Write-Host "  ssh-copy-id -i $SshKey.pub $RemoteUser@$RemoteHost" -ForegroundColor White
    Write-Host "  (or use the manual steps in docs/CONNECTIONS.md)" -ForegroundColor White
    Write-Host ""
    $continue = Read-Host "Press Enter once the key is on $RemoteHost, or Ctrl-C to abort"
}

# --------------------------------------------------------------------------
# Step 2: Test SSH connectivity
# --------------------------------------------------------------------------
Write-Host "Testing SSH connection to $RemoteHost..." -ForegroundColor Cyan
$testResult = ssh -i $SshKey -o ConnectTimeout=5 -o StrictHostKeyChecking=no `
    "$RemoteUser@$RemoteHost" "echo ssh-ok" 2>&1

if ($testResult -notmatch "ssh-ok") {
    Write-Host "ERROR: SSH connection failed: $testResult" -ForegroundColor Red
    Write-Host "Check the SSH key setup in docs/CONNECTIONS.md" -ForegroundColor Yellow
    exit 1
}
Write-Host "SSH connection OK" -ForegroundColor Green

# --------------------------------------------------------------------------
# Step 3: Add SSH config entry for convenience
# --------------------------------------------------------------------------
$sshConfig = "$env:USERPROFILE\.ssh\config"
$configEntry = @"

Host $RemoteName
    HostName $RemoteHost
    User $RemoteUser
    IdentityFile $SshKey
    StrictHostKeyChecking no
"@

$existingConfig = if (Test-Path $sshConfig) { Get-Content $sshConfig -Raw } else { "" }
if ($existingConfig -notmatch "Host $RemoteName") {
    Add-Content $sshConfig $configEntry
    Write-Host "Added SSH config entry for: $RemoteName" -ForegroundColor Green
} else {
    Write-Host "SSH config already has entry for: $RemoteName" -ForegroundColor Gray
}

# --------------------------------------------------------------------------
# Step 4: Create Docker context
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "Creating Docker context '$RemoteName'..." -ForegroundColor Cyan

# Remove existing context if present
docker context rm $RemoteName 2>&1 | Out-Null

docker context create $RemoteName `
    --description "Remote Docker on $RemoteHost" `
    --docker "host=ssh://$RemoteUser@$RemoteHost"

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to create Docker context" -ForegroundColor Red
    exit 1
}
Write-Host "Docker context '$RemoteName' created." -ForegroundColor Green

# --------------------------------------------------------------------------
# Step 5: Verify Docker context works
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "Testing Docker context..." -ForegroundColor Cyan
$dockerTest = docker --context $RemoteName version 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "Docker context working!" -ForegroundColor Green
} else {
    Write-Host "WARNING: Docker context test failed (Docker Desktop may not be running on $RemoteHost)" -ForegroundColor Yellow
    Write-Host $dockerTest
}

# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Setup complete!" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Usage examples:" -ForegroundColor Cyan
Write-Host "  docker --context $RemoteName ps" -ForegroundColor White
Write-Host "  docker --context $RemoteName compose -f chrisdesktop/docker-compose.yml up -d" -ForegroundColor White
Write-Host "  docker context use $RemoteName   # make it default" -ForegroundColor White
Write-Host "  docker context use default        # switch back" -ForegroundColor White
Write-Host ""
docker context ls
