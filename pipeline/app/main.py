"""
Media Pipeline Manager - FastAPI backend
Serves the dashboard and REST API.
"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import database as db, engine, poller
from app.config import settings
from app.bulk_intake import scan_bulk_intake

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("main")

# SSE subscribers
_sse_queues: list[asyncio.Queue] = []


async def broadcast(event: str, data: dict):
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    for q in list(_sse_queues):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

scheduler = AsyncIOScheduler()


async def _run_poll():
    await poller.poll_arm()
    await poller.poll_tdarr()
    await poller.scan_nas()
    await engine.detect_stuck_jobs()
    await engine.move_rip_complete_items()
    await engine.check_all_for_upscale()   # queue SD content for AI upscaling
    await broadcast("update", {"ts": datetime.now(timezone.utc).isoformat()})


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    scheduler.add_job(_run_poll, "interval", seconds=30, id="poll", max_instances=1)
    scheduler.start()
    # Run once immediately at startup
    asyncio.create_task(_run_poll())
    yield
    scheduler.shutdown()


app = FastAPI(title="Media Pipeline Manager", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Static files (dashboard)
# ---------------------------------------------------------------------------

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    with open(os.path.join(STATIC_DIR, "index.html"), "r") as f:
        return HTMLResponse(content=f.read())


# ---------------------------------------------------------------------------
# Server-Sent Events - real-time push to dashboard
# ---------------------------------------------------------------------------

@app.get("/api/stream")
async def sse_stream():
    q: asyncio.Queue = asyncio.Queue(maxsize=20)
    _sse_queues.append(q)

    async def generate():
        try:
            # Send initial data immediately
            data = await _build_summary()
            yield f"event: init\ndata: {json.dumps(data)}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25)
                    yield msg
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            _sse_queues.remove(q)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.get("/api/summary")
async def api_summary():
    return await _build_summary()


@app.get("/api/items")
async def api_items():
    return await db.get_all_items()


@app.get("/api/events")
async def api_events(limit: int = 60):
    return await db.get_recent_events(limit)


@app.get("/api/tdarr/status")
async def api_tdarr():
    return await poller.poll_tdarr()


@app.get("/api/arm/status")
async def api_arm():
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"{settings.ARM_URL}/api/jobs")
            return r.json()
    except Exception as e:
        raise HTTPException(502, f"Cannot reach ARM: {e}")


class ReclassifyRequest(BaseModel):
    title: str
    year: str
    media_type: str


@app.post("/api/items/{item_id}/reclassify")
async def api_reclassify(item_id: str, body: ReclassifyRequest):
    result = await engine.reclassify_item(item_id, body.title, body.year, body.media_type)
    await broadcast("update", {"reason": "reclassify", "item_id": item_id})
    return result


@app.post("/api/items/{item_id}/retry")
async def api_retry(item_id: str):
    """Clear a problem state and re-queue the item."""
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(404)
    await db.clear_problem(item_id, "rip_complete")
    await broadcast("update", {"reason": "retry", "item_id": item_id})
    return {"ok": True}


@app.post("/api/items/{item_id}/delete_orphan")
async def api_delete_orphan(item_id: str):
    result = await engine.delete_orphan(item_id)
    if not result["ok"]:
        raise HTTPException(400, result.get("error"))
    await broadcast("update", {"reason": "delete_orphan", "item_id": item_id})
    return result


@app.delete("/api/items/{item_id}")
async def api_delete_item(item_id: str):
    """Remove an item from tracking (does not delete files)."""
    async with __import__("aiosqlite").connect(settings.DB_PATH) as conn:
        await conn.execute("DELETE FROM items WHERE id=?", (item_id,))
        await conn.execute("DELETE FROM events WHERE item_id=?", (item_id,))
        await conn.commit()
    await broadcast("update", {"reason": "delete", "item_id": item_id})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Upscaler API - called by the upscaler Docker service
# ---------------------------------------------------------------------------

class UpscaleStatusRequest(BaseModel):
    state: str
    detail: dict = {}


class UpscaleCompleteRequest(BaseModel):
    upscaled_path: str


@app.post("/api/items/{item_id}/upscale_status")
async def api_upscale_status(item_id: str, body: UpscaleStatusRequest):
    """Upscaler service calls this to report progress. Updates upscale_status column only."""
    pct = body.detail.get("pct", 0) if body.detail else 0
    await db.upsert_item({
        "id": item_id,
        "upscale_status": body.state,
        "upscale_pct": pct,
    })
    if body.state == "processing" and not await db.get_item(item_id):
        pass  # item not tracked yet, skip
    await db.log_event(item_id, f"upscale:{body.state}",
                       json.dumps(body.detail) if body.detail else "")
    await broadcast("update", {
        "reason": "upscale_progress",
        "item_id": item_id,
        "upscale_state": body.state,
        "pct": pct,
    })
    return {"ok": True}


@app.post("/api/items/{item_id}/upscale_complete")
async def api_upscale_complete(item_id: str, body: UpscaleCompleteRequest):
    """Upscaler service calls this when a file is finished."""
    result = await engine.promote_upscale_complete(item_id, body.upscaled_path)
    if result["ok"]:
        await broadcast("update", {"reason": "upscale_complete", "item_id": item_id})
    return result


@app.post("/api/items/{item_id}/skip_upscale")
async def api_skip_upscale(item_id: str):
    """Mark an item to skip upscaling (e.g. user decides SD quality is fine)."""
    await db.set_state(item_id, "complete", "Upscaling skipped by user")
    await broadcast("update", {"reason": "skip_upscale", "item_id": item_id})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Bulk Intake — drop existing ripped files for end-to-end processing
# ---------------------------------------------------------------------------

class BulkIntakeRequest(BaseModel):
    dry_run: bool = False


@app.post("/api/bulk_intake/scan")
async def api_bulk_intake(body: BulkIntakeRequest = BulkIntakeRequest()):
    """
    Scan the bulk intake folder and move video files into the Lossless library.
    After moving, the pipeline scheduler will:
      1. Pick them up in the next scan (within 30s)
      2. Check their resolution and queue for upscaling if < TARGET_HEIGHT
      3. Tdarr will transcode them to H.265 for Plex

    Bulk intake folder: D:/PlexMedia/BulkIngest/  (on Chrisdesktop)
    Drop files in any structure — flat dump or pre-organised Movies/TV folders.
    """
    intake_path   = settings.PATH_BULK_INTAKE
    lossless_path = settings.PATH_NAS_LOSSLESS

    if not os.path.isdir(intake_path):
        return {"ok": False, "error": f"Bulk intake path not found: {intake_path}. Create D:\\PlexMedia\\BulkIngest\\"}

    results = await asyncio.get_event_loop().run_in_executor(
        None,
        scan_bulk_intake,
        intake_path,
        os.path.join(lossless_path, "Movies"),
        os.path.join(lossless_path, "TV"),
        body.dry_run,
    )

    moved   = [r for r in results if r.get("ok") and not r.get("dry_run")]
    failed  = [r for r in results if not r.get("ok")]
    preview = [r for r in results if r.get("dry_run")]

    if not body.dry_run:
        # Trigger immediate pipeline scan to pick up the new files
        asyncio.create_task(_run_poll())
        await broadcast("update", {"reason": "bulk_intake", "count": len(moved)})

    return {
        "ok": True,
        "dry_run": body.dry_run,
        "total_found": len(results),
        "moved": len(moved),
        "failed": len(failed),
        "preview": preview if body.dry_run else [],
        "errors": [r.get("error") for r in failed],
        "items": [{"title": r["title"], "type": r["type"], "dest": r.get("dest_dir")} for r in results],
        "next_steps": "Pipeline will pick these up within 30 seconds and check resolution, queue upscaling if < 1080p, then Tdarr transcodes to H.265 for Plex.",
    }


@app.get("/api/bulk_intake/preview")
async def api_bulk_intake_preview():
    """Preview what a bulk intake scan would do without moving anything."""
    return await api_bulk_intake(BulkIntakeRequest(dry_run=True))


@app.post("/api/refresh")
async def api_refresh():
    asyncio.create_task(_run_poll())
    return {"ok": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _build_summary() -> dict:
    items = await db.get_all_items()
    events = await db.get_recent_events(30)

    counts = {}
    for i in items:
        s = i["state"]
        counts[s] = counts.get(s, 0) + 1

    problems = [i for i in items if i["state"] == "problem"]
    in_progress = [i for i in items if i["state"] in ("ripping", "moving_to_nas", "transcoding")]
    complete = [i for i in items if i["state"] == "complete"]

    # NAS reachability
    nas_ok = os.path.exists(settings.PATH_NAS_LOSSLESS)

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "problems_count": len(problems),
        "in_progress_count": len(in_progress),
        "complete_count": len(complete),
        "nas_reachable": nas_ok,
        "arm_url": settings.ARM_URL,
        "tdarr_url": settings.TDARR_URL,
        "items": items,
        "problems": problems,
        "events": events,
    }
