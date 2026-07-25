# BackupMyMedia

A self-hosted, fully automated media ripping, transcoding, and archiving pipeline across three machines.

```
Mini PC (Ripping)  →  Chrisdesktop (Transcoding)  →  NAS (Storage)
      ARM                  Tdarr + Pipeline                Plex + Archive
```

## Architecture

| Machine | Role | Key Services |
|---------|------|-------------|
| **Mini PC** | Rips DVDs/Blu-rays to lossless MKV | ARM (Automatic Ripping Machine) |
| **Chrisdesktop** | Transcodes + manages pipeline | Tdarr, media-organizer, pipeline dashboard |
| **NAS** | Permanent storage (no Docker needed) | `\\NAS\Lossless` + `\\NAS\Plex` |

## Quick Start

### Mini PC (ripping machine)

```powershell
# 1. Copy config (fill in your API keys)
cp ripping-machine/config/arm.yaml.example ripping-machine/config/arm.yaml
# Edit arm.yaml: set OMDB_API_KEY, MAKEMKV_PERMA_KEY

# 2. Attach USB drives to WSL2 (run as Admin, repeat after each reboot)
.\scripts\setup-usb-drives.ps1

# 3. Start ARM
cd ripping-machine
docker compose up -d

# 4. Register auto-start tasks (run as Admin, one-time)
.\scripts\register-startup-task.ps1

# 5. Open ARM UI
Start-Process "http://localhost:8080"
# Default login: admin / password  ← change this immediately
```

### Chrisdesktop (transcoding + pipeline)

See [docs/SETUP-CHRISDESKTOP.md](docs/SETUP-CHRISDESKTOP.md) for the full guide.

```powershell
# Short version:
# 1. Create folder structure on D:\
# 2. Share D:\PlexMedia\Inbox as "Videoinbox"
# 3. docker compose up -d  (in chrisdesktop/ AND pipeline/)
# 4. Configure Tdarr libraries at http://localhost:8265
# 5. Open pipeline dashboard at http://localhost:8090
```

### Docker Cross-Machine Management

See [docs/CONNECTIONS.md](docs/CONNECTIONS.md) to set up SSH-based Docker contexts
so you can deploy to and manage Chrisdesktop from the Mini PC (and vice versa).

```powershell
# After setup:
docker --context chrisdesktop ps              # see Chrisdesktop containers
docker --context chrisdesktop compose -f chrisdesktop/docker-compose.yml up -d
```

## Workflow

1. Insert disc on Mini PC → ARM auto-detects, rips losslessly, ejects
2. `watch-and-sync.ps1` (background task) detects the completed rip and moves it to `\\Chrisdesktop\Videoinbox\`
3. `media-organizer` on Chrisdesktop sorts it into `D:\PlexMedia\Lossless\Movies\` or `TV\` or `Music\`
4. `Tdarr` transcodes it to H.265 → `D:\PlexMedia\Plex\Movies\` or `TV\`
5. Files sync to NAS (configure in `pipeline/docker-compose.yml`)
6. Plex serves from NAS

## Monitoring

| Service | URL | Purpose |
|---------|-----|---------|
| ARM | http://MiniPC:8080 | Rip job status, drive management |
| Tdarr | http://Chrisdesktop:8265 | Transcode queue and workers |
| Pipeline Dashboard | http://Chrisdesktop:8090 | Unified status, problem detection |

## Repo Structure

```
.
├── ripping-machine/          Mini PC - ARM container
│   ├── docker-compose.yml
│   └── config/
│       ├── arm.yaml.example  ← copy to arm.yaml, add your keys
│       └── abcde.conf        Audio CD → FLAC config
│
├── chrisdesktop/             Tdarr + media-organizer
│   ├── docker-compose.yml
│   └── media-organizer/      Python: sorts Inbox → Lossless/
│
├── pipeline/                 Pipeline dashboard + file manager
│   ├── docker-compose.yml
│   ├── Dockerfile
│   └── app/                  FastAPI + SQLite backend
│       └── static/           Web dashboard (SSE real-time)
│
├── scripts/
│   ├── setup-usb-drives.ps1        USB → WSL2 (run as Admin at each boot)
│   ├── register-startup-task.ps1   Register auto-start tasks
│   ├── setup-docker-contexts.ps1   SSH Docker contexts (cross-machine)
│   ├── cleanup-media.ps1           Clean empty dirs, move stuck files
│   ├── build-wsl2-optical-kernel.ps1  Custom WSL2 kernel (one-time)
│   └── fix-usbipd-modules.ps1      USB/IP modules fix (one-time)
│
├── transfer/
│   └── watch-and-sync.ps1    Background watcher: auto-sync to Chrisdesktop
│
└── docs/
    ├── CONNECTIONS.md        Docker cross-machine SSH context setup
    └── SETUP-CHRISDESKTOP.md Full Chrisdesktop installation guide
```

## First-Time WSL2 Kernel Setup (Mini PC only)

The default Windows WSL2 kernel doesn't include optical drive support.
Run this **once** as Administrator to build a custom kernel:

```powershell
.\scripts\build-wsl2-optical-kernel.ps1
# Takes ~20 minutes. Only needed once.
```

Then run:
```powershell
.\scripts\fix-usbipd-modules.ps1
```

## Keys & Configuration

| Key | Where to get it | Where to put it |
|-----|-----------------|-----------------|
| OMDB API key | https://www.omdbapi.com/ | `ripping-machine/config/arm.yaml` |
| MakeMKV key | https://makemkv.com/buy/ | `ripping-machine/config/arm.yaml` |

**Important:** `arm.yaml` is in `.gitignore` to keep keys out of git.
Use `arm.yaml.example` as the template.
