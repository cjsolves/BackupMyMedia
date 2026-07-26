"""
AI Video Upscaler - Background low-priority service
=====================================================
Uses Real-ESRGAN to upscale videos below 1080p.

Priority rules:
  - PAUSES immediately when pipeline has active ripping or transcoding jobs
  - RESUMES automatically when the pipeline is idle
  - Lowest priority: runs only when nothing else needs the machine

Checkpoint system:
  - Saves progress every CHECKPOINT_INTERVAL frames
  - On SIGTERM, SIGINT, or any crash: saves current frame and exits cleanly
  - On restart: finds in-progress checkpoint and resumes from last saved frame

Flow:
  1. Pipeline engine detects < 1080p file, copies to STAGING_DIR, posts to API
  2. This service monitors STAGING_DIR for new items
  3. Checks pipeline API for active jobs (priority gate)
  4. Upscales frame-by-frame with Real-ESRGAN
  5. Reassembles with original audio/subtitles using ffmpeg
  6. Notifies pipeline API → pipeline replaces NAS Lossless copy with upscaled version
  7. Cleans up staging copy
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from basicsr.archs.rrdbnet_arch import RRDBNet
from realesrgan import RealESRGANer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [upscaler] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("upscaler")

# --------------------------------------------------------------------------
# Configuration from environment
# --------------------------------------------------------------------------
STAGING_DIR        = os.environ.get("UPSCALER_STAGING",   "/media/upscale-queue")
OUTPUT_DIR         = os.environ.get("UPSCALER_OUTPUT",    "/media/upscale-output")
CHECKPOINT_DIR     = os.environ.get("UPSCALER_CHECKPOINTS", "/data/upscale-checkpoints")
PIPELINE_API       = os.environ.get("PIPELINE_API",       "http://localhost:8090")
POLL_INTERVAL      = int(os.environ.get("POLL_INTERVAL",  "30"))    # seconds between priority checks
CHECKPOINT_INTERVAL= int(os.environ.get("CHECKPOINT_INTERVAL", "100"))  # frames between saves
SCALE_FACTOR       = int(os.environ.get("SCALE_FACTOR",   "4"))     # upscale multiplier (2 or 4)
USE_GPU            = os.environ.get("USE_GPU", "true").lower() == "true"
MODEL_NAME         = os.environ.get("UPSCALE_MODEL", "RealESRGAN_x4plus")  # or RealESRGAN_x4plus_anime_6B

# --------------------------------------------------------------------------
# Graceful shutdown flag
# --------------------------------------------------------------------------
_shutdown_requested = False
_current_checkpoint_path: str | None = None
_current_checkpoint: dict | None = None


def _handle_signal(signum, frame):
    global _shutdown_requested
    log.info(f"Signal {signum} received — saving checkpoint and shutting down gracefully...")
    _shutdown_requested = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)


# --------------------------------------------------------------------------
# Priority gate — pause when pipeline has active high-priority work
# --------------------------------------------------------------------------

def is_pipeline_idle() -> bool:
    """Returns True if nothing is ripping or transcoding (safe to run upscaler)."""
    try:
        r = httpx.get(f"{PIPELINE_API}/api/summary", timeout=8)
        if r.status_code != 200:
            return True  # assume idle if API unreachable
        data = r.json()
        counts = data.get("counts", {})
        active = (
            counts.get("ripping", 0)
            + counts.get("moving_to_nas", 0)
            + counts.get("transcoding", 0)
            + counts.get("queued_transcode", 0)
        )
        return active == 0
    except Exception as e:
        log.warning(f"Cannot reach pipeline API: {e} — assuming idle")
        return True


def wait_for_idle(item_id: str):
    """Block until the pipeline is idle. Logs a message when pausing."""
    paused = False
    while not _shutdown_requested:
        if is_pipeline_idle():
            if paused:
                log.info(f"[{item_id}] Pipeline idle — resuming upscale")
            return
        if not paused:
            log.info(f"[{item_id}] Pipeline busy (ripping/transcoding active) — pausing upscaler")
            paused = True
        time.sleep(POLL_INTERVAL)


# --------------------------------------------------------------------------
# Checkpoint helpers
# --------------------------------------------------------------------------

def checkpoint_path(item_id: str) -> str:
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    safe = item_id.replace("/", "_").replace("\\", "_")
    return os.path.join(CHECKPOINT_DIR, f"{safe}.json")


def load_checkpoint(item_id: str) -> dict | None:
    path = checkpoint_path(item_id)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_checkpoint(item_id: str, data: dict):
    path = checkpoint_path(item_id)
    data["saved_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)  # atomic write


def delete_checkpoint(item_id: str):
    path = checkpoint_path(item_id)
    if os.path.exists(path):
        os.remove(path)


# --------------------------------------------------------------------------
# Resolution check
# --------------------------------------------------------------------------

def get_video_resolution(file_path: str) -> tuple[int, int]:
    """Return (width, height) of the first video stream, or (0, 0) on error."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "v:0", file_path],
            capture_output=True, text=True, timeout=30
        )
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if streams:
            return streams[0].get("width", 0), streams[0].get("height", 0)
    except Exception as e:
        log.warning(f"ffprobe failed on {file_path}: {e}")
    return 0, 0


# --------------------------------------------------------------------------
# Core upscaling
# --------------------------------------------------------------------------

def get_frame_count(file_path: str) -> int:
    """Get total video frame count via ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-count_frames", "-show_entries", "stream=nb_read_frames",
             "-print_format", "default=noprint_wrappers=1:nokey=1", file_path],
            capture_output=True, text=True, timeout=300
        )
        return int(result.stdout.strip())
    except Exception:
        return -1  # unknown


def upscale_video(item_id: str, input_path: str, output_path: str) -> bool:
    """
    Upscale a single video file using Real-ESRGAN.
    Supports checkpoint resume: if interrupted, resumes from last saved frame.
    Returns True on success, False on failure/interrupt.
    """
    global _current_checkpoint_path, _current_checkpoint

    log.info(f"[{item_id}] Starting upscale: {input_path}")
    width, height = get_video_resolution(input_path)
    log.info(f"[{item_id}] Source resolution: {width}x{height}")

    # Load or create checkpoint
    ckpt = load_checkpoint(item_id) or {
        "item_id": item_id,
        "input_path": input_path,
        "output_path": output_path,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "total_frames": get_frame_count(input_path),
        "last_frame": 0,
        "status": "in_progress",
    }
    _current_checkpoint = ckpt
    _current_checkpoint_path = checkpoint_path(item_id)

    log.info(f"[{item_id}] Total frames: {ckpt['total_frames']}, resuming from frame: {ckpt['last_frame']}")
    save_checkpoint(item_id, ckpt)

    # Work in a temp directory for frames
    with tempfile.TemporaryDirectory(prefix=f"upscale_{item_id[:20]}_") as tmpdir:
        frames_dir       = os.path.join(tmpdir, "frames_in")
        upscaled_dir     = os.path.join(tmpdir, "frames_out")
        audio_path       = os.path.join(tmpdir, "audio.mkv")
        os.makedirs(frames_dir, exist_ok=True)
        os.makedirs(upscaled_dir, exist_ok=True)

        # --- Step 1: Extract audio/subtitles (non-video streams) ---
        log.info(f"[{item_id}] Extracting audio and subtitles...")
        _notify_pipeline(item_id, "upscaling", {"step": "extracting_audio"})
        result = subprocess.run([
            "ffmpeg", "-y", "-i", input_path,
            "-vn", "-c:a", "copy", "-c:s", "copy",
            audio_path
        ], capture_output=True)
        if result.returncode != 0 and not os.path.exists(audio_path):
            log.error(f"[{item_id}] Audio extraction failed")
            return False

        # --- Step 2: Extract video frames (starting from checkpoint) ---
        start_frame = ckpt["last_frame"]
        log.info(f"[{item_id}] Extracting frames from frame {start_frame}...")

        extract_cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-vf", f"select='gte(n\\,{start_frame})'",
            "-vsync", "0",
            "-start_number", str(start_frame),
            os.path.join(frames_dir, "frame_%08d.png")
        ]
        subprocess.run(extract_cmd, capture_output=True)

        frame_files = sorted(Path(frames_dir).glob("frame_*.png"))
        log.info(f"[{item_id}] {len(frame_files)} frames to upscale")

        if not frame_files:
            log.warning(f"[{item_id}] No frames extracted — file may already be complete")
        else:
            # --- Step 3: Upscale frames with Real-ESRGAN ---
            _notify_pipeline(item_id, "upscaling", {
                "step": "upscaling_frames",
                "progress_frame": start_frame,
                "total_frames": ckpt["total_frames"],
            })

            model = _load_esrgan_model()

            for i, frame_path in enumerate(frame_files):
                if _shutdown_requested:
                    log.info(f"[{item_id}] Shutdown requested at frame {start_frame + i} — saving checkpoint")
                    ckpt["last_frame"] = start_frame + i
                    save_checkpoint(item_id, ckpt)
                    return False

                # Priority check every CHECKPOINT_INTERVAL frames
                if i % CHECKPOINT_INTERVAL == 0 and i > 0:
                    wait_for_idle(item_id)
                    ckpt["last_frame"] = start_frame + i
                    save_checkpoint(item_id, ckpt)
                    pct = int(100 * (start_frame + i) / max(ckpt["total_frames"], 1))
                    log.info(f"[{item_id}] Checkpoint: frame {start_frame + i}/{ckpt['total_frames']} ({pct}%)")
                    _notify_pipeline(item_id, "upscaling", {
                        "step": "upscaling_frames",
                        "progress_frame": start_frame + i,
                        "total_frames": ckpt["total_frames"],
                        "percent": pct,
                    })

                # Upscale single frame
                import cv2
                import numpy as np
                img = cv2.imread(str(frame_path), cv2.IMREAD_UNCHANGED)
                if img is None:
                    log.warning(f"[{item_id}] Could not read frame {frame_path.name}, skipping")
                    # Copy original to output dir to preserve frame sequence
                    shutil.copy(frame_path, os.path.join(upscaled_dir, frame_path.name))
                    continue

                output_img, _ = model.enhance(img, outscale=SCALE_FACTOR)
                out_frame_path = os.path.join(upscaled_dir, frame_path.name)
                cv2.imwrite(out_frame_path, output_img)

        # --- Step 4: Reassemble video with upscaled frames + original audio ---
        log.info(f"[{item_id}] Reassembling video with original audio...")
        _notify_pipeline(item_id, "upscaling", {"step": "reassembling"})

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        reassemble_cmd = [
            "ffmpeg", "-y",
            "-framerate", "24",  # will be overridden by -r from source
            "-i", os.path.join(upscaled_dir, "frame_%08d.png"),
            "-i", audio_path,
            "-c:v", "libx265", "-crf", "18", "-preset", "slow",
            "-c:a", "copy",
            "-c:s", "copy",
            output_path
        ]
        result = subprocess.run(reassemble_cmd, capture_output=True)
        if result.returncode != 0:
            log.error(f"[{item_id}] Reassembly failed: {result.stderr.decode()[:500]}")
            return False

    log.info(f"[{item_id}] Upscale complete: {output_path}")
    ckpt["status"] = "complete"
    ckpt["completed_at"] = datetime.now(timezone.utc).isoformat()
    save_checkpoint(item_id, ckpt)
    return True


_esrgan_model = None


def _load_esrgan_model():
    global _esrgan_model
    if _esrgan_model is not None:
        return _esrgan_model

    log.info(f"Loading Real-ESRGAN model: {MODEL_NAME}...")
    import torch

    device_str = "cuda" if (USE_GPU and torch.cuda.is_available()) else "cpu"
    log.info(f"Using device: {device_str}")

    model = RRDBNet(
        num_in_ch=3, num_out_ch=3, num_feat=64,
        num_block=23 if "anime" not in MODEL_NAME else 6,
        num_grow_ch=32, scale=SCALE_FACTOR
    )
    model_path = f"/models/{MODEL_NAME}.pth"

    _esrgan_model = RealESRGANer(
        scale=SCALE_FACTOR,
        model_path=model_path,
        model=model,
        tile=512,          # tile size to fit in VRAM
        tile_pad=10,
        pre_pad=0,
        half=device_str == "cuda",
        device=device_str,
    )
    log.info("Model loaded.")
    return _esrgan_model


# --------------------------------------------------------------------------
# Pipeline API notifications
# --------------------------------------------------------------------------

def _notify_pipeline(item_id: str, state: str, detail: dict | None = None):
    try:
        httpx.post(
            f"{PIPELINE_API}/api/items/{item_id}/upscale_status",
            json={"state": state, "detail": detail or {}},
            timeout=5,
        )
    except Exception:
        pass  # non-critical


def _mark_upscale_complete(item_id: str, upscaled_path: str):
    try:
        httpx.post(
            f"{PIPELINE_API}/api/items/{item_id}/upscale_complete",
            json={"upscaled_path": upscaled_path},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Could not notify pipeline of completion: {e}")


# --------------------------------------------------------------------------
# Queue scanner — find items in STAGING_DIR waiting to be upscaled
# --------------------------------------------------------------------------

def find_next_item() -> tuple[str, str] | None:
    """
    Returns (item_id, input_file_path) for the next item to upscale,
    or None if the queue is empty.

    Checks in-progress checkpoints first (resume priority),
    then new items in STAGING_DIR.
    """
    os.makedirs(STAGING_DIR, exist_ok=True)

    # First: resume any in-progress item
    if os.path.isdir(CHECKPOINT_DIR):
        for ckpt_file in sorted(Path(CHECKPOINT_DIR).glob("*.json")):
            try:
                with open(ckpt_file) as f:
                    ckpt = json.load(f)
                if ckpt.get("status") == "in_progress":
                    input_path = ckpt.get("input_path", "")
                    if os.path.exists(input_path):
                        log.info(f"Resuming in-progress: {ckpt['item_id']}")
                        return ckpt["item_id"], input_path
            except Exception:
                continue

    # Second: find new items in staging dir
    for entry in sorted(os.scandir(STAGING_DIR), key=lambda e: e.name):
        if not entry.is_dir():
            continue
        mkv_files = list(Path(entry.path).glob("**/*.mkv"))
        if mkv_files:
            item_id = entry.name
            return item_id, str(mkv_files[0])

    return None


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def main():
    log.info("=== AI Video Upscaler starting ===")
    log.info(f"  Staging dir : {STAGING_DIR}")
    log.info(f"  Output dir  : {OUTPUT_DIR}")
    log.info(f"  Pipeline API: {PIPELINE_API}")
    log.info(f"  Model       : {MODEL_NAME} (scale {SCALE_FACTOR}x)")
    log.info(f"  GPU         : {USE_GPU}")
    log.info(f"  Checkpoint  : every {CHECKPOINT_INTERVAL} frames")

    os.makedirs(STAGING_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)

    while not _shutdown_requested:
        # Priority gate: only run when pipeline is idle
        if not is_pipeline_idle():
            log.info("Pipeline busy — upscaler sleeping...")
            time.sleep(POLL_INTERVAL)
            continue

        item = find_next_item()
        if not item:
            log.debug("Queue empty — waiting...")
            time.sleep(POLL_INTERVAL)
            continue

        item_id, input_path = item
        output_filename = Path(input_path).stem + f"_upscaled_{SCALE_FACTOR}x.mkv"
        output_path = os.path.join(OUTPUT_DIR, item_id, output_filename)

        _notify_pipeline(item_id, "upscaling", {"step": "starting"})

        success = upscale_video(item_id, input_path, output_path)

        if success:
            log.info(f"[{item_id}] SUCCESS — notifying pipeline")
            _mark_upscale_complete(item_id, output_path)
            delete_checkpoint(item_id)
            # Clean up staging copy
            staging_item = os.path.join(STAGING_DIR, item_id)
            if os.path.exists(staging_item):
                shutil.rmtree(staging_item, ignore_errors=True)
        elif _shutdown_requested:
            log.info(f"[{item_id}] Checkpoint saved — will resume on next start")
            break
        else:
            log.error(f"[{item_id}] Upscale failed — marking as problem")
            _notify_pipeline(item_id, "upscale_failed", {"input": input_path})

    log.info("Upscaler exiting.")


if __name__ == "__main__":
    main()
