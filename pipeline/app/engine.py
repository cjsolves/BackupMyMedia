"""
Pipeline engine - moves files through stages and detects problems.
"""
import asyncio
import logging
import os
import shutil
from datetime import datetime, timezone

from app.config import settings
from app import database as db

log = logging.getLogger("engine")


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
        await asyncio.get_event_loop().run_in_executor(
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
# Stuck job detector
# ---------------------------------------------------------------------------

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
    new_id = f"{new_title} ({new_year})" if new_year else new_title
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
