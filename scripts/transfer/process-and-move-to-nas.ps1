<#
.SYNOPSIS
    Moves finished media from D:\PlexMedia\Lossless to NAS Plex (P:\Movies / P:\TV).

.DESCRIPTION
    An item is "ready" when:
      - Pipeline state = on_nas_lossless
      - upscale_status = skipped (already 1080p+, no upscaling needed)
      - OR upscale_status = complete (upscaler finished, file replaced in Lossless)

    For each ready item the script:
      1. Moves the folder from D:\Lossless to P:\ (NAS Plex)
      2. Deletes any leftover cache files on D: for that item
      3. Tells the pipeline API the item is complete (so the dashboard stays accurate)

.NOTES
    Registered as a Windows Scheduled Task — runs every 30 minutes.
    Requires: P:\ = \\NAS\Plex  (persistent net use mapping)
    Run manually:  & "D:\VidProcess\BackupMyMedia\scripts\transfer\process-and-move-to-nas.ps1"
#>

$PIPELINE_API  = "http://localhost:8090"
$LOSSLESS_ROOT = "D:\PlexMedia\Lossless"
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
$ready = $items | Where-Object {
    $_.state -eq "on_nas_lossless" -and
    $_.upscale_status -in @("skipped", "complete") -and
    ($upscaleQueue -notcontains $_.id)
}

Write-Log "=== NAS move started: $($ready.Count) item(s) ready ==="

if ($ready.Count -eq 0) {
    # Show pending counts for visibility
    $pending = $items | Where-Object { $_.state -eq "on_nas_lossless" -and $_.upscale_status -notin @("skipped","complete") }
    Write-Log "  Waiting on upscale: $($pending | Where-Object {$_.upscale_status -eq 'queued'} | Measure-Object).Count queued, $($pending | Where-Object {$_.upscale_status -eq 'processing'} | Measure-Object).Count processing, $($pending | Where-Object {-not $_.upscale_status} | Measure-Object).Count unchecked"
    exit 0
}

$moved = 0; $failed = 0

foreach ($item in $ready) {
    $id     = $item.id
    $media  = $item.media_type
    $subdir = @{movie="Movies"; tv="TV"; music="Music"}[$media]
    if (-not $subdir) { $subdir = "Movies" }

    $src = Join-Path "$LOSSLESS_ROOT\$subdir" $id
    $dst = Join-Path "$NAS_PLEX_ROOT\$subdir" $id

    if (-not (Test-Path $src)) {
        Write-Log "  SKIP (source not found): $id"
        continue
    }

    if (-not (Test-Path "$NAS_PLEX_ROOT\$subdir")) {
        New-Item "$NAS_PLEX_ROOT\$subdir" -ItemType Directory -Force | Out-Null
    }

    Write-Log "  Moving: $id"
    try {
        Move-FolderToNas $src $dst

        # Delete any remaining D: cache for this item
        Delete-CacheFor $id $subdir

        # Tell pipeline the item is complete
        $nasPath = "\\NAS\Plex\$subdir\$id"
        Invoke-RestMethod "$PIPELINE_API/api/items/$([Uri]::EscapeDataString($id))/moved_to_nas" `
            -Method POST -ContentType "application/json" `
            -Body (ConvertTo-Json @{nas_plex_path=$nasPath}) -EA SilentlyContinue | Out-Null

        Write-Log "  Done: $id -> NAS Plex\$subdir"
        $moved++
    } catch {
        Write-Log "  FAILED: $id — $_"
        $failed++
    }
}

Write-Log "=== Complete: moved=$moved failed=$failed ==="
Write-Log "  D: free: $([math]::Round((Get-PSDrive D).Free/1GB,1)) GB"
