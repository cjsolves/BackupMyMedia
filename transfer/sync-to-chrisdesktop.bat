@echo off
setlocal enabledelayedexpansion

REM ============================================================================
REM  sync-to-chrisdesktop.bat
REM
REM  Moves all completed rips from this machine to \\Chrisdesktop\Videoinbox.
REM  Uses 16 parallel Robocopy threads for fast network transfers.
REM
REM  Source:
REM    C:\BackupOfMedia\media\completed\  (lossless MKV from ARM)
REM    C:\BackupOfMedia\music\            (FLAC from ARM)
REM
REM  Destination:
REM    \\Chrisdesktop\Videoinbox\completed\
REM    \\Chrisdesktop\Videoinbox\music\
REM
REM  NOTE: /MOV deletes source files after each file is successfully copied.
REM        If you want to KEEP originals locally, replace /MOV with /E.
REM
REM  Run manually, or schedule via Task Scheduler for automatic nightly sync.
REM ============================================================================

SET SRC_VIDEO=C:\BackupOfMedia\media\completed
SET SRC_MUSIC=C:\BackupOfMedia\music
SET DST_BASE=\\Chrisdesktop\Videoinbox
SET DST_VIDEO=%DST_BASE%\completed
SET DST_MUSIC=%DST_BASE%\music
SET LOG=C:\BackupOfMedia\robocopy.log
SET THREADS=16

echo.
echo ============================================================
echo   BackupMyMedia ^> Chrisdesktop Sync
echo   %DATE% %TIME%
echo ============================================================
echo   Source video : %SRC_VIDEO%
echo   Source music : %SRC_MUSIC%
echo   Destination  : %DST_BASE%
echo   Threads      : %THREADS%
echo   Log          : %LOG%
echo ============================================================
echo.

REM -- Check Chrisdesktop share is reachable ----------------------------------
if not exist "%DST_BASE%" (
    echo ERROR: Cannot reach %DST_BASE%
    echo.
    echo   Possible causes:
    echo     - Chrisdesktop is offline or asleep
    echo     - The 'Videoinbox' share does not exist on Chrisdesktop
    echo     - You don't have write permission to the share
    echo     - Network / firewall issue
    echo.
    echo   To create the share on Chrisdesktop:
    echo     1. Create folder D:\PlexMedia\Inbox
    echo     2. Right-click -> Properties -> Sharing -> Share...
    echo     3. Share name: Videoinbox
    echo     4. Give your user Read/Write access
    echo.
    pause
    exit /b 1
)

REM -- Sync completed video rips ---------------------------------------------
echo [1/2] Syncing completed video rips...
echo       %SRC_VIDEO%
echo       --^> %DST_VIDEO%
echo.

robocopy "%SRC_VIDEO%" "%DST_VIDEO%" ^
    /E ^
    /MOV ^
    /MT:%THREADS% ^
    /R:3 ^
    /W:10 ^
    /NP ^
    /NDL ^
    /LOG+:"%LOG%" ^
    /TEE

REM Robocopy exit codes 0-7 are success (8+ = errors)
if %ERRORLEVEL% GEQ 8 (
    echo ERROR: Video sync failed with code %ERRORLEVEL%. Check %LOG%
    set SYNC_FAILED=1
) else (
    echo Video sync complete.
)
echo.

REM -- Sync music ------------------------------------------------------------
echo [2/2] Syncing music...
echo       %SRC_MUSIC%
echo       --^> %DST_MUSIC%
echo.

robocopy "%SRC_MUSIC%" "%DST_MUSIC%" ^
    /E ^
    /MOV ^
    /MT:%THREADS% ^
    /R:3 ^
    /W:10 ^
    /NP ^
    /NDL ^
    /LOG+:"%LOG%" ^
    /TEE

if %ERRORLEVEL% GEQ 8 (
    echo ERROR: Music sync failed with code %ERRORLEVEL%. Check %LOG%
    set SYNC_FAILED=1
) else (
    echo Music sync complete.
)
echo.

REM -- Summary ---------------------------------------------------------------
echo ============================================================
if defined SYNC_FAILED (
    echo   SYNC COMPLETED WITH ERRORS - review %LOG%
) else (
    echo   SYNC COMPLETED SUCCESSFULLY
)
echo   %DATE% %TIME%
echo ============================================================
echo.
pause
