# Docker Cross-Machine Connection Setup

This document records the Docker context configuration for connecting the
**Mini PC** (ripping machine) and **Chrisdesktop** (transcoding/pipeline machine)
so that either machine can deploy and manage containers on the other.

---

## Machine Details

| Role | Hostname | Internal IP |
|------|----------|-------------|
| Mini PC (ripping) | `MiniPC` | *(run `ipconfig` to confirm)* |
| Chrisdesktop (transcoding/pipeline) | `Chrisdesktop` | *(run `ipconfig` to confirm)* |
| NAS (storage only, no Docker) | `NAS` | *(check router DHCP)* |

---

## Method: SSH-based Docker Contexts

Docker contexts over SSH are the recommended approach — no TLS certificates
to manage, uses Windows OpenSSH which is already installed, and is fully
bidirectional.

### One-time SSH key setup

Run this **on the Mini PC** (source machine):

```powershell
# Generate an SSH key pair dedicated to Docker remote access
ssh-keygen -t ed25519 -C "minipc-docker-remote" -f "$env:USERPROFILE\.ssh\docker_remote" -N '""'

# Copy the public key to Chrisdesktop (you'll be prompted for the password once)
$pubKey = Get-Content "$env:USERPROFILE\.ssh\docker_remote.pub"
ssh chris@Chrisdesktop "mkdir -p ~/.ssh; echo '$pubKey' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys"

# Test the connection (should work without password)
ssh -i "$env:USERPROFILE\.ssh\docker_remote" chris@Chrisdesktop "echo connected"
```

Run this **on Chrisdesktop** (to allow Mini PC to connect back):

```powershell
ssh-keygen -t ed25519 -C "chrisdesktop-docker-remote" -f "$env:USERPROFILE\.ssh\docker_remote" -N '""'
$pubKey = Get-Content "$env:USERPROFILE\.ssh\docker_remote.pub"
ssh chris@MiniPC "mkdir -p ~/.ssh; echo '$pubKey' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys"
ssh -i "$env:USERPROFILE\.ssh\docker_remote" chris@MiniPC "echo connected"
```

### Create Docker contexts

Run the `setup-docker-contexts.ps1` script (in `scripts/`) on each machine:

```powershell
# On Mini PC — creates a context to reach Chrisdesktop
.\scripts\setup-docker-contexts.ps1

# On Chrisdesktop — creates a context to reach Mini PC
.\scripts\setup-docker-contexts.ps1
```

### Verify

```powershell
docker context ls
# Should show: default, chrisdesktop (or minipc)

docker --context chrisdesktop ps
# Should list containers running on Chrisdesktop

docker --context minipc ps
# Should list ARM container on Mini PC
```

---

## Deploying to Chrisdesktop from Mini PC

```powershell
# Start the full Chrisdesktop stack (Tdarr + media-organizer + pipeline) remotely
docker --context chrisdesktop compose -f chrisdesktop/docker-compose.yml up -d
docker --context chrisdesktop compose -f pipeline/docker-compose.yml up -d

# Check status
docker --context chrisdesktop ps

# View pipeline dashboard logs
docker --context chrisdesktop logs media-pipeline --tail 20
```

---

## NAS Access (SMB shares)

The NAS does not run Docker. Both machines access it via SMB:

```
\\NAS\Lossless   — uncompressed MKV archive (permanent)
\\NAS\Plex       — H.265 transcoded files (Plex library)
```

To verify NAS access from either machine:
```powershell
Test-Path "\\NAS\Lossless"
Test-Path "\\NAS\Plex"
```

If authentication is needed, map the drives:
```powershell
net use Z: \\NAS\Lossless /persistent:yes
net use Y: \\NAS\Plex /persistent:yes
```

---

## Mini PC Share (for Pipeline Manager to read rip output)

The pipeline service on Chrisdesktop needs to read completed rips from the Mini PC.
Create a share on the Mini PC:

```powershell
# Run as Administrator on Mini PC
New-SmbShare -Name "BackupOfMedia" -Path "C:\BackupOfMedia" -FullAccess "Everyone"
```

Accessible as: `\\MiniPC\BackupOfMedia`

---

## Docker Compose Remote Deploy Reference

```powershell
# Always pass --context before the subcommand
docker --context chrisdesktop compose -f <path>/docker-compose.yml up -d
docker --context chrisdesktop compose -f <path>/docker-compose.yml down
docker --context chrisdesktop compose -f <path>/docker-compose.yml logs -f
docker --context chrisdesktop compose -f <path>/docker-compose.yml pull

# Or switch the default context
docker context use chrisdesktop
docker compose up -d          # now targets Chrisdesktop
docker context use default    # switch back
```

---

## SSH Config for convenience (optional)

Add to `~/.ssh/config` on each machine for password-less shortcuts:

```
Host chrisdesktop
    HostName Chrisdesktop
    User chris
    IdentityFile ~/.ssh/docker_remote
    StrictHostKeyChecking no

Host minipc
    HostName MiniPC
    User chris
    IdentityFile ~/.ssh/docker_remote
    StrictHostKeyChecking no
```
