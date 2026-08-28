"""
fix-media-names.py
==================
Multi-strategy media identifier and renamer.

Identification strategies (tried in order, first confident match wins):
  1. GuessIt    — parse title/year from messy filenames  [pip install guessit]
  2. NFO        — read Kodi/XBMC .nfo sidecar file       [no deps]
  3. MKV meta   — read embedded Title tag from container  [no deps]
  4. OpenSubs   — content-hash lookup via OpenSubtitles   [OPENSUBS_API_KEY]
  5. TMDb       — confirm/correct the best candidate      [TMDB_API_KEY]

Modes:
  (default)     Scan Lossless/Movies and Lossless/TV folder-by-folder
  --flat PATH   Scan a flat folder of raw video files (GUIDs, bulk dumps, etc.)

Usage:
  python scripts/fix-media-names.py                                  # dry-run organized
  python scripts/fix-media-names.py --apply                          # apply to organized
  python scripts/fix-media-names.py --flat "D:\\Archive\\OldRips"     # dry-run flat folder
  python scripts/fix-media-names.py --flat "D:\\Archive" --move-to "D:\\PlexMedia\\Lossless" --apply

Environment:
  TMDB_API_KEY        Required — free at https://www.themoviedb.org/settings/api
  OPENSUBS_API_KEY    Optional — free at https://www.opensubtitles.com/consumers

Optional dep:
  pip install guessit   Enables robust filename parsing for release-group filenames
"""

import argparse
import json
import logging
import os
import re
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import guessit as _guessit
    GUESSIT_AVAILABLE = True
except ImportError:
    GUESSIT_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("fix-names")

VIDEO_EXTS    = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".mpg", ".mpeg"}
TMDB_BASE     = "https://api.themoviedb.org/3"
OPENSUBS_BASE = "https://api.opensubtitles.com/api/v1"

_YEAR_PAREN = re.compile(r'^(.+?)\s*[\(\[]((?:19|20)\d{2})[\)\]]\s*$')
_YEAR_BARE  = re.compile(r'^(.+?)\s+((?:19|20)\d{2})\s*$')
_GUID_RE    = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Strategy 5: TMDb confirmation/correction
# ---------------------------------------------------------------------------

def _safe_name(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '', s).strip()


def _is_guid(name: str) -> bool:
    return bool(_GUID_RE.match(Path(name).stem))


def parse_title_year(name: str) -> tuple[str, str | None]:
    """Extract (title, year), stripping common release-group junk suffixes first."""
    clean = re.sub(
        r'\b(1080p|720p|2160p|4k|uhd|bluray|bdrip|dvdrip|webrip|web-dl|'
        r'h\.?264|h\.?265|hevc|x264|x265|xvid|divx|'
        r'aac|dts|ac3|dd5\.1|truehd|atmos|remux|proper|repack|'
        r'extended|theatrical|directors\.cut|remastered)\b.*',
        '', name, flags=re.IGNORECASE
    ).strip(' .-_')
    for pattern in (_YEAR_PAREN, _YEAR_BARE):
        m = pattern.match(clean)
        if m:
            return m.group(1).strip(' .-_'), m.group(2)
    return clean.strip(' .-_'), None


# ---------------------------------------------------------------------------
# Strategy 1: GuessIt filename parsing
# ---------------------------------------------------------------------------

def guess_from_filename(filename: str) -> tuple[str, str | None] | None:
    """Parse title/year from a complex release-group filename using GuessIt."""
    if not GUESSIT_AVAILABLE:
        return None
    try:
        g = _guessit.guessit(filename)
        title = g.get("title")
        year  = str(g.get("year", "")) if g.get("year") else None
        if title and not _is_guid(title):
            return _safe_name(title), year
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Strategy 2: NFO sidecar file
# ---------------------------------------------------------------------------

def read_nfo(item: Path) -> tuple[str, str | None] | None:
    """Parse title/year from a Kodi .nfo XML or plain-text sidecar."""
    nfo_files = list(item.glob("*.nfo")) if item.is_dir() else [item.with_suffix(".nfo")]
    for nfo in nfo_files:
        if not nfo.exists():
            continue
        try:
            text = nfo.read_text(encoding="utf-8", errors="replace")
            try:  # Kodi XML: <movie><title>...</title><year>...</year></movie>
                root = ET.fromstring(text)
                t_el = root.find(".//title")
                y_el = root.find(".//year")
                if t_el is not None and t_el.text:
                    return _safe_name(t_el.text.strip()), (y_el.text.strip() if y_el is not None else None)
            except ET.ParseError:
                pass
            # Plain-text: "Title: Movie Name" or "Title  Movie Name"
            for line in text.splitlines()[:20]:
                m = re.match(r'(?:Title|Movie)\s*:\s*(.+)', line, re.IGNORECASE)
                if m:
                    t, y = parse_title_year(m.group(1).strip())
                    return _safe_name(t), y
        except OSError:
            pass
    return None


# ---------------------------------------------------------------------------
# Strategy 3: MKV embedded Title tag (pure stdlib EBML reader)
# ---------------------------------------------------------------------------

def _read_vint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode an EBML variable-length integer. Returns (value, bytes_consumed)."""
    if pos >= len(data):
        return -1, 1
    b = data[pos]
    n, mask = 1, 0x80
    while n <= 8 and not (b & mask):
        mask >>= 1
        n += 1
    if pos + n > len(data) or n > 8:
        return -1, n
    val = b & (0xFF >> n)
    for i in range(1, n):
        val = (val << 8) | data[pos + i]
    return val, n


def read_mkv_title(path: Path) -> str | None:
    """Read the Title segment-info tag embedded in an MKV container."""
    TITLE_ID = b'\x7b\xa9'  # EBML element ID for SegmentInfo.Title
    try:
        with open(path, 'rb') as f:
            data = f.read(131072)  # 128 KB covers Segment Info for virtually all files
        idx = 0
        while True:
            pos = data.find(TITLE_ID, idx)
            if pos < 0:
                break
            size, consumed = _read_vint(data, pos + 2)
            val_pos = pos + 2 + consumed
            if 1 <= size <= 512 and val_pos + size <= len(data):
                try:
                    title = data[val_pos:val_pos + size].decode("utf-8").strip("\x00 \t\r\n")
                    if title and len(title) >= 2 and title.isprintable() and not _is_guid(title):
                        return title
                except UnicodeDecodeError:
                    pass
            idx = pos + 1
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# Strategy 4: OpenSubtitles content-hash lookup
# ---------------------------------------------------------------------------

def compute_os_hash(path: Path) -> str | None:
    """Compute the OpenSubtitles movie hash (file-size XOR first+last 64 KB)."""
    try:
        size = path.stat().st_size
        if size < 131072:
            return None
        h = size
        with open(path, "rb") as f:
            for _ in range(65536 // 8):
                (v,) = struct.unpack("<Q", f.read(8))
                h = (h + v) & 0xFFFFFFFFFFFFFFFF
            f.seek(-65536, 2)
            for _ in range(65536 // 8):
                (v,) = struct.unpack("<Q", f.read(8))
                h = (h + v) & 0xFFFFFFFFFFFFFFFF
        return format(h, "016x")
    except (OSError, struct.error):
        return None


def opensubs_lookup(file_hash: str, api_key: str) -> tuple[str, str | None] | None:
    """Return (title, year) by querying OpenSubtitles with a movie hash."""
    url = f"{OPENSUBS_BASE}/subtitles?moviehash={file_hash}&moviehash_match=include"
    try:
        req = Request(url, headers={"Api-Key": api_key, "User-Agent": "BackupMyMedia/1"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        feat = (data.get("data") or [{}])[0].get("attributes", {}).get("feature_details", {})
        title = feat.get("movie_name") or feat.get("title") or feat.get("parent_title")
        year  = str(feat.get("year")) if feat.get("year") else None
        if title:
            return _safe_name(title), year
    except (URLError, Exception) as exc:
        log.debug(f"OpenSubtitles: {exc}")
    return None


def _tmdb_search(endpoint: str, params: dict, api_key: str) -> list[dict]:
    params = {**params, "api_key": api_key, "include_adult": "false"}
    url = f"{TMDB_BASE}/{endpoint}?{urlencode(params)}"
    try:
        with urlopen(url, timeout=10) as resp:
            return json.loads(resp.read()).get("results", [])
    except (URLError, Exception) as exc:
        log.warning(f"  TMDb error: {exc}")
        return []


def lookup_movie(title: str, year: str | None, api_key: str) -> dict | None:
    params: dict = {"query": title}
    if year:
        params["year"] = year
    results = _tmdb_search("search/movie", params, api_key)
    if not results:
        return None
    for r in results:
        if (r.get("title", "").lower() == title.lower()
                and (r.get("release_date") or "")[:4] == year):
            return r
    return results[0]


def lookup_tv(title: str, year: str | None, api_key: str) -> dict | None:
    params: dict = {"query": title}
    if year:
        params["first_air_date_year"] = year
    results = _tmdb_search("search/tv", params, api_key)
    if not results:
        return None
    for r in results:
        if (r.get("name", "").lower() == title.lower()
                and (r.get("first_air_date") or "")[:4] == year):
            return r
    return results[0]


def movie_folder_name(r: dict) -> str:
    title = r.get("title") or r.get("original_title") or ""
    year  = (r.get("release_date") or "")[:4]
    return _safe_name(f"{title} ({year})" if year else title)


def tv_folder_name(r: dict) -> str:
    name = r.get("name") or r.get("original_name") or ""
    year = (r.get("first_air_date") or "")[:4]
    return _safe_name(f"{name} ({year})" if year else name)


# ---------------------------------------------------------------------------
# Master identifier — chains all strategies
# ---------------------------------------------------------------------------

def _largest_video(folder: Path) -> Path | None:
    try:
        files = [f for f in folder.rglob("*") if f.is_file() and f.suffix.lower() in VIDEO_EXTS]
        return max(files, key=lambda f: f.stat().st_size) if files else None
    except OSError:
        return None


def identify(
    item: Path,
    is_tv: bool,
    tmdb_key: str,
    opensubs_key: str,
) -> dict:
    """
    Try all strategies, confirm with TMDb, return a result dict.
    Returns: {original, canonical, source, tmdb_id}
    """
    # Collect (title, year, source) candidates from each strategy
    candidates: list[tuple[str, str | None, str]] = []

    # Determine the representative video file and filename for strategies
    if item.is_file():
        video_file = item if item.suffix.lower() in VIDEO_EXTS else None
        filename   = item.name
    else:
        video_file = _largest_video(item)
        filename   = video_file.name if video_file else ""

    raw_stem = Path(filename).stem if filename else item.stem

    # 1. GuessIt
    if filename:
        g = guess_from_filename(filename)
        if g:
            candidates.append((*g, "guessit"))

    # 2. NFO sidecar
    nfo = read_nfo(item)
    if nfo:
        candidates.append((*nfo, "nfo"))

    # 3. MKV embedded title
    mkv = video_file if (video_file and video_file.suffix.lower() == ".mkv") else None
    if not mkv and item.is_dir():
        mkvs = sorted(
            (f for f in item.rglob("*.mkv") if f.is_file()),
            key=lambda f: f.stat().st_size, reverse=True
        )
        mkv = mkvs[0] if mkvs else None
    if mkv:
        mkv_t = read_mkv_title(mkv)
        if mkv_t:
            t, y = parse_title_year(mkv_t)
            if t:
                candidates.append((t, y, "mkv_meta"))

    # 4. OpenSubtitles hash — run when no title found yet, or name is a GUID
    if opensubs_key and video_file and (not candidates or _is_guid(raw_stem)):
        h = compute_os_hash(video_file)
        if h:
            obs = opensubs_lookup(h, opensubs_key)
            if obs:
                candidates.append((*obs, "opensubs"))

    # 5. Plain filename parse as fallback candidate
    plain_t, plain_y = parse_title_year(raw_stem)
    if plain_t and not _is_guid(plain_t):
        candidates.append((plain_t, plain_y, "filename"))

    # TMDb confirmation — first candidate that gets a hit wins
    tmdb_result = best_source = None
    for title, year, source in candidates:
        if not title or _is_guid(title):
            continue
        r = (tmdb_tv if is_tv else lookup_movie)(title, year, tmdb_key) if tmdb_key else None
        if r:
            tmdb_result  = r
            best_source  = source
            break

    if tmdb_result:
        canonical = (tv_folder_name if is_tv else movie_folder_name)(tmdb_result)
        tmdb_id   = tmdb_result.get("id")
    elif candidates and not _is_guid(candidates[0][0]):
        t, y, best_source = candidates[0]
        canonical = _safe_name(f"{t} ({y})" if y else t)
        best_source += "_(unconfirmed)"
        tmdb_id = None
    else:
        canonical = None if _is_guid(raw_stem) else _safe_name(item.stem if item.is_file() else item.name)
        best_source = "unidentified" if canonical is None else "original"
        tmdb_id = None

    return {
        "original":  item.stem if item.is_file() else item.name,
        "canonical": canonical,
        "source":    best_source or "none",
        "tmdb_id":   tmdb_id,
    }


# ---------------------------------------------------------------------------
# Organized mode — scan Lossless/Movies + Lossless/TV
# ---------------------------------------------------------------------------

def run_organized(
    movies_dir: Path, tv_dir: Path,
    tmdb_key: str, opensubs_key: str,
    apply: bool,
) -> None:
    entries = []
    for d in sorted(movies_dir.iterdir()):
        if d.is_dir():
            entries.append({**identify(d, False, tmdb_key, opensubs_key), "path": d, "type": "Movie"})
    for d in sorted(tv_dir.iterdir()):
        if d.is_dir():
            entries.append({**identify(d, True,  tmdb_key, opensubs_key), "path": d, "type": "TV"})
    _print_and_apply(entries, apply, move_to=None)


# ---------------------------------------------------------------------------
# Flat mode — scan a raw folder of unsorted video files
# ---------------------------------------------------------------------------

def run_flat(
    flat_dir: Path, move_to: Path | None,
    tmdb_key: str, opensubs_key: str,
    apply: bool,
) -> None:
    entries = []
    for f in sorted(flat_dir.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
            continue
        is_tv = bool(re.search(r'[Ss]\d{1,2}[Ee]\d{1,2}', f.stem))
        info  = identify(f, is_tv, tmdb_key, opensubs_key)
        entries.append({**info, "path": f, "type": "TV" if is_tv else "Movie", "is_file": True})
    _print_and_apply(entries, apply, move_to)


# ---------------------------------------------------------------------------
# Shared print + apply logic
# ---------------------------------------------------------------------------

def _print_and_apply(entries: list[dict], apply: bool, move_to: Path | None) -> None:
    if not entries:
        print("No items found.")
        return

    col = min(max(len(e["original"]) for e in entries), 55)
    print()
    print(f"{'TYPE':<6}  {'CURRENT':<{col}}  {'CANONICAL':<{col}}  SOURCE            ACTION")
    print("-" * (col * 2 + 40))

    renames = []
    confirmed = unidentified = 0

    for e in entries:
        orig      = e["original"][:col]
        canonical = (e["canonical"] or "(unidentified)")[:col]
        source    = e["source"]
        is_file   = e.get("is_file", False)

        if e["canonical"] is None:
            action_tag = "SKIP"
            unidentified += 1
        elif e["canonical"] == e["original"] and not move_to:
            action_tag = "OK"
            confirmed += 1
        else:
            verb = "MOVE" if (move_to and is_file) else "RENAME"
            action_tag = verb if apply else f"WOULD {verb}"
            renames.append(e)

        print(f"{e['type']:<6}  {orig:<{col}}  {canonical:<{col}}  {source:<18}  {action_tag}")

    print()
    print(f"  Confirmed    : {confirmed}")
    print(f"  To rename    : {len(renames)}")
    print(f"  Unidentified : {unidentified}")
    print()

    if not renames:
        print("Nothing to rename.")
        return
    if not apply:
        print("Dry-run — nothing changed. Re-run with --apply to apply.")
        return

    ok = fail = 0
    for e in renames:
        path, canonical, is_file = e["path"], e["canonical"], e.get("is_file", False)
        try:
            if is_file and move_to:
                dest_dir = move_to / ("TV" if e["type"] == "TV" else "Movies") / canonical
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / path.name
                if not dest.exists():
                    path.rename(dest)
                    print(f"  MOVED   '{path.name}'  ->  {dest_dir.relative_to(move_to)}/")
                    ok += 1
                else:
                    print(f"  SKIP    '{path.name}'  (already at destination)")
            elif is_file:
                new_path = path.parent / (canonical + path.suffix)
                if not new_path.exists():
                    path.rename(new_path)
                    print(f"  RENAMED '{path.name}'  ->  '{new_path.name}'")
                    ok += 1
                else:
                    print(f"  SKIP    '{path.name}'  (destination exists)")
            else:
                new_path = path.parent / canonical
                if not new_path.exists():
                    path.rename(new_path)
                    print(f"  RENAMED '{path.name}'  ->  '{canonical}'")
                    ok += 1
                else:
                    print(f"  SKIP    '{path.name}'  (destination exists)")
        except OSError as exc:
            print(f"  FAIL    '{path.name}': {exc}")
            fail += 1

    print(f"\nDone: {ok} renamed/moved, {fail} failed.")
    if ok:
        print("NOTE: Re-run bulk intake via the pipeline dashboard to register any")
        print("      newly moved files that are not yet tracked in the pipeline database.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Identify and rename media files using multiple lookup strategies."
    )
    parser.add_argument("--movies", default=r"D:\PlexMedia\Lossless\Movies",
                        help="Lossless Movies folder (organized mode)")
    parser.add_argument("--tv",     default=r"D:\PlexMedia\Lossless\TV",
                        help="Lossless TV folder (organized mode)")
    parser.add_argument("--flat",   metavar="PATH",
                        help="Flat folder of unsorted video files")
    parser.add_argument("--move-to", metavar="PATH",
                        help="With --flat: move identified files into this Lossless/ root")
    parser.add_argument("--api-key", default=os.environ.get("TMDB_API_KEY", ""),
                        help="TMDb API key (or set TMDB_API_KEY env var)")
    parser.add_argument("--opensubs-key", default=os.environ.get("OPENSUBS_API_KEY", ""),
                        help="OpenSubtitles API key (or set OPENSUBS_API_KEY env var)")
    parser.add_argument("--apply",   action="store_true",
                        help="Actually rename/move (default is dry-run)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.api_key:
        print("ERROR: No TMDb API key. Set TMDB_API_KEY env var or pass --api-key KEY.")
        print("       Free key: https://www.themoviedb.org/settings/api")
        sys.exit(1)

    if not GUESSIT_AVAILABLE:
        print("TIP: pip install guessit  — enables robust filename parsing")
    if not args.opensubs_key:
        print("TIP: Set OPENSUBS_API_KEY for content-hash lookup of GUID-named files")

    if args.flat:
        flat_dir = Path(args.flat)
        if not flat_dir.is_dir():
            print(f"ERROR: --flat path not found: {flat_dir}")
            sys.exit(1)
        move_to = Path(args.move_to) if args.move_to else None
        run_flat(flat_dir, move_to, args.api_key, args.opensubs_key, args.apply)
    else:
        movies_dir, tv_dir = Path(args.movies), Path(args.tv)
        for p in (movies_dir, tv_dir):
            if not p.is_dir():
                print(f"ERROR: Directory not found: {p}")
                sys.exit(1)
        run_organized(movies_dir, tv_dir, args.api_key, args.opensubs_key, args.apply)


if __name__ == "__main__":
    main()
