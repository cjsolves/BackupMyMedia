"""
Polls ARM and Tdarr APIs to sync their state into our pipeline database.
Also scans NAS directories to discover completed files.
"""
import logging
import os
import re
from datetime import datetime, timedelta

import httpx

from app.config import settings
from app import database as db

log = logging.getLogger("poller")

ARM_HEADERS = {"Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# ARM poller
# ---------------------------------------------------------------------------

async def poll_arm():
    """Fetch current jobs from ARM and upsert into pipeline DB."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{settings.ARM_URL}/api/jobs", headers=ARM_HEADERS)
            if r.status_code != 200:
                log.warning(f"ARM returned {r.status_code}")
                return
            jobs = r.json()
    except Exception as e:
        log.warning(f"Cannot reach ARM: {e}")
        return

    for job in jobs:
        item_id = _arm_item_id(job)
        state = _arm_state(job)
        problem = None
        problem_detail = None

        # Detect unclassified discs
        if not job.get("title") or job.get("title", "").startswith("UNKNOWN"):
            problem = "unclassified"
            problem_detail = f"Disc label: {job.get('label', 'unknown')} — open ARM UI to set title manually"
            state = "problem"

        existing = await db.get_item(item_id)
        if existing and existing["state"] not in ("ripping", "problem"):
            # Already progressed past ripping - don't regress
            continue

        await db.upsert_item({
            "id": item_id,
            "title": job.get("title") or job.get("label", "Unknown"),
            "year": str(job.get("year", "")),
            "media_type": _media_type(job),
            "disctype": job.get("disctype", ""),
            "state": state,
            "problem": problem,
            "problem_detail": problem_detail,
            "arm_job_id": job.get("job_id"),
            "src_path": settings.PATH_MINIPC_COMPLETED,
        })

        if problem:
            await db.log_event(item_id, "problem:unclassified", problem_detail)


def _arm_item_id(job: dict) -> str:
    title = job.get("title") or job.get("label", "unknown")
    year = job.get("year", "")
    clean = re.sub(r"[^a-zA-Z0-9 ()-]", "", title).strip()
    return f"{clean} ({year})" if year else clean


def _arm_state(job: dict) -> str:
    status = (job.get("status") or "").lower()
    if "ripping" in status or "waiting" in status or "identify" in status:
        return "ripping"
    if "success" in status or "complete" in status:
        return "rip_complete"
    if "error" in status or "fail" in status:
        return "problem"
    return "ripping"


def _media_type(job: dict) -> str:
    vt = (job.get("video_type") or job.get("videotype") or "").lower()
    dt = (job.get("disctype") or "").lower()
    if "series" in vt or "tv" in vt:
        return "tv"
    if "movie" in vt:
        return "movie"
    if "music" in dt or "cd" in dt:
        return "music"
    return "unknown"


# ---------------------------------------------------------------------------
# Tdarr poller
# ---------------------------------------------------------------------------

async def poll_tdarr():
    """Fetch Tdarr worker + job state."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Get worker status
            r = await client.post(
                f"{settings.TDARR_URL}/api/v2/cruddb",
                json={"data": {"collection": "TableWorkers", "mode": "getAll", "docid": ""}},
            )
            workers = r.json() if r.status_code == 200 else []

            # Get job queue
            r2 = await client.post(
                f"{settings.TDARR_URL}/api/v2/cruddb",
                json={"data": {"collection": "TableJobs", "mode": "getAll", "docid": ""}},
            )
            jobs = r2.json() if r2.status_code == 200 else []
    except Exception as e:
        log.warning(f"Cannot reach Tdarr: {e}")
        return {"workers": [], "jobs": [], "error": str(e)}

    # Match Tdarr jobs back to pipeline items
    for job in (jobs if isinstance(jobs, list) else []):
        file_path = job.get("file", "")
        item_id = _path_to_item_id(file_path)
        if not item_id:
            continue

        status = (job.get("statusTs") or job.get("status") or "").lower()
        tdarr_state = "queued_transcode"
        if "transcode" in status or "worker" in status:
            tdarr_state = "transcoding"
        elif "success" in status or "done" in status:
            tdarr_state = "complete"
        elif "error" in status or "fail" in status:
            await db.set_problem(item_id, "transcode_failed",
                                 job.get("errorStats", {}).get("lastError", ""))
            continue

        existing = await db.get_item(item_id)
        if existing and existing["state"] in ("on_nas_lossless", "queued_transcode", "transcoding"):
            await db.set_state(item_id, tdarr_state)
            if tdarr_state == "complete":
                await db.upsert_item({
                    "id": item_id,
                    "tdarr_job_id": job.get("_id", ""),
                    "nas_plex_path": job.get("outputFilePath", ""),
                })

    return {"workers": workers, "jobs": jobs}


def _path_to_item_id(path: str) -> str | None:
    """Extract 'Title (Year)' from a file path."""
    m = re.search(r"([^/\\]+)\s*\((\d{4})\)", path)
    if m:
        return f"{m.group(1).strip()} ({m.group(2)})"
    return None


# ---------------------------------------------------------------------------
# NAS scanner
# ---------------------------------------------------------------------------

async def scan_nas():
    """
    Walk NAS directories to discover new completed items and orphans.
    Updates pipeline state for items that have appeared on NAS.
    """
    all_items = {i["id"]: i for i in await db.get_all_items()}

    # --- Scan NAS Lossless ---
    await _scan_nas_directory(
        settings.PATH_NAS_LOSSLESS,
        all_items,
        "nas_lossless_path",
        "on_nas_lossless",
    )

    # --- Scan NAS Plex ---
    await _scan_nas_directory(
        settings.PATH_NAS_PLEX,
        all_items,
        "nas_plex_path",
        "complete",
    )

    # --- Detect orphans on Mini PC ---
    await _detect_minipc_orphans(all_items)


async def _scan_nas_directory(base_path: str, all_items: dict, path_field: str, target_state: str):
    if not os.path.exists(base_path):
        log.warning(f"NAS path not reachable: {base_path}")
        return

    for subdir in ("Movies", "TV", "Music"):
        folder = os.path.join(base_path, subdir)
        if not os.path.isdir(folder):
            continue
        for entry in os.scandir(folder):
            if not entry.is_dir():
                continue
            item_id = entry.name

            if item_id in all_items:
                existing = all_items[item_id]
                # Skip items already at or past target state with path already recorded
                if existing["state"] == "complete":
                    continue
                if existing["state"] == target_state and existing.get(path_field) == entry.path:
                    continue
                size = _folder_size(entry.path)
                await _advance_state(item_id, target_state, entry.path, path_field, size)
            else:
                size = _folder_size(entry.path)
                await _register_nas_item(item_id, subdir, target_state, entry.path, path_field, size)


async def _advance_state(item_id, state, path, path_field, size):
    await db.upsert_item({"id": item_id, path_field: path, "size_bytes": size})
    await db.set_state(item_id, state)


async def _register_nas_item(item_id, subdir, state, path, path_field, size):
    m = re.match(r"^(.+?)\s*\((\d{4})\)$", item_id)
    title = m.group(1).strip() if m else item_id
    year = m.group(2) if m else ""
    media = {"Movies": "movie", "TV": "tv", "Music": "music"}.get(subdir, "unknown")
    await db.upsert_item({
        "id": item_id, "title": title, "year": year,
        "media_type": media, "state": state,
        path_field: path, "size_bytes": size,
    })


async def _detect_minipc_orphans(all_items: dict):
    completed_path = settings.PATH_MINIPC_COMPLETED
    if not os.path.exists(completed_path):
        return

    tracked_src = {i["id"] for i in all_items.values() if i.get("src_path")}
    for entry in os.scandir(completed_path):
        if entry.is_dir() and entry.name not in tracked_src:
            # Files on Mini PC not in pipeline
            await db.upsert_item({
                "id": entry.name, "title": entry.name,
                "state": "problem", "problem": "orphan_minipc",
                "problem_detail": f"Found at {entry.path} but not tracked in pipeline",
                "src_path": entry.path,
            })


def _folder_size(path: str) -> int:
    total = 0
    try:
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total
