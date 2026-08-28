"""One-off ffprobe scan — checks all unclassified items and marks skipped/height in DB."""
import sqlite3, subprocess, json, os, sys

DB = "/data/pipeline.db"
TARGET = 1080

db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row

rows = db.execute(
    "SELECT id, nas_lossless_path FROM items "
    "WHERE state='on_nas_lossless' AND (upscale_status IS NULL OR upscale_status='') AND media_type != 'music'"
).fetchall()

print(f"Checking {len(rows)} items...", flush=True)
skipped = queued = failed = 0

for i, row in enumerate(rows):
    item_id = row["id"]
    lossless = row["nas_lossless_path"] or ""
    if not lossless or not os.path.isdir(lossless):
        failed += 1
        continue

    mkv = None; best = 0
    for d, _, files in os.walk(lossless):
        for f in files:
            if f.lower().endswith(".mkv"):
                p = os.path.join(d, f); s = os.path.getsize(p)
                if s > best: best = s; mkv = p

    if not mkv:
        failed += 1
        continue

    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "v:0", mkv],
            capture_output=True, text=True, timeout=30
        )
        streams = json.loads(r.stdout).get("streams", [])
        h = streams[0].get("height", 0) if streams else 0
        w = streams[0].get("width", 0) if streams else 0
    except Exception as e:
        print(f"  ffprobe failed: {item_id} — {e}", flush=True)
        failed += 1
        continue

    if h == 0:
        failed += 1
        continue

    if h >= TARGET:
        db.execute(
            "UPDATE items SET upscale_status='skipped', original_width=?, original_height=?, updated_at=datetime('now') WHERE id=?",
            (w, h, item_id)
        )
        skipped += 1
    else:
        db.execute(
            "UPDATE items SET original_width=?, original_height=?, updated_at=datetime('now') WHERE id=?",
            (w, h, item_id)
        )
        queued += 1

    if (i + 1) % 20 == 0:
        db.commit()
        print(f"  Progress: {i+1}/{len(rows)} — skipped={skipped} below1080p={queued} failed={failed}", flush=True)

db.commit()
print(f"\nDone: {skipped} already >=1080p (will go to NAS now), {queued} below 1080p (queued for upscaling), {failed} missing/error")
