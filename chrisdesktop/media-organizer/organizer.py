"""
media-organizer.py
==================
Watches the Inbox folders for new content delivered by Robocopy from the
local ARM ripping machine and automatically organises it into a structured
Lossless library for Plex and Tdarr.

ARM naming conventions (ARM Docker output):
  Movies:    <Inbox>/completed/Movie Title (Year)/Movie Title (Year)_t00.mkv
  TV Shows:  <Inbox>/completed/Show Name (Year)/Season 01/Show - S01E01 - Title.mkv
  Music:     <Inbox>/music/Artist/Album/01 - Track.flac

Classification logic:
  - Any folder in completed/ whose immediate children contain a 'Season XX'
    subdirectory is classified as a TV show --> Lossless/TV/
  - All other folders in completed/          --> Lossless/Movies/
  - All folders in music/                    --> Lossless/Music/

Stability check:
  After a new folder is detected, we poll every POLL_INTERVAL seconds and
  compare file sizes+mtimes. Once nothing has changed for STABILITY_SECONDS
  consecutive seconds, the folder is moved. This prevents moving partially-
  transferred files from Robocopy.

Each folder is processed in its own background thread so multiple concurrent
rips can be organised in parallel.
"""

import os
import re
import shutil
import time
import logging
import threading
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import lookup

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("organizer")

# ---------------------------------------------------------------------------
# Configuration (from environment variables with sensible defaults)
# ---------------------------------------------------------------------------
TMDB_API_KEY      = os.environ.get("TMDB_API_KEY", "")
OPENSUBS_API_KEY  = os.environ.get("OPENSUBS_API_KEY", "")
NEEDS_REVIEW      = os.environ.get("NEEDS_REVIEW",      "/media/needs-review")

INBOX_COMPLETED   = os.environ.get("INBOX_COMPLETED",   "/media/inbox/completed")
INBOX_MUSIC       = os.environ.get("INBOX_MUSIC",       "/media/inbox/music")
LOSSLESS_MOVIES   = os.environ.get("LOSSLESS_MOVIES",   "/media/lossless/Movies")
LOSSLESS_TV       = os.environ.get("LOSSLESS_TV",       "/media/lossless/TV")
LOSSLESS_MUSIC    = os.environ.get("LOSSLESS_MUSIC",    "/media/lossless/Music")
STABILITY_SECONDS = int(os.environ.get("STABILITY_SECONDS", "60"))
POLL_INTERVAL     = int(os.environ.get("POLL_INTERVAL",     "10"))
MAX_WAIT_SECONDS  = int(os.environ.get("MAX_WAIT_SECONDS",  "7200"))  # 2 hours

# Matches: Season 1 / Season 01 / season 1 etc.
SEASON_PATTERN = re.compile(r"^season\s*\d+$", re.IGNORECASE)

# Tracks folders currently being processed (prevents duplicate processing)
_in_progress: set[str] = set()
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def folder_snapshot(folder: Path) -> dict[str, tuple[int, float]]:
    """
    Return a mapping of {filepath: (size_bytes, mtime)} for all files
    recursively under folder. Used to detect when Robocopy has finished writing.
    """
    snapshot: dict[str, tuple[int, float]] = {}
    try:
        for f in folder.rglob("*"):
            if f.is_file():
                try:
                    s = f.stat()
                    snapshot[str(f)] = (s.st_size, s.st_mtime)
                except OSError:
                    pass
    except OSError:
        pass
    return snapshot


def wait_until_stable(folder: Path) -> bool:
    """
    Poll the folder until its file sizes and modification times have been
    unchanged for at least STABILITY_SECONDS, indicating Robocopy has finished.

    Returns:
        True  if the folder is stable and ready to move.
        False if the folder disappeared, is empty, or the max wait was exceeded.
    """
    log.info(f"Waiting for stability ({STABILITY_SECONDS}s quiet): {folder.name}")
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    last_change = time.monotonic()
    prev = folder_snapshot(folder)

    if not prev:
        log.warning(f"Folder appears empty, skipping: {folder.name}")
        return False

    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)

        if not folder.exists():
            log.warning(f"Folder disappeared: {folder.name}")
            return False

        curr = folder_snapshot(folder)

        if curr != prev:
            last_change = time.monotonic()
            prev = curr
        elif (time.monotonic() - last_change) >= STABILITY_SECONDS:
            log.info(f"Stable - ready to move: {folder.name}")
            return True

    log.warning(f"Max wait ({MAX_WAIT_SECONDS}s) exceeded: {folder.name}")
    return False


def verify_and_rename(folder: Path, is_tv: bool) -> Path | None:
    """
    Try all identification strategies (GuessIt → NFO → MKV meta → OpenSubs → TMDb).
    Renames the folder to the canonical 'Title (Year)' and returns the new path.
    Returns None if the content cannot be identified — caller should route to NEEDS_REVIEW.
    """
    if not TMDB_API_KEY:
        return folder  # no API key — pass through unchanged

    canonical = lookup.identify_folder(folder, is_tv, TMDB_API_KEY, OPENSUBS_API_KEY)

    if canonical is None:
        return None  # completely unidentified

    if canonical == folder.name:
        return folder

    new_path = folder.parent / canonical
    if new_path.exists():
        log.warning(f"Canonical '{canonical}' already exists, keeping '{folder.name}'")
        return folder

    folder.rename(new_path)
    return new_path


def is_tv_show(folder: Path) -> bool:
    """
    Return True if folder contains at least one immediate subdirectory whose
    name matches 'Season XX' (ARM creates these for TV series).
    """
    try:
        return any(
            SEASON_PATTERN.match(entry.name)
            for entry in folder.iterdir()
            if entry.is_dir()
        )
    except OSError:
        return False


def move_to(src: Path, dst_parent: Path) -> None:
    """
    Move src directory into dst_parent.
    If a directory with the same name already exists at the destination,
    the contents are merged (items are moved individually; duplicates skipped).
    """
    dst_parent.mkdir(parents=True, exist_ok=True)
    dst = dst_parent / src.name

    if dst.exists():
        log.info(f"Merging into existing destination: {dst}")
        for item in list(src.iterdir()):
            target = dst / item.name
            if not target.exists():
                shutil.move(str(item), str(target))
                log.debug(f"  Merged: {item.name}")
            else:
                log.warning(f"  Skipping duplicate: {target}")
        # Clean up empty source directory
        try:
            src.rmdir()
        except OSError:
            log.warning(f"Could not remove source directory (not empty?): {src}")
    else:
        shutil.move(str(src), str(dst))
        log.info(f"Moved: {src.name}  -->  .../{dst_parent.name}/")


# ---------------------------------------------------------------------------
# Content handlers
# ---------------------------------------------------------------------------

def handle_completed(folder: Path) -> None:
    """
    Classify a completed ARM rip as Movie or TV, then move it to the
    appropriate Lossless/ destination.
    """
    key = str(folder)
    with _lock:
        if key in _in_progress:
            return
        _in_progress.add(key)

    try:
        if not wait_until_stable(folder):
            return
        if not folder.exists():
            return

        if is_tv_show(folder):
            dest = Path(LOSSLESS_TV)
            label = "TV"
        else:
            dest = Path(LOSSLESS_MOVIES)
            label = "Movie"

        renamed = verify_and_rename(folder, is_tv=(label == "TV"))
        if renamed is None:
            # Could not identify — quarantine in Needs-Review for manual fix
            log.warning(f"[UNIDENTIFIED] Moving to Needs-Review: {folder.name}")
            move_to(folder, Path(NEEDS_REVIEW))
            return
        folder = renamed
        log.info(f"[{label}] {folder.name}")
        move_to(folder, dest)

    except Exception:
        log.exception(f"Unexpected error handling: {folder}")
    finally:
        with _lock:
            _in_progress.discard(key)


def handle_music(folder: Path) -> None:
    """
    Move a music artist/album folder from Inbox/music/ to Lossless/Music/.
    ARM + abcde create Artist/Album/track.flac structure automatically.
    """
    key = str(folder)
    with _lock:
        if key in _in_progress:
            return
        _in_progress.add(key)

    try:
        if not wait_until_stable(folder):
            return
        if not folder.exists():
            return

        log.info(f"[Music] {folder.name}")
        move_to(folder, Path(LOSSLESS_MUSIC))

    except Exception:
        log.exception(f"Unexpected error handling music: {folder}")
    finally:
        with _lock:
            _in_progress.discard(key)


# ---------------------------------------------------------------------------
# Watchdog event handler
# ---------------------------------------------------------------------------

class DirectoryHandler(FileSystemEventHandler):
    """Fires handler_fn in a new thread whenever a top-level directory is created."""

    def __init__(self, root: Path, handler_fn):
        super().__init__()
        self.root = root
        self.handler_fn = handler_fn

    def on_created(self, event):
        if not event.is_directory:
            return
        created = Path(event.src_path)
        # Only react to top-level subdirectories (not deeper nested events)
        if created.parent == self.root:
            log.info(f"New directory detected: {created.name}")
            threading.Thread(
                target=self.handler_fn,
                args=(created,),
                daemon=True,
                name=f"process-{created.name}",
            ).start()


# ---------------------------------------------------------------------------
# Startup scan
# ---------------------------------------------------------------------------

def scan_existing(inbox: Path, handler_fn) -> None:
    """
    On startup, queue any directories that were already waiting in the inbox
    (e.g. dropped while the container was stopped).
    """
    if not inbox.exists():
        return
    for entry in inbox.iterdir():
        if entry.is_dir():
            log.info(f"Queuing pre-existing directory: {entry.name}")
            threading.Thread(
                target=handler_fn,
                args=(entry,),
                daemon=True,
                name=f"startup-{entry.name}",
            ).start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("=== media-organizer starting ===")
    log.info(f"  INBOX completed   : {INBOX_COMPLETED}")
    log.info(f"  INBOX music       : {INBOX_MUSIC}")
    log.info(f"  LOSSLESS Movies   : {LOSSLESS_MOVIES}")
    log.info(f"  LOSSLESS TV       : {LOSSLESS_TV}")
    log.info(f"  LOSSLESS Music    : {LOSSLESS_MUSIC}")
    log.info(f"  Needs-Review      : {NEEDS_REVIEW}")
    log.info(f"  Stability timeout : {STABILITY_SECONDS}s")
    log.info(f"  Max wait          : {MAX_WAIT_SECONDS}s")
    log.info(f"  TMDb lookup       : {'enabled' if TMDB_API_KEY else 'DISABLED (no TMDB_API_KEY)'}")
    log.info(f"  OpenSubs hash     : {'enabled' if OPENSUBS_API_KEY else 'disabled'}")
    log.info("")
    log.info("  DROP FOLDERS:")
    log.info(f"    Re-process / rescue unidentified files  →  {INBOX_COMPLETED}")
    log.info(f"    Files that still can't be identified    →  {NEEDS_REVIEW}  (fix manually then re-drop)")

    # Create all required directories
    for path_str in [
        INBOX_COMPLETED, INBOX_MUSIC,
        LOSSLESS_MOVIES, LOSSLESS_TV, LOSSLESS_MUSIC,
        NEEDS_REVIEW,
    ]:
        Path(path_str).mkdir(parents=True, exist_ok=True)

    # Process anything already sitting in the inboxes
    scan_existing(Path(INBOX_COMPLETED), handle_completed)
    scan_existing(Path(INBOX_MUSIC),     handle_music)

    # Start filesystem watchers
    observer = Observer()
    observer.schedule(
        DirectoryHandler(Path(INBOX_COMPLETED), handle_completed),
        INBOX_COMPLETED,
        recursive=False,
    )
    observer.schedule(
        DirectoryHandler(Path(INBOX_MUSIC), handle_music),
        INBOX_MUSIC,
        recursive=False,
    )
    observer.start()
    log.info("Watching for new content...")

    try:
        while observer.is_alive():
            observer.join(timeout=POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("Interrupt received, shutting down...")
    finally:
        observer.stop()
        observer.join()
        log.info("Stopped.")


if __name__ == "__main__":
    main()
