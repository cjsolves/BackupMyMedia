"""SQLite database - tracks every item through the pipeline."""
import aiosqlite
from app.config import settings

DB = settings.DB_PATH
_TIMEOUT = 10  # seconds to wait on a locked DB before raising


def _connect():
    return aiosqlite.connect(DB, timeout=_TIMEOUT)

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,        -- canonical "Title (Year)" or disc label
    title       TEXT,
    year        TEXT,
    media_type  TEXT,                    -- movie / tv / music / unknown
    disctype    TEXT,                    -- dvd / bluray / cd
    state       TEXT DEFAULT 'ripping',  -- see STATES below
    problem     TEXT,                    -- null or problem code
    problem_detail TEXT,
    arm_job_id  INTEGER,
    tdarr_job_id TEXT,
    src_path    TEXT,                    -- current file location
    nas_lossless_path TEXT,
    nas_plex_path     TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    size_bytes  INTEGER DEFAULT 0,
    -- Upscale track (runs in parallel, never blocks the main pipeline state)
    upscale_status     TEXT DEFAULT NULL,  -- null/queued/processing/complete/failed/skipped
    upscale_pct        INTEGER DEFAULT 0,  -- 0-100 progress
    original_width     INTEGER DEFAULT 0,
    original_height    INTEGER DEFAULT 0,
    upscale_started_at TEXT,
    upscale_completed_at TEXT,
    upscale_node       TEXT DEFAULT NULL   -- which node owns this job (null = unclaimed)
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    TEXT,
    ts         TEXT DEFAULT (datetime('now')),
    event      TEXT,
    detail     TEXT
);
"""

UPSCALE_MIGRATE_SQL = """
ALTER TABLE items ADD COLUMN upscale_status TEXT DEFAULT NULL;
ALTER TABLE items ADD COLUMN upscale_pct INTEGER DEFAULT 0;
ALTER TABLE items ADD COLUMN original_width INTEGER DEFAULT 0;
ALTER TABLE items ADD COLUMN original_height INTEGER DEFAULT 0;
ALTER TABLE items ADD COLUMN upscale_started_at TEXT;
ALTER TABLE items ADD COLUMN upscale_completed_at TEXT;
ALTER TABLE items ADD COLUMN upscale_node TEXT DEFAULT NULL;
"""

# Valid pipeline states in order
STATES = [
    "ripping",           # ARM is ripping
    "rip_complete",      # on Mini PC, waiting to move
    "moving_to_nas",     # being copied to NAS
    "on_nas_lossless",   # NAS Lossless folder (archive)
    "queued_upscale",    # < 1080p — waiting for AI upscaler (lowest priority)
    "upscaling",         # Real-ESRGAN is processing
    "queued_transcode",  # waiting in Tdarr queue
    "transcoding",       # Tdarr is processing
    "complete",          # both NAS Lossless + NAS Plex populated
    "problem",           # needs human attention
    "upscale_failed",    # upscaler encountered an error
]

PROBLEM_CODES = {
    "unclassified":      "ARM could not identify this disc - title/year unknown",
    "stuck_ripping":     "No progress on rip job for over 3 hours",
    "stuck_transcoding": "No progress on transcode job for over 4 hours",
    "move_failed":       "Failed to move files to NAS",
    "transcode_failed":  "Tdarr reported a transcode error",
    "orphan_minipc":     "Files on Mini PC not tracked in pipeline",
    "orphan_nas":        "Files on NAS with no pipeline record",
    "nas_unreachable":   "Cannot reach NAS share",
}


async def init_db():
    async with _connect() as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.executescript(CREATE_SQL)
        # Add upscale columns to existing databases (migration)
        existing_cols = {row[1] async for row in await db.execute("PRAGMA table_info(items)")}
        for stmt in UPSCALE_MIGRATE_SQL.strip().split(";"):
            stmt = stmt.strip()
            if not stmt:
                continue
            col_name = stmt.split("ADD COLUMN")[1].split()[0].strip()
            if col_name not in existing_cols:
                try:
                    await db.execute(stmt)
                except Exception:
                    pass
        await db.commit()


async def upsert_item(item: dict):
    async with _connect() as db:
        cols = ", ".join(item.keys())
        placeholders = ", ".join("?" for _ in item)
        updates = ", ".join(f"{k}=excluded.{k}" for k in item if k != "id")
        await db.execute(
            f"INSERT INTO items ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}, updated_at=datetime('now')",
            list(item.values()),
        )
        await db.commit()


async def set_state(item_id: str, state: str, detail: str = ""):
    async with _connect() as db:
        await db.execute(
            "UPDATE items SET state=?, updated_at=datetime('now') WHERE id=?",
            (state, item_id),
        )
        await db.execute(
            "INSERT INTO events (item_id, event, detail) VALUES (?,?,?)",
            (item_id, f"state_change:{state}", detail),
        )
        await db.commit()


async def set_problem(item_id: str, code: str, detail: str = ""):
    async with _connect() as db:
        await db.execute(
            "UPDATE items SET state='problem', problem=?, problem_detail=?, updated_at=datetime('now') WHERE id=?",
            (code, detail or PROBLEM_CODES.get(code, ""), item_id),
        )
        await db.execute(
            "INSERT INTO events (item_id, event, detail) VALUES (?,?,?)",
            (item_id, f"problem:{code}", detail),
        )
        await db.commit()


async def clear_problem(item_id: str, new_state: str):
    async with _connect() as db:
        await db.execute(
            "UPDATE items SET state=?, problem=NULL, problem_detail=NULL, updated_at=datetime('now') WHERE id=?",
            (new_state, item_id),
        )
        await db.commit()


async def log_event(item_id: str, event: str, detail: str = ""):
    async with _connect() as db:
        await db.execute(
            "INSERT INTO events (item_id, event, detail) VALUES (?,?,?)",
            (item_id, event, detail),
        )
        await db.commit()


async def get_all_items() -> list[dict]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM items ORDER BY updated_at DESC"
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_recent_events(limit: int = 50) -> list[dict]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT e.*, i.title FROM events e "
            "LEFT JOIN items i ON e.item_id=i.id "
            "ORDER BY e.id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_item(item_id: str) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM items WHERE id=?", (item_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def claim_upscale_job(node_id: str) -> dict | None:
    """Atomically claim the next unclaimed queued upscale job for a node."""
    item_id = None
    async with _connect() as conn:
        cur = await conn.execute(
            "SELECT id FROM items WHERE upscale_status='queued' AND upscale_node IS NULL "
            "ORDER BY created_at LIMIT 1"
        )
        row = await cur.fetchone()
        if not row:
            return None
        item_id = row[0]
        # Atomic update — WHERE condition prevents double-claim in concurrent requests
        await conn.execute(
            "UPDATE items SET upscale_node=?, upscale_status='processing', "
            "upscale_started_at=datetime('now'), updated_at=datetime('now') "
            "WHERE id=? AND upscale_node IS NULL",
            (node_id, item_id),
        )
        await conn.commit()
    item = await get_item(item_id)
    return item if item and item.get("upscale_node") == node_id else None

