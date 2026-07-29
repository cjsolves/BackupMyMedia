#!/bin/bash
# =============================================================================
#  BackupMyMedia - NAS Setup Script
#  One-command installer for NAS media library storage
#
#  Supports:
#    - Synology DSM 6/7
#    - TrueNAS SCALE (Debian-based)
#    - TrueNAS CORE (FreeBSD)
#    - Unraid
#    - Generic Linux NAS (Ubuntu/Debian/Alpine)
#
#  What this does:
#    1. Creates the folder structure for Lossless archive + Plex library
#    2. Sets up SMB shares accessible from Chrisdesktop and Mini PC
#    3. Configures Plex media library paths (if Plex is installed)
#    4. Creates a library rescan script for when new content arrives
#    5. Sets correct permissions for all services
#
#  Usage:
#    ssh admin@NAS "bash <(curl -fsSL https://raw.githubusercontent.com/cjsolves/BackupMyMedia/main/install-nas.sh)"
#    -- OR --
#    git clone https://github.com/cjsolves/BackupMyMedia /tmp/BackupMyMedia
#    bash /tmp/BackupMyMedia/install-nas.sh
# =============================================================================

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
BOLD='\033[1m'; RESET='\033[0m'

log()  { echo -e "${GREEN}[OK]${RESET} $*"; }
warn() { echo -e "${YELLOW}[!!]${RESET} $*"; }
info() { echo -e "${CYAN}[--]${RESET} $*"; }
err()  { echo -e "${RED}[ERR]${RESET} $*"; exit 1; }

echo -e "${BOLD}${CYAN}"
echo "================================================"
echo "  BackupMyMedia - NAS Media Library Setup"
echo "================================================"
echo -e "${RESET}"

# =============================================================================
# Step 1: Detect NAS type and base storage path
# =============================================================================
detect_nas() {
    if [ -f /etc/synoinfo.conf ]; then
        NAS_TYPE="synology"
        # Find the first data volume
        VOLUME=$(ls /volume1 2>/dev/null && echo "/volume1" || ls /volume2 2>/dev/null && echo "/volume2" || echo "/volume1")
        info "Detected: Synology DSM (volume: $VOLUME)"
    elif grep -qi "truenas" /etc/os-release 2>/dev/null; then
        NAS_TYPE="truenas"
        VOLUME="${TRUENAS_POOL:-/mnt/$(ls /mnt | head -1)}"
        info "Detected: TrueNAS SCALE (pool: $VOLUME)"
    elif [ -f /etc/unraid-version ]; then
        NAS_TYPE="unraid"
        VOLUME="${UNRAID_SHARE:-/mnt/user}"
        info "Detected: Unraid (path: $VOLUME)"
    elif [ -f /etc/freebsd-update.conf ]; then
        NAS_TYPE="freebsd"
        VOLUME="${FREEBSD_POOL:-/mnt}"
        info "Detected: FreeBSD/TrueNAS CORE"
    else
        NAS_TYPE="linux"
        info "Detected: Generic Linux NAS"
        VOLUME=""
    fi

    if [ -z "$VOLUME" ]; then
        echo ""
        warn "Could not auto-detect storage volume."
        printf "Enter base storage path (e.g. /volume1 or /mnt/data): "
        read -r VOLUME
    fi

    # Ensure trailing slash removed
    VOLUME="${VOLUME%/}"
    log "Using storage path: $VOLUME"
}

detect_nas

# =============================================================================
# Step 2: Create folder structure
# =============================================================================
echo ""
info "Creating folder structure..."

LOSSLESS="$VOLUME/Lossless"
PLEX="$VOLUME/Plex"
DIRS=(
    "$LOSSLESS/Movies"
    "$LOSSLESS/TV"
    "$LOSSLESS/Music"
    "$PLEX/Movies"
    "$PLEX/TV"
    "$VOLUME/BulkIngest"        # drop existing libraries here for pipeline processing
)

for DIR in "${DIRS[@]}"; do
    mkdir -p "$DIR"
    log "Created: $DIR"
done

# =============================================================================
# Step 3: Set permissions
# =============================================================================
echo ""
info "Setting permissions (world-readable, group-writable)..."

# Create a shared group if it doesn't exist
if ! getent group mediagroup >/dev/null 2>&1; then
    groupadd -f mediagroup 2>/dev/null || true
    log "Created group: mediagroup"
fi

chown -R root:mediagroup "$LOSSLESS" "$PLEX" "$VOLUME/BulkIngest" 2>/dev/null || \
    chown -R nobody:mediagroup "$LOSSLESS" "$PLEX" "$VOLUME/BulkIngest" 2>/dev/null || true

chmod -R 775 "$LOSSLESS" "$PLEX" "$VOLUME/BulkIngest" 2>/dev/null || true
find "$LOSSLESS" "$PLEX" "$VOLUME/BulkIngest" -type d -exec chmod 775 {} \; 2>/dev/null || true
log "Permissions set (775 + mediagroup)"

# =============================================================================
# Step 4: Configure SMB/Samba shares
# =============================================================================
echo ""
info "Configuring SMB shares..."

setup_samba_shares() {
    local SMB_CONF="/etc/samba/smb.conf"

    if [ ! -f "$SMB_CONF" ]; then
        warn "Samba config not found at $SMB_CONF"
        warn "On Synology/TrueNAS: shares must be created via the web UI"
        warn "Required shares:"
        echo "  - Share name: Lossless  →  $LOSSLESS"
        echo "  - Share name: Plex      →  $PLEX"
        echo "  - Share name: BulkIngest → $VOLUME/BulkIngest"
        return
    fi

    # Check if shares already exist
    SHARES_TO_ADD=""
    for share_def in \
        "[Lossless]|$LOSSLESS|Lossless MKV archive - read/write for transcoding machines" \
        "[Plex]|$PLEX|H.265 Plex library - read/write for transcoding machines" \
        "[BulkIngest]|$VOLUME/BulkIngest|Drop existing ripped files here for processing"
    do
        SHARE_NAME=$(echo "$share_def" | cut -d'|' -f1)
        SHARE_PATH=$(echo "$share_def" | cut -d'|' -f2)
        SHARE_COMMENT=$(echo "$share_def" | cut -d'|' -f3)

        if grep -q "^$SHARE_NAME" "$SMB_CONF" 2>/dev/null; then
            warn "Share $SHARE_NAME already exists in $SMB_CONF — skipping"
        else
            cat >> "$SMB_CONF" << EOF

$SHARE_NAME
   comment = $SHARE_COMMENT
   path = $SHARE_PATH
   browsable = yes
   writable = yes
   guest ok = yes
   create mask = 0664
   directory mask = 0775
   force group = mediagroup
EOF
            log "Added SMB share: $SHARE_NAME → $SHARE_PATH"
        fi
    done

    # Restart Samba
    if command -v systemctl >/dev/null 2>&1; then
        systemctl reload smbd 2>/dev/null || systemctl restart smbd 2>/dev/null || true
    elif command -v service >/dev/null 2>&1; then
        service smbd restart 2>/dev/null || true
    fi
    log "Samba reloaded"
}

setup_samba_shares

# =============================================================================
# Step 5: Plex Media Server library configuration
# =============================================================================
echo ""
info "Configuring Plex Media Server..."

PLEX_DATA=""
PLEX_BIN=""

# Detect Plex installation
for PLEX_CANDIDATE in \
    "/var/packages/PlexMediaServer/shares/PlexMediaServer" \
    "/var/lib/plexmediaserver" \
    "/usr/local/plexdata-plexpass" \
    "/usr/local/plexdata" \
    "/mnt/user/appdata/PlexMediaServer"
do
    if [ -d "$PLEX_CANDIDATE" ]; then
        PLEX_DATA="$PLEX_CANDIDATE"
        break
    fi
done

for PLEX_BIN_CANDIDATE in \
    "/usr/lib/plexmediaserver/Plex Media Scanner" \
    "/var/packages/PlexMediaServer/target/Plex Media Scanner" \
    "/Applications/Plex Media Server.app/Contents/MacOS/Plex Media Scanner"
do
    if [ -f "$PLEX_BIN_CANDIDATE" ]; then
        PLEX_BIN="$PLEX_BIN_CANDIDATE"
        break
    fi
done

if [ -n "$PLEX_DATA" ]; then
    log "Found Plex data at: $PLEX_DATA"

    # Create the plex-refresh.sh script
    cat > /usr/local/bin/plex-refresh.sh << PLEXREFRESH
#!/bin/bash
# Trigger Plex library scan for all sections
# Call this after new content arrives (pipeline does this automatically)
PLEX_TOKEN_FILE="\${PLEX_DATA:-$PLEX_DATA}/Library/Application Support/Plex Media Server/token"
if [ -f "\$PLEX_TOKEN_FILE" ]; then
    TOKEN=\$(cat "\$PLEX_TOKEN_FILE")
    curl -s "http://localhost:32400/library/sections/all/refresh?X-Plex-Token=\$TOKEN" > /dev/null
    echo "Plex library refresh triggered"
else
    echo "Plex token not found - scan via Plex web UI or set PLEX_TOKEN env var"
fi
PLEXREFRESH
    chmod +x /usr/local/bin/plex-refresh.sh
    log "Created: /usr/local/bin/plex-refresh.sh"
else
    warn "Plex Media Server not detected on this NAS"
    warn "Install Plex from your NAS package manager, then point libraries at:"
    echo "  Movies: $PLEX/Movies"
    echo "  TV:     $PLEX/TV"
    echo "  Music:  $LOSSLESS/Music"
fi

# =============================================================================
# Step 6: Save config for other machines to reference
# =============================================================================
NAS_HOSTNAME=$(hostname)
CONFIG_FILE="$VOLUME/BackupMyMedia-NAS-Config.txt"
cat > "$CONFIG_FILE" << CONFIG
# BackupMyMedia NAS Configuration
# Generated: $(date)
NAS_HOSTNAME=$NAS_HOSTNAME
NAS_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
NAS_TYPE=$NAS_TYPE
VOLUME=$VOLUME
LOSSLESS_PATH=$LOSSLESS
PLEX_PATH=$PLEX
BULK_INGEST_PATH=$VOLUME/BulkIngest

# SMB share paths (use these in Chrisdesktop docker-compose.yml)
NAS_LOSSLESS_UNC=\\\\$NAS_HOSTNAME\\Lossless
NAS_PLEX_UNC=\\\\$NAS_HOSTNAME\\Plex

# Plex library paths (use these in Plex Media Server)
PLEX_MOVIES=$PLEX/Movies
PLEX_TV=$PLEX/TV
PLEX_MUSIC=$LOSSLESS/Music
CONFIG
log "Config saved: $CONFIG_FILE"

# =============================================================================
# Summary
# =============================================================================
echo ""
echo -e "${BOLD}${GREEN}================================================${RESET}"
echo -e "${BOLD}${GREEN}  NAS Setup Complete!${RESET}"
echo -e "${BOLD}${GREEN}================================================${RESET}"
echo ""
echo -e "${BOLD}Folder structure created:${RESET}"
echo "  $LOSSLESS/Movies     ← Lossless MKV archive (permanent)"
echo "  $LOSSLESS/TV"
echo "  $LOSSLESS/Music      ← FLAC music (Plex music library)"
echo "  $PLEX/Movies         ← H.265 transcoded (Plex video library)"
echo "  $PLEX/TV"
echo "  $VOLUME/BulkIngest   ← Drop large existing libraries here"
echo ""
echo -e "${BOLD}SMB shares (access from Windows):${RESET}"
echo "  \\\\${NAS_HOSTNAME}\\Lossless"
echo "  \\\\${NAS_HOSTNAME}\\Plex"
echo "  \\\\${NAS_HOSTNAME}\\BulkIngest"
echo ""
echo -e "${BOLD}Plex library paths:${RESET}"
echo "  Movies: $PLEX/Movies"
echo "  TV:     $PLEX/TV"
echo "  Music:  $LOSSLESS/Music"
echo ""
echo -e "${BOLD}${YELLOW}NEXT STEPS:${RESET}"
echo "  1. On Chrisdesktop: pull the latest repo and update docker-compose.yml"
echo "     with the NAS hostname/IP in the volume mounts"
echo "  2. On Chrisdesktop: docker compose up -d (restarts with NAS mounts)"
echo "  3. In Plex: add the three library paths listed above"
echo "  4. Drop existing ripped files in: $VOLUME/BulkIngest"
echo "     Then trigger: POST http://Chrisdesktop:8090/api/bulk_intake/scan"
echo ""
if [ -n "$PLEX_DATA" ]; then
    echo -e "  5. To manually trigger Plex rescan: /usr/local/bin/plex-refresh.sh"
fi
