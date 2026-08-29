"""
Pipeline engine - moves files through stages and detects problems.
"""
import asyncio
import logging
import os
import shutil
import subprocess
import json
from datetime import datetime, timezone

from app.config import settings
from app import database as db

log = logging.getLogger("engine")

# Upscaler staging path — the upscaler Docker service watches this
UPSCALE_STAGING = os.environ.get("PATH_UPSCALE_STAGING", "/media/upscale-queue")
UPSCALE_OUTPUT  = os.environ.get("PATH_UPSCALE_OUTPUT",  "/media/upscale-output")
LOSSLESS_ROOT   = settings.PATH_NAS_LOSSLESS


def find_main_mkv(lossless_path: str) -> str | None:
    """Return the largest MKV under a lossless folder, or None."""
    if not lossless_path or not os.path.isdir(lossless_path):
        return None
    files = [(os.path.getsize(fp), fp)
             for root, _, fs in os.walk(lossless_path)
             for f in fs
             if (fp := os.path.join(root, f)).lower().endswith(".mkv")]
    return sorted(files, reverse=True)[0][1] if files else None


# ---------------------------------------------------------------------------
# File mover: Mini PC completed → NAS Lossless
# ---------------------------------------------------------------------------

async def move_rip_complete_items():
    """
    Find all items in 'rip_complete' state and move them to NAS Lossless.
    Called by scheduler every 2 minutes.
    """
    items = await db.get_all_items()
    for item in items:
        if item["state"] != "rip_complete":
            continue
        await move_to_nas_lossless(item)


async def move_to_nas_lossless(item: dict):
    item_id = item["id"]
    media = item.get("media_type", "unknown")
    src = _find_source(item)

    if not src:
        log.warning(f"[{item_id}] Cannot find source files to move")
        await db.set_problem(item_id, "move_failed", "Source path not found on Mini PC")
        return

    # Determine destination subfolder on NAS
    subdir = {"movie": "Movies", "tv": "TV", "music": "Music"}.get(media, "Movies")
    dst_parent = os.path.join(settings.PATH_NAS_LOSSLESS, subdir)
    dst = os.path.join(dst_parent, item_id)

    if not _nas_reachable(settings.PATH_NAS_LOSSLESS):
        await db.set_problem(item_id, "nas_unreachable", f"Cannot reach {settings.PATH_NAS_LOSSLESS}")
        return

    await db.set_state(item_id, "moving_to_nas", f"{src} → {dst}")
    log.info(f"[{item_id}] Moving {src} → {dst}")

    try:
        os.makedirs(dst_parent, exist_ok=True)
        await asyncio.get_running_loop().run_in_executor(
            None, _move_directory, src, dst
        )
        await db.upsert_item({"id": item_id, "nas_lossless_path": dst, "src_path": None})
        await db.set_state(item_id, "on_nas_lossless", f"Moved to {dst}")
        log.info(f"[{item_id}] Move complete → on_nas_lossless")
    except Exception as e:
        log.error(f"[{item_id}] Move failed: {e}")
        await db.set_problem(item_id, "move_failed", str(e))


def _move_directory(src: str, dst: str):
    """Move a directory, merging if destination exists."""
    if os.path.exists(dst):
        # Merge: move each file individually
        for item in os.scandir(src):
            target = os.path.join(dst, item.name)
            if not os.path.exists(target):
                shutil.move(item.path, target)
        # Remove empty source
        try:
            shutil.rmtree(src)
        except Exception:
            pass
    else:
        shutil.move(src, dst)


def _find_source(item: dict) -> str | None:
    """Find where the ripped files actually are on the Mini PC share."""
    media = item.get("media_type", "unknown")
    item_id = item["id"]

    candidates = []

    # ARM organises into movies/ or series/ or music/ subdirs
    for subdir in ("movies", "series", "music", ""):
        for base in (settings.PATH_MINIPC_COMPLETED, settings.PATH_MINIPC_MUSIC):
            p = os.path.join(base, subdir, item_id) if subdir else os.path.join(base, item_id)
            candidates.append(p)

    for c in candidates:
        if os.path.isdir(c):
            return c
    return None


def _nas_reachable(path: str) -> bool:
    return os.path.exists(path)


# ---------------------------------------------------------------------------
# Post-completion local cleanup
# ---------------------------------------------------------------------------

async def cleanup_completed_local():
    """
    Delete local Lossless and Plex copies after a full pipeline run.
    Only fires when state=='complete', both NAS paths exist on disk, and
    AUTO_CLEANUP_LOCAL is true. Runs AFTER Plex has already been refreshed.
    """
    if not settings.AUTO_CLEANUP_LOCAL:
        return

    local_lossless = settings.PATH_LOCAL_LOSSLESS
    local_plex     = settings.PATH_LOCAL_PLEX

    # Silently skip if local volumes are not mounted in this container
    if not os.path.isdir(local_lossless) or not os.path.isdir(local_plex):
        return

    items = await db.get_all_items()
    for item in items:
        if item["state"] != "complete":
            continue

        nas_lossless = item.get("nas_lossless_path")
        nas_plex     = item.get("nas_plex_path")

        # Both NAS copies must be confirmed reachable before we delete locally
        if not (nas_lossless and nas_plex):
            continue
        if not (os.path.isdir(nas_lossless) and os.path.isdir(nas_plex)):
            continue

        item_id = item["id"]
        media   = item.get("media_type", "unknown")
        subdir  = {"movie": "Movies", "tv": "TV", "music": "Music"}.get(media, "Movies")

        for base, label in ((local_lossless, "Lossless"), (local_plex, "Plex")):
            local_path = os.path.join(base, subdir, item_id)
            if os.path.isdir(local_path):
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, shutil.rmtree, local_path
                    )
                    log.info(f"[{item_id}] Deleted local {label}: {local_path}")
                    await db.log_event(item_id, "local_cleanup", f"Deleted local {label} copy")
                except Exception as e:
                    log.warning(f"[{item_id}] Cleanup of local {label} failed: {e}")

async def cleanup_lossless_after_transcode():
    """
    Delete D:\\Lossless originals once Tdarr has produced a Plex version.
    Does NOT require NAS — works purely on local D: storage.
    Frees the bulk of D: space as items complete transcoding.
    """
    local_lossless = settings.PATH_LOCAL_LOSSLESS
    local_plex     = settings.PATH_LOCAL_PLEX

    if not os.path.isdir(local_lossless) or not os.path.isdir(local_plex):
        return

    items = await db.get_all_items()
    for item in items:
        if item["state"] != "complete":
            continue

        item_id = item["id"]
        media  = item.get("media_type", "unknown")
        subdir = {"movie": "Movies", "tv": "TV", "music": "Music"}.get(media, "Movies")

        plex_path     = os.path.join(local_plex, subdir, item_id)
        lossless_path = os.path.join(local_lossless, subdir, item_id)

        # Only delete Lossless if the Plex version actually exists
        if os.path.isdir(plex_path) and os.path.isdir(lossless_path):
            try:
                await asyncio.get_running_loop().run_in_executor(None, shutil.rmtree, lossless_path)
                log.info(f"[{item_id}] Deleted Lossless original (Plex version confirmed)")
                await db.log_event(item_id, "lossless_cleanup", "Deleted Lossless after transcode")
            except Exception as e:
                log.warning(f"[{item_id}] Lossless cleanup failed: {e}")


async def detect_stuck_jobs():
    """Flag jobs that haven't progressed in too long."""
    items = await db.get_all_items()
    now = datetime.now(timezone.utc)

    for item in items:
        state = item["state"]
        updated = _parse_dt(item.get("updated_at"))
        if not updated:
            continue

        age_min = (now - updated).total_seconds() / 60

        if state == "ripping" and age_min > settings.STUCK_THRESHOLD_RIPPING:
            await db.set_problem(item["id"], "stuck_ripping",
                                 f"Rip job has not progressed in {int(age_min)} minutes")

        elif state == "moving_to_nas" and age_min > settings.STUCK_THRESHOLD_MOVING:
            await db.set_problem(item["id"], "move_failed",
                                 f"Move has been running for {int(age_min)} minutes with no progress")

        elif state == "transcoding" and age_min > settings.STUCK_THRESHOLD_TRANSCODING:
            await db.set_problem(item["id"], "stuck_transcoding",
                                 f"Transcode has not progressed in {int(age_min)} minutes")

        # Upscale track: reset dead node claims only when heartbeats stop,
        # not merely because a long-running upscale exceeded total runtime.
        if item.get("upscale_status") == "processing":
            last_progress = _parse_dt(item.get("updated_at")) or _parse_dt(item.get("upscale_started_at"))
            if last_progress:
                silent_min = (now - last_progress).total_seconds() / 60
                if silent_min > settings.STUCK_THRESHOLD_UPSCALING:
                    node = item.get("upscale_node", "unknown")
                    await db.upsert_item({
                        "id": item["id"],
                        "upscale_status": "queued",
                        "upscale_node": None,
                        "upscale_pct": 0,
                        "upscale_started_at": None,
                    })
                    await db.log_event(item["id"], "upscale_reset",
                                       f"Node '{node}' silent for >{int(silent_min)}min — re-queued")


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Orphan cleanup
# ---------------------------------------------------------------------------

async def delete_orphan(item_id: str) -> dict:
    """Delete orphaned files and remove from DB."""
    item = await db.get_item(item_id)
    if not item or item.get("problem") not in ("orphan_minipc", "orphan_nas"):
        return {"ok": False, "error": "Item is not an orphan or not found"}

    src = item.get("src_path") or item.get("nas_lossless_path") or item.get("nas_plex_path")
    deleted = []
    if src and os.path.exists(src):
        try:
            shutil.rmtree(src)
            deleted.append(src)
        except Exception as e:
            return {"ok": False, "error": f"Delete failed: {e}"}

    async with __import__("aiosqlite").connect(settings.DB_PATH) as conn:
        await conn.execute("DELETE FROM items WHERE id=?", (item_id,))
        await conn.execute("DELETE FROM events WHERE item_id=?", (item_id,))
        await conn.commit()

    return {"ok": True, "deleted": deleted}


# ---------------------------------------------------------------------------
# Manual classification fix
# ---------------------------------------------------------------------------

async def reclassify_item(item_id: str, new_title: str, new_year: str,
                          media_type: str) -> dict:
    """
    After user provides the correct title, update the item and clear the problem.
    ARM's job won't be automatically updated - user must also fix it in ARM's UI.
    """
    new_id = f"{new_title} ({new_year})" if new_year else new_title  # noqa: F841 — ID change not implemented, kept for reference
    await db.upsert_item({
        "id": item_id,
        "title": new_title,
        "year": new_year,
        "media_type": media_type,
        "problem": None,
        "problem_detail": None,
        "state": "rip_complete",  # ready to move now that it's classified
    })
    await db.log_event(item_id, "reclassified",
                       f"Manually set to: {new_title} ({new_year}) [{media_type}]")
    return {"ok": True, "new_state": "rip_complete"}


# ---------------------------------------------------------------------------
# Resolution detection + upscale queue management
# ---------------------------------------------------------------------------

def get_video_resolution(file_path: str) -> tuple[int, int]:
    """Use ffprobe to get video dimensions. Returns (width, height) or (0,0)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "v:0", file_path],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if streams:
            return streams[0].get("width", 0), streams[0].get("height", 0)
    except Exception as e:
        log.warning(f"ffprobe failed on {file_path}: {e}")
    return 0, 0


async def check_and_queue_upscale(item_id: str):
    """
    Checks resolution via ffprobe and classifies the item.
    """
    item = await db.get_item(item_id)
    if not item or item.get("media_type") == "music":
        return  # music never needs upscaling

    # Already queued/processed
    if item.get("upscale_status") in ("queued", "processing", "complete", "skipped", "failed"):
        return

    lossless_path = item.get("nas_lossless_path", "")
    if not lossless_path or not os.path.exists(lossless_path):
        return

    # Find the main MKV file (largest file in the folder)
    mkv_files = []
    for root, _, files in os.walk(lossless_path):
        for f in files:
            if f.lower().endswith(".mkv"):
                full = os.path.join(root, f)
                mkv_files.append((os.path.getsize(full), full))

    if not mkv_files:
        return

    main_mkv = sorted(mkv_files, reverse=True)[0][1]
    width, height = await asyncio.get_running_loop().run_in_executor(
        None, get_video_resolution, main_mkv
    )

    log.info(f"[{item_id}] Resolution: {width}x{height}")

    # Store original resolution regardless
    await db.upsert_item({
        "id": item_id,
        "original_width": width,
        "original_height": height,
    })

    threshold = settings.TARGET_UPSCALE_HEIGHT

    if height >= threshold:
        log.info(f"[{item_id}] Already {height}p >= {threshold}p — no upscaling needed")
        await db.upsert_item({"id": item_id, "upscale_status": "skipped"})
        return

    # Below threshold: mark as queued — any available node will claim via /api/upscale/claim
    log.info(f"[{item_id}] {height}p < {threshold}p — queuing for AI upscale (pipeline continues unblocked)")
    await db.upsert_item({
        "id": item_id,
        "upscale_status": "queued",
        "upscale_pct": 0,
        "upscale_node": None,
        "upscale_started_at": None,
        "upscale_completed_at": None,
        "upscale_step": None,
        "upscale_error": None,
    })
    await db.log_event(
        item_id, "upscale_queued",
        f"{width}x{height} → queued for AI upscale to {threshold}p. Main pipeline continues."
    )


async def promote_upscale_complete(item_id: str, upscaled_path: str) -> dict:
    """
    Called by the upscaler service when it finishes.
    Replaces the NAS Lossless copy with the upscaled version.
    The main pipeline state is NOT changed here — if the item is already complete
    (Tdarr finished the SD version), we re-queue it for Tdarr to re-transcode the
    upscaled version. If still transcoding, Tdarr will pick up the updated file.
    """
    item = await db.get_item(item_id)
    if not item:
        return {"ok": False, "error": "Item not found"}

    lossless_path = item.get("nas_lossless_path", "")
    if not lossless_path or not os.path.exists(lossless_path):
        return {"ok": False, "error": f"NAS Lossless path not found: {lossless_path}"}

    if not os.path.exists(upscaled_path):
        return {"ok": False, "error": f"Upscaled file not found: {upscaled_path}"}
    if os.path.getsize(upscaled_path) <= 0:
        return {"ok": False, "error": f"Upscaled file is zero bytes: {upscaled_path}"}

    try:
        upscaled_filename = os.path.basename(upscaled_path)
        dst = os.path.join(lossless_path, upscaled_filename)
        shutil.move(upscaled_path, dst)
        log.info(f"[{item_id}] Placed upscaled file: {dst}")
    except Exception as e:
        return {"ok": False, "error": f"Failed to replace NAS copy: {e}"}

    if not os.path.exists(dst) or os.path.getsize(dst) <= 0:
        return {"ok": False, "error": f"Promoted output invalid or zero bytes: {dst}"}

    # Delete original MKV(s) — upscaled version supersedes them
    for dirpath, _, files in os.walk(lossless_path):
        for fname in files:
            fpath = os.path.join(dirpath, fname)
            if fpath != dst and fname.lower().endswith(".mkv"):
                try:
                    os.remove(fpath)
                    log.info(f"[{item_id}] Deleted pre-upscale original: {fname}")
                except Exception as e:
                    log.warning(f"[{item_id}] Could not delete original {fname}: {e}")

    # Clean up staging copy
    staging = os.path.join(UPSCALE_STAGING, item_id)
    if os.path.exists(staging):
        shutil.rmtree(staging, ignore_errors=True)

    # Mark upscale track as complete
    await db.upsert_item({
        "id": item_id,
        "upscale_status": "complete",
        "upscale_pct": 100,
        "upscale_completed_at": datetime.now(timezone.utc).isoformat(),
        "upscale_node": None,
        "upscale_step": "complete",
        "upscale_error": None,
    })
    await db.log_event(item_id, "upscale_complete",
                       "NAS Lossless replaced with 1080p upscaled version")

    # If main pipeline is already 'complete', re-queue for Tdarr to re-transcode
    if item.get("state") == "complete":
        await db.set_state(item_id, "on_nas_lossless",
                           "Re-queuing for Tdarr to transcode upscaled version")
        log.info(f"[{item_id}] Re-queued for Tdarr transcode of upscaled version")

    return {"ok": True, "replaced": dst}


async def check_all_for_upscale():
    """
    Scan all 'on_nas_lossless' items to see if any need upscaling.
    Sets upscale_status='queued'; nodes claim jobs via /api/upscale/claim.
    """
    items = await db.get_all_items()
    for item in items:
        if item["state"] != "on_nas_lossless" or item.get("problem"):
            continue
        if item.get("upscale_status") in ("queued", "processing", "complete", "skipped"):
            continue
        await check_and_queue_upscale(item["id"])


# ---------------------------------------------------------------------------
# Plex library refresh
# ---------------------------------------------------------------------------

async def trigger_plex_refresh():
    """
    Trigger a Plex Media Server library refresh.
    Called after an item reaches 'complete' state.
    Non-blocking: failure is logged but does not affect the pipeline.
    """
    if not settings.PLEX_TOKEN:
        return  # no token configured, skip silently

    import httpx
    base = f"http://{settings.PLEX_HOST}:{settings.PLEX_PORT}"
    token_param = f"?X-Plex-Token={settings.PLEX_TOKEN}"
    try:
        # Get all library sections
        r = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: __import__("httpx").get(f"{base}/library/sections{token_param}", timeout=8)
        )
        if r.status_code == 200:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(r.text)
            for section in root.findall("Directory"):
                key = section.get("key")
                __import__("httpx").get(
                    f"{base}/library/sections/{key}/refresh{token_param}", timeout=5
                )
            log.info(f"Plex library refresh triggered ({len(root.findall('Directory'))} sections)")
        else:
            log.warning(f"Plex refresh returned {r.status_code}")
    except Exception as e:
        log.warning(f"Plex refresh failed (non-fatal): {e}")

