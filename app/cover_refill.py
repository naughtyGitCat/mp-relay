"""Cover-image refill for already-scraped Jellyfin folders that are missing
poster/fanart/thumb images.

Why this exists:
   mdcx scrape sometimes succeeds at metadata (NFO is written, file moved to
   library) but fails to download cover images — either because:
     - JavBus is gated behind Cloudflare driver-verify (observed 2026-05-05)
       and any image-URL extraction breaks
     - mdcx fell back to a different scraper that didn't return image URLs
     - mdcx's ``ignore_pic_fail`` setting hides the error
   Result: a folder with ``MOVIE.mp4`` + ``MOVIE.nfo`` but no ``.jpg``.
   Jellyfin then falls back to TMDB lookup which returns garbage for adult
   codes. Audit on 2026-05-05 found 210 of 2168 (~10%) library entries in
   this state.

Strategy:
   For each cover-missing folder, peek into the NFO:
     1. ``<javdbid>`` present → fetch ``c0.jdbstatic.com/covers/<prefix>/<id>.jpg``
        directly. Verified URL pattern + Referer requirement on 2026-05-05.
     2. ``<num>`` present but no javdbid → search JavDB for the code, take
        the first matching result, then fall back to (1).
     3. Neither → skip; user can deal with these manually.
   Save the fetched cover under all the names Jellyfin recognizes:
   ``<code>-poster.jpg``, ``<code>-fanart.jpg``, ``<code>-thumb.jpg``,
   ``folder.jpg``. JavDB covers are ~800x540 (between 16:9 and 4:3) — they
   work fine as fanart and pass as poster too. Cropping for proper 2:3
   poster ratio could be a follow-up.

Concurrency:
   Bounded by ``_REFILL_CONCURRENCY`` to avoid hammering JavDB. Per-folder
   work is small (~100 KB image fetch + 4 file writes) so 4 in flight is
   plenty. Driving it harder would risk JavDB throttling us, since refill
   shares the same Cloudflare gate as ``jav_search._fetch_javdb``.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urljoin

import httpx

from .config import settings

log = logging.getLogger(__name__)


_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# JavDB image CDN. Verified 2026-05-05: returns 200 + JPEG when called with
# Referer: https://javdb.com/. Lowercase 2-char prefix derived from javdbid.
_JAVDB_CDN_BASE: str = "https://c0.jdbstatic.com/covers"
_JAVDB_REFERER: str = "https://javdb.com/"

# Image extensions Jellyfin treats as covers — we use these to detect
# "folder already has images, skip".
_IMG_EXTS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif",
})

# Limit concurrent JavDB requests so we don't get rate-limited / banned.
_REFILL_CONCURRENCY: int = 4
_javdb_semaphore: asyncio.Semaphore = asyncio.Semaphore(_REFILL_CONCURRENCY)

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


def _has_image(folder: Path) -> bool:
    """True if the folder already contains ANY image file. Used as the
    early-skip gate so we don't redownload existing covers."""
    try:
        return any(p.suffix.lower() in _IMG_EXTS for p in folder.iterdir() if p.is_file())
    except OSError:
        return False


# ---------------------------------------------------------------------------
# JavDB fetchers
# ---------------------------------------------------------------------------

def _javdb_cover_url(javdbid: str) -> str:
    """Build the canonical JavDB cover URL for a javdbid.

    Pattern (verified 2026-05-05):
        https://c0.jdbstatic.com/covers/<lower(id[:2])>/<id>.jpg
    """
    prefix = javdbid[:2].lower()
    return f"{_JAVDB_CDN_BASE}/{prefix}/{javdbid}.jpg"


async def _fetch_cover_bytes(client: httpx.AsyncClient, javdbid: str) -> Optional[bytes]:
    """Download a JavDB cover. Returns bytes on 200 + non-empty body, else None.
    Acquires the module-level semaphore so callers can fan out without
    coordinating concurrency themselves."""
    url = _javdb_cover_url(javdbid)
    async with _javdb_semaphore:
        try:
            r = await client.get(url, headers={
                "User-Agent": _USER_AGENT,
                "Referer": _JAVDB_REFERER,
            })
        except httpx.HTTPError as e:
            log.warning("javdb cover fetch %s failed: %s", url, e)
            return None
    if r.status_code != 200:
        log.info("javdb cover %s → HTTP %s", url, r.status_code)
        return None
    if not r.content:
        return None
    return r.content


async def _search_javdb_for_id(client: httpx.AsyncClient, code: str) -> Optional[str]:
    """For folders missing javdbid but with <num>, search JavDB for the code
    and return the javdbid embedded in the first matching detail link.

    JavDB detail URLs look like ``/v/<javdbid>``. We pluck that ID from the
    href rather than scraping the page body. Search is currently best-effort:
    if it fails (no cookie, Cloudflare gate, code not in JavDB), we just
    return None and the caller falls through to ``skip_no_id``.
    """
    cookie = (settings.javdb_cookie or "").strip()
    base = (settings.javdb_base or "https://javdb.com").rstrip("/")
    search_url = f"{base}/search?q={quote(code)}&f=all"

    headers = {"User-Agent": _USER_AGENT, "Referer": _JAVDB_REFERER}
    if cookie:
        headers["Cookie"] = cookie

    async with _javdb_semaphore:
        try:
            r = await client.get(search_url, headers=headers)
        except httpx.HTTPError as e:
            log.warning("javdb search failed for %s: %s", code, e)
            return None
    if r.status_code != 200:
        log.info("javdb search %s → HTTP %s", code, r.status_code)
        return None

    # First /v/<id> link in the result page. We don't try to verify the code
    # matches — JavDB's search returns relevance-ranked results, and a wrong
    # match here just means we'd write the wrong cover. The caller can run
    # in dry_run first to spot-check.
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
    # Folder names look like "MOVIE-001 actor1,actor2" — take first whitespace
    # token. Fall back to the whole name if no whitespace.
    first = folder_name.split()[0] if folder_name else "unknown"
    return re.sub(r"[^\w\-]", "_", first) or "cover"


def _write_covers(folder: Path, code: str, body: bytes, *, dry_run: bool) -> list[str]:
    """Write the same image bytes under all four Jellyfin-recognized names.

    JavDB covers are ~16:10 ratio; using the same image as poster + fanart +
    thumb works fine in practice (Jellyfin won't reject any of them) and
    beats the alternative of letting Jellyfin's TMDB lookup fill in random
    movies. A future improvement could crop to 2:3 for the poster.
    """
    # Naming convention matches what mdcx's healthy scrapes produce, so
    # Jellyfin treats the refilled folders identically.
    names = [
        f"{code}-poster.jpg",
        f"{code}-fanart.jpg",
        f"{code}-thumb.jpg",
        "folder.jpg",
    ]
    written: list[str] = []
    for name in names:
        target = folder / name
        if target.exists():
            continue
        if dry_run:
            written.append(name)
            continue
        try:
            target.write_bytes(body)
            written.append(name)
        except OSError as e:
            log.warning("can't write %s: %s", target, e)
    return written


async def refill_one(client: httpx.AsyncClient, folder: Path, *, dry_run: bool) -> RefillResult:
    """Attempt to refill cover images for a single folder.

    Walks through the strategy ladder:
      1. Has image? → ``skip_has_img``
      2. Has javdbid in NFO? → fetch + write (or dry_run)
      3. Has num but no javdbid? → search JavDB, then fetch + write
      4. Neither → ``skip_no_id``
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

    if not javdbid:
        if not num:
            res.status = "skip_no_id"
            res.reason = "NFO has no <javdbid> or <num>"
            return res
        # Strategy: search by num
        log.info("refill: searching javdb for %s", num)
        javdbid = await _search_javdb_for_id(client, num) or ""
        if not javdbid:
            res.status = "skip_no_id"
            res.reason = f"javdb search for {num} found no matching id"
            return res
        res.javdbid = javdbid

    body = await _fetch_cover_bytes(client, javdbid)
    if not body:
        res.status = "error"
        res.reason = f"could not fetch cover for javdbid={javdbid}"
        return res

    res.files_written = _write_covers(folder, res.code, body, dry_run=dry_run)
    res.status = "dry_run" if dry_run else "refilled"
    return res


# ---------------------------------------------------------------------------
# Library walk
# ---------------------------------------------------------------------------

def _enumerate_movie_folders(root: Path) -> list[Path]:
    """Walk a Jellyfin-style library: ``<root>/<studio_or_actor>/<movie_dir>``.

    Only folders containing an .nfo are considered movie folders — extras
    (actor headshots, behind-the-scenes) don't have NFOs. This matches the
    audit script that flagged the missing-image folders in the first place.
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
                       limit: Optional[int] = None) -> dict:
    """Walk ``root`` and refill every cover-missing folder. Returns summary.

    ``dry_run=True`` (the safe default) walks, plans, and reports what *would*
    be written, but doesn't touch the disk.
    """
    root_path = Path(root)
    folders = _enumerate_movie_folders(root_path)
    log.info("cover-refill scanning %s: %d movie folders", root, len(folders))

    # Pre-filter: only call refill_one on folders that DON'T already have an
    # image. Cheap is_dir+iterdir; saves spinning up a coroutine for every
    # of the 2k+ healthy folders in a typical library.
    candidates = [f for f in folders if not _has_image(f)]
    log.info("cover-refill candidates (no images): %d", len(candidates))
    if limit is not None:
        candidates = candidates[:limit]

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        results = await asyncio.gather(
            *(refill_one(client, f, dry_run=dry_run) for f in candidates),
            return_exceptions=True,
        )

    summary: dict[str, int] = {}
    out_results: list[dict] = []
    for r in results:
        if isinstance(r, Exception):
            summary["error"] = summary.get("error", 0) + 1
            out_results.append({"folder": "", "status": "error", "reason": str(r)[:200]})
            continue
        summary[r.status] = summary.get(r.status, 0) + 1
        out_results.append({
            "folder": r.folder,
            "code": r.code,
            "javdbid": r.javdbid,
            "status": r.status,
            "reason": r.reason,
            "files_written": r.files_written,
        })

    return {
        "root": root,
        "dry_run": dry_run,
        "scanned_folders": len(folders),
        "missing_image_candidates": len(candidates),
        "summary": summary,
        "results": out_results,
    }
