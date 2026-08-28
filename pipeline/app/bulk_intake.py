"""
Bulk Intake Scanner
===================
Scans a bulk drop folder for existing MKV/video files and registers
them into the pipeline at the correct stage (on_nas_lossless).

Usage:
  POST /api/bulk_intake/scan   — trigger a scan of PATH_BULK_INTAKE
  GET  /api/bulk_intake/status — see what was found / registered

The bulk intake folder expects files in ONE of these structures:

  Structure A — flat dump (organizer will classify by filename):
    BulkIngest/
      The Dark Knight (2008).mkv
      Breaking Bad S01E01.mkv

  Structure B — pre-organised (fastest, no guessing):
    BulkIngest/
      Movies/
        The Dark Knight (2008)/
          The Dark Knight (2008).mkv
      TV/
        Breaking Bad/
          Season 01/
            Breaking Bad - S01E01.mkv

The scanner:
  1. Walks the intake directory recursively for .mkv files
  2. For each, tries to classify as Movie or TV:
     - If parent path contains 'Season' folder -> TV
     - If filename matches SxxExx pattern -> TV
     - Otherwise -> Movie
  3. Moves each item to D:/PlexMedia/Lossless/Movies/ or TV/
  4. Registers in pipeline DB at state 'on_nas_lossless'
  5. Triggers resolution check -> queues upscale if < TARGET_HEIGHT
  6. Tdarr picks up automatically for H.265 transcode -> Plex

NOTE: Files are MOVED (not copied) from BulkIngest into the Lossless library.
"""

import os
import re
import shutil
import logging
from pathlib import Path
from app import lookup
from app.config import settings

log = logging.getLogger("bulk_intake")

TARGET_HEIGHT = int(os.environ.get("TARGET_HEIGHT", "1080"))


def _is_tv(file_path: str, item_name: str) -> bool:
    """Heuristic: determine if a file is TV content."""
    # SxxExx pattern in filename
    if re.search(r'[Ss]\d{1,2}[Ee]\d{1,2}', file_path):
        return True
    # 'Season' in path
    if re.search(r'\bseason\s*\d+\b', file_path, re.IGNORECASE):
        return True
    # Path contains /TV/ or /Series/
    if re.search(r'[/\\](tv|series|shows)[/\\]', file_path, re.IGNORECASE):
        return True
    return False


def _clean_title(name: str) -> str:
    """Strip file extension, normalise dashes/underscores."""
    name = Path(name).stem
    name = re.sub(r'[_.]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def scan_bulk_intake(
    intake_dir: str,
    lossless_movies: str,
    lossless_tv: str,
    dry_run: bool = False,
) -> list[dict]:
    """
    Scan intake_dir and move files to the appropriate Lossless folder.
    Returns list of dicts describing each moved item.
    """
    if not os.path.isdir(intake_dir):
        log.warning(f"Bulk intake dir not found: {intake_dir}")
        return []

    results = []
    video_exts = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts"}

    for root, dirs, files in os.walk(intake_dir):
        # Skip already-processed subdirs inside Lossless/ destinations
        for f in files:
            ext = Path(f).suffix.lower()
            if ext not in video_exts:
                continue

            full_path = os.path.join(root, f)
            rel_path  = os.path.relpath(full_path, intake_dir)

            is_tv = _is_tv(rel_path, f)
            title = _clean_title(f)
            dest_base = lossless_tv if is_tv else lossless_movies

            # For TV, try to preserve show/season structure from path
            if is_tv:
                parts = Path(rel_path).parts
                # Find show name (first non-Season folder)
                show = None
                season = None
                for p in parts[:-1]:
                    if re.match(r'^season\s*\d+$', p, re.IGNORECASE):
                        season = p
                    elif show is None and p.lower() not in ("tv", "series", "shows"):
                        show = p
                if show:
                    dest_dir = os.path.join(dest_base, show, season or "Season 01")
                else:
                    dest_dir = os.path.join(dest_base, title)
            else:
                # For movies, resolve to 'Title (Year)' — confirm via TMDb if key set
                m = re.search(r'(.+?)\s*[\(\[](19|20)\d{2}[\)\]]', title)
                raw_folder = m.group(0) if m else title
                parsed_title, parsed_year = lookup.parse_title_year(raw_folder)
                if settings.TMDB_API_KEY:
                    result = lookup.lookup_movie(parsed_title, parsed_year, settings.TMDB_API_KEY)
                    clean  = lookup.movie_folder_name(result) if result else raw_folder
                    if result and clean != raw_folder:
                        log.info(f"TMDb rename: '{raw_folder}' -> '{clean}'")
                else:
                    clean = raw_folder
                dest_dir = os.path.join(dest_base, clean)

            result = {
                "source":   full_path,
                "dest_dir": dest_dir,
                "filename": f,
                "type":     "tv" if is_tv else "movie",
                "title":    title,
            }

            if not dry_run:
                try:
                    os.makedirs(dest_dir, exist_ok=True)
                    dest_file = os.path.join(dest_dir, f)
                    shutil.move(full_path, dest_file)
                    result["moved_to"] = dest_file
                    result["ok"] = True
                    log.info(f"Moved {f} -> {'TV' if is_tv else 'Movie'}: {dest_dir}")
                except Exception as e:
                    result["ok"] = False
                    result["error"] = str(e)
                    log.error(f"Failed to move {f}: {e}")
            else:
                result["ok"] = True
                result["dry_run"] = True

            results.append(result)

    # Clean up empty directories left in intake_dir
    if not dry_run:
        for root, dirs, files in os.walk(intake_dir, topdown=False):
            if root != intake_dir:
                try:
                    os.rmdir(root)  # only removes if empty
                except OSError:
                    pass

    return results
