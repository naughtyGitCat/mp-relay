"""Phase 2 — actor discovery via JavBus HTML scraping.

JavBus has stable URL patterns:
  - /searchstar/<name>            → list of matching actors (or 1 actor's page directly)
  - /star/<id>                    → first page of an actor's films
  - /star/<id>/<page>             → subsequent pages (1 page = 30 items)

We scrape minimum-needed fields and cache aggressively in SQLite (24h TTL).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Optional
from urllib.parse import urljoin, quote

import httpx
from bs4 import BeautifulSoup

from . import store
from .config import settings

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _make_client() -> httpx.AsyncClient:
    kw = dict(
        headers={
            "User-Agent": _UA,
            "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        follow_redirects=True,
        timeout=30.0,
    )
    if settings.discover_proxy:
        kw["proxy"] = settings.discover_proxy
    return httpx.AsyncClient(**kw)


# ---------------------------------------------------------------------------
# JavBus HTML parsing
# ---------------------------------------------------------------------------


def _parse_actor_search(html: str, base_url: str) -> list[dict]:
    """Parse /searchstar/<name> result page → list of actors.

    JavBus returns either:
      - a list page (multiple <a class='avatar-box'> with star info), OR
      - a redirect-like page with the single matching star already loaded.
    """
    soup = BeautifulSoup(html, "lxml")
    actors: list[dict] = []

    # Primary pattern: <a class="avatar-box" href="/star/<id>">
    for a in soup.select("a.avatar-box"):
        href = a.get("href", "")
        m = re.search(r"/star/([a-z0-9]+)", href)
        if not m:
            continue
        star_id = m.group(1)
        photo = ""
        img = a.select_one("img")
        if img:
            photo = img.get("src", "") or ""
            if photo and not photo.startswith("http"):
                photo = urljoin(base_url, photo)
        name_span = a.select_one(".photo-info span") or a.select_one(".photo-frame + .photo-info span")
        name = (name_span.get_text(strip=True) if name_span else "") or ""
        if not name:
            # Try img alt
            name = (img.get("title", "") if img else "") or (img.get("alt", "") if img else "")
        if name:
            actors.append({"id": star_id, "name": name, "photo_url": photo})

    return actors


def _parse_film_list(html: str, base_url: str) -> tuple[list[dict], bool]:
    """Parse /star/<id>[/<page>] → (films, has_next_page)."""
    soup = BeautifulSoup(html, "lxml")
    films: list[dict] = []

    for box in soup.select("a.movie-box"):
        href = box.get("href", "") or ""
        # Detail URL like /SSIS-001
        code_m = re.search(r"/([A-Z0-9]+-?[A-Z0-9]+)/?$", href.upper())
        # Code lives in the date row's first child or in URL last segment
        code = ""
        date_div = box.select_one(".photo-info date")
        # JavBus shows date *after* code in two <date> elements; first <date> is code, second is date
        dates = box.select(".photo-info date")
        if len(dates) >= 1:
            code = dates[0].get_text(strip=True)
        if not code and code_m:
            code = code_m.group(1)
        release_date = ""
        if len(dates) >= 2:
            release_date = dates[1].get_text(strip=True)

        title_el = box.select_one(".photo-info span")
        title = title_el.get_text(strip=True) if title_el else ""

        cover_url = ""
        img = box.select_one("img")
        if img:
            cover_url = img.get("src", "") or ""
            if cover_url and not cover_url.startswith("http"):
                cover_url = urljoin(base_url, cover_url)

        if code:
            films.append({
                "code": code.upper(),
                "title": title,
                "release_date": release_date,
                "cover_url": cover_url,
                "detail_url": urljoin(base_url, href) if href else "",
            })

    # Pagination: <a id="next" href="..."> exists if there is a next page
    has_next = bool(soup.select_one("a#next"))
    return films, has_next


# ---------------------------------------------------------------------------
# Scrapers (cache-aware)
# ---------------------------------------------------------------------------


async def search_actor(name: str, *, force_refresh: bool = False) -> list[dict]:
    """Find actor IDs matching the name (cache TTL respected)."""
    cached = store.actor_search_cache_get(name) if not force_refresh else None
    if cached is not None:
        return cached

    base = settings.javbus_base
    encoded = quote(name)
    url = f"{base}/searchstar/{encoded}"
    log.info("javbus search actor: %s", url)

    async with _make_client() as c:
        try:
            r = await c.get(url)
        except httpx.HTTPError as e:
            log.warning("javbus search failed: %s", e)
            return []
        if r.status_code != 200:
            log.warning("javbus search %s → HTTP %s", url, r.status_code)
            return []
        actors = _parse_actor_search(r.text, base)

    store.actor_search_cache_set(name, actors)
    return actors


async def actor_films(actor_id: str, *, max_pages: Optional[int] = None,
                      force_refresh: bool = False) -> list[dict]:
    """Fetch all films of an actor across paginated pages."""
    cached = store.actor_films_cache_get(actor_id) if not force_refresh else None
    if cached is not None:
        return cached

    if max_pages is None:
        max_pages = settings.discover_max_pages
    base = settings.javbus_base
    films: list[dict] = []
    seen_codes: set[str] = set()

    async with _make_client() as c:
        for page in range(1, max_pages + 1):
            url = f"{base}/star/{actor_id}" if page == 1 else f"{base}/star/{actor_id}/{page}"
            log.info("javbus actor films: %s", url)
            try:
                r = await c.get(url)
            except httpx.HTTPError as e:
                log.warning("javbus film page %s failed: %s", url, e)
                break
            if r.status_code != 200:
                log.warning("javbus film page %s → HTTP %s", url, r.status_code)
                break

            page_films, has_next = _parse_film_list(r.text, base)
            new_count = 0
            for f in page_films:
                if f["code"] not in seen_codes:
                    seen_codes.add(f["code"])
                    films.append(f)
                    new_count += 1
            if not new_count:
                # Defensive: if a page yields no new entries, stop (avoid infinite loop)
                break
            if not has_next:
                break

    store.actor_films_cache_set(actor_id, films)
    return films


# ---------------------------------------------------------------------------
# Owned status — batch check against E:\Jav
# ---------------------------------------------------------------------------


def _scan_owned_codes() -> set[str]:
    """One-shot: walk the JAV library + staging, build a normalized set of
    every code we likely have.

    The set members are normalized: uppercase, dashes/spaces/underscores stripped.
    """
    from pathlib import Path

    norms: set[str] = set()

    code_re = re.compile(r"([A-Z]{2,5}-?\d{3,4}(?:-[A-Z])?|FC2[-_ ]?PPV[-_ ]?\d{6,7}|HEYZO[-_ ]?\d{4})", re.I)

    for root in (settings.jav_library, settings.jav_staging_extra):
        base = Path(root)
        if not base.is_dir():
            continue
        for level1 in base.iterdir():
            if not level1.is_dir():
                continue
            _harvest_codes(level1.name, code_re, norms)
            try:
                for level2 in level1.iterdir():
                    if level2.is_dir():
                        _harvest_codes(level2.name, code_re, norms)
            except (PermissionError, OSError):
                pass
    return norms


def _harvest_codes(name: str, code_re: re.Pattern, out: set[str]) -> None:
    for m in code_re.finditer(name):
        raw = m.group(1).upper()
        out.add(re.sub(r"[\s_\-\.]+", "", raw))


def annotate_owned(films: list[dict], owned_codes_norm: Optional[set[str]] = None) -> list[dict]:
    """Mutate film dicts in place to add `owned: bool` + return them.

    Pass owned_codes_norm to avoid rescanning the filesystem on each call.
    """
    if owned_codes_norm is None:
        owned_codes_norm = _scan_owned_codes()
    for f in films:
        norm = re.sub(r"[\s_\-\.]+", "", f["code"].upper())
        f["owned"] = norm in owned_codes_norm
    return films
