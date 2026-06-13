"""Cover-image refill for already-scraped Jellyfin folders that are missing
poster/fanart/thumb images.

Why this exists:
   mdcx scrape sometimes succeeds at metadata (NFO is written, file moved to
   library) but fails to download cover images — either because:
     - JavBus is gated behind Cloudflare driver-verify / an age interstitial
       and any image-URL extraction breaks
     - mdcx fell back to a different scraper that didn't return image URLs
     - mdcx's ``ignore_pic_fail`` setting hides the error
   Result: a folder with ``MOVIE.mp4`` + ``MOVIE.nfo`` but no ``.jpg``.
   Jellyfin then falls back to TMDB lookup which returns garbage for adult
   codes. Audit on 2026-05-05 found 210 of 2168 (~10%) library entries in
   this state.

Strategy (multi-site, 2026-06-04):
   For each cover-missing folder, peek into the NFO for ``<num>`` (the 番号)
   and ``<javdbid>``, then try cover sources in order until one yields a real
   image:
     1. **JavBus** — ``{base}/{code}`` detail page → ``a.bigImage`` cover URL.
        Falls back to ``{base}/search/{code}`` → first result → detail. Covers
        the bulk of mainstream censored codes. The ``Accept: text/html`` header
        is mandatory or JavBus serves an age-verification interstitial.
     2. **AVSOX** — ``{base}/cn/search/{code}`` → first ``a.movie-box`` →
        detail → ``a.bigImage``. Best-effort fallback for uncensored codes
        JavBus doesn't carry.
     3. **JavDB CDN** — if the NFO has ``<javdbid>`` (or we can resolve one via
        JavDB search), fetch ``c0.jdbstatic.com/covers/<prefix>/<id>.jpg``.
        JavDB is Cloudflare-protected so this is last and often unavailable.
   Save the fetched cover under all the names Jellyfin recognizes. The
   **poster** + **folder** images are cropped to a portrait front-cover (a
   standard JAV cover is a wide ``[ back | front ]`` image, so the right
   portion is the poster) for a clean poster grid; **fanart** + **thumb** keep
   the full wide image for the backdrop. VR / odd-ratio / already-portrait
   covers are written full-frame to all four. A folder whose only image is a
   corrupt placeholder is treated as image-less and re-filled.

Network:
   All of these sites are GFW-blocked / require an egress proxy. The httpx
   client routes through ``settings.discover_proxy`` (the same proxy the
   discover / jav_search features use) unless an explicit ``proxy`` is passed.

Concurrency:
   Bounded by ``_REFILL_CONCURRENCY`` network slots so we don't hammer the
   sites (and the shared proxy). Per-folder work is small (~150 KB image +
   a couple of HTML fetches).
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

try:
    from PIL import Image
    _PIL_OK = True
except ImportError:  # pragma: no cover - Pillow is a declared dependency
    _PIL_OK = False

from .config import settings

log = logging.getLogger(__name__)


_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Headers that defeat JavBus's age-verification interstitial. The
# ``Accept: text/html`` value is critical — without it JavBus (and clones like
# AVSOX) serve an "Age Verification" page instead of the movie, breaking cover
# extraction. Confirmed via real-host A/B test (see jav_search._make_client).
_HTML_HEADERS: dict[str, str] = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
}

# JavBus ``existmag`` controls a content filter (mag/all/online) and
# incidentally dismisses the age modal on subsequent loads.
_JAVBUS_COOKIES: dict[str, str] = {"existmag": "all"}

# JavDB image CDN. Verified 2026-05-05: returns 200 + JPEG when called with
# Referer: https://javdb.com/. Lowercase 2-char prefix derived from javdbid.
_JAVDB_CDN_BASE: str = "https://c0.jdbstatic.com/covers"
_JAVDB_REFERER: str = "https://javdb.com/"

# Image extensions Jellyfin treats as covers — we use these to detect
# "folder already has images, skip".
_IMG_EXTS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif",
})

# Poster cropping. Standard JAV "pl" covers are a wide ``[ back | front ]``
# image (~1.49 ratio); the front cover is the right portion. Cropping from this
# fraction of the width yields a ~2:3 portrait poster that fills Jellyfin's
# poster grid properly instead of a squished landscape. Verified against
# DMM/JavBus covers. Covers that are already portrait, or VR / montage covers
# (no single front cover), are left full-frame.
_POSTER_CROP_LEFT: float = 0.5375
_CROP_MIN_RATIO: float = 1.20   # below this it's already portrait — don't crop
_CROP_MAX_RATIO: float = 1.58   # at/above this it's VR/montage/odd — don't crop

# Below this size a file can't be a real cover — it's a 0-byte stub, a
# truncated download, or an HTML error page saved as ``.jpg``. Treated as "no
# image" so the folder gets (re)filled instead of skipped on junk.
_MIN_IMAGE_BYTES: int = 2000

# Magic-byte prefixes for the image formats these sites serve. Used to reject
# HTML error pages / Cloudflare challenges that come back with a 200 but aren't
# actually an image, so we never write a ``.jpg`` that's really HTML.
_IMG_MAGIC: tuple[bytes, ...] = (
    b"\xff\xd8\xff",   # JPEG
    b"\x89PNG\r\n",    # PNG
    b"GIF8",           # GIF
    b"BM",             # BMP
)

# Limit concurrent outbound requests so we don't get rate-limited / banned and
# don't saturate the shared egress proxy.
_REFILL_CONCURRENCY: int = 4
_fetch_semaphore: asyncio.Semaphore = asyncio.Semaphore(_REFILL_CONCURRENCY)

# Compiled NFO field extractors. The ``<num>`` regex is permissive because
# older mdcx variants sometimes wrap codes in CDATA or whitespace.
_RE_JAVDBID = re.compile(r"<javdbid>(.*?)</javdbid>", re.S)
_RE_NUM = re.compile(r"<num>(.*?)</num>", re.S)


@dataclass
class RefillResult:
    """Outcome of one folder's refill attempt. Returned to the caller so the
    /api endpoint can render a per-folder report."""
    folder: str
    code: str = ""
    javdbid: str = ""
    source: str = ""          # javbus | avsox | javdb — which site supplied the cover
    status: str = "pending"   # pending | refilled | skip_has_img | skip_no_id | dry_run | error
    reason: str = ""          # detail for error / skip
    files_written: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# NFO parsing
# ---------------------------------------------------------------------------

def _read_nfo(folder: Path) -> Optional[str]:
    """Return the first .nfo's content, or None if no NFO."""
    for f in folder.iterdir():
        if f.suffix.lower() == ".nfo" and f.is_file():
            try:
                return f.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                log.warning("can't read %s: %s", f, e)
                return None
    return None


def _extract_ids(nfo: str) -> tuple[str, str]:
    """Return ``(javdbid, num)`` extracted from NFO. Either may be ''."""
    jid = ""
    num = ""
    m = _RE_JAVDBID.search(nfo)
    if m:
        jid = m.group(1).strip()
    m = _RE_NUM.search(nfo)
    if m:
        num = m.group(1).strip()
    return jid, num


def _is_valid_image(p: Path) -> bool:
    """True if ``p`` is a real, decodable image. Tiny stubs, truncated files,
    and HTML/error pages saved with an image extension return False so the
    folder isn't skipped on a junk placeholder."""
    try:
        if p.stat().st_size < _MIN_IMAGE_BYTES:
            return False
    except OSError:
        return False
    if not _PIL_OK:
        return True  # can't decode without Pillow; trust extension + size
    try:
        with Image.open(p) as im:
            return im.width > 0 and im.height > 0
    except Exception:
        return False


def _has_image(folder: Path) -> bool:
    """True if the folder already contains a VALID image. Used as the early-skip
    gate so we don't redownload existing covers — but a corrupt/placeholder
    image doesn't count, so those folders get healed on the next run."""
    try:
        return any(
            _is_valid_image(p)
            for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in _IMG_EXTS
        )
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Shared HTTP / HTML helpers
# ---------------------------------------------------------------------------

def _abs_url(base: str, href: str) -> str:
    """Resolve a possibly-relative/protocol-relative href to an absolute URL."""
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http"):
        return href
    return urljoin(base.rstrip("/") + "/", href.lstrip("/"))


def _looks_like_image(content: bytes) -> bool:
    """Reject empty bodies, tiny tracking pixels, and HTML error pages that
    come back as 200 (Cloudflare / age-gate) so we never save non-images."""
    if not content or len(content) < 2000:
        return False
    if content[8:12] == b"WEBP":   # RIFF....WEBP
        return True
    return any(content.startswith(m) for m in _IMG_MAGIC)


async def _guarded_get(client: httpx.AsyncClient, url: str, *,
                       referer: str = "") -> Optional[httpx.Response]:
    """GET ``url`` under the concurrency semaphore. The client already carries
    the anti-age-gate headers + existmag cookie; ``referer`` is added per-call
    for image CDNs that require it. Returns None on transport error."""
    headers = {"Referer": referer} if referer else None
    async with _fetch_semaphore:
        try:
            return await client.get(url, headers=headers)
        except httpx.HTTPError as e:
            log.warning("cover-refill GET %s failed: %s", url, e)
            return None


def _cover_url_from_detail(html: str, base: str) -> str:
    """Extract the cover image URL from a JavBus/AVSOX-style detail page.
    Prefers ``a.bigImage`` (the full DVD cover), falls back to ``og:image``."""
    soup = BeautifulSoup(html, "html.parser")
    big = soup.select_one("a.bigImage")
    if big:
        href = big.get("href") or ""
        if not href:
            img = big.select_one("img")
            href = img.get("src", "") if img else ""
        if href:
            return _abs_url(base, href)
    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content"):
        return _abs_url(base, og["content"])
    return ""


def _first_movie_box(html: str, base: str) -> str:
    """Return the absolute detail URL of the first movie result on a search
    page. ``a.movie-box`` is the JavBus/AVSOX result-item anchor (actor
    avatars use ``a.avatar-box``, which we intentionally skip)."""
    soup = BeautifulSoup(html, "html.parser")
    box = soup.select_one("a.movie-box")
    if box and box.get("href"):
        return _abs_url(base, box["href"])
    return ""


# ---------------------------------------------------------------------------
# Cover sources
# ---------------------------------------------------------------------------

async def _fetch_cover_javbus(client: httpx.AsyncClient, code: str) -> Optional[bytes]:
    """Primary source. Try the direct detail page first, then search.
    Returns JPEG/PNG bytes or None."""
    base = settings.javbus_base.rstrip("/")
    cover_url = ""

    r = await _guarded_get(client, f"{base}/{code}")
    if r is not None and r.status_code == 200:
        cover_url = _cover_url_from_detail(r.text, base)

    if not cover_url:
        rs = await _guarded_get(client, f"{base}/search/{quote(code)}")
        if rs is not None and rs.status_code == 200:
            detail = _first_movie_box(rs.text, base)
            if detail:
                rd = await _guarded_get(client, detail)
                if rd is not None and rd.status_code == 200:
                    cover_url = _cover_url_from_detail(rd.text, base)

    if not cover_url:
        return None
    ri = await _guarded_get(client, cover_url, referer=f"{base}/")
    if ri is not None and ri.status_code == 200 and _looks_like_image(ri.content):
        return ri.content
    return None


async def _fetch_cover_avsox(client: httpx.AsyncClient, code: str) -> Optional[bytes]:
    """Uncensored fallback. AVSOX only exposes a search endpoint; resolve the
    first movie result, then read its detail page cover."""
    base = settings.avsox_base.rstrip("/")
    rs = await _guarded_get(client, f"{base}/cn/search/{quote(code)}")
    if rs is None or rs.status_code != 200:
        return None
    detail = _first_movie_box(rs.text, base)
    if not detail:
        return None
    rd = await _guarded_get(client, detail)
    if rd is None or rd.status_code != 200:
        return None
    cover_url = _cover_url_from_detail(rd.text, base)
    if not cover_url:
        return None
    ri = await _guarded_get(client, cover_url, referer=f"{base}/")
    if ri is not None and ri.status_code == 200 and _looks_like_image(ri.content):
        return ri.content
    return None


# ---------------------------------------------------------------------------
# JavDB fetchers (last-resort source; Cloudflare-gated)
# ---------------------------------------------------------------------------

def _javdb_cover_url(javdbid: str) -> str:
    """Build the canonical JavDB cover URL for a javdbid.

    Pattern (verified 2026-05-05):
        https://c0.jdbstatic.com/covers/<lower(id[:2])>/<id>.jpg
    """
    prefix = javdbid[:2].lower()
    return f"{_JAVDB_CDN_BASE}/{prefix}/{javdbid}.jpg"


async def _fetch_cover_bytes(client: httpx.AsyncClient, javdbid: str) -> Optional[bytes]:
    """Download a JavDB cover. Returns bytes on 200 + image body, else None."""
    url = _javdb_cover_url(javdbid)
    r = await _guarded_get(client, url, referer=_JAVDB_REFERER)
    if r is None or r.status_code != 200:
        if r is not None:
            log.info("javdb cover %s → HTTP %s", url, r.status_code)
        return None
    if not _looks_like_image(r.content):
        return None
    return r.content


async def _search_javdb_for_id(client: httpx.AsyncClient, code: str) -> Optional[str]:
    """Search JavDB for ``code`` and return the javdbid in the first ``/v/<id>``
    detail link. Best-effort: JavDB is Cloudflare-gated, so this usually only
    works when ``settings.javdb_cookie`` carries a valid browser session."""
    cookie = (settings.javdb_cookie or "").strip()
    base = (settings.javdb_base or "https://javdb.com").rstrip("/")
    search_url = f"{base}/search?q={quote(code)}&f=all"

    headers = {"Referer": _JAVDB_REFERER}
    if cookie:
        headers["Cookie"] = cookie

    async with _fetch_semaphore:
        try:
            r = await client.get(search_url, headers=headers)
        except httpx.HTTPError as e:
            log.warning("javdb search failed for %s: %s", code, e)
            return None
    if r.status_code != 200:
        log.info("javdb search %s → HTTP %s", code, r.status_code)
        return None

    m = re.search(r'/v/([A-Za-z0-9]+)', r.text)
    if not m:
        return None
    return m.group(1)


# ---------------------------------------------------------------------------
# Per-folder refill
# ---------------------------------------------------------------------------

def _safe_code(folder_name: str, num: str) -> str:
    """Return a filename-safe code stem for naming written images.
    Prefer ``<num>`` (from NFO); fall back to the folder name's first token.
    """
    if num:
        return re.sub(r"[^\w\-]", "_", num)
    first = folder_name.split()[0] if folder_name else "unknown"
    return re.sub(r"[^\w\-]", "_", first) or "cover"


def _make_poster(body: bytes, raw_code: str = "") -> bytes:
    """Return portrait poster bytes cropped from a wide JAV cover, or ``body``
    unchanged when cropping doesn't apply (no Pillow, already portrait, a
    VR/montage cover, or any decode error). The crop takes the front-cover
    (right) portion of a standard ``[ back | front ]`` cover."""
    if not _PIL_OK or "VR" in (raw_code or "").upper():
        return body
    try:
        with Image.open(io.BytesIO(body)) as im:
            w, h = im.size
            if not h:
                return body
            ratio = w / h
            if ratio < _CROP_MIN_RATIO or ratio >= _CROP_MAX_RATIO:
                return body
            left = int(w * _POSTER_CROP_LEFT)
            cropped = im.crop((left, 0, w, h)).convert("RGB")
        buf = io.BytesIO()
        cropped.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception as e:
        log.warning("poster crop failed (%s); using full cover", e)
        return body


def _write_covers(folder: Path, code: str, body: bytes, *, dry_run: bool,
                  raw_code: str = "") -> list[str]:
    """Write cover images under the Jellyfin-recognized names. ``poster`` and
    ``folder`` get a portrait crop of the front cover (for a clean poster grid);
    ``fanart`` and ``thumb`` keep the full wide image (for the backdrop). VR /
    odd / already-portrait covers are written full-frame to all four."""
    poster = body if dry_run else _make_poster(body, raw_code)
    # Order preserved for a stable ``written`` list; poster + folder share the
    # cropped bytes, fanart + thumb keep the wide bytes.
    name_bytes: list[tuple[str, bytes]] = [
        (f"{code}-poster.jpg", poster),
        (f"{code}-fanart.jpg", body),
        (f"{code}-thumb.jpg", body),
        ("folder.jpg", poster),
    ]
    written: list[str] = []
    for name, data in name_bytes:
        target = folder / name
        if target.exists():
            continue
        if dry_run:
            written.append(name)
            continue
        try:
            target.write_bytes(data)
            written.append(name)
        except OSError as e:
            log.warning("can't write %s: %s", target, e)
    return written


async def refill_one(client: httpx.AsyncClient, folder: Path, *, dry_run: bool) -> RefillResult:
    """Attempt to refill cover images for a single folder.

    Strategy ladder:
      1. Has image? → ``skip_has_img``
      2. No NFO? → ``error``
      3. Have a 番号 → try JavBus, then AVSOX.
      4. Still nothing + have/resolvable javdbid → JavDB CDN.
      5. Nothing anywhere → ``skip_no_id``.
    """
    res = RefillResult(folder=str(folder))

    if _has_image(folder):
        res.status = "skip_has_img"
        return res

    nfo = _read_nfo(folder)
    if not nfo:
        res.status = "error"
        res.reason = "no NFO in folder"
        return res

    javdbid, num = _extract_ids(nfo)
    res.javdbid = javdbid
    res.code = _safe_code(folder.name, num)
    raw_code = (num or "").strip()

    # Fallback: many older/uncensored NFOs omit <num>, but the 番号 is reliably
    # the first whitespace token of the Jellyfin folder name (e.g.
    # "DANDY-386 actor" / "040414-001 actor"). Use it for the site lookup only
    # when it looks like a code (contains a digit) so we don't search on a
    # bare actor name.
    if not raw_code:
        first = folder.name.split()[0] if folder.name else ""
        if first and any(ch.isdigit() for ch in first):
            raw_code = first

    if not raw_code and not javdbid:
        res.status = "skip_no_id"
        res.reason = "no <num>/<javdbid> in NFO and folder name has no code"
        return res

    body: Optional[bytes] = None

    # Primary: 番号-based lookup on JavBus, then AVSOX.
    if raw_code:
        body = await _fetch_cover_javbus(client, raw_code)
        if body:
            res.source = "javbus"
        if not body:
            body = await _fetch_cover_avsox(client, raw_code)
            if body:
                res.source = "avsox"

    # Last resort: JavDB CDN (resolve an id first if we only have a 番号).
    if not body:
        if not javdbid and raw_code:
            javdbid = await _search_javdb_for_id(client, raw_code) or ""
            if javdbid:
                res.javdbid = javdbid
        if javdbid:
            body = await _fetch_cover_bytes(client, javdbid)
            if body:
                res.source = "javdb"

    if not body:
        res.status = "skip_no_id"
        res.reason = f"no cover on javbus/avsox/javdb for {raw_code or javdbid!r}"
        return res

    res.files_written = _write_covers(folder, res.code, body, dry_run=dry_run, raw_code=raw_code)
    res.status = "dry_run" if dry_run else "refilled"
    return res


# ---------------------------------------------------------------------------
# Library walk
# ---------------------------------------------------------------------------

def _enumerate_movie_folders(root: Path) -> list[Path]:
    """Walk a Jellyfin-style library: ``<root>/<studio_or_actor>/<movie_dir>``.

    Only folders containing an .nfo are considered movie folders — extras
    (actor headshots, behind-the-scenes) don't have NFOs.
    """
    out: list[Path] = []
    if not root.is_dir():
        return out
    for studio in root.iterdir():
        if not studio.is_dir():
            continue
        try:
            children = list(studio.iterdir())
        except OSError:
            continue
        for d in children:
            if not d.is_dir():
                continue
            try:
                files = list(d.iterdir())
            except OSError:
                continue
            if any(f.suffix.lower() == ".nfo" for f in files):
                out.append(d)
    return out


async def refill_root(root: str, *, dry_run: bool = True,
                       limit: Optional[int] = None,
                       proxy: Optional[str] = None) -> dict:
    """Walk ``root`` and refill every cover-missing folder. Returns summary.

    ``dry_run=True`` (the safe default) walks, plans, and reports what *would*
    be written, but doesn't touch the disk.

    ``proxy`` overrides ``settings.discover_proxy`` for this run. The sites
    queried (JavBus / AVSOX / JavDB) are GFW-blocked, so a working egress
    proxy is required for any cover to be fetched.
    """
    root_path = Path(root)
    folders = _enumerate_movie_folders(root_path)
    log.info("cover-refill scanning %s: %d movie folders", root, len(folders))

    # Pre-filter: only call refill_one on folders that DON'T already have an
    # image. Saves spinning up a coroutine for every healthy folder.
    candidates = [f for f in folders if not _has_image(f)]
    log.info("cover-refill candidates (no images): %d", len(candidates))
    if limit is not None:
        candidates = candidates[:limit]

    effective_proxy = proxy if proxy is not None else (settings.discover_proxy or None)
    client_kw: dict = dict(
        timeout=25.0,
        follow_redirects=True,
        headers=_HTML_HEADERS,
        cookies=_JAVBUS_COOKIES,
    )
    if effective_proxy:
        client_kw["proxy"] = effective_proxy

    async with httpx.AsyncClient(**client_kw) as client:
        results = await asyncio.gather(
            *(refill_one(client, f, dry_run=dry_run) for f in candidates),
            return_exceptions=True,
        )

    summary: dict[str, int] = {}
    by_source: dict[str, int] = {}
    out_results: list[dict] = []
    for r in results:
        if isinstance(r, Exception):
            summary["error"] = summary.get("error", 0) + 1
            out_results.append({"folder": "", "status": "error", "reason": str(r)[:200]})
            continue
        summary[r.status] = summary.get(r.status, 0) + 1
        if r.source:
            by_source[r.source] = by_source.get(r.source, 0) + 1
        out_results.append({
            "folder": r.folder,
            "code": r.code,
            "javdbid": r.javdbid,
            "source": r.source,
            "status": r.status,
            "reason": r.reason,
            "files_written": r.files_written,
        })

    return {
        "root": root,
        "dry_run": dry_run,
        "proxy_used": bool(effective_proxy),
        "scanned_folders": len(folders),
        "missing_image_candidates": len(candidates),
        "summary": summary,
        "by_source": by_source,
        "results": out_results,
    }
