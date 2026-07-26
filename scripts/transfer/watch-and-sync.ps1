<#
.SYNOPSIS
    Watches C:\BackupOfMedia for completed ARM rips and automatically syncs
    them to \\Chrisdesktop\Videoinbox as each job finishes.

.DESCRIPTION
    ARM moves completed rips from media/raw/ -> media/completed/ atomically
    (same-volume rename), so files are fully written the moment they appear.
    This watcher fires on that event and immediately robocopy-moves the folder
    to Chrisdesktop, then removes the local copy.

    Also watches the music/ folder for completed CD rips.

    Run at startup via Task Scheduler (registered by register-startup-task.ps1).
    Runs silently in the background; all activity logged to C:\BackupOfMedia\sync.log

.NOTES
    Does NOT require Administrator - only needs file system + network access.
#>

$SRC_COMPLETED_MOVIES      = "C:\BackupOfMedia\media\completed\movies"
$SRC_COMPLETED_UNIDENTIFIED = "C:\BackupOfMedia\media\completed\unidentified"
$SRC_MUSIC       = "C:\BackupOfMedia\music"
$DST_BASE=        "\\\\Chrisdesktop\\Videoinbox"
$DST_COMPLETED   = "$DST_BASE\completed"
$DST_MUSIC       = "$DST_BASE\music"
$LOG              = "C:\BackupOfMedia\sync.log"
$RETRY_INTERVAL   = 300   # seconds between offline-retry sweeps
$MUSIC_SETTLE_SEC = 60    # seconds to wait after last music file write before syncing

# --------------------------------------------------------------------------
function Write-Log {
    param([string]$Msg, [string]$Level = "INFO")
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [$Level] $Msg"
    Add-Content -Path $LOG -Value $line -Encoding UTF8
    Write-Host $line
}

# --------------------------------------------------------------------------
function Test-Destination {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        Write-Log "Destination unreachable: $Path - will retry later" "WARN"
        return $false
    }
    return $true
}

# --------------------------------------------------------------------------
function Sync-Folder {
    param(
        [string]$SrcFolder,
        [string]$DstParent,
        [string]$Label
    )
    $folderName = Split-Path $SrcFolder -Leaf
    $dst = Join-Path $DstParent $folderName

    if (-not (Test-Destination $DstParent)) { return $false }

    Write-Log "[$Label] Syncing: $folderName"
    $result = robocopy "$SrcFolder" "$dst" /E /MOV /MT:16 /R:3 /W:10 /NP /NDL /LOG+:"$LOG" 2>&1
    $rc = $LASTEXITCODE

    # Robocopy exit codes: 0-7 = success (8+ = errors)
    if ($rc -ge 8) {
        Write-Log "[$Label] Robocopy error (exit $rc) for: $folderName" "ERROR"
        return $false
    }

    # Remove now-empty source directory
    if ((Get-ChildItem $SrcFolder -Recurse -ErrorAction SilentlyContinue | Measure-Object).Count -eq 0) {
        Remove-Item $SrcFolder -Recurse -Force -ErrorAction SilentlyContinue
    }

    Write-Log "[$Label] Done: $folderName -> $DstParent"
    return $true
}

# --------------------------------------------------------------------------
# Sync any items already in completed/ or music/ at startup
# (handles leftover items from when Chrisdesktop was offline)
function Sync-Existing {
    Write-Log "Scanning for existing completed items..."

    foreach ($dir in (Get-ChildItem $SRC_COMPLETED_MOVIES -Directory -ErrorAction SilentlyContinue)) {
        Sync-Folder -SrcFolder $dir.FullName -DstParent $DST_COMPLETED -Label "Video"
    }
    foreach ($dir in (Get-ChildItem $SRC_COMPLETED_UNIDENTIFIED -Directory -ErrorAction SilentlyContinue)) {
        $count = (Get-ChildItem $dir.FullName -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count
        if ($count -gt 0) {
            Write-Log "UNIDENTIFIED disc: $($dir.Name) ($count files) - identify in ARM UI or rename to 'Title (Year)'" "WARN"
        }
    }
    foreach ($dir in (Get-ChildItem $SRC_MUSIC -Directory -ErrorAction SilentlyContinue)) {
        Sync-Folder -SrcFolder $dir.FullName -DstParent $DST_MUSIC -Label "Music"
    }
}

# --------------------------------------------------------------------------
# Track music folders being written to (FLAC encoding takes time)
$musicPending = @{}   # folderPath -> last-write DateTime

# --------------------------------------------------------------------------
Write-Log "=== watch-and-sync starting ==="
Write-Log "Watching: $SRC_COMPLETED_MOVIES"
Write-Log "Watching: $SRC_COMPLETED_UNIDENTIFIED (unidentified - logged as warnings)"
Write-Log "Watching: $SRC_MUSIC"
Write-Log "Destination: \\Chrisdesktop\Videoinbox"

# Ensure source directories exist
foreach ($d in @($SRC_COMPLETED_MOVIES, $SRC_COMPLETED_UNIDENTIFIED, $SRC_MUSIC)) {
    New-Item -ItemType Directory -Path $d -Force -ErrorAction SilentlyContinue | Out-Null
}

# Process any leftovers from previous sessions
Sync-Existing

# --------------------------------------------------------------------------
# Set up FileSystemWatcher for completed/movies/ (video rips)
$watcherVideo = New-Object System.IO.FileSystemWatcher
$watcherVideo.Path                   = $SRC_COMPLETED_MOVIES
$watcherVideo.IncludeSubdirectories  = $false
$watcherVideo.NotifyFilter           = [System.IO.NotifyFilters]::DirectoryName
$watcherVideo.EnableRaisingEvents    = $true

# Also watch unidentified/ for new disc labels (log them as warnings)
$watcherUnident = New-Object System.IO.FileSystemWatcher
$watcherUnident.Path                 = $SRC_COMPLETED_UNIDENTIFIED
$watcherUnident.IncludeSubdirectories = $false
$watcherUnident.NotifyFilter         = [System.IO.NotifyFilters]::DirectoryName
$watcherUnident.EnableRaisingEvents  = $true

$actionVideo = {
    $path = $Event.SourceEventArgs.FullPath
    $name = $Event.SourceEventArgs.Name
    # Small delay to let ARM finish the directory rename
    Start-Sleep -Seconds 5
    if (Test-Path $path) {
        # Call Sync-Folder in the same scope
        $result = & {
            $SRC_COMPLETED = $using:SRC_COMPLETED_MOVIES
            $DST_COMPLETED = $using:DST_COMPLETED
            $LOG           = $using:LOG
            function Write-Log { param($m,$l="INFO"); $line="$(Get-Date -f 'yyyy-MM-dd HH:mm:ss') [$l] $m"; Add-Content $using:LOG $line -Encoding UTF8; Write-Host $line }
            function Test-Destination { param($p); if(-not(Test-Path $p)){Write-Log "Destination unreachable: $p" "WARN";return $false};return $true }
            Write-Log "[Video] New rip detected: $name"
            if (Test-Destination $DST_COMPLETED) {
                $dst = Join-Path $DST_COMPLETED $name
                robocopy $path $dst /E /MOV /MT:16 /R:3 /W:10 /NP /NDL /LOG+:$LOG 2>&1 | Out-Null
                if ($LASTEXITCODE -lt 8) {
                    Remove-Item $path -Recurse -Force -ErrorAction SilentlyContinue
                    Write-Log "[Video] Synced: $name"
                } else {
                    Write-Log "[Video] Robocopy failed (exit $LASTEXITCODE): $name" "ERROR"
                }
            }
        }
    }
}
Register-ObjectEvent -InputObject $watcherVideo -EventName Created -Action $actionVideo -SourceIdentifier "VideoRipCreated" | Out-Null

# --------------------------------------------------------------------------
# Set up FileSystemWatcher for music/ (CD rips - files written over time)
$watcherMusic = New-Object System.IO.FileSystemWatcher
$watcherMusic.Path                  = $SRC_MUSIC
$watcherMusic.IncludeSubdirectories = $true
$watcherMusic.NotifyFilter          = [System.IO.NotifyFilters]'LastWrite,FileName,DirectoryName'
$watcherMusic.EnableRaisingEvents   = $true

$actionMusic = {
    # Record the top-level artist folder as having recent activity
    $fullPath = $Event.SourceEventArgs.FullPath
    $rel      = $fullPath.Substring($using:SRC_MUSIC.Length).TrimStart('\').Split('\')[0]
    if ($rel) {
        $script:musicPending[$rel] = [DateTime]::Now
    }
}
Register-ObjectEvent -InputObject $watcherMusic -EventName Created -Action $actionMusic -SourceIdentifier "MusicCreated" | Out-Null
Register-ObjectEvent -InputObject $watcherMusic -EventName Changed -Action $actionMusic -SourceIdentifier "MusicChanged" | Out-Null

# --------------------------------------------------------------------------
Write-Log "Watching for new rips... (Ctrl-C to stop)"

# Main loop: check music pending queue every 15 seconds
while ($true) {
    Start-Sleep -Seconds 15

    # Sync music folders that have been quiet for MUSIC_SETTLE_SEC seconds
    $now    = [DateTime]::Now
    $toSync = @($musicPending.Keys | Where-Object {
        ($now - $musicPending[$_]).TotalSeconds -ge $MUSIC_SETTLE_SEC
    })
    foreach ($artistFolder in $toSync) {
        $srcPath = Join-Path $SRC_MUSIC $artistFolder
        if (Test-Path $srcPath) {
            Sync-Folder -SrcFolder $srcPath -DstParent $DST_MUSIC -Label "Music"
        }
        $musicPending.Remove($artistFolder)
    }
}
