<#
.SYNOPSIS
    Moves completed media from D:\PlexMedia\Lossless to NAS (L:\)
    after upscaling and processing are confirmed done.

.NOTES
    Registered as a Windows Scheduled Task — runs every hour.
    Uses MOVE (not copy) so D: space is freed automatically.
    Skips files currently open by the upscaler (UpscaleQueue).
#>

$LOG = "D:\sync-to-nas.log"

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content $LOG $line -Encoding UTF8
    Write-Host $line
}

# Skip folders that are currently staged for upscaling
$upscaleQueue = (Get-ChildItem "D:\PlexMedia\UpscaleQueue" -Directory -EA SilentlyContinue).Name

function Sync-Folder($srcRoot, $dstRoot, $label) {
    if (-not (Test-Path $srcRoot)) { return }
    if (-not (Test-Path $dstRoot)) { New-Item $dstRoot -ItemType Directory -Force | Out-Null }

    $moved = 0; $skipped = 0

    Get-ChildItem $srcRoot -Directory | ForEach-Object {
        $folder = $_

        # Skip anything currently being upscaled
        if ($upscaleQueue -contains $folder.Name) {
            $skipped++
            return
        }

        $dst = Join-Path $dstRoot $folder.Name
        if (Test-Path $dst) {
            # Destination exists — merge any missing files then remove src
            Get-ChildItem $folder.FullName -Recurse -File | ForEach-Object {
                $rel  = $_.FullName.Substring($folder.FullName.Length)
                $dest = Join-Path $dst $rel
                if (-not (Test-Path $dest)) {
                    New-Item (Split-Path $dest) -ItemType Directory -Force | Out-Null
                    Move-Item $_.FullName $dest -Force
                }
            }
            Remove-Item $folder.FullName -Recurse -Force -EA SilentlyContinue
        } else {
            Move-Item $folder.FullName $dstRoot -Force
        }
        $moved++
    }

    if ($moved -gt 0 -or $skipped -gt 0) {
        Write-Log "[$label] Moved: $moved  Skipped (in upscale queue): $skipped"
    }
}

Write-Log "=== NAS sync started ==="
# Already-H.265 files go directly to NAS Plex (P:\) — no re-encoding needed
# New lossless ARM rips should go to NAS Lossless (L:\) first, then Tdarr compresses to P:\
# For the existing library (already compressed): sync straight to Plex
Sync-Folder "D:\PlexMedia\Lossless\Movies" "P:\Movies" "Movies→NAS Plex"
Sync-Folder "D:\PlexMedia\Lossless\TV"     "P:\TV"     "TV→NAS Plex"
Sync-Folder "D:\PlexMedia\Lossless\Music"  "L:\Music"  "Music→NAS Lossless"
Write-Log "=== NAS sync complete ==="
