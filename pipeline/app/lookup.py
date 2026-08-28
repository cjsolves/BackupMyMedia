"""
TMDb title lookup — confirms and corrects movie/TV folder names.
Requires TMDB_API_KEY env var (free key at https://www.themoviedb.org/settings/api).
If the key is not set, all functions return None and callers skip renaming.
"""
import json
import logging
import os
import re
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

log = logging.getLogger("lookup")

TMDB_BASE = "https://api.themoviedb.org/3"

# 'Movie Title (2008)' or 'Movie Title [2008]'
_YEAR_PAREN = re.compile(r'^(.+?)\s*[\(\[]((?:19|20)\d{2})[\)\]]\s*$')
# 'Movie Title 2008' (bare year suffix)
_YEAR_BARE  = re.compile(r'^(.+?)\s+((?:19|20)\d{2})\s*$')


def parse_title_year(name: str) -> tuple[str, str | None]:
    """Extract (title, year) from a folder name like 'The Matrix (1999)'."""
    m = _YEAR_PAREN.match(name)
    if m:
        return m.group(1).strip(), m.group(2)
    m = _YEAR_BARE.match(name)
    if m:
        return m.group(1).strip(), m.group(2)
    return name.strip(), None


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
