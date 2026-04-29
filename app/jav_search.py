"""Phase 1 — find magnets for a JAV code.

Source priority:
  1. sukebei.nyaa.si RSS (no auth, fast, broad coverage)
  2. (TODO) javdb / javbus magnet AJAX as fallback

Returned candidates are ranked: quality (4K/FHD/HD/other) → seeders → size desc.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import quote

import httpx

from . import store
from .config import settings

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_NS = {"nyaa": "https://sukebei.nyaa.si/xmlns/nyaa"}


def _make_client() -> httpx.AsyncClient:
    kw = dict(
        headers={"User-Agent": _UA},
        follow_redirects=True,
        timeout=30.0,
    )
    if settings.discover_proxy:
        kw["proxy"] = settings.discover_proxy
    return httpx.AsyncClient(**kw)


# ---------------------------------------------------------------------------
# Quality / ranking
# ---------------------------------------------------------------------------

# Higher number = better.
_QUALITY_LEVELS: list[tuple[int, list[str]]] = [
    (5, ["8K", "4320P"]),
    (4, ["4K", "2160P", "UHD"]),
    (3, ["FHD", "1080P", "BLURAY", "BDRIP", "BLU-RAY"]),
    (2, ["720P", "HD"]),
    (1, ["540P", "DVD"]),
]


def _quality_score(title: str) -> int:
    upper = title.upper()
    for score, tokens in _QUALITY_LEVELS:
        if any(t in upper for t in tokens):
            return score
    return 0


def _has_chinese_subs(title: str) -> bool:
    """Heuristic: title carries Chinese-subtitle indicator."""
    upper = title.upper()
    indicators = ["中文", "中字", "字幕", "CHS", "CHT", "CHINESE", "SUBTITLES"]
    return any(t in upper for t in indicators)


def _parse_size_to_mib(size_str: str) -> float:
    """Parse '1.4 GiB' / '650 MiB' / '5250MB' / '2.2 GiB' → MiB float."""
    if not size_str:
        return 0.0
    s = size_str.strip()
    m = re.match(r"^([\d.]+)\s*([KMGTP])i?B?$", s, re.I)
    if not m:
        return 0.0
    value = float(m.group(1))
    unit = m.group(2).upper()
    factors = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024, "P": 1024 ** 3}
    return value * factors.get(unit, 0)


_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
]


def _make_magnet(info_hash: str, title: str) -> str:
    trackers = "&".join(f"tr={quote(t, safe='')}" for t in _TRACKERS)
    return f"magnet:?xt=urn:btih:{info_hash}&dn={quote(title)}&{trackers}"


# ---------------------------------------------------------------------------
# Sukebei RSS parser
# ---------------------------------------------------------------------------


def _parse_sukebei_rss(xml_text: str) -> list[dict]:
    candidates: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("sukebei RSS parse failed: %s", e)
        return []

    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        info_hash = (item.findtext("nyaa:infoHash", namespaces=_NS) or "").strip().lower()
        if not info_hash or len(info_hash) < 32:
            continue

        seeders = int(item.findtext("nyaa:seeders", default="0", namespaces=_NS) or 0)
        leechers = int(item.findtext("nyaa:leechers", default="0", namespaces=_NS) or 0)
        downloads = int(item.findtext("nyaa:downloads", default="0", namespaces=_NS) or 0)
        size_str = (item.findtext("nyaa:size", namespaces=_NS) or "").strip()
        size_mib = _parse_size_to_mib(size_str)
        view_url = item.findtext("guid") or ""
        pub_date = item.findtext("pubDate") or ""

        candidates.append({
            "title": title,
            "magnet": _make_magnet(info_hash, title),
            "info_hash": info_hash,
            "seeders": seeders,
            "leechers": leechers,
            "downloads": downloads,
            "size_str": size_str,
            "size_mib": size_mib,
            "quality_score": _quality_score(title),
            "has_chinese_subs": _has_chinese_subs(title),
            "view_url": view_url,
            "pub_date": pub_date,
            "source": "sukebei",
        })
    return candidates


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def search_jav_code(code: str, *, limit: int = 30,
                          force_refresh: bool = False) -> list[dict]:
    """Search sukebei for the JAV code, return ranked candidates."""
    code = code.strip().upper()
    if not code:
        return []

    cached = store.jav_search_cache_get(code) if not force_refresh else None
    if cached is not None:
        return cached[:limit]

    url = f"https://sukebei.nyaa.si/?page=rss&q={quote(code)}&f=0&c=0_0"
    log.info("sukebei search: %s", url)
    async with _make_client() as c:
        try:
            r = await c.get(url)
        except httpx.HTTPError as e:
            log.warning("sukebei search failed: %s", e)
            return []
        if r.status_code != 200:
            log.warning("sukebei %s → HTTP %s", url, r.status_code)
            return []
        candidates = _parse_sukebei_rss(r.text)

    # Strict filter: title must contain the code (sukebei's search is fuzzy)
    code_norm = re.sub(r"[\s_\-\.]+", "", code)
    candidates = [
        x for x in candidates
        if code_norm in re.sub(r"[\s_\-\.]+", "", x["title"].upper())
    ]

    # Rank: quality desc, then seeders desc, then size desc (bigger usually = better master)
    candidates.sort(key=lambda x: (
        -x["quality_score"],
        -x["seeders"],
        -x["size_mib"],
    ))

    store.jav_search_cache_set(code, candidates)
    return candidates[:limit]


def best_candidate(candidates: list[dict]) -> Optional[dict]:
    """Pick the single best candidate for batch operations."""
    if not candidates:
        return None
    # Prefer: has Chinese subs > seeders > quality
    # but fall back to highest seeders if nothing has Chinese subs
    cs = sorted(
        candidates,
        key=lambda x: (
            -1 if x["has_chinese_subs"] else 0,
            -x["seeders"],
            -x["quality_score"],
            -x["size_mib"],
        ),
    )
    return cs[0]
