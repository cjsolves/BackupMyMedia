<#
.SYNOPSIS
    Triggers a Plex Media Server library refresh after new content arrives.
    Run this on Chrisdesktop after new content has been transcoded.
    The pipeline dashboard calls this automatically — run manually if needed.

.NOTES
    Set $env:PLEX_TOKEN or pass -PlexToken to authenticate.
    Find your Plex token: https://support.plex.tv/articles/204059436
#>
param(
    [string]$PlexHost  = "NAS",          # hostname or IP of the machine running Plex
    [string]$PlexPort  = "32400",
    [string]$PlexToken = $env:PLEX_TOKEN  # set via environment or pass as parameter
)

$BaseUrl = "http://${PlexHost}:${PlexPort}"

if ([string]::IsNullOrEmpty($PlexToken)) {
    Write-Host "No PLEX_TOKEN set. Getting sections anonymously (may fail if auth required)..." -ForegroundColor Yellow
    $TokenParam = ""
} else {
    $TokenParam = "?X-Plex-Token=$PlexToken"
}

try {
    # Get all library sections
    $sectionsUrl = "$BaseUrl/library/sections$TokenParam"
    $response = Invoke-WebRequest $sectionsUrl -UseBasicParsing -TimeoutSec 10
    $sections = ([xml]$response.Content).MediaContainer.Directory

    if (-not $sections) {
        Write-Host "No Plex libraries found. Is Plex running on $PlexHost`:$PlexPort ?" -ForegroundColor Red
        exit 1
    }

    Write-Host "Plex libraries found:" -ForegroundColor Cyan
    foreach ($section in $sections) {
        $key = $section.key
        $title = $section.title
        $type = $section.type
        Write-Host "  [$key] $title ($type)" -ForegroundColor Gray

        # Trigger refresh for this section
        $refreshUrl = "$BaseUrl/library/sections/$key/refresh$TokenParam"
        Invoke-WebRequest $refreshUrl -UseBasicParsing -Method GET -TimeoutSec 10 | Out-Null
        Write-Host "  → Refresh triggered" -ForegroundColor Green
    }

    Write-Host ""
    Write-Host "Plex library refresh complete. Scanning $($sections.Count) librar$(if($sections.Count -eq 1){'y'}else{'ies'})." -ForegroundColor Green

} catch {
    Write-Host "Failed to contact Plex at $BaseUrl`: $_" -ForegroundColor Red
    Write-Host ""
    Write-Host "Manual refresh: open http://${PlexHost}:32400/web and click Dashboard → Libraries → Scan" -ForegroundColor Yellow
}
