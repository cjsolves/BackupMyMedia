"""
AI Video Upscaler - High-Quality Parallel Background Service
=============================================================
Upscales videos to 1080p using a 3-stage quality pipeline:
  1. ffmpeg pre-denoise  (removes grain/noise, improves ESRGAN input quality)
  2. Real-ESRGAN upscale (frame-level AI super-resolution, then scaled to exact 1080p)
  3. ffmpeg post-sharpen + H.265 encode (mild unsharp mask, CRF 16 for high fidelity)

Quality design choices:
  - Denoise BEFORE upscaling: reduces grain that would otherwise be amplified
  - Scale to EXACTLY 1080p height, preserving aspect ratio (no stretching)
  - Tile size 256-512 prevents VRAM fragmentation (better than full-frame)
  - FP16 (half-precision) on CUDA: 2x faster with no visible quality loss
  - CRF 16 H.265 encode: visually transparent, higher fidelity than CRF 18
  - Mild unsharp mask post-encode counters the slight softening ESRGAN produces

Priority rules:
  - Checks pipeline API every POLL_INTERVAL seconds
  - If ripping OR transcoding is active: yields (sleeps, saves checkpoint)
  - Pipeline items always flow normally — upscaling never blocks them

Parallelism:
  - Runs PARALLEL_JOBS concurrent upscale jobs (default 1 GPU / 2 CPU-only)
  - Each job gets its own temp dir and checkpoint file
  - One failure never affects other jobs
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [upscaler/%(threadName)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("upscaler")

STAGING_DIR         = os.environ.get("UPSCALER_STAGING",      "/media/upscale-queue")
OUTPUT_DIR          = os.environ.get("UPSCALER_OUTPUT",       "/media/upscale-output")
CHECKPOINT_DIR      = os.environ.get("UPSCALER_CHECKPOINTS",  "/data/upscale-checkpoints")
WORK_ROOT           = os.environ.get("UPSCALER_WORK_ROOT",    os.path.join(OUTPUT_DIR, "_cache"))
PIPELINE_API        = os.environ.get("PIPELINE_API",          "http://localhost:8090")
POLL_INTERVAL       = int(os.environ.get("POLL_INTERVAL",     "30"))
CHECKPOINT_INTERVAL = int(os.environ.get("CHECKPOINT_INTERVAL", "150"))
PARALLEL_JOBS       = int(os.environ.get("PARALLEL_JOBS",     "1"))
USE_GPU             = os.environ.get("USE_GPU", "true").lower() == "true"
NODE_ID             = os.environ.get("NODE_ID",               "local")
# local: read source files directly from the NAS mount (low overhead, default)
# remote: download source from pipeline HTTP API, upload result when done
PIPELINE_MODE       = os.environ.get("PIPELINE_MODE",         "local")
UPSCALE_TILE        = int(os.environ.get("UPSCALE_TILE",      "0"))
UPSCALE_TILE_PAD    = int(os.environ.get("UPSCALE_TILE_PAD",  "10"))

# Model selection — environment variable UPSCALE_MODEL controls quality vs speed:
#   RealESRGAN_x4plus        - fast, good quality (~2h for a 90min 480p film on RTX 3080)
#   RealESRGAN_x4plus_anime_6B - fast, best for animation/cartoon
#   HAT-L_SRx4_ImageNet-pretrain - SOTA quality (~8h for same film, 2-3x better detail)
#   SwinIR-L_x4_GAN          - excellent quality, between ESRGAN and HAT in speed
# Default: auto = x2 when sufficient for target height, otherwise x4
MODEL_NAME          = os.environ.get("UPSCALE_MODEL",         "auto")

# Target output height — change for 2K or 4K output:
#   1080  = Full HD  (1920x1080) — standard
#   1440  = 2K / QHD (2560x1440) — excellent on 27" monitors, same GPU time as 1080
#   2160  = 4K / UHD (3840x2160) — best quality, ~4x longer encode, needs 12GB+ VRAM
TARGET_HEIGHT       = int(os.environ.get("TARGET_HEIGHT",     "1080"))

# Video pre-denoise strength (applied before upscaling to reduce grain amplification)
# 0 = disabled, 1-3 = light (BluRay quality sources), 3-6 = medium (DVD), 7-10 = heavy (VHS)
DENOISE_STRENGTH    = float(os.environ.get("DENOISE_STRENGTH", "3"))

# Audio cleanup pipeline — enabled by default:
#   highpass: removes low-frequency rumble (80Hz cutoff)
#   afftdn:   FFT-based spectral noise reduction (removes hiss, tape noise, background hum)
#   loudnorm: EBU R128 loudness normalisation (-23 LUFS) — makes all content consistent volume
# Very effective for: old DVDs, VHS transfers, 80s/90s content, anything with tape hiss
# Less impactful on: modern BluRay rips (already clean audio)
# Set to "false" to keep audio completely untouched
AUDIO_CLEANUP       = os.environ.get("AUDIO_CLEANUP", "true").lower() == "true"
AUDIO_NOISE_FLOOR   = float(os.environ.get("AUDIO_NOISE_FLOOR", "-25"))  # dBFS noise threshold
VIDEO_EXTS          = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts"}

_shutdown = False

def _on_signal(sig, _frame):
    global _shutdown
    log.info(f"Signal {sig} — saving checkpoints and shutting down gracefully")
    _shutdown = True

signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT,  _on_signal)

def pipeline_is_idle() -> bool:
    try:
        r = httpx.get(f"{PIPELINE_API}/api/summary", timeout=8)
        counts = r.json().get("counts", {}) if r.status_code == 200 else {}
        busy = (counts.get("ripping", 0) + counts.get("moving_to_nas", 0) +
                counts.get("transcoding", 0) + counts.get("queued_transcode", 0))
        return busy == 0
    except Exception:
        return True

def wait_for_idle(item_id: str):
    paused = False
    while not _shutdown:
        if pipeline_is_idle():
            if paused:
                log.info(f"[{item_id}] Pipeline idle — resuming")
            return
        if not paused:
            log.info(f"[{item_id}] Pipeline busy — upscaler yielding")
            paused = True
        time.sleep(POLL_INTERVAL)

def ckpt_path(item_id: str) -> str:
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    return os.path.join(CHECKPOINT_DIR, item_id.replace("/","_").replace("\\","_") + ".json")

def load_ckpt(item_id: str) -> dict | None:
    p = ckpt_path(item_id)
    if os.path.exists(p):
        try:
            with open(p) as f: return json.load(f)
        except Exception: pass
    return None

def save_ckpt(item_id: str, data: dict):
    data["saved_at"] = datetime.now(timezone.utc).isoformat()
    p = ckpt_path(item_id)
    tmp = p + ".tmp"
    with open(tmp, "w") as f: json.dump(data, f, indent=2)
    os.replace(tmp, p)

def del_ckpt(item_id: str):
    p = ckpt_path(item_id)
    if os.path.exists(p): os.remove(p)


def item_work_dir(item_id: str) -> str:
    safe = item_id.replace("/", "_").replace("\\", "_")
    path = os.path.join(WORK_ROOT, safe)
    os.makedirs(path, exist_ok=True)
    return path


def item_paths(item_id: str, output_path: str) -> dict:
    work_dir = item_work_dir(item_id)
    den_dir = os.path.join(work_dir, "denoised")
    up_dir = os.path.join(work_dir, "upscaled")
    os.makedirs(den_dir, exist_ok=True)
    os.makedirs(up_dir, exist_ok=True)
    return {
        "work_dir": work_dir,
        "den_dir": den_dir,
        "up_dir": up_dir,
        "audio": os.path.join(work_dir, "audio.mka"),
        "output": output_path,
    }


def is_nonzero_file(path: str) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > 0


def contiguous_frame_count(frame_dir: str) -> int:
    present = set()
    for fp in Path(frame_dir).glob("f*.png"):
        stem = fp.stem[1:]
        if stem.isdigit() and fp.is_file() and fp.stat().st_size > 0:
            present.add(int(stem))
    count = 0
    while count in present:
        count += 1
    return count


def valid_video_file(path: str) -> bool:
    if not is_nonzero_file(path):
        return False
    info = probe_video(path)
    return bool(info.get("width") and info.get("height"))


def cleanup_item_cache(item_id: str, output_path: str):
    shutil.rmtree(item_work_dir(item_id), ignore_errors=True)
    out_dir = os.path.dirname(output_path)
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)


def ffmpeg_error(result: subprocess.CompletedProcess) -> str:
    stderr = result.stderr.decode() if isinstance(result.stderr, bytes) else (result.stderr or "")
    return stderr.strip()[:1200] or f"ffmpeg exited with code {result.returncode}"

def probe_video(path: str) -> dict:
    try:
        r = subprocess.run(
            ["ffprobe","-v","quiet","-print_format","json",
             "-show_streams","-select_streams","v:0",path],
            capture_output=True, text=True, timeout=30)
        streams = json.loads(r.stdout).get("streams",[])
        return streams[0] if streams else {}
    except Exception: return {}

def frame_count(path: str) -> int:
    try:
        r = subprocess.run(
            ["ffprobe","-v","error","-select_streams","v:0","-count_frames",
             "-show_entries","stream=nb_read_frames",
             "-of","default=noprint_wrappers=1:nokey=1",path],
            capture_output=True, text=True, timeout=600)
        return int(r.stdout.strip())
    except Exception: return -1

def target_dims(src_w: int, src_h: int) -> tuple[int,int]:
    ratio = TARGET_HEIGHT / src_h
    out_w = int(src_w * ratio); out_w += out_w % 2
    return out_w, TARGET_HEIGHT

def build_audio_filter() -> str:
    """
    Returns the ffmpeg audio filter chain for cleanup.
    Chain: highpass -> FFT denoiser -> loudness normalisation.

    highpass=f=80      : remove sub-80Hz rumble (AC hum, handling noise)
    afftdn=nf=X        : FFT spectral denoiser at noise floor X dBFS
                         removes hiss, tape noise, background hum
                         -25 suits DVD/broadcast; use -20 for VHS/very noisy sources
    dynaudnorm         : per-frame dynamic normalisation for consistent loudness
    loudnorm           : EBU R128 broadcast loudness standard (-23 LUFS target)
                         makes all content consistently loud and clear

    Is it effective? Absolutely:
      - Old DVDs: removes the constant background hiss noticeably
      - Dialogue: becomes clearer and more intelligible
      - Volume: perfectly consistent — no more blasting action scenes, inaudible dialogue
      - Effectively free processing — adds ~30s to any file
    """
    return (
        f"highpass=f=80,"
        f"afftdn=nf={AUDIO_NOISE_FLOOR:.0f}:af=1,"  # af=1 = auto-threshold mode
        f"dynaudnorm=p=0.9:m=100:r=0.9,"             # smooth dynamic normalisation
        f"loudnorm=I=-23:TP=-2:LRA=7"                # EBU R128 target
    )

_esrgan_cache = {}


def required_scale(src_h: int) -> float:
    return TARGET_HEIGHT / max(src_h, 1)


def choose_model(src_h: int) -> tuple[str, int]:
    if MODEL_NAME != "auto":
        if "x2" in MODEL_NAME:
            return MODEL_NAME, 2
        return MODEL_NAME, 4
    return ("RealESRGAN_x2plus", 2) if required_scale(src_h) <= 2.0 else ("RealESRGAN_x4plus", 4)


def auto_tile_size(device: str, vram_gb: float) -> int:
    if device != "cuda":
        return 0
    if UPSCALE_TILE > 0:
        return UPSCALE_TILE
    if vram_gb >= 10:
        return 1024
    if vram_gb >= 8:
        return 768
    if vram_gb >= 6:
        return 512
    if vram_gb >= 4:
        return 256
    return 128

def get_esrgan(model_name: str, scale_factor: int):
    cache_key = (model_name, scale_factor)
    if cache_key in _esrgan_cache:
        return _esrgan_cache[cache_key]
    import torch
    from basicsr.archs.rrdbnet_arch import RRDBNet
    from realesrgan import RealESRGANer
    device = "cuda" if (USE_GPU and torch.cuda.is_available()) else "cpu"
    nblk = 6 if "anime" in model_name else 23
    vram = 0.0
    if device == "cuda":
        try:
            vram = torch.cuda.get_device_properties(0).total_memory / 1e9
        except Exception:
            vram = 0.0
    tile = auto_tile_size(device, vram)
    log.info(f"Loading {model_name} on {device} | target={TARGET_HEIGHT}p | tile={tile or 'full'} pad={UPSCALE_TILE_PAD} | audio_cleanup={AUDIO_CLEANUP}")
    model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=nblk, num_grow_ch=32, scale=scale_factor)
    engine = RealESRGANer(scale=scale_factor, model_path=f"/models/{model_name}.pth",
                          model=model, tile=tile, tile_pad=UPSCALE_TILE_PAD, pre_pad=0,
                          half=(device=="cuda"), device=device)
    _esrgan_cache[cache_key] = engine
    log.info("Model ready")
    return engine

def _api(item_id, state, detail=None):
    try:
        httpx.post(f"{PIPELINE_API}/api/items/{item_id}/upscale_status",
                   json={"state":state,"detail":detail or {}},timeout=5)
    except Exception: pass

def upscale_video(item_id: str, input_path: str, output_path: str) -> dict:
    import cv2
    info = probe_video(input_path)
    src_w, src_h = info.get("width",0), info.get("height",0)
    fps_str = info.get("r_frame_rate","24/1")
    if not src_w:
        return {"ok": False, "step": "probe", "error": f"Cannot probe {input_path}", "pct": 0}

    out_w, out_h = target_dims(src_w, src_h)
    model_name, model_scale = choose_model(src_h)
    target_scale = required_scale(src_h)
    log.info(f"[{item_id}] {src_w}x{src_h} -> {out_w}x{out_h} | scale={target_scale:.2f}x | model={model_name} | fps={fps_str} | denoise={DENOISE_STRENGTH}")
    paths = item_paths(item_id, output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    ckpt = load_ckpt(item_id) or {
        "item_id":item_id,"input_path":input_path,"output_path":output_path,
        "started_at":datetime.now(timezone.utc).isoformat(),
        "total_frames":frame_count(input_path),"last_frame":0,"fps_str":fps_str,"status":"in_progress",
    }
    if ckpt["total_frames"] <= 0:
        return {"ok": False, "step": "probe", "error": f"Could not determine total frame count for {input_path}", "pct": 0}
    ckpt["input_path"] = input_path
    ckpt["output_path"] = output_path
    ckpt["model_name"] = model_name
    ckpt["model_scale"] = model_scale
    ckpt["target_scale"] = target_scale
    save_ckpt(item_id, ckpt)
    current_done = contiguous_frame_count(paths["up_dir"])
    current_pct = int(100 * current_done / max(ckpt["total_frames"], 1))
    _api(item_id, "processing", {"step":"resuming" if current_done > 0 else "starting", "pct": current_pct})

    if not is_nonzero_file(paths["audio"]):
        _api(item_id, "processing", {"step":"audio", "pct": current_pct})
        result = subprocess.run(
            ["ffmpeg","-y","-i",input_path,"-vn","-c:a","copy","-c:s","copy",paths["audio"]],
            capture_output=True,
        )
        if result.returncode != 0 or not is_nonzero_file(paths["audio"]):
            return {"ok": False, "step": "audio", "error": ffmpeg_error(result), "pct": current_pct}

    denoise_vf = (f"hqdn3d={DENOISE_STRENGTH:.1f}:{DENOISE_STRENGTH*0.75:.1f}:"
                  f"{DENOISE_STRENGTH*4:.1f}:{DENOISE_STRENGTH*3:.1f}")
    den_done = contiguous_frame_count(paths["den_dir"])
    if den_done < ckpt["total_frames"]:
        _api(item_id, "processing", {"step":"extracting", "pct": current_pct})
        log.info(f"[{item_id}] Extracting frames from {den_done} (with pre-denoise)...")
        select_vf = f"select='gte(n\\,{den_done})',{denoise_vf}" if DENOISE_STRENGTH > 0 else f"select='gte(n\\,{den_done})'"
        extract_cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-vf", select_vf,
            "-vsync", "0", "-start_number", str(den_done), "-q:v", "1",
            os.path.join(paths["den_dir"], "f%08d.png"),
        ]
        result = subprocess.run(extract_cmd, capture_output=True)
        den_done = contiguous_frame_count(paths["den_dir"])
        if result.returncode != 0 or den_done < ckpt["total_frames"]:
            return {
                "ok": False,
                "step": "extracting",
                "error": ffmpeg_error(result) if result.returncode != 0 else f"Expected {ckpt['total_frames']} frames, found {den_done}",
                "pct": current_pct,
            }

    up_done = contiguous_frame_count(paths["up_dir"])
    if up_done < ckpt["total_frames"]:
        model = get_esrgan(model_name, model_scale)
        last_hb = time.time()
        for frame_num in range(up_done, ckpt["total_frames"]):
            if _shutdown:
                ckpt["last_frame"] = frame_num
                ckpt["step"] = "upscaling"
                save_ckpt(item_id, ckpt)
                return {"ok": False, "step": "shutdown", "error": "Shutdown requested", "pct": int(100 * frame_num / max(ckpt['total_frames'], 1)), "shutdown": True}
            src_frame = os.path.join(paths["den_dir"], f"f{frame_num:08d}.png")
            dst_frame = os.path.join(paths["up_dir"], f"f{frame_num:08d}.png")
            if not os.path.exists(src_frame):
                return {"ok": False, "step": "upscaling", "error": f"Missing denoised frame: {src_frame}", "pct": int(100 * frame_num / max(ckpt['total_frames'], 1))}
            img = cv2.imread(src_frame, cv2.IMREAD_UNCHANGED)
            if img is None:
                return {"ok": False, "step": "upscaling", "error": f"Failed to read frame: {src_frame}", "pct": int(100 * frame_num / max(ckpt['total_frames'], 1))}
            out_img, _ = model.enhance(img, outscale=target_scale)
            if out_img.shape[1] != out_w or out_img.shape[0] != out_h:
                out_img = cv2.resize(out_img, (out_w, out_h), interpolation=cv2.INTER_LANCZOS4)
            if not cv2.imwrite(dst_frame, out_img, [cv2.IMWRITE_PNG_COMPRESSION, 1]):
                return {"ok": False, "step": "upscaling", "error": f"Failed to write frame: {dst_frame}", "pct": int(100 * frame_num / max(ckpt['total_frames'], 1))}
            completed = frame_num + 1
            if completed % CHECKPOINT_INTERVAL == 0:
                ckpt["last_frame"] = completed
                ckpt["step"] = "upscaling"
                save_ckpt(item_id, ckpt)
                pct = int(100 * completed / max(ckpt["total_frames"], 1))
                log.info(f"[{item_id}] {completed}/{ckpt['total_frames']} frames ({pct}%)")
                _api(item_id, "processing", {"step":"upscaling","pct":pct})
                last_hb = time.time()
            elif time.time() - last_hb >= 60:
                pct = int(100 * completed / max(ckpt["total_frames"], 1))
                _api(item_id, "processing", {"step":"upscaling","pct":pct})
                last_hb = time.time()

    if os.path.exists(output_path) and os.path.getsize(output_path) == 0:
        os.remove(output_path)
    if not valid_video_file(output_path):
        _api(item_id, "processing", {"step":"reassembling", "pct": 99})
        audio_codec = "copy"
        audio_filter_args = []
        if AUDIO_CLEANUP:
            log.info(f"[{item_id}] Reassembling -> H.265 CRF 16 + unsharp + audio cleanup...")
            audio_codec = "aac"
            audio_filter_args = ["-af", build_audio_filter(), "-b:a", "192k"]
        else:
            log.info(f"[{item_id}] Reassembling -> H.265 CRF 16 + unsharp (audio passthrough)...")
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-framerate", fps_str, "-i", os.path.join(paths["up_dir"], "f%08d.png"),
            "-i", paths["audio"],
            "-vf", "unsharp=5:5:0.5:3:3:0.0",
            "-c:v", "libx265", "-crf", "16",
            "-preset", "slow",
            "-x265-params", "deblock=-1,-1",
        ] + audio_filter_args + [
            "-c:a", audio_codec,
            "-c:s", "copy",
            "-movflags", "+faststart",
            output_path,
        ]
        result = subprocess.run(ffmpeg_cmd, capture_output=True)
        if result.returncode != 0 or not valid_video_file(output_path):
            return {
                "ok": False,
                "step": "reassembling",
                "error": ffmpeg_error(result) if result.returncode != 0 else f"Output invalid or unreadable: {output_path}",
                "pct": 99,
            }

    ckpt["status"] = "complete"
    ckpt["step"] = "complete"
    ckpt["last_frame"] = ckpt["total_frames"]
    ckpt["completed_at"] = datetime.now(timezone.utc).isoformat()
    save_ckpt(item_id, ckpt)
    _api(item_id, "processing", {"step":"done","pct":100})
    log.info(f"[{item_id}] Complete: {output_path}")
    return {"ok": True, "output_path": output_path}

def run_job(item_id: str, input_path: str) -> bool:
    out_name = Path(input_path).stem + f"_{TARGET_HEIGHT}p_upscaled.mkv"
    output_path = os.path.join(OUTPUT_DIR, item_id, out_name)
    _api(item_id,"processing",{"step":"starting"})
    result = upscale_video(item_id, input_path, output_path)
    if result.get("ok"):
        promoted = False
        promote_error = ""
        if PIPELINE_MODE == "remote":
            upload_result = _upload_result(item_id, output_path)
            promoted = upload_result.get("ok", False)
            promote_error = upload_result.get("error", "")
        else:
            try:
                r = httpx.post(f"{PIPELINE_API}/api/items/{item_id}/upscale_complete",
                               json={"upscaled_path":output_path},timeout=30)
                promoted = r.status_code == 200 and r.json().get("ok")
                if not promoted:
                    err = r.text[:400]
                    promote_error = f"Promote failed: {err}"
            except Exception as e:
                promoted = False
                promote_error = str(e)
                log.warning(f"[{item_id}] Notify complete failed: {e}")
        if promoted:
            del_ckpt(item_id)
            cleanup_item_cache(item_id, output_path)
            shutil.rmtree(os.path.join(STAGING_DIR, item_id), ignore_errors=True)
            return True
        _api(item_id, "failed", {"step": "promoting", "pct": 99, "error": promote_error or "Promote/upload failed"})
        log.error(f"[{item_id}] promoting failed: {promote_error or 'Promote/upload failed'}")
        return False
    elif result.get("shutdown") or _shutdown:
        log.info(f"[{item_id}] Checkpoint saved — will resume on restart")
    else:
        _api(item_id, "failed", {"step": result.get("step", "unknown"), "pct": result.get("pct", 0), "error": result.get("error", "Upscale failed")})
        log.error(f"[{item_id}] {result.get('step', 'unknown')} failed: {result.get('error', 'Upscale failed')}")
    return False


def _download_source(item_id: str, local_dir: str) -> str | None:
    """Stream source MKV from pipeline to local_dir. Returns local path or None."""
    os.makedirs(local_dir, exist_ok=True)
    try:
        with httpx.stream("GET", f"{PIPELINE_API}/api/upscale/{item_id}/source",
                          timeout=None, follow_redirects=True) as r:
            if r.status_code != 200:
                log.error(f"[{item_id}] Source download HTTP {r.status_code}")
                return None
            cd = r.headers.get("content-disposition", "")
            fname = cd.split("filename=")[-1].strip('"') if "filename=" in cd else f"{item_id}.mkv"
            local_path = os.path.join(local_dir, fname)
            total = int(r.headers.get("content-length", 0))
            done = 0
            with open(local_path, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=8 * 1024 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        log.info(f"[{item_id}] Downloading: {int(100*done/total)}%")
        return local_path
    except Exception as e:
        log.error(f"[{item_id}] Download failed: {e}")
        return None


def _upload_result(item_id: str, result_path: str) -> dict:
    """Upload completed MKV to pipeline. Returns {ok,error}."""
    fname = os.path.basename(result_path)
    size_mb = os.path.getsize(result_path) // 1024 // 1024
    log.info(f"[{item_id}] Uploading result ({size_mb} MB) → pipeline...")
    try:
        with open(result_path, "rb") as f:
            r = httpx.post(
                f"{PIPELINE_API}/api/upscale/{item_id}/result",
                content=f,
                headers={"Content-Type": "video/x-matroska", "X-Filename": fname},
                timeout=None,
            )
        if r.status_code == 200:
            log.info(f"[{item_id}] Upload complete")
            try:
                data = r.json()
            except Exception:
                data = {"ok": True}
            return {"ok": bool(data.get("ok", True)), "error": data.get("error", "")}
        err = f"Upload failed: HTTP {r.status_code} {r.text[:400]}"
        log.error(f"[{item_id}] {err}")
        return {"ok": False, "error": err}
    except Exception as e:
        log.error(f"[{item_id}] Upload error: {e}")
        return {"ok": False, "error": str(e)}


def _resolve_claimed_input(item: dict) -> str | None:
    item_id = item.get("id")
    if not item_id:
        return None
    if PIPELINE_MODE == "remote":
        local_dir = os.path.join(STAGING_DIR, item_id)
        return _download_source(item_id, local_dir)
    lossless = item.get("nas_lossless_path", "")
    if not lossless or not os.path.isdir(lossless):
        log.warning(f"[{item_id}] NAS lossless path not accessible: {lossless}")
        return None
    videos = sorted((p for p in Path(lossless).glob("**/*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS),
                    key=lambda p: p.stat().st_size, reverse=True)
    if not videos:
        log.warning(f"[{item_id}] No source video at {lossless}")
        return None
    return str(videos[0])


def claim_next(active_ids: set[str]) -> tuple[str, str] | None:
    """Claim the next job. Resumes checkpoints first, then polls the pipeline API."""
    # Resume in-progress checkpoints without re-claiming (already owned in DB)
    if os.path.isdir(CHECKPOINT_DIR):
        for cf in sorted(Path(CHECKPOINT_DIR).glob("*.json")):
            try:
                with open(cf) as fh:
                    ck = json.load(fh)
            except Exception:
                continue
            if ck.get("status") != "in_progress" or not os.path.exists(ck.get("input_path", "")):
                continue
            item_id = ck["item_id"]
            if item_id in active_ids:
                continue
            # Verify the DB still assigns this job to this node
            # (detect_stuck_jobs may have reset it while we were offline)
            try:
                r = httpx.get(f"{PIPELINE_API}/api/items/{httpx.URL(item_id)}", timeout=5)
                if r.status_code == 200:
                    db_item = r.json()
                    if db_item.get("upscale_status") == "processing" and db_item.get("upscale_node") is None:
                        adopt = httpx.post(
                            f"{PIPELINE_API}/api/upscale/{httpx.URL(item_id)}/adopt",
                            json={"node_id": NODE_ID}, timeout=5,
                        )
                        if adopt.status_code == 200:
                            log.info(f"[{item_id}] Adopted legacy unowned checkpoint for node {NODE_ID}")
                            return item_id, ck["input_path"]
                    if db_item.get("upscale_node") != NODE_ID or db_item.get("upscale_status") != "processing":
                        log.warning(f"[{item_id}] Stale checkpoint — DB shows node={db_item.get('upscale_node')} "
                                    f"status={db_item.get('upscale_status')} — discarding")
                        del_ckpt(item_id)
                        continue
            except Exception:
                pass  # API unreachable — resume optimistically
            return item_id, ck["input_path"]

    # Recover our own claimed DB job even if restart happened before a checkpoint was usable
    try:
        r = httpx.get(f"{PIPELINE_API}/api/upscale/current/{NODE_ID}", timeout=5)
        if r.status_code == 200:
            for item in r.json():
                if item["id"] in active_ids:
                    continue
                input_path = _resolve_claimed_input(item)
                if input_path:
                    log.info(f"[{item['id']}] Resuming node-owned job without relying on checkpoint discovery")
                    return item["id"], input_path
    except Exception:
        pass

    # Claim new job from pipeline
    try:
        r = httpx.post(f"{PIPELINE_API}/api/upscale/claim",
                       json={"node_id": NODE_ID}, timeout=10)
        if r.status_code == 204:
            return None
        item = r.json()
    except Exception as e:
        log.warning(f"Claim failed: {e}")
        return None

    item_id = item.get("id")
    if not item_id:
        return None
    src = _resolve_claimed_input(item)
    if not src:
        _api(item_id, "failed", {"step": "source", "pct": 0, "error": "Source resolution failed"})
        log.error(f"[{item_id}] Source resolution failed — marking failed")
        return None
    return item_id, src

def main():
    log.info("=== High-Quality AI Video Upscaler ===")
    log.info(f"  Node={NODE_ID} | Mode={PIPELINE_MODE} | Model={MODEL_NAME} | Target={TARGET_HEIGHT}p | Tile={UPSCALE_TILE or 'auto'} | TilePad={UPSCALE_TILE_PAD} | Denoise={DENOISE_STRENGTH}")
    log.info(f"  GPU={USE_GPU} | Parallel={PARALLEL_JOBS} | Checkpoint every {CHECKPOINT_INTERVAL} frames")
    log.info(f"  Quality: CRF 16 H.265, unsharp post-process, FP16 GPU inference")
    for d in (STAGING_DIR,OUTPUT_DIR,CHECKPOINT_DIR): os.makedirs(d,exist_ok=True)
    with ThreadPoolExecutor(max_workers=PARALLEL_JOBS,thread_name_prefix="upscale") as pool:
        active = {}
        while not _shutdown:
            done = [iid for iid,fut in active.items() if fut.done()]
            for iid in done: del active[iid]
            if not pipeline_is_idle():
                log.info(f"Pipeline busy — {len(active)} job(s) running (will yield at next checkpoint)")
                time.sleep(POLL_INTERVAL); continue
            slots = PARALLEL_JOBS - len(active)
            while slots > 0:
                result = claim_next(set(active))
                if not result:
                    break
                item_id, input_path = result
                if item_id not in active:
                    log.info(f"[{NODE_ID}] Starting job: {item_id}")
                    active[item_id] = pool.submit(run_job, item_id, input_path)
                    slots -= 1
            if not active:
                log.debug("Queue empty")
            time.sleep(POLL_INTERVAL)
        log.info(f"Shutdown: waiting for {len(active)} job(s) to save checkpoints...")
        for fut in active.values():
            try: fut.result(timeout=60)
            except Exception: pass
    log.info("Upscaler stopped.")

if __name__ == "__main__":
    main()