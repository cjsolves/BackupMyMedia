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
