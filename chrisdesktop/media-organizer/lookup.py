"""
Multi-strategy title lookup: GuessIt → NFO → MKV metadata → OpenSubtitles hash → TMDb.
Set TMDB_API_KEY env var (free at themoviedb.org/settings/api).
Set OPENSUBS_API_KEY env var (free at opensubtitles.com/consumers) for hash-based lookup.
"""
import json
import logging
import os
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import guessit as _guessit
    _GUESSIT = True
except ImportError:
    _GUESSIT = False

log = logging.getLogger("lookup")

TMDB_BASE     = "https://api.themoviedb.org/3"
OPENSUBS_BASE = "https://api.opensubtitles.com/api/v1"

_YEAR_PAREN = re.compile(r'^(.+?)\s*[\(\[]((?:19|20)\d{2})[\)\]]\s*$')
_YEAR_BARE  = re.compile(r'^(.+?)\s+((?:19|20)\d{2})\s*$')
_GUID_RE    = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE
)
_JUNK_RE    = re.compile(
    r'\b(1080p|720p|2160p|4k|uhd|bluray|bdrip|dvdrip|webrip|web-dl|'
    r'h\.?264|h\.?265|hevc|x264|x265|xvid|divx|'
    r'aac|dts|ac3|dd5\.1|truehd|atmos|remux|proper|repack|'
    r'extended|theatrical|directors\.cut|remastered)\b.*',
    re.IGNORECASE,
)


def parse_title_year(name: str) -> tuple[str, str | None]:
    """Extract (title, year) from a folder name, stripping release-group junk first."""
    clean = _JUNK_RE.sub('', name).strip(' .-_')
    m = _YEAR_PAREN.match(clean)
    if m:
        return m.group(1).strip(' .-_'), m.group(2)
    m = _YEAR_BARE.match(clean)
    if m:
        return m.group(1).strip(' .-_'), m.group(2)
    return clean.strip(' .-_'), None


def _safe_name(s: str) -> str:
    """Strip characters that are illegal in Windows/Linux directory names."""
    return re.sub(r'[<>:"/\\|?*]', '', s).strip()


def _search(endpoint: str, params: dict, api_key: str) -> list[dict]:
    params = {**params, "api_key": api_key, "include_adult": "false"}
    url = f"{TMDB_BASE}/{endpoint}?{urlencode(params)}"
    try:
        with urlopen(url, timeout=10) as resp:
            return json.loads(resp.read()).get("results", [])
    except (URLError, Exception) as exc:
        log.warning("TMDb lookup failed for %r: %s", params.get("query"), exc)
        return []


def lookup_movie(title: str, year: str | None, api_key: str) -> dict | None:
    """Return the best TMDb movie result for a title/year, or None."""
    params: dict = {"query": title}
    if year:
        params["year"] = year
    results = _search("search/movie", params, api_key)
    if not results:
        return None
    # Prefer exact title + year match
    for r in results:
        if (r.get("title", "").lower() == title.lower()
                and (r.get("release_date") or "")[:4] == year):
            return r
    # Fall back to highest-relevance result (TMDb already ranks by relevance)
    return results[0]


def lookup_tv(title: str, year: str | None, api_key: str) -> dict | None:
    """Return the best TMDb TV result for a title/year, or None."""
    params: dict = {"query": title}
    if year:
        params["first_air_date_year"] = year
    results = _search("search/tv", params, api_key)
    if not results:
        return None
    for r in results:
        if (r.get("name", "").lower() == title.lower()
                and (r.get("first_air_date") or "")[:4] == year):
            return r
    return results[0]


def movie_folder_name(result: dict) -> str:
    """Return the canonical 'Title (Year)' folder name from a TMDb movie result."""
    title = result.get("title") or result.get("original_title") or ""
    year  = (result.get("release_date") or "")[:4]
    return _safe_name(f"{title} ({year})" if year else title)


def tv_folder_name(result: dict) -> str:
    """Return the canonical 'Title (Year)' folder name from a TMDb TV result."""
    name = result.get("name") or result.get("original_name") or ""
    year = (result.get("first_air_date") or "")[:4]
    return _safe_name(f"{name} ({year})" if year else name)


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _is_guid(name: str) -> bool:
    return bool(_GUID_RE.match(Path(name).stem))


def _largest_mkv(folder: Path) -> Path | None:
    try:
        files = [f for f in folder.rglob("*.mkv") if f.is_file()]
        return max(files, key=lambda f: f.stat().st_size) if files else None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Strategy 1: GuessIt filename parsing (optional — pip install guessit)
# ---------------------------------------------------------------------------

def _guess_filename(filename: str) -> tuple[str, str | None] | None:
    if not _GUESSIT:
        return None
    try:
        g = _guessit.guessit(filename)
        t = g.get("title")
        y = str(g.get("year")) if g.get("year") else None
        if t and not _is_guid(t):
            return _safe_name(t), y
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Strategy 2: NFO sidecar (Kodi XML or plain-text)
# ---------------------------------------------------------------------------

def _read_nfo(folder: Path) -> tuple[str, str | None] | None:
    for nfo in folder.glob("*.nfo"):
        try:
            text = nfo.read_text(encoding="utf-8", errors="replace")
            try:
                root = ET.fromstring(text)
                t_el = root.find(".//title")
                y_el = root.find(".//year")
                if t_el is not None and t_el.text:
                    return _safe_name(t_el.text.strip()), (y_el.text.strip() if y_el is not None else None)
            except ET.ParseError:
                pass
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


def _read_mkv_title(path: Path) -> str | None:
    TITLE_ID = b'\x7b\xa9'
    try:
        with open(path, 'rb') as f:
            data = f.read(131072)
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

def _os_hash(path: Path) -> str | None:
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


def _opensubs_lookup(file_hash: str, api_key: str) -> tuple[str, str | None] | None:
    url = f"{OPENSUBS_BASE}/subtitles?moviehash={file_hash}&moviehash_match=include"
    try:
        req = Request(url, headers={"Api-Key": api_key, "User-Agent": "BackupMyMedia/1"})
        with urlopen(req, timeout=10) as resp:
            feat = (json.loads(resp.read()).get("data") or [{}])[0] \
                       .get("attributes", {}).get("feature_details", {})
        title = feat.get("movie_name") or feat.get("title") or feat.get("parent_title")
        year  = str(feat.get("year")) if feat.get("year") else None
        if title:
            return _safe_name(title), year
    except (URLError, Exception) as exc:
        log.debug("OpenSubtitles lookup failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Master identifier — chains all 5 strategies then TMDb-confirms the winner
# ---------------------------------------------------------------------------

def identify_folder(
    folder: Path,
    is_tv: bool,
    tmdb_api_key: str,
    opensubs_key: str = "",
) -> str | None:
    """
    Return the canonical 'Title (Year)' folder name using all available strategies,
    or None if the content cannot be identified.
    """
    candidates: list[tuple[str, str | None]] = []
    mkv = _largest_mkv(folder)

    # 1. GuessIt on the largest MKV filename
    if mkv:
        g = _guess_filename(mkv.name)
        if g:
            candidates.append(g)

    # 2. NFO sidecar
    nfo = _read_nfo(folder)
    if nfo:
        candidates.append(nfo)

    # 3. MKV embedded title
    if mkv:
        raw = _read_mkv_title(mkv)
        if raw:
            t, y = parse_title_year(raw)
            if t:
                candidates.append((t, y))

    # 4. OpenSubtitles hash — prioritised for GUID names with no other signal
    if opensubs_key and mkv and (not candidates or _is_guid(folder.name)):
        h = _os_hash(mkv)
        if h:
            obs = _opensubs_lookup(h, opensubs_key)
            if obs:
                candidates.insert(0, obs)  # hash hit is high-confidence, put it first

    # 5. Plain folder-name parse as a last-resort candidate
    plain_t, plain_y = parse_title_year(folder.name)
    if plain_t and not _is_guid(plain_t):
        candidates.append((plain_t, plain_y))

    # TMDb confirmation — first candidate that gets a hit wins
    for title, year in candidates:
        if not title or _is_guid(title):
            continue
        if is_tv:
            r = lookup_tv(title, year, tmdb_api_key)
            if r:
                log.info("Identified '%s' as TV: %s", folder.name, tv_folder_name(r))
                return tv_folder_name(r)
        else:
            r = lookup_movie(title, year, tmdb_api_key)
            if r:
                log.info("Identified '%s' as Movie: %s", folder.name, movie_folder_name(r))
                return movie_folder_name(r)

    log.warning("Could not identify: '%s'", folder.name)
    return None
