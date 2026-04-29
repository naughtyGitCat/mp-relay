"""Phase 1 — find magnets for a JAV code, multi-source.

Sources (queried concurrently per `JAV_SEARCH_SOURCES`):
  - sukebei.nyaa.si RSS — fast, broad, raw seeder counts
  - JavBus              — curated, detail-page AJAX endpoint, quality/CN-sub tags
  - JavDB               — curated, search → detail page, recent uploads
  - MissAV              — primarily streaming; magnets where present (best-effort)

Returned candidates are deduped by ``info_hash`` (first seen wins) and ranked
by: suspicion ASC → quality DESC → seeders DESC → size DESC.

Each candidate carries a ``source`` field so the UI / metrics can attribute
where a magnet came from.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import xml.etree.ElementTree as ET
from typing import Awaitable, Callable, Optional
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from . import metrics as m
from . import store
from .config import settings

log = logging.getLogger(__name__)

_UA: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_NS: dict[str, str] = {"nyaa": "https://sukebei.nyaa.si/xmlns/nyaa"}

# Per-source request timeout. Cloudflare'd sites can be slow; sukebei usually fast.
_TIMEOUT_SEC: float = 25.0


def _make_client(*, timeout: float = _TIMEOUT_SEC,
                 cookies: Optional[dict[str, str]] = None,
                 cookie_header: str = "") -> httpx.AsyncClient:
    """Build an httpx client.

    The ``Accept: text/html...`` header is critical — JavBus serves an
    age-verification interstitial when this header is missing or wrong, so we
    always send it. Confirmed via real-host A/B test 2026-04-29.

    For Cloudflare-protected sites (JavDB, MissAV), pass a raw ``cookie_header``
    extracted from a logged-in browser session — that's the only way through
    without a JS-challenge solver.
    """
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    kw: dict = dict(
        headers=headers,
        follow_redirects=True,
        timeout=timeout,
    )
    # cookies and cookie_header are intentionally exclusive — Cookie header
    # takes precedence (Cloudflare is happier with raw browser cookies).
    if cookies and not cookie_header:
        kw["cookies"] = cookies
    if settings.discover_proxy:
        kw["proxy"] = settings.discover_proxy
    return httpx.AsyncClient(**kw)


# Cookies for sites that gate movie pages behind interstitials we can skip
# without consenting interactively. JavBus's `existmag` controls a content
# filter (mag/all/online) and incidentally also dismisses the age modal on
# subsequent loads.
_JAVBUS_COOKIES: dict[str, str] = {"existmag": "all"}


# ---------------------------------------------------------------------------
# Quality / suspicion scoring (shared across sources)
# ---------------------------------------------------------------------------

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
    upper = title.upper()
    indicators = ["中文", "中字", "字幕", "CHS", "CHT", "CHINESE", "SUBTITLES"]
    return any(t in upper for t in indicators)


_SUSPICION_MARKERS: list[tuple[int, list[str]]] = [
    (3, ["+++", "广告", "promo", "AD-", "[AD]"]),
    (2, ["无修", "破解", "破解版", "RIP"]),
    (1, ["MP4]", "[CN]", "CRAWLER", "DUMP"]),
]


def _suspicion_score(title: str) -> int:
    upper = title.upper()
    score = 0
    for weight, tokens in _SUSPICION_MARKERS:
        for t in tokens:
            if t.upper() in upper:
                score += weight
    return score


def _parse_size_to_mib(size_str: str) -> float:
    """Parse '1.4 GiB' / '650 MiB' / '5250MB' / '5.97GB' → MiB float."""
    if not size_str:
        return 0.0
    s = size_str.strip()
    match = re.match(r"^([\d.]+)\s*([KMGTP])i?B?$", s, re.I)
    if not match:
        return 0.0
    value = float(match.group(1))
    unit = match.group(2).upper()
    factors = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024, "P": 1024 ** 3}
    return value * factors.get(unit, 0)


_TRACKERS: list[str] = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
]


def _make_magnet(info_hash: str, title: str) -> str:
    trackers = "&".join(f"tr={quote(t, safe='')}" for t in _TRACKERS)
    return f"magnet:?xt=urn:btih:{info_hash}&dn={quote(title)}&{trackers}"


def _info_hash_from_magnet(magnet: str) -> Optional[str]:
    match = re.search(r"btih:([a-f0-9]+)", magnet, re.I)
    return match.group(1).lower() if match else None


def _build_candidate(
    *,
    title: str,
    magnet: str,
    info_hash: str,
    seeders: int = 0,
    leechers: int = 0,
    downloads: int = 0,
    size_str: str = "",
    view_url: str = "",
    pub_date: str = "",
    source: str,
    extra_text: str = "",
) -> dict:
    """Construct a uniformly-shaped candidate dict for any source.

    ``extra_text`` is an additional string (e.g. tag pills "高清 中文字幕") used
    for quality / suspicion / chinese-subs heuristics on top of ``title``.
    """
    scoring_text = f"{title} {extra_text}".strip()
    return {
        "title": title,
        "magnet": magnet,
        "info_hash": info_hash.lower(),
        "seeders": seeders,
        "leechers": leechers,
        "downloads": downloads,
        "size_str": size_str,
        "size_mib": _parse_size_to_mib(size_str),
        "quality_score": _quality_score(scoring_text),
        "suspicion_score": _suspicion_score(scoring_text),
        "has_chinese_subs": _has_chinese_subs(scoring_text),
        "view_url": view_url,
        "pub_date": pub_date,
        "source": source,
    }


# ---------------------------------------------------------------------------
# Source 1: sukebei.nyaa.si (RSS)
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

        candidates.append(_build_candidate(
            title=title,
            magnet=_make_magnet(info_hash, title),
            info_hash=info_hash,
            seeders=seeders,
            leechers=leechers,
            downloads=downloads,
            size_str=size_str,
            view_url=item.findtext("guid") or "",
            pub_date=item.findtext("pubDate") or "",
            source="sukebei",
        ))
    return candidates


async def _fetch_sukebei(code: str) -> list[dict]:
    url = f"https://sukebei.nyaa.si/?page=rss&q={quote(code)}&f=0&c=0_0"
    log.info("sukebei search: %s", url)
    async with _make_client() as c:
        try:
            r = await c.get(url)
        except httpx.HTTPError as e:
            log.warning("sukebei fetch failed: %s", e)
            m.JAV_SEARCH_TOTAL.labels(source="sukebei", result="error").inc()
            return []
        if r.status_code != 200:
            log.warning("sukebei → HTTP %s", r.status_code)
            m.JAV_SEARCH_TOTAL.labels(source="sukebei", result="error").inc()
            return []
        out = _parse_sukebei_rss(r.text)
    m.JAV_SEARCH_TOTAL.labels(
        source="sukebei", result="hit" if out else "empty",
    ).inc()
    return out


# ---------------------------------------------------------------------------
# Source 2: JavBus (detail page → AJAX magnet endpoint)
# ---------------------------------------------------------------------------

def _extract_javbus_ajax_vars(html: str) -> Optional[dict[str, str]]:
    """Extract gid / img / uc from the movie detail page's inline JS.

    ``lang`` is a global constant ('zh') and ``floor`` is generated client-side
    on each call (``Math.floor(1e3*Math.random()+1)``); both are injected by
    the caller, NOT scraped from the page.

    Reference: /js/gallery.js shows the AJAX URL is built as
    ``../ajax/uncledatoolsbyajax.php?gid=<gid>&lang=<lang>&img=<img>&uc=<uc>&floor=<random>``
    """
    out: dict[str, str] = {}
    patterns = {
        "gid": r"var\s+gid\s*=\s*(\d+)",
        "img": r"var\s+img\s*=\s*['\"]([^'\"]+)['\"]",
        "uc": r"var\s+uc\s*=\s*(\d+)",
    }
    for name, pat in patterns.items():
        match = re.search(pat, html)
        if not match:
            return None
        out[name] = match.group(1)
    return out


def _parse_javbus_magnet_html(html: str, view_url: str) -> list[dict]:
    """Parse the HTML fragment returned by JavBus's AJAX endpoint."""
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    for tr in soup.select("tr"):
        link = tr.select_one("a[href^='magnet:']")
        if not link:
            continue
        magnet = (link.get("href") or "").strip()
        info_hash = _info_hash_from_magnet(magnet)
        if not info_hash or len(info_hash) < 32:
            continue
        title = link.get_text(strip=True)

        cells = tr.select("td")
        size_str = cells[1].get_text(strip=True) if len(cells) >= 2 else ""
        date = cells[2].get_text(strip=True) if len(cells) >= 3 else ""

        # Tag pills next to the magnet link: 高清 / 无码 / 中文字幕 / 4K …
        tags = " ".join(t.get_text(strip=True) for t in tr.select("a.btn"))

        out.append(_build_candidate(
            title=title,
            magnet=magnet,
            info_hash=info_hash,
            size_str=size_str,
            view_url=view_url,
            pub_date=date,
            source="javbus",
            extra_text=tags,
        ))
    return out


async def _fetch_javbus(code: str) -> list[dict]:
    base = settings.javbus_base.rstrip("/")
    detail_url = f"{base}/{code}"
    async with _make_client(cookies=_JAVBUS_COOKIES) as c:
        # Step 1: detail page → JS vars
        try:
            r = await c.get(detail_url)
        except httpx.HTTPError as e:
            log.warning("javbus detail fetch failed: %s", e)
            m.JAV_SEARCH_TOTAL.labels(source="javbus", result="error").inc()
            return []
        if r.status_code != 200:
            log.info("javbus %s → HTTP %s (probably no such code)", detail_url, r.status_code)
            m.JAV_SEARCH_TOTAL.labels(source="javbus", result="empty").inc()
            return []
        ajax_vars = _extract_javbus_ajax_vars(r.text)
        if not ajax_vars:
            log.warning("javbus: AJAX vars not found in %s", detail_url)
            m.JAV_SEARCH_TOTAL.labels(source="javbus", result="error").inc()
            return []

        # Step 2: AJAX endpoint → magnet table.
        # `floor` is a per-request random in [1, 1000] (JS does the same).
        ajax_url = f"{base}/ajax/uncledatoolsbyajax.php"
        params = {
            **ajax_vars,
            "lang": "zh",
            "floor": str(random.randint(1, 1000)),
        }
        try:
            r2 = await c.get(ajax_url, params=params, headers={"Referer": detail_url})
        except httpx.HTTPError as e:
            log.warning("javbus AJAX fetch failed: %s", e)
            m.JAV_SEARCH_TOTAL.labels(source="javbus", result="error").inc()
            return []
        if r2.status_code != 200:
            log.warning("javbus AJAX → HTTP %s", r2.status_code)
            m.JAV_SEARCH_TOTAL.labels(source="javbus", result="error").inc()
            return []
        out = _parse_javbus_magnet_html(r2.text, detail_url)

    m.JAV_SEARCH_TOTAL.labels(
        source="javbus", result="hit" if out else "empty",
    ).inc()
    return out


# ---------------------------------------------------------------------------
# Source 3: JavDB (search → first matching detail page → magnets section)
# ---------------------------------------------------------------------------

def _parse_javdb_search(html: str, code: str, base_url: str) -> Optional[str]:
    """Find the JavDB detail page URL whose displayed code matches ``code``.
    Returns the absolute detail URL or None.
    """
    soup = BeautifulSoup(html, "lxml")
    code_norm = re.sub(r"[\s_\-\.]+", "", code.upper())
    for a in soup.select("a.box[href*='/v/']"):
        # JavDB renders the code as the first <strong> in .video-title
        strong = a.select_one(".video-title strong, strong")
        if not strong:
            continue
        page_code = re.sub(r"[\s_\-\.]+", "", strong.get_text(strip=True).upper())
        if page_code == code_norm:
            href = a.get("href") or ""
            if href:
                return urljoin(base_url, href)
    return None


def _parse_javdb_magnets(html: str, view_url: str) -> list[dict]:
    """Parse the magnet rows on a JavDB detail page (#magnets-content .item)."""
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    container = soup.select_one("#magnets-content") or soup
    for item in container.select(".item"):
        link = item.select_one("a[href^='magnet:']")
        if not link:
            continue
        magnet = (link.get("href") or "").strip()
        info_hash = _info_hash_from_magnet(magnet)
        if not info_hash or len(info_hash) < 32:
            continue

        # Title block — JavDB shows filename in .magnet-name then meta
        name_el = item.select_one(".name, .magnet-name .name")
        title = name_el.get_text(strip=True) if name_el else link.get_text(strip=True)

        size_el = item.select_one(".meta")
        size_str = ""
        date = ""
        if size_el:
            # ".meta" usually has comma-separated "size, date"
            parts = [p.strip() for p in size_el.get_text(",").split(",") if p.strip()]
            if parts:
                size_str = parts[0]
            if len(parts) > 1:
                date = parts[-1]

        # Tag pills like 高清 / 中文字幕
        tags = " ".join(s.get_text(strip=True) for s in item.select(".tags span, .tags .tag"))

        out.append(_build_candidate(
            title=title,
            magnet=magnet,
            info_hash=info_hash,
            size_str=size_str,
            view_url=view_url,
            pub_date=date,
            source="javdb",
            extra_text=tags,
        ))
    return out


async def _fetch_javdb(code: str) -> list[dict]:
    if not settings.javdb_cookie.strip():
        # Cloudflare-protected; without a real session cookie we'll just 403.
        log.debug("javdb skipped: no JAVDB_COOKIE configured")
        m.JAV_SEARCH_TOTAL.labels(source="javdb", result="empty").inc()
        return []
    base = settings.javdb_base.rstrip("/")
    search_url = f"{base}/search?q={quote(code)}&f=all"
    async with _make_client(cookie_header=settings.javdb_cookie) as c:
        try:
            r = await c.get(search_url)
        except httpx.HTTPError as e:
            log.warning("javdb search failed: %s", e)
            m.JAV_SEARCH_TOTAL.labels(source="javdb", result="error").inc()
            return []
        if r.status_code != 200:
            log.warning("javdb search → HTTP %s", r.status_code)
            m.JAV_SEARCH_TOTAL.labels(source="javdb", result="error").inc()
            return []
        detail_url = _parse_javdb_search(r.text, code, base)
        if not detail_url:
            m.JAV_SEARCH_TOTAL.labels(source="javdb", result="empty").inc()
            return []
        try:
            r2 = await c.get(detail_url)
        except httpx.HTTPError as e:
            log.warning("javdb detail failed: %s", e)
            m.JAV_SEARCH_TOTAL.labels(source="javdb", result="error").inc()
            return []
        if r2.status_code != 200:
            log.warning("javdb detail → HTTP %s", r2.status_code)
            m.JAV_SEARCH_TOTAL.labels(source="javdb", result="error").inc()
            return []
        out = _parse_javdb_magnets(r2.text, detail_url)

    m.JAV_SEARCH_TOTAL.labels(
        source="javdb", result="hit" if out else "empty",
    ).inc()
    return out


# ---------------------------------------------------------------------------
# Source 4: MissAV (best-effort — primarily a streaming site)
# ---------------------------------------------------------------------------

def _parse_missav_magnets(html: str, view_url: str) -> list[dict]:
    """Find any magnet links on a MissAV detail page.

    MissAV is primarily streaming; magnets exist sporadically. This parser is
    intentionally generic — find any `magnet:?xt=urn:btih:...` and extract size
    from nearby text if available.
    """
    out: list[dict] = []
    seen: set[str] = set()
    soup = BeautifulSoup(html, "lxml")

    for a in soup.find_all("a", href=re.compile(r"^magnet:\?xt=urn:btih:", re.I)):
        magnet = (a.get("href") or "").strip()
        info_hash = _info_hash_from_magnet(magnet)
        if not info_hash or len(info_hash) < 32 or info_hash in seen:
            continue
        seen.add(info_hash)

        title = a.get_text(strip=True) or ""

        # Best-effort: search siblings/parent for a size string
        size_str = ""
        parent = a.parent
        if parent:
            sib_text = parent.get_text(" ", strip=True)
            size_match = re.search(r"\b\d+(?:\.\d+)?\s*[KMGT]i?B\b", sib_text, re.I)
            if size_match:
                size_str = size_match.group(0)

        out.append(_build_candidate(
            title=title or "(missav)",
            magnet=magnet,
            info_hash=info_hash,
            size_str=size_str,
            view_url=view_url,
            source="missav",
        ))
    return out


async def _fetch_missav(code: str) -> list[dict]:
    if not settings.missav_cookie.strip():
        log.debug("missav skipped: no MISSAV_COOKIE configured")
        m.JAV_SEARCH_TOTAL.labels(source="missav", result="empty").inc()
        return []
    base = settings.missav_base.rstrip("/")
    code_lower = code.lower()
    # MissAV path conventions vary; try the bare code first then a /cn/ variant.
    detail_urls = [f"{base}/{code_lower}", f"{base}/cn/{code_lower}", f"{base}/en/{code_lower}"]
    async with _make_client(cookie_header=settings.missav_cookie) as c:
        for url in detail_urls:
            try:
                r = await c.get(url)
            except httpx.HTTPError as e:
                log.debug("missav %s failed: %s", url, e)
                continue
            if r.status_code == 200:
                out = _parse_missav_magnets(r.text, url)
                m.JAV_SEARCH_TOTAL.labels(
                    source="missav", result="hit" if out else "empty",
                ).inc()
                return out
            log.debug("missav %s → HTTP %s", url, r.status_code)
    m.JAV_SEARCH_TOTAL.labels(source="missav", result="empty").inc()
    return []


# ---------------------------------------------------------------------------
# Concurrent fanout + dedup + ranking
# ---------------------------------------------------------------------------

# Map source name → fetcher coroutine. Lets us add/remove via .env CSV.
_SOURCE_FETCHERS: dict[str, Callable[[str], Awaitable[list[dict]]]] = {
    "sukebei": _fetch_sukebei,
    "javbus": _fetch_javbus,
    "javdb": _fetch_javdb,
    "missav": _fetch_missav,
}


def _enabled_sources() -> list[str]:
    csv = settings.jav_search_sources.strip()
    if not csv:
        return ["sukebei"]
    out: list[str] = []
    for name in csv.split(","):
        name = name.strip().lower()
        if name and name in _SOURCE_FETCHERS:
            out.append(name)
    return out or ["sukebei"]


def _rank_key(c: dict) -> tuple:
    """Same ordering used everywhere: suspicion ASC → quality DESC → seeders DESC → size DESC."""
    return (
        c.get("suspicion_score", 0),
        -c.get("quality_score", 0),
        -c.get("seeders", 0),
        -c.get("size_mib", 0.0),
    )


def _merge_dedupe(results: list[list[dict]]) -> list[dict]:
    """Concatenate per-source results, dedupe by info_hash (first wins).

    Source order in ``results`` controls which entry survives a duplicate. Use
    sukebei first when present so we keep its real seeders / leechers, then
    JavBus / JavDB which often have richer titles + tags.
    """
    seen: set[str] = set()
    merged: list[dict] = []
    for batch in results:
        for c in batch:
            h = c.get("info_hash") or ""
            if not h or h in seen:
                continue
            seen.add(h)
            merged.append(c)
    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def search_jav_code(code: str, *, limit: int = 30,
                          force_refresh: bool = False) -> list[dict]:
    """Search all enabled sources concurrently for ``code``, return ranked candidates.

    Cache-aware (24h TTL). Identical info_hash from multiple sources is deduped.
    """
    code = code.strip().upper()
    if not code:
        return []

    cached = store.jav_search_cache_get(code) if not force_refresh else None
    if cached is not None:
        m.JAV_SEARCH_TOTAL.labels(source="cache", result="cached").inc()
        return cached[:limit]

    sources = _enabled_sources()
    log.info("jav search %s sources=%s", code, sources)

    # Run all sources concurrently; gather with return_exceptions so one
    # source's hiccup doesn't kill the others.
    with m.JAV_SEARCH_DURATION.time():
        coros = [_SOURCE_FETCHERS[name](code) for name in sources]
        gathered: list = await asyncio.gather(*coros, return_exceptions=True)

    per_source: list[list[dict]] = []
    for name, result in zip(sources, gathered):
        if isinstance(result, Exception):
            log.warning("jav source %s raised: %s", name, result)
            m.JAV_SEARCH_TOTAL.labels(source=name, result="error").inc()
            per_source.append([])
        else:
            per_source.append(result)

    # Strict filter: title must contain the code (sukebei's search is fuzzy;
    # other sources query by code so this is mostly a no-op for them).
    code_norm = re.sub(r"[\s_\-\.]+", "", code)
    for batch in per_source:
        batch[:] = [
            x for x in batch
            if not x["title"]                                          # MissAV may have empty titles — keep them
            or code_norm in re.sub(r"[\s_\-\.]+", "", x["title"].upper())
        ]

    candidates = _merge_dedupe(per_source)
    candidates.sort(key=_rank_key)

    store.jav_search_cache_set(code, candidates)
    return candidates[:limit]


async def search_keyword(keyword: str, *, limit: int = 30) -> list[dict]:
    """Free-text magnet search — for cases where the user has a Japanese title
    (e.g. from a Bangumi match) but no JAV code.

    Only sukebei is queried — JavBus / JavDB / MissAV all fetch by exact code
    URL (e.g. /SSIS-001) so they can't search arbitrary text.

    Skips the strict ``code in title`` filter that ``search_jav_code`` applies,
    because for free-text we want broader recall (sukebei's own search already
    does fuzzy matching on the keyword).

    No SQLite cache — caller is expected to drive this from a button click,
    not a hot path. (We could add a separate cache table later if it shows up
    as a perf issue.)
    """
    keyword = keyword.strip()
    if not keyword:
        return []

    # Reuse the sukebei fetcher with the keyword in place of a code. The
    # underlying RSS endpoint accepts any free-text in ?q= so this works
    # for JP titles, English titles, mixed, etc.
    candidates = await _fetch_sukebei(keyword)
    candidates.sort(key=_rank_key)
    return candidates[:limit]


def best_candidate(candidates: list[dict], *,
                   exclude_hashes: Optional[set[str]] = None) -> Optional[dict]:
    """Pick the single best candidate for batch operations.

    exclude_hashes: skip candidates whose info_hash is in this set
    (used by retry-on-QC-fail to pick the next-best alternative).
    """
    if not candidates:
        return None
    pool = candidates
    if exclude_hashes:
        pool = [c for c in candidates if c.get("info_hash") not in exclude_hashes]
    if not pool:
        return None
    cs = sorted(
        pool,
        key=lambda x: (
            x.get("suspicion_score", 0),
            -1 if x.get("has_chinese_subs") else 0,
            -x.get("seeders", 0),
            -x.get("quality_score", 0),
            -x.get("size_mib", 0.0),
        ),
    )
    return cs[0]
