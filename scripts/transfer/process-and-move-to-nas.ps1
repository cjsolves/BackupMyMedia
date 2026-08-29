<#
.SYNOPSIS
        Moves finished media to NAS only after the correct pipeline stage is complete.

.DESCRIPTION
        Video items are "ready" only when Tdarr has already produced the local Plex copy:
            - Pipeline state = complete
            - media_type = movie or tv
            - Source folder exists under D:\PlexMedia\Plex\Movies or D:\PlexMedia\Plex\TV

        Music bypasses Tdarr and is ready when:
            - Pipeline state = on_nas_lossless
            - media_type = music
            - Source folder exists under D:\PlexMedia\Lossless\Music

    For each ready item the script:
            1. Moves the final local output to the correct NAS destination
      2. Deletes any leftover cache files on D: for that item
      3. Tells the pipeline API the item is complete (so the dashboard stays accurate)

.NOTES
    Registered as a Windows Scheduled Task — runs every 30 minutes.
    Requires: P:\ = \\NAS\Plex  (persistent net use mapping)
    Run manually:  & "D:\VidProcess\BackupMyMedia\scripts\transfer\process-and-move-to-nas.ps1"
#>

$PIPELINE_API  = "http://localhost:8090"
$LOSSLESS_ROOT = "D:\PlexMedia\Lossless"
$PLEX_ROOT     = "D:\PlexMedia\Plex"
$NAS_LOSSLESS_ROOT = "L:\"
$NAS_PLEX_ROOT = "P:\"
$LOG           = "D:\VidProcess\process-to-nas.log"

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content $LOG $line -Encoding UTF8
    Write-Host $line
}

function Move-FolderToNas($src, $dst) {
    if (Test-Path $dst) {
        # Destination exists — merge missing files then remove source
        Get-ChildItem $src -Recurse -File | ForEach-Object {
            $rel  = $_.FullName.Substring($src.Length)
            $dest = $dst + $rel
            if (-not (Test-Path $dest)) {
                New-Item (Split-Path $dest) -ItemType Directory -Force | Out-Null
                Move-Item $_.FullName $dest -Force
            }
        }
        Remove-Item $src -Recurse -Force -EA SilentlyContinue
    } else {
        New-Item (Split-Path $dst -Parent) -ItemType Directory -Force | Out-Null
        Move-Item $src $dst -Force
    }
}

function Delete-CacheFor($itemId, $subdir) {
    # Remove any leftover D: copies for this item (Lossless and Plex cache)
    foreach ($base in @("$LOSSLESS_ROOT\$subdir", "D:\PlexMedia\Plex\$subdir")) {
        $path = Join-Path $base $itemId
        if (Test-Path $path) {
            Remove-Item $path -Recurse -Force -EA SilentlyContinue
            Write-Log "  Deleted cache: $path"
        }
    }
}

# ---- Skip items currently being upscaled
$upscaleQueue = (Get-ChildItem "D:\PlexMedia\UpscaleQueue" -Directory -EA SilentlyContinue).Name

# ---- Get pipeline items
try {
    $items = Invoke-RestMethod "$PIPELINE_API/api/items" -EA Stop
} catch {
    Write-Log "ERROR: Cannot reach pipeline API — is the pipeline container running?"
    exit 1
}

# ---- Find ready items
$readyVideo = $items | Where-Object {
    $_.state -eq "complete" -and
    $_.media_type -in @("movie", "tv") -and
    ($upscaleQueue -notcontains $_.id)
}

$readyMusic = $items | Where-Object {
    $_.state -eq "on_nas_lossless" -and
    $_.media_type -eq "music" -and
    ($upscaleQueue -notcontains $_.id)
}

$ready = @($readyVideo) + @($readyMusic)

Write-Log "=== NAS move started: $($ready.Count) item(s) ready ==="

if ($ready.Count -eq 0) {
    # Show pending counts for visibility
    $waitingUpscale = $items | Where-Object { $_.state -eq "on_nas_lossless" -and $_.media_type -ne "music" }
    $waitingTdarr   = $items | Where-Object { $_.media_type -in @("movie", "tv") -and $_.state -in @("on_nas_lossless", "queued_transcode", "transcoding") }
    Write-Log "  Waiting on upscale: $((@($waitingUpscale | Where-Object {$_.upscale_status -eq 'queued'})).Count) queued, $((@($waitingUpscale | Where-Object {$_.upscale_status -eq 'processing'})).Count) processing, $((@($waitingUpscale | Where-Object {-not $_.upscale_status})).Count) unchecked"
    Write-Log "  Waiting on Tdarr/Plex output: $($waitingTdarr.Count)"
    exit 0
}

$moved = 0; $failed = 0

foreach ($item in $ready) {
    $id     = $item.id
    $media  = $item.media_type
    $subdir = @{movie="Movies"; tv="TV"; music="Music"}[$media]
    if (-not $subdir) { $subdir = "Movies" }

    if ($media -eq "music") {
        $src = Join-Path "$LOSSLESS_ROOT\Music" $id
        $dstRoot = "$NAS_LOSSLESS_ROOT\Music"
        $dst = Join-Path $dstRoot $id
        $nasPath = "\\NAS\Lossless\Music\$id"
        $label = "NAS Lossless\Music"
    } else {
        $src = Join-Path "$PLEX_ROOT\$subdir" $id
        $dstRoot = "$NAS_PLEX_ROOT\$subdir"
        $dst = Join-Path $dstRoot $id
        $nasPath = "\\NAS\Plex\$subdir\$id"
        $label = "NAS Plex\$subdir"
    }

    if (-not (Test-Path $src)) {
        Write-Log "  SKIP (source not found): $id"
        continue
    }

    if (-not (Test-Path $dstRoot)) {
        New-Item $dstRoot -ItemType Directory -Force | Out-Null
    }

    Write-Log "  Moving: $id"
    try {
        Move-FolderToNas $src $dst

        # Delete any remaining D: cache for this item
        Delete-CacheFor $id $subdir

        # Tell pipeline the item is complete on NAS
        Invoke-RestMethod "$PIPELINE_API/api/items/$([Uri]::EscapeDataString($id))/moved_to_nas" `
            -Method POST -ContentType "application/json" `
            -Body (ConvertTo-Json @{nas_plex_path=$nasPath}) -EA SilentlyContinue | Out-Null

        Write-Log "  Done: $id -> $label"
        $moved++
    } catch {
        Write-Log "  FAILED: $id — $_"
        $failed++
    }
}

Write-Log "=== Complete: moved=$moved failed=$failed ==="
Write-Log "  D: free: $([math]::Round((Get-PSDrive D).Free/1GB,1)) GB"
