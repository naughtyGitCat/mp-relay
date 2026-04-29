"""Phase 2d — fall back to gfriends/gfriends for actor portraits when mdcx misses.

Source: https://github.com/gfriends/gfriends — a public, MIT-licensed collection
of Japanese AV actor portraits with a single ``Filetree.json`` index at the
repo root that maps every bucket → filenames.

Why this is a separate concern from the JAV pipeline:
- mdcx pulls posters from JavBus / DMM / FANZA which sometimes lack actor faces
- gfriends has wide coverage but stale data is fine (refreshed monthly upstream)
- A missing portrait is a soft failure — we should never block scraping on it

This module exposes:
- ``find_actor_avatar_url(name)`` — resolve a name to a raw.githubusercontent URL, or None
- ``fetch_avatar_bytes(name)``    — download and return JPEG bytes, or None
- ``save_avatar(name, dest)``     — write to a local path (creates parent dirs)

A 7-day in-memory cache backs the filetree fetch so repeated lookups don't hit
GitHub on every call. Process restarts re-fetch — acceptable given file size
(~ 1 MB) and infrequent updates.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Optional

import httpx

from . import metrics as m
from .config import settings

log = logging.getLogger(__name__)


_FILETREE_URL: str = "https://raw.githubusercontent.com/gfriends/gfriends/master/Filetree.json"
_RAW_BASE: str = "https://raw.githubusercontent.com/gfriends/gfriends/master"
_CACHE_TTL_SEC: int = 7 * 24 * 3600


# In-memory cache of (fetched_at, parsed_filetree, name_index)
_filetree_state: dict = {"fetched_at": 0.0, "tree": None, "index": None}
_lock: asyncio.Lock = asyncio.Lock()


def _normalize(name: str) -> str:
    """Strip whitespace, fullwidth/halfwidth differences, and lowercase for fuzzy match.

    gfriends file names are typically the actor's name as it appears on JavBus
    (kana / kanji / latin), with whitespace collapsed. Strip everything that
    doesn't carry meaning so "葵 つかさ" matches "葵つかさ".
    """
    if not name:
        return ""
    # Collapse all whitespace + remove common separators
    out = re.sub(r"[\s　_·・.]+", "", name)
    return out.lower()


def _build_index(tree: dict) -> dict[str, str]:
    """Walk the gfriends Filetree.json and return ``{normalized_name: relative_path}``.

    Filetree shape (observed):
        {"Content": {"AA": {"葵つかさ.jpg": {"sha": "..."}, ...}, ...}, ...}
    """
    idx: dict[str, str] = {}
    content = tree.get("Content") or {}
    if not isinstance(content, dict):
        return idx
    for bucket_name, files in content.items():
        if not isinstance(files, dict):
            continue
        for filename in files.keys():
            if not isinstance(filename, str):
                continue
            # Take stem (drop extension)
            stem = filename.rsplit(".", 1)[0] if "." in filename else filename
            norm = _normalize(stem)
            if not norm:
                continue
            idx[norm] = f"Content/{bucket_name}/{filename}"
    return idx


def _make_client() -> httpx.AsyncClient:
    kw: dict = dict(timeout=30.0, follow_redirects=True)
    if settings.discover_proxy:
        kw["proxy"] = settings.discover_proxy
    return httpx.AsyncClient(**kw)


async def _refresh_filetree() -> Optional[dict]:
    """Download and parse Filetree.json. Returns the parsed dict or None on error."""
    async with _make_client() as c:
        try:
            r = await c.get(_FILETREE_URL)
        except httpx.HTTPError as e:
            log.warning("gfriends filetree fetch failed: %s", e)
            return None
        if r.status_code != 200:
            log.warning("gfriends filetree HTTP %s", r.status_code)
            return None
        try:
            return r.json()
        except Exception as e:
            log.warning("gfriends filetree JSON parse failed: %s", e)
            return None


async def _get_index() -> Optional[dict[str, str]]:
    """Lock-protected lazy load + cache."""
    async with _lock:
        now = time.time()
        cached = _filetree_state["index"]
        if cached and (now - _filetree_state["fetched_at"]) < _CACHE_TTL_SEC:
            return cached
        tree = await _refresh_filetree()
        if tree is None:
            return cached  # may be None on first call; caller handles
        idx = _build_index(tree)
        _filetree_state["fetched_at"] = now
        _filetree_state["tree"] = tree
        _filetree_state["index"] = idx
        log.info("gfriends index refreshed: %d actors", len(idx))
        return idx


async def find_actor_avatar_url(name: str) -> Optional[str]:
    """Return a raw.githubusercontent.com URL for the actor's portrait, or None."""
    if not name:
        return None
    idx = await _get_index()
    if not idx:
        m.GFRIENDS_LOOKUP.labels(result="error").inc()
        return None
    rel = idx.get(_normalize(name))
    if not rel:
        m.GFRIENDS_LOOKUP.labels(result="miss").inc()
        return None
    m.GFRIENDS_LOOKUP.labels(result="hit").inc()
    # Filenames in the index can contain spaces / Chinese chars — let httpx encode on fetch
    return f"{_RAW_BASE}/{rel}"


async def fetch_avatar_bytes(name: str) -> Optional[bytes]:
    """Resolve + download the actor's portrait as bytes, or None."""
    url = await find_actor_avatar_url(name)
    if not url:
        return None
    async with _make_client() as c:
        try:
            r = await c.get(url)
        except httpx.HTTPError as e:
            log.warning("gfriends avatar fetch %s failed: %s", name, e)
            return None
        if r.status_code != 200:
            log.warning("gfriends avatar %s → HTTP %s", name, r.status_code)
            return None
        return r.content


async def save_avatar(name: str, dest: Path, *, overwrite: bool = False) -> bool:
    """Download avatar and save to dest. Returns True on success."""
    dest = Path(dest)
    if dest.exists() and not overwrite:
        return False
    data = await fetch_avatar_bytes(name)
    if not data:
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True
    except (OSError, PermissionError) as e:
        log.warning("gfriends save_avatar(%s → %s) failed: %s", name, dest, e)
        return False


async def healthcheck() -> Optional[str]:
    """Verify gfriends Filetree.json is fetchable. Returns None on success, error string otherwise."""
    idx = await _get_index()
    if idx is None:
        return "Filetree.json fetch failed — gfriends unreachable"
    if len(idx) < 100:
        return f"Filetree.json parse looks broken ({len(idx)} entries)"
    return None
