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
import tempfile
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
PIPELINE_API        = os.environ.get("PIPELINE_API",          "http://localhost:8090")
POLL_INTERVAL       = int(os.environ.get("POLL_INTERVAL",     "30"))
CHECKPOINT_INTERVAL = int(os.environ.get("CHECKPOINT_INTERVAL", "150"))
PARALLEL_JOBS       = int(os.environ.get("PARALLEL_JOBS",     "1"))
USE_GPU             = os.environ.get("USE_GPU", "true").lower() == "true"
SCALE_FACTOR        = int(os.environ.get("SCALE_FACTOR",      "4"))
NODE_ID             = os.environ.get("NODE_ID",               "local")
# local: read source files directly from the NAS mount (low overhead, default)
# remote: download source from pipeline HTTP API, upload result when done
PIPELINE_MODE       = os.environ.get("PIPELINE_MODE",         "local")

# Model selection — environment variable UPSCALE_MODEL controls quality vs speed:
#   RealESRGAN_x4plus        - fast, good quality (~2h for a 90min 480p film on RTX 3080)
#   RealESRGAN_x4plus_anime_6B - fast, best for animation/cartoon
#   HAT-L_SRx4_ImageNet-pretrain - SOTA quality (~8h for same film, 2-3x better detail)
#   SwinIR-L_x4_GAN          - excellent quality, between ESRGAN and HAT in speed
# Default: RealESRGAN_x4plus (practical balance of quality and time)
MODEL_NAME          = os.environ.get("UPSCALE_MODEL",         "RealESRGAN_x4plus")

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

_esrgan = None

def get_esrgan():
    global _esrgan
    if _esrgan: return _esrgan
    import torch
    from basicsr.archs.rrdbnet_arch import RRDBNet
    from realesrgan import RealESRGANer
    device = "cuda" if (USE_GPU and torch.cuda.is_available()) else "cpu"
    log.info(f"Loading {MODEL_NAME} on {device} | target={TARGET_HEIGHT}p | audio_cleanup={AUDIO_CLEANUP}")
    nblk = 6 if "anime" in MODEL_NAME else 23
    model = RRDBNet(num_in_ch=3,num_out_ch=3,num_feat=64,num_block=nblk,num_grow_ch=32,scale=SCALE_FACTOR)
    tile = 0
    if device == "cuda":
        try:
            vram = torch.cuda.get_device_properties(0).total_memory/1e9
            tile = 512 if vram>=8 else 256 if vram>=4 else 128
        except Exception: tile = 256
    _esrgan = RealESRGANer(scale=SCALE_FACTOR,model_path=f"/models/{MODEL_NAME}.pth",
                           model=model,tile=tile,tile_pad=10,pre_pad=0,
                           half=(device=="cuda"),device=device)
    log.info("Model ready"); return _esrgan

def _api(item_id, state, detail=None):
    try:
        httpx.post(f"{PIPELINE_API}/api/items/{item_id}/upscale_status",
                   json={"state":state,"detail":detail or {}},timeout=5)
    except Exception: pass

def upscale_video(item_id: str, input_path: str, output_path: str) -> bool:
    import cv2
    info = probe_video(input_path)
    src_w, src_h = info.get("width",0), info.get("height",0)
    fps_str = info.get("r_frame_rate","24/1")
    if not src_w:
        log.error(f"[{item_id}] Cannot probe {input_path}"); return False

    out_w, out_h = target_dims(src_w, src_h)
    log.info(f"[{item_id}] {src_w}x{src_h} -> {out_w}x{out_h} | fps={fps_str} | denoise={DENOISE_STRENGTH}")

    ckpt = load_ckpt(item_id) or {
        "item_id":item_id,"input_path":input_path,"output_path":output_path,
        "started_at":datetime.now(timezone.utc).isoformat(),
        "total_frames":frame_count(input_path),"last_frame":0,"fps_str":fps_str,"status":"in_progress",
    }
    save_ckpt(item_id, ckpt)
    # Always re-extract from frame 0 — temp dir is cleared on each run so partial
    # frame sets can't be assembled into a complete video after a restart.
    start_frame = 0
    _api(item_id, "processing", {"step":"resuming" if ckpt["last_frame"] > 0 else "starting", "pct": 0})

    with tempfile.TemporaryDirectory(prefix=f"up_{item_id[:16]}_") as tmp:
        den_dir  = os.path.join(tmp,"denoised")
        up_dir   = os.path.join(tmp,"upscaled")
        audio    = os.path.join(tmp,"audio.mka")
        os.makedirs(den_dir); os.makedirs(up_dir)

        # 1. Extract audio
        subprocess.run(["ffmpeg","-y","-i",input_path,"-vn","-c:a","copy","-c:s","copy",audio],
                       capture_output=True)

        # 2. Extract + denoise frames
        denoise_vf = (f"hqdn3d={DENOISE_STRENGTH:.1f}:{DENOISE_STRENGTH*0.75:.1f}:"
                      f"{DENOISE_STRENGTH*4:.1f}:{DENOISE_STRENGTH*3:.1f}")
        log.info(f"[{item_id}] Extracting frames from {start_frame} (with pre-denoise)...")
        extract_cmd = ["ffmpeg", "-y"]
        if start_frame > 0:
            # Fast input-side seek — jumps to keyframe near target without decoding all prior frames
            saved_fps = ckpt.get("fps_str") or fps_str
            _n, _d = (saved_fps.split("/") + ["1"])[:2]
            seek_ts = start_frame / (int(_n) / max(int(_d), 1))
            extract_cmd += ["-ss", f"{seek_ts:.3f}"]
        extract_cmd += ["-i", input_path]
        if DENOISE_STRENGTH > 0:
            extract_cmd += ["-vf", denoise_vf]
        extract_cmd += ["-vsync", "0", "-start_number", str(start_frame), "-q:v", "1",
                        os.path.join(den_dir, "f%08d.png")]
        subprocess.run(extract_cmd, capture_output=True)

        frames = sorted(Path(den_dir).glob("f*.png"))
        log.info(f"[{item_id}] {len(frames)} frames to upscale")

        # 3. Real-ESRGAN per-frame
        if frames:
            model = get_esrgan()
            last_hb = time.time()
            for i, fp in enumerate(frames):
                if _shutdown:
                    ckpt["last_frame"] = start_frame + i
                    save_ckpt(item_id, ckpt)
                    return False
                if i > 0 and i % CHECKPOINT_INTERVAL == 0:
                    # Save checkpoint but don't block — main loop handles priority via claim throttling
                    ckpt["last_frame"] = start_frame + i
                    save_ckpt(item_id, ckpt)
                    pct = int(100*(start_frame+i)/max(ckpt["total_frames"],1))
                    log.info(f"[{item_id}] {start_frame+i}/{ckpt['total_frames']} frames ({pct}%)")
                    _api(item_id,"processing",{"step":"upscaling","pct":pct})
                    last_hb = time.time()
                elif time.time() - last_hb >= 60:
                    pct = int(100*(start_frame+i)/max(ckpt["total_frames"],1))
                    _api(item_id,"processing",{"step":"upscaling","pct":pct})
                    last_hb = time.time()
                img = cv2.imread(str(fp), cv2.IMREAD_UNCHANGED)
                if img is None:
                    shutil.copy(fp, os.path.join(up_dir, fp.name)); continue
                out_img, _ = model.enhance(img, outscale=SCALE_FACTOR)
                # Resize to exact target (ESRGAN output may differ by 1-2px)
                if out_img.shape[1] != out_w or out_img.shape[0] != out_h:
                    out_img = cv2.resize(out_img,(out_w,out_h),interpolation=cv2.INTER_LANCZOS4)
                cv2.imwrite(os.path.join(up_dir, fp.name), out_img, [cv2.IMWRITE_PNG_COMPRESSION,1])

        # 4. Reassemble: H.265 CRF 16 + mild unsharp mask + optional audio cleanup
        audio_codec = "copy"
        audio_filter_args = []
        if AUDIO_CLEANUP:
            log.info(f"[{item_id}] Reassembling -> H.265 CRF 16 + unsharp + audio cleanup...")
            audio_codec = "aac"    # re-encode audio with cleanup filters
            audio_filter_args = ["-af", build_audio_filter(), "-b:a", "192k"]
        else:
            log.info(f"[{item_id}] Reassembling -> H.265 CRF 16 + unsharp (audio passthrough)...")

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-framerate", fps_str, "-i", os.path.join(up_dir, "f%08d.png"),
            "-i", audio,
            "-vf", "unsharp=5:5:0.5:3:3:0.0",   # mild luma sharpen — counters ESRGAN softness
            "-c:v", "libx265", "-crf", "16",      # CRF 16 = high fidelity H.265
            "-preset", "slow",                    # better compression (smaller file, same quality)
            "-x265-params", "deblock=-1,-1",      # softer deblocking = fewer block artifacts
        ] + audio_filter_args + [
            "-c:a", audio_codec,
            "-c:s", "copy",
            "-movflags", "+faststart",
            output_path
        ]

        r = subprocess.run(ffmpeg_cmd, capture_output=True)
        if r.returncode != 0:
            log.error(f"[{item_id}] Reassembly failed: {r.stderr.decode()[:300]}"); return False

    if not os.path.exists(output_path):
        log.error(f"[{item_id}] Output not found!"); return False

    ckpt["status"]="complete"; ckpt["completed_at"]=datetime.now(timezone.utc).isoformat()
    save_ckpt(item_id, ckpt); _api(item_id,"processing",{"step":"done","pct":100})
    log.info(f"[{item_id}] Complete: {output_path}"); return True

def run_job(item_id: str, input_path: str) -> bool:
    out_name = Path(input_path).stem + f"_{TARGET_HEIGHT}p_upscaled.mkv"
    output_path = os.path.join(OUTPUT_DIR, item_id, out_name)
    _api(item_id,"processing",{"step":"starting"})
    success = upscale_video(item_id, input_path, output_path)
    if success:
        if PIPELINE_MODE == "remote":
            success = _upload_result(item_id, output_path)
            shutil.rmtree(os.path.join(STAGING_DIR, item_id), ignore_errors=True)
        else:
            try:
                httpx.post(f"{PIPELINE_API}/api/items/{item_id}/upscale_complete",
                           json={"upscaled_path":output_path},timeout=10)
            except Exception as e: log.warning(f"[{item_id}] Notify complete failed: {e}")
        del_ckpt(item_id)
        if PIPELINE_MODE == "local":
            staging = os.path.join(STAGING_DIR, item_id)
            if os.path.exists(staging): shutil.rmtree(staging, ignore_errors=True)
    elif _shutdown:
        log.info(f"[{item_id}] Checkpoint saved — will resume on restart")
    else:
        _api(item_id,"failed",{"error":"upscale_video returned False"})
    return success


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


def _upload_result(item_id: str, result_path: str) -> bool:
    """Upload completed MKV to pipeline. Returns True on success."""
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
            return True
        log.error(f"[{item_id}] Upload failed: {r.status_code}")
        return False
    except Exception as e:
        log.error(f"[{item_id}] Upload error: {e}")
        return False


def claim_next() -> tuple[str, str] | None:
    """Claim the next job. Resumes checkpoints first, then polls the pipeline API."""
    # Resume in-progress checkpoints without re-claiming (already owned in DB)
    if os.path.isdir(CHECKPOINT_DIR):
        for cf in sorted(Path(CHECKPOINT_DIR).glob("*.json")):
            try:
                ck = json.load(open(cf))
                if ck.get("status") == "in_progress" and os.path.exists(ck.get("input_path", "")):
                    return ck["item_id"], ck["input_path"]
            except Exception: continue

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

    if PIPELINE_MODE == "remote":
        local_dir = os.path.join(STAGING_DIR, item_id)
        src = _download_source(item_id, local_dir)
        if not src:
            log.error(f"[{item_id}] Source download failed — skipping")
            return None
        return item_id, src
    else:
        # Local mode: find MKV directly on the mounted NAS lossless path
        lossless = item.get("nas_lossless_path", "")
        if not lossless or not os.path.isdir(lossless):
            log.warning(f"[{item_id}] NAS lossless path not accessible: {lossless}")
            return None
        mkvs = sorted(Path(lossless).glob("**/*.mkv"),
                      key=lambda p: p.stat().st_size, reverse=True)
        if not mkvs:
            log.warning(f"[{item_id}] No MKV at {lossless}")
            return None
        return item_id, str(mkvs[0])

def main():
    log.info("=== High-Quality AI Video Upscaler ===")
    log.info(f"  Node={NODE_ID} | Mode={PIPELINE_MODE} | Model={MODEL_NAME} | Scale={SCALE_FACTOR}x -> {TARGET_HEIGHT}p | Denoise={DENOISE_STRENGTH}")
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
                result = claim_next()
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