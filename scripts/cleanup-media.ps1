<#
.SYNOPSIS
    Cleans up ARM media storage:
    - Removes empty folder structures left after files were moved by robocopy
    - Moves files stuck in raw/ to completed/
    - Reports unidentified discs that need manual intervention

    Run from the Mini PC (or anywhere with access to C:\BackupOfMedia).

.NOTES
    Uses -WhatIf mode by default. Run with -Execute to apply changes.
#>

param(
    [switch]$Execute,
    [string]$MediaRoot = "C:\BackupOfMedia\media"
)

$ErrorActionPreference = "Continue"

$dryRun = -not $Execute
if ($dryRun) {
    Write-Host "DRY RUN MODE - no changes will be made. Use -Execute to apply." -ForegroundColor Yellow
    Write-Host ""
}

$completed   = Join-Path $MediaRoot "completed"
$raw         = Join-Path $MediaRoot "raw"
$transcode   = Join-Path $MediaRoot "transcode"

$deleted     = 0
$moved       = 0
$warnings    = @()

# --------------------------------------------------------------------------
function Remove-EmptyDirs {
    param([string]$Path, [string]$Label)
    if (-not (Test-Path $Path)) { return }

    # Process deepest-first so parent dirs become empty after children are removed
    $allDirs = Get-ChildItem $Path -Recurse -Directory -ErrorAction SilentlyContinue |
               Sort-Object { $_.FullName.Length } -Descending

    foreach ($dir in $allDirs) {
        $children = Get-ChildItem $dir.FullName -ErrorAction SilentlyContinue
        if (($children | Measure-Object).Count -eq 0) {
            $rel = $dir.FullName.Replace($MediaRoot + "\", "")
            if ($dryRun) {
                Write-Host "  [DELETE empty] $rel" -ForegroundColor DarkGray
            } else {
                Remove-Item $dir.FullName -Force -ErrorAction SilentlyContinue
                Write-Host "  Deleted: $rel" -ForegroundColor Green
            }
            $script:deleted++
        }
    }
}

function Move-RawToCompleted {
    param([string]$SrcFolder, [string]$ProperTitle)
    $dst = Join-Path $completed "movies" $ProperTitle
    $rel = $SrcFolder.Replace($MediaRoot + "\", "")

    if ($dryRun) {
        Write-Host "  [MOVE] $rel" -ForegroundColor Cyan
        Write-Host "      -> completed\movies\$ProperTitle" -ForegroundColor Cyan
    } else {
        New-Item -ItemType Directory -Path (Split-Path $dst) -Force | Out-Null
        if (Test-Path $dst) {
            # Merge if destination exists
            Get-ChildItem $SrcFolder | ForEach-Object {
                $target = Join-Path $dst $_.Name
                if (-not (Test-Path $target)) { Move-Item $_.FullName $target }
            }
            Remove-Item $SrcFolder -Recurse -Force -ErrorAction SilentlyContinue
        } else {
            Move-Item $SrcFolder $dst
        }
        Write-Host "  Moved: $rel -> completed\movies\$ProperTitle" -ForegroundColor Green
    }
    $script:moved++
}

# --------------------------------------------------------------------------
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  Media Storage Cleanup" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# ---- Step 1: Move stuck files from raw/ ----
Write-Host "=== Step 1: Moving stuck files from raw/ ===" -ForegroundColor Yellow

if (Test-Path $raw) {
    $rawDirs = Get-ChildItem $raw -Directory -ErrorAction SilentlyContinue |
               Where-Object {
                   (Get-ChildItem $_.FullName -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count -gt 0
               }

    if ($rawDirs.Count -eq 0) {
        Write-Host "  raw/ is clean - no stuck files." -ForegroundColor Green
    }

    foreach ($dir in $rawDirs) {
        $name  = $dir.Name
        $files = Get-ChildItem $dir.FullName -Recurse -File -ErrorAction SilentlyContinue
        $totalMB = [math]::Round(($files | Measure-Object Length -Sum).Sum / 1MB, 0)

        Write-Host ""
        Write-Host "  Found in raw/: $name ($totalMB MB, $($files.Count) files)" -ForegroundColor White

        # Try to derive a clean title from the folder name
        # e.g. "Chicken-Little" -> "Chicken-Little (2005)" is known
        # e.g. "Bringing-the-Legend..." -> keep as-is, just move
        $cleanName = $name -replace '_\d+$', ''  # remove trailing _<timestamp>

        # Known mappings (ARM uses disc labels as folder names when identification fails)
        $knownTitles = @{
            "Chicken-Little"  = "Chicken-Little (2005)"
            "LB1"             = "Chicken-Little (2005)"
        }

        $properTitle = $knownTitles[$cleanName]
        if (-not $properTitle) { $properTitle = $cleanName }

        Move-RawToCompleted -SrcFolder $dir.FullName -ProperTitle $properTitle
    }
}

# ---- Step 2: Remove all empty directories ----
Write-Host ""
Write-Host "=== Step 2: Removing empty directories ===" -ForegroundColor Yellow

Write-Host "  Scanning completed/..." -ForegroundColor Gray
Remove-EmptyDirs -Path $completed -Label "completed"

Write-Host "  Scanning transcode/ (should all be empty with SKIP_TRANSCODE=true)..." -ForegroundColor Gray
Remove-EmptyDirs -Path $transcode -Label "transcode"

Write-Host "  Scanning raw/ (mopping up any leftover empty shells)..." -ForegroundColor Gray
Remove-EmptyDirs -Path $raw -Label "raw"

# ---- Step 3: Report unidentified discs ----
Write-Host ""
Write-Host "=== Step 3: Unidentified discs needing attention ===" -ForegroundColor Yellow

$unidentifiedPath = Join-Path $completed "unidentified"
if (Test-Path $unidentifiedPath) {
    $unidentified = Get-ChildItem $unidentifiedPath -Directory -ErrorAction SilentlyContinue |
                    Where-Object {
                        (Get-ChildItem $_.FullName -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count -gt 0
                    }
    if ($unidentified.Count -eq 0) {
        Write-Host "  No unidentified discs with content." -ForegroundColor Green
    }
    foreach ($dir in $unidentified) {
        $files = Get-ChildItem $dir.FullName -Recurse -File -ErrorAction SilentlyContinue
        $sizeMB = [math]::Round(($files | Measure-Object Length -Sum).Sum / 1MB, 0)
        Write-Host ""
        Write-Host "  NEEDS ATTENTION: $($dir.Name)" -ForegroundColor Red
        Write-Host "    Path: $($dir.FullName)" -ForegroundColor White
        Write-Host "    Size: $sizeMB MB, $($files.Count) files" -ForegroundColor White
        Write-Host "    Files: $(($files | Select-Object -First 3 -ExpandProperty Name) -join ', ')..." -ForegroundColor Gray
        Write-Host "    Action: Log in to ARM at http://localhost:8080 and identify this disc," -ForegroundColor Yellow
        Write-Host "            OR rename the folder manually to 'Movie Title (Year)'" -ForegroundColor Yellow
    }
}

# ---- Step 4: Show final state ----
Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  Summary" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
if ($dryRun) {
    Write-Host "  [DRY RUN] Would delete: $deleted empty dirs" -ForegroundColor Yellow
    Write-Host "  [DRY RUN] Would move:   $moved items from raw/ to completed/" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Run with -Execute to apply these changes:" -ForegroundColor Cyan
    Write-Host "    .\scripts\cleanup-media.ps1 -Execute" -ForegroundColor White
} else {
    Write-Host "  Deleted: $deleted empty dirs" -ForegroundColor Green
    Write-Host "  Moved:   $moved items from raw/ to completed/" -ForegroundColor Green
}

Write-Host ""
Write-Host "Current media state:" -ForegroundColor Cyan
Get-ChildItem $completed -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -match '\.(mkv|mp4|avi|flac)' } |
    ForEach-Object { "  $([math]::Round($_.Length/1GB,2)) GB  $($_.FullName.Replace($MediaRoot+'\',''))" }
