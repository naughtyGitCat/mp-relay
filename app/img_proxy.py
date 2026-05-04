"""Image proxy — fetches hotlinked images server-side with the right Referer
so the browser can load them on a page served from a different origin.

Why: JavBus's Cloudflare CDN blocks image requests that don't carry a
``Referer: https://www.javbus.com/`` header (verified on 2026-05-04 — bare
GET returns 403, with Referer returns 200). The browser can't be coerced
into sending an arbitrary Referer for security reasons, so any direct
``<img src="https://www.javbus.com/pics/...">`` from a page on
``http://10.100.100.13:5000`` shows broken. The discover page's actor
photos and film covers all hit this.

Fix: route those URLs through ``/api/img-proxy?url=<encoded>``. mp-relay
fetches with the right Referer + UA, streams the bytes back. Browser sees
a same-origin image so no Referer / CORS issues.

Safety:
- Strict host whitelist (``_ALLOWED_HOSTS``) so this isn't an open SSRF.
- Tiny in-process LRU cache keeps the most-recent N images warm; capped by
  count rather than bytes (image sizes are bounded by the upstream CDN).
- Fail-soft: bad URLs / unreachable upstream return None and the endpoint
  serves a 404. UI broken-image icon falls back gracefully.
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Optional
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)


# Hosts whose images we'll fetch on the user's behalf. Keep tight to prevent
# SSRF and bandwidth abuse. Add new hosts only when a UI surface needs them.
_ALLOWED_HOSTS: frozenset[str] = frozenset({
    "www.javbus.com",
    "javbus.com",
    "img.javbus.com",
    "lain.bgm.tv",          # Bangumi cover thumbnails
    "pics.dmm.co.jp",       # JavBus film covers sometimes link DMM directly
})

# Per-host Referer to send when fetching. Falls back to ``https://<host>/``
# if not listed. Hotlink-protected CDNs check this header before serving.
_REFERER_BY_HOST: dict[str, str] = {
    "www.javbus.com": "https://www.javbus.com/",
    "javbus.com": "https://www.javbus.com/",
    "img.javbus.com": "https://www.javbus.com/",
    "lain.bgm.tv": "https://bgm.tv/",
    "pics.dmm.co.jp": "https://www.dmm.co.jp/",
}

_UA: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# In-process cache. Each entry: (body_bytes, content_type). Capacity in
# entries; not bytes — JavBus thumbnails are 30-100 KiB, so 200 entries ~
# 20 MiB upper bound. Move-to-end on hit gives LRU behavior.
_CACHE_MAX: int = 200
_cache: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
_lock: asyncio.Lock = asyncio.Lock()


def is_allowed(url: str) -> bool:
    """Return True iff ``url`` is HTTP(S) AND its host is on the allowlist.
    Used by the API endpoint as the SSRF gate."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return host in _ALLOWED_HOSTS


async def fetch(url: str) -> Optional[tuple[bytes, str]]:
    """Fetch ``url`` (after host check), return ``(body, content_type)``.

    Returns None if:
    - URL fails the host check
    - upstream returns non-200
    - network error
    """
    # Cache lookup before the host check — a cached response is already vetted.
    async with _lock:
        cached = _cache.get(url)
        if cached is not None:
            _cache.move_to_end(url)
            return cached

    if not is_allowed(url):
        return None

    host = (urlparse(url).hostname or "").lower()
    referer = _REFERER_BY_HOST.get(host, f"https://{host}/")

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": _UA, "Referer": referer})
    except httpx.HTTPError as e:
        log.warning("img-proxy %s failed: %s", url, e)
        return None
    if r.status_code != 200:
        log.info("img-proxy %s → HTTP %s", url, r.status_code)
        return None

    content_type = r.headers.get("content-type", "image/jpeg").split(";", 1)[0].strip()
    body = r.content
    if not body:
        return None

    async with _lock:
        _cache[url] = (body, content_type)
        _cache.move_to_end(url)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return body, content_type


def cache_stats() -> dict:
    """Cache size for debugging / metrics."""
    return {"size": len(_cache), "capacity": _CACHE_MAX}


def cache_clear() -> int:
    """Drop all cached images. Returns count cleared. Useful from tests / CLI."""
    n = len(_cache)
    _cache.clear()
    return n
